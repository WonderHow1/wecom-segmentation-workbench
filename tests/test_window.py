from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from wecom_segmentation import ollama
from wecom_segmentation.__main__ import main
from wecom_segmentation.domain import Extraction, Message
from wecom_segmentation.feishu import Record
from wecom_segmentation.store import InputError, Store
from wecom_segmentation.sync import sync_table
from wecom_segmentation.window import (
    CUSTOMER_PILOT_SCOPE, WINDOW_CONTACT, WINDOW_SCOPE, Bubble, WindowError, WindowSnapshot, new_bubbles,
)


def bubble(text, speaker="employee"):
    return Bubble(speaker=speaker, text=text)


BASELINE = [bubble("基线一"), bubble("基线二", "customer"), bubble("基线三"), bubble("基线四")]
REPEATED = bubble("我上高二，想补数学基础", "customer")


def snapshot(messages, header=None):
    return WindowSnapshot(header=header or ["测试同事", "@同事"], messages=messages)


class WindowTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "window.sqlite3"

    def run_cli(self, *args, expected_exit=0):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = main(["--db", str(self.db), *args])
        self.assertEqual(result, expected_exit, err.getvalue() or out.getvalue())
        return json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def capture(self, messages, header=None, *, expected_exit=0):
        native_output = snapshot(messages, header).model_dump_json()
        with patch("wecom_segmentation.window.subprocess.run",
                   return_value=CompletedProcess([], 0, native_output, "")) as reader:
            result = self.run_cli("capture-window", "--title", "测试同事", expected_exit=expected_exit)
            self.assertEqual(reader.call_args.kwargs["input"], "测试同事\n")
        return result

    def test_visible_prefix_can_disappear_and_duplicate_bubbles_are_distinct(self):
        self.assertEqual(self.capture(BASELINE)["新增消息"], 4)
        current = BASELINE[1:] + [REPEATED, REPEATED]
        self.assertEqual(self.capture(current)["新增消息"], 2)
        self.assertEqual(self.capture(current)["新增消息"], 0)
        with closing(Store(self.db)) as store:
            messages = store.conversation(WINDOW_CONTACT)
            self.assertEqual([message.text for message in messages[-2:]], [REPEATED.text] * 2)
            self.assertNotEqual(messages[-2].message_id, messages[-1].message_id)
            self.assertEqual(store.pending()[0]["revision"], 2)

    def test_ambiguous_missing_and_changed_overlap_are_rejected(self):
        for messages in (BASELINE[-3:] * 2, BASELINE[:3], [bubble("意外变化")] + BASELINE[1:]):
            with self.subTest(messages=messages), self.assertRaises(WindowError):
                new_bubbles(BASELINE, messages)
        with self.assertRaises(WindowError):
            new_bubbles([], [REPEATED] * 4)

    def test_one_question_and_answer_can_be_captured_and_extended_without_duplicates(self):
        first = [bubble("读几年级？"), bubble("我读高二。", "customer")]
        self.assertEqual(self.capture(first)["新增消息"], 2)
        self.assertEqual(self.capture(first)["新增消息"], 0)
        later = first + [bubble("最近哪一科吃力？"), bubble("函数题容易丢分。", "customer")]
        self.assertEqual(self.capture(later)["新增消息"], 2)
        self.assertEqual(self.capture(later)["新增消息"], 0)
        self.assertIn("无法唯一定位", self.capture(later[-2:], expected_exit=1))
        with closing(Store(self.db)) as store:
            self.assertEqual(len(store.conversation(WINDOW_CONTACT)), 4)

    def test_wrong_title_or_changed_header_does_not_advance_checkpoint(self):
        self.assertEqual(self.capture(BASELINE)["新增消息"], 4)
        newer = BASELINE + [REPEATED]
        self.assertIn("指定标题不符", self.capture(newer, ["另一人"], expected_exit=1))
        self.assertIn("绑定", self.capture(newer, ["测试同事", "@外部"], expected_exit=1))
        self.assertEqual(self.capture(newer)["新增消息"], 1)

    def test_sample_and_window_databases_cannot_mix(self):
        sample_file = self.db.with_suffix(".jsonl")
        sample_file.write_text(json.dumps({"message_id": "sample", "contact_id": "sample",
                                           "time": "2026-11-12T09:00:00+08:00",
                                           "speaker": "customer", "text": "合成样本"}))
        sample_db = self.db.with_name("sample.sqlite3")
        with closing(Store(sample_db)) as store:
            store.import_file(sample_file)
            with self.assertRaises(WindowError):
                store.capture_window(lambda: self.fail("should reject before reading the window"))
        self.capture(BASELINE)
        self.assertIn("另一份数据库", self.run_cli("import", "--file", str(sample_file), expected_exit=1))

    def test_window_classification_and_sync_send_only_structured_fields(self):
        self.capture(BASELINE)
        self.capture(BASELINE + [REPEATED, REPEATED])
        with patch.object(ollama, "extract", return_value=Extraction(facts=[])) as extract:
            self.assertEqual(self.run_cli("classify")["本次已分层"], 1)
            self.assertFalse(extract.call_args.kwargs["allow_relative_booking"])
            self.assertEqual(len(extract.call_args.args[0]), 6)
        row, = self.run_cli("sync", "--dry-run")
        self.assertEqual(row["数据范围"], WINDOW_SCOPE)
        self.assertEqual(row["同步版本"], 2)
        self.assertIsNone(row["最后互动时间"])
        with closing(Store(self.db)) as store, patch("wecom_segmentation.sync.write_pending") as write:
            api = Mock()
            with self.assertRaisesRegex(InputError, "--window-test"):
                sync_table(api, store, "test")
            api.ensure_fields.assert_not_called()
            api.ensure_fields.return_value = 0
            api.find.return_value = None
            self.assertEqual(sync_table(api, store, "test", window_test=True)["新增"], 1)
            payload = json.loads(write.call_args.args[3]["pending_fields"])
            self.assertEqual(payload["数据范围"], WINDOW_SCOPE)
            self.assertNotIn(REPEATED.text, json.dumps(payload, ensure_ascii=False))
            self.assertNotIn("测试同事", json.dumps(payload, ensure_ascii=False))

    def test_relative_appointment_not_inferred_from_capture_time(self):
        candidate = datetime.fromisoformat("2026-11-14T19:00:00+08:00")
        observation = ollama.Observation(booking=candidate)
        message = Message(message_id="id", contact_id="test", time=candidate, speaker="customer",
                          text="确认明天19点试听")
        self.assertEqual([fact.kind for fact in ollama.evidenced_facts(message, observation)], ["booking"])
        self.assertEqual([fact.kind for fact in ollama.evidenced_facts(
            message, observation, allow_relative_booking=False)], ["booking_pending"])
        absolute = message.model_copy(update={"text": "确认试听，时间是 2026 年 11 月 14 日北京时间 19:00"})
        self.assertEqual([fact.kind for fact in ollama.evidenced_facts(
            absolute, observation, allow_relative_booking=False)], ["booking"])

    def test_customer_pilot_sessions_use_independent_checkpoints_and_remote_rows(self):
        api = Mock()
        api.ensure_fields.return_value = 0
        records = {}

        def create(fields, _token):
            record_id = f"rec{len(records) + 1}"
            records[fields["线索 ID"]] = Record(record_id, fields["同步版本"])
            self.assertEqual(fields["数据范围"], CUSTOMER_PILOT_SCOPE)
            self.assertNotIn("客户甲", json.dumps(fields, ensure_ascii=False))
            return record_id

        def update(record_id, fields):
            identity = fields["线索 ID"]
            self.assertEqual(records[identity].record_id, record_id)
            records[identity] = Record(record_id, fields["同步版本"])

        api.find.side_effect = records.get
        api.create.side_effect = create
        api.update.side_effect = update
        leads = []
        for index, title in enumerate(("客户甲", "客户乙")):
            self.db = self.db.with_name(f"customer-{index}.sqlite3")
            baseline = snapshot(BASELINE, [title])
            current = snapshot(BASELINE + [REPEATED, REPEATED], [title])
            with closing(Store(self.db)) as store, patch.object(ollama, "extract", return_value=Extraction(facts=[])):
                with patch("wecom_segmentation.window.subprocess.run",
                           return_value=CompletedProcess([], 0, baseline.model_dump_json(), "")):
                    self.assertEqual(self.run_cli("capture-window", "--customer-pilot", "--title", title)["新增消息"], 4)
                self.assertEqual(self.run_cli("classify")["本次已分层"], 1)
                for options in ({}, {"window_test": True}):
                    with self.assertRaisesRegex(InputError, "--customer-pilot"):
                        sync_table(api, store, "test", **options)
                self.assertEqual(sync_table(api, store, "test", customer_pilot=True)["新增"], 1)
                with self.assertRaises(WindowError):
                    store.capture_window(lambda: self.fail("must check scope before reading"))
                with self.assertRaises(WindowError):
                    store.capture_window(lambda: snapshot(current.messages, ["其他客户"]), customer_pilot=True)
                self.assertEqual(store.capture_window(lambda: current, customer_pilot=True)["新增消息"], 2)
                self.assertEqual(store.capture_window(lambda: current, customer_pilot=True)["新增消息"], 0)
                self.assertEqual(self.run_cli("classify")["本次已分层"], 1)
                self.assertEqual(sync_table(api, store, "test", customer_pilot=True)["更新"], 1)
                self.assertEqual(sync_table(api, store, "test", customer_pilot=True)["已是最新"], 1)
                row, = store.preview()
                leads.append(row["线索 ID"])
                self.assertEqual(len(store.conversation(WINDOW_CONTACT)), 6)
        self.assertNotEqual(*leads)
        self.assertEqual([records[lead].version for lead in leads], [2, 2])
        self.assertEqual(api.create.call_count, 2)
        self.assertEqual(api.update.call_count, 2)

    def test_legacy_acquisition_label_is_normalized_in_preview_and_pending_sync(self):
        with closing(Store(self.db)) as store:
            store.capture_window(lambda: snapshot(BASELINE), customer_pilot=True)
            lead, = store.pending()
            legacy = {"线索 ID": lead["lead_id"], "数据范围": "企微客户试跑"}
            store.save(lead, legacy, Extraction(facts=[]), "fixture", 1, 1)
            row, = store.preview()
            self.assertEqual(row["数据范围"], "企微获客")
            self.assertEqual(row["同步版本"], 1)
            store.begin_sync("test", {**legacy, "同步版本": 1})
            api = Mock()
            api.find.return_value = None
            api.create.return_value = "rec1"
            self.assertEqual(sync_table(api, store, "test", customer_pilot=True)["恢复"], 1)
            sent, _ = api.create.call_args.args
            self.assertEqual(sent, {**row, "数据范围": "企微获客"})
