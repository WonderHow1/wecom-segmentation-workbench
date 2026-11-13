from contextlib import closing, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wecom_segmentation import operator
from wecom_segmentation.__main__ import main
from wecom_segmentation.feishu import Record
from wecom_segmentation.model_api import ApiModel
from wecom_segmentation.ollama import ExtractionError, Observation
from wecom_segmentation.store import InputError, Store
from wecom_segmentation.sync import sync_table
from wecom_segmentation.window import WindowSnapshot


def response(status=200, *, content="{}", finish="stop"):
    data = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
    result = BytesIO(json.dumps(data).encode())
    result.status = status
    return result


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.root = Path(folder)
        self.config = self.root / "model-api.env"
        self.db = self.root / "session.sqlite3"
        self.write_config("https://model.example/v1/")
        self.api = ApiModel.load(self.config)

    def write_config(self, url):
        self.config.write_text(f"MODEL_API_BASE_URL={url}\nMODEL_API_MODEL=test-model\nMODEL_API_KEY=secret-token\n")

    def test_config_requires_https_base_and_keeps_key_out_of_repr(self):
        self.assertNotIn("secret-token", repr(self.api))
        for invalid in ("http://model.example", "https://user:secret@model.example", "https://model.example?v=key",
                        "https://model.example/v1/chat/completions", "https://model.example:0"):
            with self.subTest(invalid=invalid):
                self.write_config(invalid)
                with self.assertRaises(InputError):
                    ApiModel.load(self.config)

    def test_transport_uses_json_mode_and_only_recent_context(self):
        previous = [{"speaker": "employee", "text": str(index)} for index in range(8)]
        with patch("wecom_segmentation.model_api.HTTPSConnection") as factory:
            connection = factory.return_value
            connection.getresponse.return_value = response(content='{"grade":"高二"}')
            self.assertEqual(self.api.observe({"speaker": "customer", "text": "我高二"}, previous).grade, "高二")
            self.assertEqual(factory.call_args.args, ("model.example",))
            self.assertEqual(connection.request.call_args.args, ("POST", "/v1/chat/completions"))
            request = connection.request.call_args.kwargs
            self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")
            body = json.loads(request["body"])
            self.assertEqual(body["model"], "test-model")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            user = json.loads(body["messages"][1]["content"])
            self.assertEqual(user["context"], previous[-6:])
            self.assertEqual(user["target"]["text"], "我高二")
            connection.close.assert_called_once()

    def test_api_errors_do_not_echo_request_or_response_contents(self):
        with patch("wecom_segmentation.model_api.HTTPSConnection") as factory:
            connection = factory.return_value
            for status, content, finish, expected in (
                (401, "secret-token", "stop", "鉴权失败"), (429, "secret-token", "stop", "限流"),
                (302, "secret-token", "stop", "HTTP 302"), (200, "{}", "length", "未完成提取"),
                (200, '{"grade":"private text"}', "stop", "无效的结构化结果"),
            ):
                with self.subTest(status=status, content=content):
                    connection.getresponse.return_value = response(status, content=content, finish=finish)
                    with self.assertRaisesRegex(ExtractionError, expected) as failure:
                        self.api.observe({"text": "private text"}, [])
                    self.assertNotIn("secret-token", str(failure.exception))
                    self.assertNotIn("private text", str(failure.exception))

    def test_api_reprocess_updates_same_lead_and_failure_preserves_previous_result(self):
        sample = self.root / "messages.jsonl"
        sample.write_text(json.dumps({"message_id": "message-1", "contact_id": "private-id", "speaker": "customer",
                                      "time": "2026-11-13T10:00:00+08:00", "text": "我高二，数学跟不上。我的选科是物化生。"}))

        def run(*args):
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                code = main(["--db", str(self.db), *args])
            return code, json.loads(out.getvalue())

        with patch("wecom_segmentation.ollama.observe", return_value=Observation()) as local:
            self.assertEqual(run("import", "--file", str(sample))[0], 0)
            with patch.object(ApiModel, "observe") as api_observe:
                self.assertEqual(run("classify")[0], 0)
                local.assert_called_once()
                api_observe.assert_not_called()
        with closing(Store(self.db)) as store:
            first, = store.preview()
        self.assertEqual(first["同步版本"], 1)

        args = ("classify", "--api", "--api-config", str(self.config))
        with patch("wecom_segmentation.model_api.HTTPSConnection") as transport:
            connection = transport.return_value
            self.assertEqual(run(*args)[1]["本次已分层"], 0)
            transport.assert_not_called()
            connection.getresponse.return_value = response(401)
            code, result = run(*args, "--reprocess")
            self.assertEqual(code, 1)
            self.assertIn("鉴权失败", next(iter(result["失败原因"])))
            with closing(Store(self.db)) as store:
                self.assertEqual(store.preview(), [first])
            connection.getresponse.return_value = response(content='{"subject_selection":["生物","化学","物理"]}')
            self.assertEqual(run(*args, "--reprocess")[1]["本次已分层"], 1)
            payload = connection.request.call_args.kwargs["body"].decode()
            self.assertNotIn("private-id", payload)
            self.assertNotIn("message-1", payload)

        with closing(Store(self.db)) as store, patch("wecom_segmentation.feishu.Feishu") as feishu:
            second, = store.preview()
            self.assertEqual(second["线索 ID"], first["线索 ID"])
            self.assertEqual(second["同步版本"], 2)
            self.assertEqual(second["科目"], ["物理", "化学", "生物"])
            self.assertEqual(store.db.execute("SELECT model FROM leads").fetchone()["model"],
                             "api:model.example/test-model")
            client = feishu.return_value
            client.find.return_value = Record("rec1", 1)
            self.assertEqual(sync_table(client, store, "test-table")["更新"], 1)
            self.assertEqual(client.update.call_args.args[0], "rec1")
            client.create.assert_not_called()

    def test_operator_api_reprocesses_saved_messages_without_opening_wecom(self):
        session = operator.Session(title="同事测试", db=self.db, customer=False)
        snapshot = WindowSnapshot.model_validate({
            "header": [session.title],
            "messages": [{"speaker": "customer", "text": text} for text in ("我高二", "数学跟不上", "想试听")],
        })
        with closing(Store(self.db)) as store:
            store.capture_window(lambda: snapshot)
        with (
            patch.object(operator, "DEFAULT_CONFIG", self.config),
            patch("builtins.input", side_effect=["", "2"]),
            patch.object(ApiModel, "observe", return_value=Observation()) as observer,
            patch.object(operator, "read_window") as reader,
            patch.object(operator, "local_model") as local,
            redirect_stdout(StringIO()) as out,
        ):
            operator.operate(session, "4")
            observer.assert_not_called()
            operator.operate(session, "4")
            self.assertEqual(observer.call_count, 3)
            reader.assert_not_called()
            local.assert_not_called()
        self.assertIn("model.example", out.getvalue())
        with closing(Store(self.db)) as store:
            self.assertEqual(store.preview()[0]["同步版本"], 1)

    def test_operator_can_capture_then_use_api_and_skip_unchanged_messages(self):
        session = operator.Session(title="同事测试", db=self.db, customer=False)
        snapshot = WindowSnapshot.model_validate({
            "header": [session.title],
            "messages": [{"speaker": "customer", "text": text} for text in ("我高二", "数学跟不上", "想试听")],
        })
        with (
            patch.object(operator, "DEFAULT_CONFIG", self.config),
            patch("builtins.input", side_effect=["1", "", "1", ""]),
            patch.object(operator.subprocess, "run"),
            patch.object(operator, "sleep"),
            patch.object(operator, "read_window", return_value=snapshot),
            patch.object(operator, "local_model") as local,
            patch.object(ApiModel, "observe", return_value=Observation()) as observer,
            redirect_stdout(StringIO()) as out,
        ):
            operator.operate(session, "4")
            operator.operate(session, "4")
            self.assertEqual(observer.call_count, 3)
            local.assert_not_called()
        self.assertIn("新增 3 条", out.getvalue())
        self.assertIn("新增 0 条", out.getvalue())
