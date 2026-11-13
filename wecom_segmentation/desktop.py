"""Local worker for the native macOS workbench."""
from contextlib import closing
import json
import os
from secrets import token_urlsafe
import sqlite3
import sys
import traceback
from threading import Lock, Thread

from pydantic import ValidationError

from . import operator
from .domain import follow_up_needs_sync
from .store import InputError, Store
from .window import WindowSnapshot


ACTIONS = {
    "capture": "正在采集企微窗口",
    "classify": "正在本地分层",
    "local_reprocess": "正在使用本机 Ollama 重新分层",
    "api_capture": "正在采集企微窗口",
    "api_reprocess": "正在使用模型 API 重新分层",
    "sync": "正在同步飞书测试表",
}


def api_destination(api):
    return {"url": api.url.geturl(), "model": api.model}


def session_key(session):
    return session.db.stem


def session_summary(session, config):
    summary = {"id": session_key(session), "title": session.title, "customer": session.customer,
               "status": "未采集", "sync": "未同步", "preview": None}
    if not session.db.is_file():
        return summary
    with closing(Store(session.db)) as store:
        if store.window_scope() not in (None, session.scope):
            summary["status"] = "会话范围不符"
            return summary
        status = store.status()
        if status["待分层"]:
            summary["status"] = "分层失败" if status["分层失败"] else "待分层"
            return summary
        rows = store.preview()
        if len(rows) == 1 and store.window_scope() == session.scope:
            summary.update(status="已分层", preview=operator.preview_fields(store))
            if config:
                row, = rows
                state = store.sync_state(config.target, row["线索 ID"])
                if state:
                    if state["pending_fields"] is not None:
                        summary["sync"] = "待确认"
                    elif state["confirmed_version"] > row["同步版本"]:
                        summary["sync"] = "远端较新"
                    elif (state["confirmed_version"] < row["同步版本"]
                          or follow_up_needs_sync(row.get("跟进状态"), state["confirmed_follow_up"])):
                        summary["sync"] = "待更新"
                    else:
                        summary["sync"] = "已同步"
    return summary


def optional_config(load, path):
    try:
        return load(path)
    except InputError:
        return None


def perform(session, action, api, snapshot, report):
    capture = action in ("capture", "api_capture")
    if not capture and not session.db.is_file():
        raise InputError("这条会话还没有采集记录，请先采集并分层")
    with closing(Store(session.db)) as store:
        if store.window_scope() not in (None, session.scope):
            raise InputError("会话用途与本地数据库不符")
        if capture:
            captured = store.capture_window(lambda: snapshot, customer_pilot=session.customer)
            report(captured=captured["新增消息"],
                   message="正在使用模型 API 分层" if api else "正在本地分层")
        if action == "sync":
            synced = operator.sync_session(session, store)
            report(synced=synced)
            operator.check_sync_result(synced)
            return "飞书同步完成"
        operator.classify_session(store, api=api, reprocess=action in ("api_reprocess", "local_reprocess"))
        operator.preview_fields(store)
        return "分层结果已就绪"


class Workspace:
    def __init__(self):
        self.lock = Lock()
        self.job = None

    def state(self):
        job = self.current_job()
        config = optional_config(operator.Config.load, operator.LOCAL / "feishu.env")
        api = optional_config(operator.ApiModel.load, operator.DEFAULT_CONFIG)
        return {
            "sessions": [session_summary(session, config) for session in operator.Sessions.load().entries],
            "feishu": config is not None,
            "feishu_url": config.table_url if config else None,
            "api": api_destination(api) if api else None,
            "job": job,
        }

    def current_job(self):
        with self.lock:
            return self.job.copy() if self.job else None

    def add(self, body):
        title = body.get("title")
        customer = body.get("customer")
        if (not isinstance(title, str) or "\n" in title or "\r" in title
                or not isinstance(customer, bool)):
            raise InputError("请填写会话标题并选择用途")
        with self.lock:
            session = operator.Sessions.open_or_add(title, customer)
        return {"id": session_key(session)}

    def start(self, body):
        action = body.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise InputError("请选择操作")
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise InputError("已有任务在运行，请等待完成")
            session = next((item for item in operator.Sessions.load().entries
                            if session_key(item) == body.get("session")), None)
            if session is None:
                raise InputError("请选择已添加的会话")
            snapshot = None
            if action in ("capture", "api_capture"):
                try:
                    snapshot = WindowSnapshot.model_validate(body.get("snapshot"))
                except ValidationError:
                    raise InputError("工作台未提供有效的企微窗口采集结果，请重新采集") from None
                if snapshot.header[0] != session.title:
                    raise InputError("当前企微会话标题与指定标题不符")
            api = None
            if action in ("api_capture", "api_reprocess"):
                api = operator.ApiModel.load(operator.DEFAULT_CONFIG)
                if body.get("destination") != api_destination(api):
                    raise InputError("请刷新页面并确认当前模型 API 的发送目的地")
            self.job = {"id": token_urlsafe(12), "session": session_key(session), "action": action,
                        "status": "running", "message": ACTIONS[action]}
            Thread(target=self.run, args=(session, action, api, snapshot)).start()
            return self.job.copy()

    def report(self, **changes):
        with self.lock:
            self.job.update(changes)

    def run(self, session, action, api, snapshot):
        try:
            message = perform(session, action, api, snapshot, self.report)
        except Exception as error:
            self.report(status="error", message=error_message(error))
        else:
            self.report(status="done", message=message)


def error_message(error):
    if isinstance(error, (InputError, operator.WindowError, operator.FeishuError)):
        return str(error)
    if isinstance(error, (OSError, sqlite3.Error)):
        return "无法访问本地文件或程序，请检查安装和目录权限"
    traceback.print_exception(error, file=sys.stderr)
    return "操作未完成，请查看 .local/workbench.log 后重试"


def respond(workspace, request):
    method = request.get("method")
    if method == "state":
        return workspace.state()
    if method == "add":
        return workspace.add(request)
    if method == "start":
        return workspace.start(request)
    if method == "save_api":
        api = operator.ApiModel.save(operator.DEFAULT_CONFIG, request.get("url"), request.get("model"),
                                     request.get("key"))
        return api_destination(api)
    raise InputError("无法识别操作")


def main():
    os.umask(0o077)
    operator.LOCAL.mkdir(exist_ok=True)
    workspace = Workspace()
    output = sys.stdout
    sys.stdout = sys.stderr
    for line in sys.stdin:
        request = json.loads(line)
        try:
            response = {"data": respond(workspace, request)}
        except Exception as error:
            response = {"error": error_message(error)}
        print(json.dumps({"id": request["id"], **response}, ensure_ascii=False), file=output, flush=True)


if __name__ == "__main__":
    main()
