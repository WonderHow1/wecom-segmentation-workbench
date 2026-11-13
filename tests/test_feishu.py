from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from requests.exceptions import Timeout

from wecom_segmentation.__main__ import main
from wecom_segmentation.domain import STUDY_PROFILE_FIELD, Extraction, Fact, StudyDetail, classify
from wecom_segmentation.feishu import FIELD_TYPES, MANUAL_FIELD, SINGLE_SELECT_OPTIONS, WECHAT_ID_FIELD, Config, Feishu, FeishuError
from wecom_segmentation.store import InputError, Store
from wecom_segmentation.sync import sync_lock, sync_table


class TestTable:
    """Use the real SDK transport and emulate only Feishu's HTTP responses."""

    def __init__(self):
        self.fields = {}
        self.field_options = {}
        self.records = {}
        self.tokens = {}
        self.create_requests = []
        self.update_requests = []
        self.lost_create_response = False
        self.rejected_creates = 0
        self.lost_update_response = False

    @staticmethod
    def reply(data=None, code=0, status=200):
        return SimpleNamespace(status_code=status, headers={"Content-Type": "application/json"},
                               content=json.dumps({"code": code, "msg": "", **(data or {})}).encode())

    def request(self, method, url, *, headers, params, data, timeout):
        body = json.loads(data) if data else {}
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return self.reply({"tenant_access_token": "test-token", "expire": 7200})
        assert headers["Authorization"] == "Bearer test-token"
        assert url.startswith("https://open.feishu.cn/open-apis/bitable/v1/apps/baseA/tables/tblA/")
        endpoint = url.split("/tables/tblA/")[1]
        if (method, endpoint) == ("GET", "fields"):
            items = [{"field_name": name, "type": kind} for name, kind in self.fields.items()]
            for item in items:
                if item["field_name"] == "跟进状态" and item["type"] == 3:
                    item["field_id"] = "fldFollowUp"
                    item["property"] = {"options": [
                        {"id": f"opt{index}", "name": name, "color": index}
                        for index, name in enumerate(self.field_options.get("跟进状态", ("推进中", "暂缓", "拒绝")))
                    ]}
            return self.reply({"data": {"items": items, "has_more": False}})
        if (method, endpoint) == ("PUT", "fields/fldFollowUp"):
            options = body["property"]["options"]
            assert options[0] == {"id": "opt0", "name": "推进中", "color": 0}
            self.field_options["跟进状态"] = [option["name"] for option in options]
            return self.reply({"data": {"field": body}})
        if (method, endpoint) == ("POST", "fields"):
            self.fields[body["field_name"]] = body["type"]
            if body.get("property", {}).get("options"):
                self.field_options[body["field_name"]] = [item["name"] for item in body["property"]["options"]]
            return self.reply({"data": {"field": body}})
        if endpoint == "records/search":
            condition, = body["filter"]["conditions"]
            assert condition["operator"] == "is" and condition["field_name"] == "线索 ID"
            lead, = condition["value"]
            items = [self.response_record(record_id) for record_id, fields in self.records.items()
                     if fields["线索 ID"] == lead]
            return self.reply({"data": {"items": items, "has_more": False}})
        if (method, endpoint) == ("POST", "records"):
            token = dict(params)["client_token"]
            assert UUID(token).version == 4
            self.create_requests.append((token, body))
            if self.rejected_creates:
                self.rejected_creates -= 1
                return self.reply(code=1254291)
            if token not in self.tokens:
                record_id = f"rec{len(self.records) + 1}"
                self.records[record_id] = body["fields"].copy()
                self.tokens[token] = record_id
            record_id = self.tokens[token]
            if self.lost_create_response:
                self.lost_create_response = False
                raise Timeout()
            return self.reply({"data": {"record": {"record_id": record_id}}})
        if method == "PUT" and endpoint.startswith("records/"):
            record_id = endpoint.split("/")[1]
            self.update_requests.append((record_id, body))
            self.records[record_id].update(body["fields"])
            if self.lost_update_response:
                self.lost_update_response = False
                raise Timeout()
            return self.reply({"data": {"record": {"record_id": record_id}}})
        raise AssertionError((method, endpoint))

    def response_record(self, record_id):
        fields = self.records[record_id]
        response = {
            "线索 ID": [{"type": "text", "text": fields["线索 ID"]}], "同步版本": float(fields["同步版本"]),
        }
        if fields.get(WECHAT_ID_FIELD):
            response[WECHAT_ID_FIELD] = [{"type": "text", "text": fields[WECHAT_ID_FIELD]}]
        if fields.get("跟进状态"):
            response["跟进状态"] = fields["跟进状态"]
        if fields.get(STUDY_PROFILE_FIELD):
            response[STUDY_PROFILE_FIELD] = [{"type": "text", "text": fields[STUDY_PROFILE_FIELD]}]
        return {"record_id": record_id, "fields": response}


class FeishuSyncTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "state.sqlite3"
        self.config = Config(f"cli_{uuid4().hex}", "dummy-secret", "baseA", "tblA")
        self.client = Feishu(self.config)
        self.table = TestTable()
        mock = patch("lark_oapi.core.http.transport.requests.request", self.table.request)
        mock.start()
        self.addCleanup(mock.stop)
        pause = patch("wecom_segmentation.sync.sleep")
        self.pause = pause.start()
        self.addCleanup(pause.stop)

    def prepare(self, store, contacts=1, message_version=1, text="合成测试", extraction=None):
        path = self.root / f"messages-{message_version}.jsonl"
        path.write_text("\n".join(json.dumps({"message_id": f"msg{index}-{message_version}", "contact_id": f"c{index}",
                                             "time": "2026-11-12T09:00:00+08:00", "speaker": "customer", "text": text})
                                  for index in range(contacts)), encoding="utf-8")
        store.import_file(path)
        for lead in store.pending():
            evidence = extraction or Extraction(facts=[])
            fields = classify(store.conversation(lead["contact_id"]), evidence, datetime.fromisoformat(lead["first_seen"]))
            fields.update({"线索 ID": lead["lead_id"], "同步版本": lead["revision"], "数据范围": "样本回放",
                           "跟进状态": "待改期" if message_version == 1 else None})
            store.save(lead, fields, evidence, "test", 1, 1)

    def sync(self, store, setup=False):
        return sync_table(self.client, store, self.config.target, setup=setup)

    def test_unbooked_lead_rolls_over_at_beijing_midnight_without_new_messages(self):
        with closing(Store(self.path)) as store:
            self.prepare(store)
            lead, = store.pending(reprocess=True)
            snapshot, = store.preview()
            snapshot.update({"跟进状态": "推进中", "_last_customer_at": "2026-11-13T15:30:00+00:00"})
            store.save(lead, snapshot, Extraction(facts=[]), "test", 1, 1, reprocess=True)
            with patch("wecom_segmentation.store.datetime", wraps=datetime) as clock:
                clock.fromisoformat.side_effect = datetime.fromisoformat
                clock.now.return_value = datetime.fromisoformat("2026-11-13T15:59:59+00:00")
                self.assertEqual(self.sync(store, setup=True)["新增"], 1)
                self.assertEqual(self.table.records["rec1"]["跟进状态"], "推进中")
                lead_id = snapshot["线索 ID"]
                self.assertEqual(store.sync_state(self.config.target, lead_id)["confirmed_follow_up"], "推进中")
                clock.now.return_value = datetime.fromisoformat("2026-11-13T16:00:00+00:00")
                self.assertEqual(store.preview()[0]["跟进状态"], "暂缓")
                self.assertEqual(self.sync(store)["更新"], 1)
                self.assertEqual(self.table.records["rec1"]["跟进状态"], "暂缓")
                self.assertEqual(store.sync_state(self.config.target, lead_id)["confirmed_follow_up"], "暂缓")
                self.assertEqual(self.sync(store)["已是最新"], 1)
                self.assertEqual(len(self.table.records), 1)

    def test_existing_follow_up_column_adds_pending_option_and_syncs_it(self):
        self.table.fields.update(FIELD_TYPES)
        self.table.field_options["跟进状态"] = ["推进中", "暂缓", "拒绝", "人工旧选项"]
        with closing(Store(self.path)) as store:
            self.prepare(store)
            lead, = store.pending(reprocess=True)
            row, = store.preview()
            row["跟进状态"] = "预约时间待确认"
            store.save(lead, row, Extraction(facts=[]), "test", 1, 1, reprocess=True)
            self.assertEqual(self.sync(store)["新增"], 1)
            self.assertEqual(self.table.records["rec1"]["跟进状态"], "预约时间待确认")
            self.assertEqual(self.table.field_options["跟进状态"],
                             ["推进中", "暂缓", "拒绝", "人工旧选项", "预约时间待确认"])
            self.assertEqual(self.sync(store)["已是最新"], 1)

    def test_feishu_receives_quoted_customer_excerpts(self):
        extraction = Extraction(facts=[Fact(kind="need", message_id="msg0-1", value="补基础")])
        with closing(Store(self.path)) as store:
            self.prepare(store, text="我想补函数基础，手机号是13800138000", extraction=extraction)
            self.assertEqual(self.sync(store, setup=True)["新增"], 1)
            self.assertEqual(self.table.records["rec1"]["证据摘要"], "“我想补函数基础”")

    def prepare_study_profile(self, store, version, excerpt):
        detail = StudyDetail(category="recent_score", excerpt=excerpt)
        evidence = Extraction(facts=[Fact(kind="study_detail", message_id=f"msg0-{version}", study_detail=detail)])
        self.prepare(store, message_version=version, text=excerpt, extraction=evidence)

    def test_study_profile_adds_text_column_and_updates_original_row_until_manually_edited(self):
        self.table.fields.update({name: kind for name, kind in FIELD_TYPES.items() if name != STUDY_PROFILE_FIELD})
        with closing(Store(self.path)) as store:
            self.prepare(store)
            result = self.sync(store)
            self.assertEqual((result["新增字段"], result["新增"]), (1, 1))
            self.assertEqual(self.table.fields[STUDY_PROFILE_FIELD], 1)
            row = self.table.records["rec1"]
            self.assertNotIn(STUDY_PROFILE_FIELD, row)
            self.prepare_study_profile(store, 2, "数学月考72分")
            self.assertEqual(self.sync(store)["更新"], 1)
            self.assertEqual(row[STUDY_PROFILE_FIELD], "数学月考72分")

        with closing(Store(self.path)) as store:
            self.prepare_study_profile(store, 3, "数学期中85分")
            self.assertEqual(self.sync(store)["更新"], 1)
            self.assertEqual(row[STUDY_PROFILE_FIELD], "数学期中85分")
            row[STUDY_PROFILE_FIELD] = "手填：重点跟进函数"
            self.prepare_study_profile(store, 4, "数学期末91分")
            self.assertEqual(self.sync(store)["更新"], 1)
            self.assertEqual(row[STUDY_PROFILE_FIELD], "手填：重点跟进函数")
            self.assertEqual(self.sync(store)["已是最新"], 1)
            self.assertEqual(len(self.table.records), 1)

    def test_study_profile_survives_lost_ack_and_respects_manual_clear_before_retry(self):
        with closing(Store(self.path)) as store:
            self.prepare_study_profile(store, 1, "数学月考72分")
            self.table.lost_create_response = True
            self.assertEqual(self.sync(store, setup=True)["恢复"], 1)
            self.prepare_study_profile(store, 2, "数学期中85分")
            self.table.lost_update_response = True
            self.assertEqual(self.sync(store)["恢复"], 1)
            self.prepare_study_profile(store, 3, "数学月考89分")
            self.assertEqual(self.sync(store)["更新"], 1)
            row = self.table.records["rec1"]
            self.assertEqual(row.pop(STUDY_PROFILE_FIELD), "数学月考89分")

            self.prepare_study_profile(store, 4, "数学期末91分")
            fields, = store.preview()
            store.begin_sync(self.config.target, fields, "rec1")
            self.assertEqual(self.sync(store)["恢复"], 1)
            self.assertNotIn(STUDY_PROFILE_FIELD, row)
            self.assertNotIn(STUDY_PROFILE_FIELD, self.table.update_requests[-1][1]["fields"])

    def test_existing_sync_database_preserves_prepopulated_study_profile(self):
        with closing(Store(self.path)) as store:
            self.prepare(store)
            self.assertEqual(self.sync(store, setup=True)["新增"], 1)
            with store.db:
                store.db.execute("ALTER TABLE feishu_sync DROP COLUMN synced_study_profile_hash")
        row = self.table.records["rec1"]
        row[STUDY_PROFILE_FIELD] = "运营预先填写的学情"

        with closing(Store(self.path)) as store:
            self.prepare_study_profile(store, 2, "数学月考72分")
            self.assertEqual(self.sync(store)["更新"], 1)
            self.assertEqual(row[STUDY_PROFILE_FIELD], "运营预先填写的学情")
            self.assertEqual(len(self.table.records), 1)

    def test_feishu_creates_fixed_choices_and_accepts_existing_text_columns(self):
        with closing(Store(self.path)) as store:
            self.prepare(store)
            self.sync(store, setup=True)
            self.assertEqual(self.table.fields["来源"], 3)
            self.assertEqual(self.table.field_options["来源"], ["抖音", "小红书", "其他", "未知"])
            self.assertEqual(self.table.field_options["科目"], ["物理", "化学", "生物", "政治", "历史", "地理"])
            self.assertIn("预算", self.table.field_options["主要卡点"])
            self.table.fields.update(dict.fromkeys(SINGLE_SELECT_OPTIONS, 1))
            self.assertEqual(self.sync(store)["已是最新"], 1)

    def test_existing_table_adds_wechat_id_and_sync_preserves_manual_value(self):
        self.table.fields.update({name: kind for name, kind in FIELD_TYPES.items() if name != WECHAT_ID_FIELD})
        with closing(Store(self.path)) as store:
            self.prepare(store)
            self.assertEqual(self.sync(store)["新增字段"], 1)
            self.assertEqual(self.table.fields[WECHAT_ID_FIELD], 1)
            self.assertNotIn(WECHAT_ID_FIELD, self.table.records["rec1"])
            self.table.records["rec1"][WECHAT_ID_FIELD] = "wx_example_001"

            self.prepare(store, message_version=2, text="我的微信号是 wx_detected")
            self.assertEqual(self.sync(store)["更新"], 1)
            self.assertEqual(self.table.records["rec1"][WECHAT_ID_FIELD], "wx_example_001")
            self.assertNotIn(WECHAT_ID_FIELD, self.table.update_requests[0][1]["fields"])

    def test_wechat_id_backfills_blank_cell_and_repeated_sync_keeps_manual_edit(self):
        with closing(Store(self.path)) as store:
            self.prepare(store, text="微信ID：wx_detected")
            self.assertEqual(self.sync(store, setup=True)["新增"], 1)
            row = self.table.records["rec1"]
            self.assertEqual(row.pop(WECHAT_ID_FIELD), "wx_detected")
            self.assertEqual(self.sync(store)["更新"], 1)
            self.assertEqual(row[WECHAT_ID_FIELD], "wx_detected")
            row[WECHAT_ID_FIELD] = "wx_manual"
            self.assertEqual(self.sync(store)["已是最新"], 1)
            self.assertEqual(row[WECHAT_ID_FIELD], "wx_manual")

    def test_retry_omits_wechat_id_filled_after_update_was_queued(self):
        with closing(Store(self.path)) as store:
            self.prepare(store)
            self.sync(store, setup=True)
            self.prepare(store, message_version=2, text="加我微信 wx_detected")
            fields, = store.preview()
            store.begin_sync(self.config.target, fields, "rec1")
            self.table.records["rec1"][WECHAT_ID_FIELD] = "wx_manual"
            self.sync(store)
            self.assertEqual(self.table.records["rec1"][WECHAT_ID_FIELD], "wx_manual")
            self.assertNotIn(WECHAT_ID_FIELD, self.table.update_requests[-1][1]["fields"])

    def test_eight_rows_create_update_dedupe_and_clear_fields(self):
        with closing(Store(self.path)) as store:
            self.prepare(store, contacts=8)
            new = self.sync(store, setup=True)
            self.assertEqual(new["新增字段"], len(FIELD_TYPES))
            self.assertEqual(new["新增"], 8)
            self.table.records["rec1"][MANUAL_FIELD] = "保留原备注"

            same = self.sync(store, setup=True)
            self.assertEqual((same["已是最新"], same["新增字段"]), (8, 0))
            self.prepare(store, message_version=2)
            update = self.sync(store)
            self.assertEqual((update["更新"], update["已是最新"], update["失败"]), (1, 7, 0))
            self.assertEqual(len(self.table.records), 8)
            self.assertEqual(self.table.records["rec1"]["同步版本"], 2)
            self.assertIsNone(self.table.records["rec1"]["跟进状态"])
            self.assertEqual(self.table.records["rec1"][MANUAL_FIELD], "保留原备注")
            self.assertNotIn(MANUAL_FIELD, self.table.update_requests[0][1]["fields"])
            self.assertEqual(len(self.table.create_requests), 8)

    def test_lost_create_or_update_acknowledgement_recovers_by_query(self):
        self.table.lost_create_response = True
        with closing(Store(self.path)) as store:
            self.prepare(store)
            result = self.sync(store, setup=True)
            self.assertEqual((result["恢复"], result["重试"], result["失败"]), (1, 1, 0))
            self.assertEqual(len(self.table.create_requests), 1)
            self.assertEqual(len(self.table.records), 1)
            self.table.lost_update_response = True
            self.prepare(store, message_version=2)
            result = self.sync(store)
            self.assertEqual((result["恢复"], result["重试"], result["失败"]), (1, 1, 0))
            self.assertEqual(len(self.table.update_requests), 1)
            self.assertEqual(self.table.records["rec1"]["同步版本"], 2)

    def test_existing_table_adds_audience_field_on_normal_sync(self):
        self.table.fields.update({name: kind for name, kind in FIELD_TYPES.items() if name != "人群标签"})
        with closing(Store(self.path)) as store:
            self.prepare(store)
            result = self.sync(store)
            self.assertEqual((result["新增字段"], result["新增"]), (1, 1))
            self.assertEqual(self.table.fields["人群标签"], 4)
            self.assertEqual(self.table.records["rec1"]["人群标签"], [])
            self.assertEqual(self.sync(store)["新增字段"], 0)

    def test_failed_create_keeps_token_and_payload_across_reopen(self):
        self.table.rejected_creates = 3
        with closing(Store(self.path)) as store:
            self.prepare(store)
            failed = self.sync(store, setup=True)
            self.assertEqual((failed["失败"], failed["重试"]), (1, 2))
            self.assertEqual(len(self.table.records), 0)
        with closing(Store(self.path)) as store:
            self.prepare(store, message_version=2)
            recovered = self.sync(store)
            self.assertEqual((recovered["更新"], recovered["失败"]), (1, 0))
            self.assertEqual(self.table.create_requests, [self.table.create_requests[0]] * 4)
            self.assertEqual(len(self.table.records), 1)
            self.assertEqual(self.table.records["rec1"]["同步版本"], 2)

    def test_duplicate_or_replaced_remote_row_blocks_writes(self):
        with closing(Store(self.path)) as store:
            self.prepare(store)
            self.sync(store, setup=True)
            self.table.records["rec2"] = self.table.records["rec1"].copy()
            self.assertEqual(self.sync(store)["失败"], 1)
            del self.table.records["rec1"]
            self.assertEqual(self.sync(store)["失败"], 1)
            self.assertEqual(len(self.table.create_requests), 1)
            self.assertFalse(self.table.update_requests)

    def test_newer_remote_version_is_left_alone(self):
        with closing(Store(self.path)) as store:
            self.prepare(store)
            self.sync(store, setup=True)
            self.table.records["rec1"]["同步版本"] = 4
            self.assertEqual(self.sync(store)["远端较新"], 1)
            self.assertEqual(self.table.records["rec1"]["同步版本"], 4)
            self.assertFalse(self.table.update_requests)

    def test_incomplete_search_response_cannot_trigger_create(self):
        self.table.fields.update(FIELD_TYPES)
        original = self.client.request

        def incomplete_search(method, suffix, *args, **kwargs):
            if suffix == "records/search":
                return {"total": 1, "has_more": False}
            return original(method, suffix, *args, **kwargs)

        with closing(Store(self.path)) as store, patch.object(self.client, "request", incomplete_search):
            self.prepare(store)
            self.assertEqual(self.sync(store)["失败"], 1)
        self.assertFalse(self.table.create_requests)

    def test_invalid_existing_field_type_stops_before_setup(self):
        self.table.fields["同步版本"] = 1
        with closing(Store(self.path)) as store:
            self.prepare(store)
            with self.assertRaisesRegex(FeishuError, "同名字段类型不符"):
                self.sync(store, setup=True)
            self.assertEqual(self.table.fields, {"同步版本": 1})
            self.assertFalse(self.table.records)

    def test_second_sync_cannot_acquire_database_lock(self):
        with sync_lock(self.path), self.assertRaisesRegex(InputError, "已有飞书同步任务"):
            with sync_lock(self.path):
                self.fail("acquired existing lock")

    def test_cli_loads_local_configuration_and_syncs(self):
        config_path = self.root / "config.env"
        config_path.write_text(f"FEISHU_APP_ID={self.config.app_id}\nFEISHU_APP_SECRET=dummy-secret\n"
                               "FEISHU_TABLE_URL=https://team.feishu.cn/base/baseA?table=tblA&view=vewA\n")
        with closing(Store(self.path)) as store:
            self.prepare(store)
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--db", str(self.path), "sync", "--setup", "--config", str(config_path)])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["新增"], 1)
        self.assertNotIn("dummy-secret", stdout.getvalue())
