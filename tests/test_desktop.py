from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from stat import S_IMODE
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from wecom_segmentation import desktop, operator
from wecom_segmentation.domain import Extraction
from wecom_segmentation.feishu import Config, Record
from wecom_segmentation.store import InputError, Store
from wecom_segmentation.window import WindowSnapshot


class DesktopTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(operator, LOCAL=root, PROFILES=root / "sessions.json",
                                         DEFAULT_CONFIG=root / "model-api.env"))
        self.workspace = desktop.Workspace()
        self.session = operator.Sessions.load().add("同事测试", customer=False)
        self.key = desktop.session_key(self.session)
        self.baseline = WindowSnapshot.model_validate({
            "header": [self.session.title],
            "messages": [{"speaker": "customer", "text": f"原始文字 {index}"} for index in range(3)],
        })

    def start(self, action, **fields):
        if action in ("capture", "api_capture"):
            fields.setdefault("snapshot", self.baseline.model_dump())
        return self.workspace.start({"session": self.key, "action": action, **fields})

    def wait_for_job(self, status):
        deadline = monotonic() + 3
        job = self.workspace.current_job()
        while job["status"] == "running" and monotonic() < deadline:
            sleep(0.01)
            job = self.workspace.current_job()
        self.assertEqual(job["status"], status, job)
        return job

    def stub_model(self):
        self.enterContext(patch.object(operator, "local_model"))
        return self.enterContext(patch("wecom_segmentation.ollama.extract", return_value=Extraction(facts=[])))

    def test_existing_profiles_addition_and_capture_preview_sync(self):
        existing, = self.workspace.state()["sessions"]
        self.assertEqual((existing["id"], existing["status"]), (self.key, "未采集"))
        self.assertFalse(self.session.db.exists())
        added = self.workspace.add({"title": "  新客户  ", "customer": True})
        self.assertNotEqual(added["id"], self.key)
        self.assertEqual(operator.Sessions.load().entries[1].title, "新客户")
        self.stub_model()
        self.start("capture")
        self.assertEqual(self.wait_for_job("done")["captured"], 3)
        result = self.workspace.state()["sessions"][0]
        self.assertEqual(result["preview"]["同步版本"], 1)
        self.assertNotIn("原始文字", json.dumps(result, ensure_ascii=False))

        config = Config("cli_test", "test-secret", "base", "tbl")
        with patch.object(operator.Config, "load", return_value=config), patch.object(operator, "Feishu") as feishu:
            client = feishu.return_value
            client.ensure_fields.return_value = 0
            client.find.side_effect = [None, Record("rec", 1), Record("rec", 1)]
            client.create.return_value = "rec"
            self.start("sync")
            self.assertEqual(self.wait_for_job("done")["synced"]["新增"], 1)
            self.start("sync")
            self.assertEqual(self.wait_for_job("done")["synced"]["已是最新"], 1)
            client.create.assert_called_once()
            state = self.workspace.state()
            self.assertEqual(state["sessions"][0]["sync"], "已同步")
            self.assertNotIn("test-secret", json.dumps(state, ensure_ascii=False))

            reopened = desktop.Workspace().add({"title": "  同事测试  ", "customer": True})
            self.assertEqual(reopened, {"id": self.key})
            entries = operator.Sessions.load().entries
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0], self.session)
            current = self.baseline.model_dump()
            current["messages"].append({"speaker": "customer", "text": "我现在上高二"})
            self.start("capture", snapshot=current)
            self.assertEqual(self.wait_for_job("done")["captured"], 1)
            self.start("sync")
            self.assertEqual(self.wait_for_job("done")["synced"]["更新"], 1)
            client.create.assert_called_once()
            record_id, updated = client.update.call_args.args
            self.assertEqual((record_id, updated["同步版本"]), ("rec", 2))
            self.assertEqual(updated["线索 ID"], client.create.call_args.args[0]["线索 ID"])

    def test_midnight_follow_up_marks_same_version_as_pending_sync(self):
        self.stub_model()
        config = Config("cli_test", "test-secret", "base", "tbl")
        with patch.object(operator.Config, "load", return_value=config), patch("wecom_segmentation.store.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = datetime.fromisoformat("2026-11-13T23:55:00+08:00")
            self.start("capture")
            self.wait_for_job("done")
            with closing(Store(self.session.db)) as store:
                row, = store.preview()
                store.confirm_sync(config.target, row["线索 ID"], "rec1", row["同步版本"], follow_up="推进中")
            self.assertEqual(self.workspace.state()["sessions"][0]["sync"], "已同步")
            clock.now.return_value = datetime.fromisoformat("2026-11-14T00:05:00+08:00")
            session = self.workspace.state()["sessions"][0]
            self.assertEqual(session["preview"]["跟进状态"], "暂缓")
            self.assertEqual(session["preview"]["同步版本"], row["同步版本"])
            self.assertEqual(session["sync"], "待更新")

    def test_capture_failure_can_retry_without_recapturing(self):
        with patch.object(operator, "local_model", side_effect=InputError("模型未启动")):
            self.start("capture")
            job = self.wait_for_job("error")
            self.assertEqual((job["captured"], job["message"]), (3, "模型未启动"))
        self.assertEqual(self.workspace.state()["sessions"][0]["status"], "待分层")
        self.stub_model()
        self.start("classify")
        self.wait_for_job("done")
        with closing(Store(self.session.db)) as store:
            self.assertEqual(store.preview()[0]["同步版本"], 1)

    def test_capture_rejects_missing_snapshot_and_wrong_window(self):
        with self.assertRaisesRegex(InputError, "未提供有效"):
            self.start("capture", snapshot=None)
        with self.assertRaisesRegex(InputError, "会话标题"):
            self.start("capture", snapshot={**self.baseline.model_dump(), "header": ["另一条会话"]})
        self.assertFalse(self.session.db.exists())

    def test_polling_continues_if_job_finishes_while_reading_sessions(self):
        self.workspace.job = {"status": "running"}

        def complete_during_summary(*args):
            self.workspace.report(status="done")
            return {"status": "待分层"}

        with patch.object(desktop, "session_summary", side_effect=complete_during_summary):
            self.assertEqual(self.workspace.state()["job"]["status"], "running")
        self.assertEqual(self.workspace.state()["job"]["status"], "done")

    def test_api_requires_current_destination_and_blocks_parallel_jobs(self):
        operator.DEFAULT_CONFIG.write_text(
            "MODEL_API_BASE_URL=https://model.example/v1\nMODEL_API_MODEL=test\nMODEL_API_KEY=test-secret\n",
            encoding="utf-8",
        )
        destination = self.workspace.state()["api"]
        self.assertEqual(destination, {"url": "https://model.example/v1", "model": "test"})
        with self.assertRaisesRegex(InputError, "发送目的地"):
            self.start("api_capture", destination={**destination, "url": "https://old.example"})
        entered, release = Event(), Event()

        def classify(_store, *, api, reprocess):
            self.assertEqual(api.model, "test")
            self.assertFalse(reprocess)
            entered.set()
            release.wait(3)
            raise InputError("测试失败")

        with patch.object(operator, "classify_session", side_effect=classify):
            self.start("api_capture", destination=destination)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(InputError, "已有任务"):
                    self.start("classify")
            finally:
                release.set()
                self.wait_for_job("error")
        with closing(Store(self.session.db)) as store:
            self.assertEqual(store.status()["待分层"], 1)

    def test_stdio_worker_starts_and_keeps_request_ids(self):
        folder = self.enterContext(TemporaryDirectory())
        source = Path(__file__).resolve().parents[1]
        requests = [{"id": "a", "method": "add", "title": "会话", "customer": True},
                    {"id": "b", "method": "state"}]
        result = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(source)!r}); "
             "from wecom_segmentation.desktop import main; main()"],
            cwd=folder, input="".join(json.dumps(request) + "\n" for request in requests),
            text=True, capture_output=True, check=True, timeout=15,
        )
        added, state = map(json.loads, result.stdout.splitlines())
        self.assertEqual((added["id"], state["id"]), ("a", "b"))
        self.assertEqual(state["data"]["sessions"][0]["id"], added["data"]["id"])
        self.assertTrue(state["data"]["sessions"][0]["customer"])

    def test_configured_table_link_preserves_view_and_excludes_credentials(self):
        url = "https://team.feishu.cn/base/baseA?table=tblA&view=vewA"
        (operator.LOCAL / "feishu.env").write_text(
            f"FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET=test-secret\nFEISHU_TABLE_URL={url}\n",
            encoding="utf-8",
        )
        state = self.workspace.state()
        self.assertEqual(state["feishu_url"], url)
        self.assertNotIn("test-secret", json.dumps(state))

    def test_add_model_api_persists_locally_and_invalid_edit_keeps_previous_config(self):
        form = {"method": "save_api", "url": "https://model.example/v1", "model": "chat", "key": "fixture-key"}
        saved = desktop.respond(self.workspace, form)
        self.assertEqual(saved, {"url": form["url"], "model": form["model"]})
        self.assertEqual(S_IMODE(operator.DEFAULT_CONFIG.stat().st_mode), 0o600)
        self.assertEqual(desktop.Workspace().state()["api"], saved)
        with self.assertRaisesRegex(InputError, "HTTPS"):
            desktop.respond(self.workspace, {**form, "url": "http://model.example/v1"})
        self.assertEqual(operator.ApiModel.load(operator.DEFAULT_CONFIG).key, "fixture-key")
