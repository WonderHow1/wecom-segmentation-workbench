from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wecom_segmentation import operator
from wecom_segmentation.domain import Extraction
from wecom_segmentation.feishu import Config, Record
from wecom_segmentation.store import InputError, Store
from wecom_segmentation.window import WINDOW_CONTACT, WindowSnapshot


def snapshot(title, *texts):
    return WindowSnapshot.model_validate({
        "header": [title],
        "messages": [{"speaker": "customer", "text": text} for text in texts],
    })


class OperatorTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(patch.multiple(operator, LOCAL=self.root, PROFILES=self.root / "sessions.json"))
        self.stdout = self.enterContext(redirect_stdout(StringIO()))
        self.session = operator.Sessions.load().add("同事测试", customer=False)

    def capture(self, messages):
        with (
            patch("builtins.input", return_value=""),
            patch.object(operator.subprocess, "run"),
            patch.object(operator, "sleep"),
            patch.object(operator, "read_window", return_value=messages),
            patch.object(operator, "local_model") as model,
            patch("wecom_segmentation.ollama.extract", return_value=Extraction(facts=[])) as extract,
        ):
            operator.operate(self.session, "1")
            return model.call_count, extract.call_count

    def test_persisted_session_resumes_and_replays_without_inference(self):
        other = operator.Sessions.load().add("客户试跑", customer=True)
        self.assertNotEqual(other.db, self.session.db)
        with patch("builtins.input", return_value="1"):
            self.assertEqual(operator.select_session(), self.session)
        with self.assertRaises(InputError):
            operator.Sessions.load().add("同事测试", customer=True)

        baseline = snapshot(self.session.title, "基线甲", "基线乙", "基线丙")
        self.assertEqual(self.capture(baseline), (1, 1))
        current = snapshot(self.session.title, "基线甲", "基线乙", "基线丙", "同一条", "同一条")
        self.assertEqual(self.capture(current), (1, 1))
        self.assertEqual(self.capture(current), (0, 0))
        with closing(Store(self.session.db)) as store:
            messages = store.conversation(WINDOW_CONTACT)
            self.assertEqual(len(messages), 5)
            self.assertNotEqual(messages[-1].message_id, messages[-2].message_id)
            self.assertEqual(store.preview()[0]["同步版本"], 2)
        self.assertIn("新增 0 条", self.stdout.getvalue())
        self.assertNotIn("同一条", self.stdout.getvalue())

    def test_sync_can_retry_without_recapture_and_preserves_record(self):
        self.capture(snapshot(self.session.title, "甲", "乙", "丙"))
        config = Config("cli_test", "example", "baseA", "tblA")
        with patch.object(operator.Config, "load", return_value=config), patch.object(operator, "Feishu") as feishu:
            client = feishu.return_value
            client.ensure_fields.return_value = 0
            client.find.side_effect = [None, Record("rec1", 1)]
            client.create.return_value = "rec1"
            operator.operate(self.session, "2")
            operator.operate(self.session, "2")
            client.create.assert_called_once()
            fields, _ = client.create.call_args.args
            self.assertNotIn(self.session.title, json.dumps(fields, ensure_ascii=False))
            client.update.assert_not_called()
            self.assertIn('"已是最新": 1', self.stdout.getvalue())

    def test_unclassified_capture_stays_local_and_can_resume(self):
        messages = snapshot(self.session.title, "甲", "乙", "丙")
        with (
            patch("builtins.input", return_value=""),
            patch.object(operator.subprocess, "run"),
            patch.object(operator, "sleep"),
            patch.object(operator, "read_window", return_value=messages),
            patch.object(operator, "local_model", side_effect=InputError("模型未启动")),
        ):
            with self.assertRaisesRegex(InputError, "模型未启动"):
                operator.operate(self.session, "1")
        with patch.object(operator, "Feishu") as feishu:
            with self.assertRaisesRegex(InputError, "分层尚未完成"):
                operator.operate(self.session, "2")
            feishu.assert_not_called()
        with (
            patch.object(operator, "read_window") as reader,
            patch.object(operator, "local_model"),
            patch("wecom_segmentation.ollama.extract", return_value=Extraction(facts=[])),
        ):
            operator.operate(self.session, "3")
            reader.assert_not_called()
        with closing(Store(self.session.db)) as store:
            self.assertEqual(store.preview()[0]["同步版本"], 1)
