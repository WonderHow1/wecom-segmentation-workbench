from contextlib import closing, contextmanager
from datetime import datetime
import fcntl
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
from shutil import which
import sqlite3
import subprocess
from time import sleep
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from .classification import classify_pending
from .domain import AUDIENCE_TAG_FIELD, STUDY_PROFILE_FIELD, WECHAT_ID_FIELD, is_subject_selection
from .feishu import Config, Feishu, FeishuError
from .model_api import DEFAULT_CONFIG, ApiModel
from .ollama import MODEL
from .store import InputError, Store
from .sync import sync_lock, sync_table
from .window import CUSTOMER_PILOT_SCOPE, WINDOW_SCOPE, WindowError, read_window


LOCAL = Path(".local")
PROFILES = LOCAL / "operator-sessions.json"
READER = LOCAL / "wecom-window-reader"
BUNDLE = "com.tencent.WeWorkMac"
PREVIEW_FIELDS = (
    "数据范围", "同步版本", "有效线索判定", "当前阶段", "来源", "咨询人身份", WECHAT_ID_FIELD, "年级", "科目", AUDIENCE_TAG_FIELD,
    "学习目标", "主要卡点", "跟进状态", "到课待核实", "付费待核实", "当前有效试听场次", STUDY_PROFILE_FIELD, "证据摘要",
)


class Session(BaseModel):
    title: str
    db: Path
    customer: bool

    @property
    def scope(self):
        return CUSTOMER_PILOT_SCOPE if self.customer else WINDOW_SCOPE


class Sessions(BaseModel):
    entries: list[Session]

    @classmethod
    def load(cls):
        try:
            sessions = cls.model_validate_json(PROFILES.read_bytes())
        except FileNotFoundError:
            return cls(entries=[])
        except (OSError, ValidationError):
            raise InputError("无法读取本机会话列表，请检查 .local/operator-sessions.json") from None
        return sessions

    def add(self, title: str, customer: bool):
        if not title or any(item.title == title for item in self.entries):
            raise InputError("请填写能唯一识别的企微会话标题。已添加的会话可直接从列表选择")
        session = Session(title=title, db=LOCAL / "sessions" / f"{uuid4().hex}.sqlite3", customer=customer)
        self.entries.append(session)
        temporary = PROFILES.with_name(f".{PROFILES.name}.{uuid4().hex}")
        try:
            temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(PROFILES)
        finally:
            temporary.unlink(missing_ok=True)
        return session

    @classmethod
    def open_or_add(cls, title: str, customer: bool):
        title = title.strip()
        if not title or "\n" in title or "\r" in title:
            raise InputError("请填写企微聊天顶部显示的完整会话名")
        with PROFILES.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            sessions = cls.load()
            existing = next((item for item in sessions.entries if item.title == title), None)
            return existing if existing else sessions.add(title, customer)


def installed_models() -> set[str] | None:
    connection = HTTPConnection("127.0.0.1", 11434, timeout=2)
    try:
        connection.request("GET", "/api/tags")
        response = connection.getresponse()
        if response.status == 200:
            return {item["name"] for item in json.load(response)["models"]}
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        return None
    finally:
        connection.close()
    return None


@contextmanager
def local_model():
    models = installed_models()
    process = None
    try:
        if models is None:
            binary = which("ollama")
            binary_config = LOCAL / "ollama-bin"
            if binary_config.is_file():
                binary = binary_config.read_text(encoding="utf-8").strip()
            if not binary:
                raise InputError("请启动本机 Ollama，或把 Ollama 可执行文件路径填入 .local/ollama-bin")
            print("正在启动本机 Ollama…", flush=True)
            env = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1"}
            if (LOCAL / "ollama-models").is_dir():
                env["OLLAMA_MODELS"] = str((LOCAL / "ollama-models").resolve())
            with (LOCAL / "operator-ollama.log").open("a", encoding="utf-8") as log:
                process = subprocess.Popen([binary, "serve"], stdout=log, stderr=log, env=env)
            for _ in range(30):
                models = installed_models()
                if models is not None or process.poll() is not None:
                    break
                sleep(0.5)
        if models is None:
            raise InputError("本机 Ollama 未就绪，请查看 .local/operator-ollama.log")
        if MODEL not in models:
            raise InputError(f"本机 Ollama 缺少 {MODEL}，请运行 ollama pull {MODEL}")
        yield
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def select_session():
    sessions = Sessions.load()
    print("选择要处理的企微私聊")
    for index, session in enumerate(sessions.entries, 1):
        print(f"{index}. {session.title}（{session.scope}）")
    choice = input("输入会话编号，或输入 N 添加会话，回车退出：").strip().lower()
    if not choice:
        return None
    if choice == "n":
        title = input("企微聊天顶部显示的完整会话名：").strip()
        kind = input("选择用途，1 同事测试，2 企微获客：").strip()
        if kind not in ("1", "2"):
            raise InputError("请选择同事测试或企微获客")
        return Sessions.open_or_add(title, customer=kind == "2")
    try:
        index = int(choice)
        if index > 0:
            return sessions.entries[index - 1]
    except (ValueError, IndexError):
        pass
    raise InputError("会话编号无效")


def preview_fields(store: Store):
    rows = store.preview()
    if len(rows) != 1:
        raise InputError("当前没有可预览的分层结果，请先采集并分层")
    row, = rows
    if not is_subject_selection(row.get("科目")):
        row = {**row, "科目": [], "证据摘要": None}
    return {key: row[key] for key in PREVIEW_FIELDS if row.get(key) is not None}


def show_preview(store: Store):
    fields = preview_fields(store)
    if appointment := fields.get("当前有效试听场次"):
        fields["当前有效试听场次"] = datetime.fromtimestamp(appointment / 1000).strftime("%Y-%m-%d %H:%M")
    print(json.dumps(fields, ensure_ascii=False, indent=2))


def capture_session(session: Session, store: Store):
    subprocess.run(["/usr/bin/open", "-b", BUNDLE], check=True)
    sleep(1)
    return store.capture_window(lambda: read_window(READER, session.title), customer_pilot=session.customer)


def classify_session(store: Store, *, api=None, reprocess=False):
    if not store.pending(reprocess=reprocess):
        return
    if api:
        result = classify_pending(store, api=api, reprocess=reprocess)
    else:
        with local_model():
            result = classify_pending(store, reprocess=reprocess)
    if result["失败原因"] or result["分层失败"] or result["待分层"]:
        reasons = "；".join(result["失败原因"]) or "有待分层记录"
        raise InputError(f"分层尚未完成。已采集消息可在操作界面重新处理。{reasons}")


def sync_session(session: Session, store: Store):
    if store.window_scope() != session.scope:
        raise InputError("这条会话还没有可同步的分层结果")
    with sync_lock(session.db):
        if store.pending() or len(store.preview()) != 1:
            raise InputError("这条会话的分层尚未完成，请先采集并预览")
        config = Config.load(LOCAL / "feishu.env")
        return sync_table(Feishu(config), store, config.target,
                          window_test=not session.customer, customer_pilot=session.customer)


def check_sync_result(result):
    if result["失败"] or result["远端较新"] or not any(result[key] for key in ("新增", "更新", "已是最新", "恢复")):
        reasons = "；".join(result["失败原因"])
        raise InputError("飞书尚未确认本次同步，请检查结果后重试" + (f"。{reasons}" if reasons else ""))


def operate(session: Session, action: str):
    if action not in ("1", "2", "3", "4"):
        raise InputError("请选择菜单中列出的操作")
    api = None
    capture = action == "1"
    reprocess = False
    if action == "4":
        api = ApiModel.load(DEFAULT_CONFIG)
        print(f"API 分层会把聊天文字和近期上下文发送到 {api.url.netloc}，使用模型 {api.model}。")
        mode = input("1 采集并处理待分层会话   2 用已采集消息重新分层   回车返回：").strip()
        if not mode:
            return
        if mode not in ("1", "2"):
            raise InputError("请选择 API 分层方式")
        capture = mode == "1"
        reprocess = mode == "2"
    if not capture and not session.db.is_file():
        raise InputError("这条会话还没有采集记录，请先采集并分层")
    with closing(Store(session.db)) as store:
        if store.window_scope() not in (None, session.scope):
            raise InputError("会话用途与本地数据库不符")
        if capture:
            input("在企微打开这条会话并滚动到最新消息，然后回到此处按回车开始。")
            result = capture_session(session, store)
            print(f"新增 {result['新增消息']} 条文字消息", flush=True)
        if action != "2":
            classify_session(store, api=api, reprocess=reprocess)
            show_preview(store)
            return

        result = sync_session(session, store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        check_sync_result(result)


def main():
    os.umask(0o077)
    LOCAL.mkdir(exist_ok=True)
    while True:
        try:
            session = select_session()
            if session is None:
                return 0
            action = input("1 采集并预览   2 同步飞书测试表   3 查看或重试本地分层   4 用模型 API 分层   回车返回：").strip()
            if action:
                operate(session, action)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        except (InputError, WindowError, FeishuError) as error:
            print(error)
        except subprocess.CalledProcessError:
            print("无法打开企业微信，请启动 Mac 版企微后重试")
        except (OSError, sqlite3.Error):
            print("无法访问本地文件或程序，请检查安装和目录权限")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
