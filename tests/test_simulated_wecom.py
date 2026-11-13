"""Thirty synthetic WeCom conversations using local Ollama and a simulated Feishu table."""
from contextlib import closing
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_feishu import TestTable
from wecom_segmentation import operator
from wecom_segmentation.feishu import Config, FIELD_TYPES
from wecom_segmentation.store import Store
from wecom_segmentation.window import WindowSnapshot


APPOINTMENT = "可以约 2026 年 11 月 14 日北京时间 19:00 试听。"
CASES = (
    ("资料领取", ("发我一份高二数学资料，谢谢。",), {"有效线索判定": "资料领取", "科目": []}),
    ("缺年级", ("我数学跟不上，想找补课。",), {"有效线索判定": "待识别", "学习目标": "跟课困难"}),
    ("初中生及家长", ("我家孩子上初三，想补基础。",),
     {"有效线索判定": "明确不符合", "年级": "初三", "人群标签": ["初中生", "家长"]}),
    ("高中生家长", ("我家孩子高二，物理跟不上。",),
     {"有效线索判定": "有效", "当前阶段": "有效线索", "人群标签": ["家长"]}),
    ("小红书来源", ("我是在小红书看到的。孩子高二，数学跟不上。",), {"来源": "小红书"}),
    ("抖音来源", ("我是从抖音来的。孩子高三，想备考。",), {"来源": "抖音"}),
    ("员工提及渠道", (("employee", "你是不是从抖音来的？"), "我先不说渠道。"), {"来源": "未知"}),
    ("政史地简写", ("高三 政史地",), {"科目": ["政治", "历史", "地理"]}),
    ("物化生简写", ("我的选科是物化生。",), {"科目": ["物理", "化学", "生物"]}),
    ("跨组全称", ("我选科已确定：物理、历史和地理。",), {"科目": ["物理", "历史", "地理"]}),
    ("混合简写", ("我的选科是物化地。",), {"科目": ["物理", "化学", "地理"]}),
    ("不足三门", ("我选了物理和化学，第三门还没定。",), {"科目": []}),
    ("咨询非选科", ("高二，物理、化学和生物都跟不上，想找课。",), {"科目": [], "有效线索判定": "有效"}),
    ("假设选科", ("如果我选了政史地会怎样？",), {"科目": []}),
    ("否认选科", ("我没选政史地。",), {"科目": []}),
    ("更换选科", ("我选了物化生。", "我现在改选政史地。"), {"科目": ["政治", "历史", "地理"]}),
    ("确认试听", (("employee", APPOINTMENT), "可以，就约这个时间。"), {"当前阶段": "已约试听"}),
    ("只提议试听", (("employee", APPOINTMENT), "我先考虑一下。"), {"当前有效试听场次": None}),
    ("取消试听", (("employee", APPOINTMENT), "可以，就约这个时间。", "取消这次试听。"),
     {"当前有效试听场次": None}),
    ("试听改期", (("employee", APPOINTMENT), "可以，就约这个时间。", "这个时间不行，得改期。"),
     {"当前有效试听场次": None, "跟进状态": "暂缓"}),
    ("相对日期", (("employee", "明天晚上 7 点可以试听。"), "可以，就约这个时间。"),
     {"当前有效试听场次": None, "跟进状态": "预约时间待确认"}),
    ("报名意向", ("孩子高三，想报化学备考课。我确定报名，明早付款。",),
     {"当前阶段": "有报名意向", "付费待核实": None}),
    ("已付待核实", ("我刚刚已经付款了。",), {"付费待核实": "待核实"}),
    ("未付款", ("我尚未付款。",), {"付费待核实": None}),
    ("条件付款", ("如果满意，我再付款。",), {"付费待核实": None}),
    ("到课待核实", ("我昨天已经上完试听了。",), {"到课待核实": "待核实"}),
    ("未到课", ("我还没试听。",), {"到课待核实": None}),
    ("明确暂缓", ("暂时先缓一缓，过段时间再考虑。",), {"跟进状态": "暂缓"}),
    ("明确拒绝", ("不需要课程了，以后也不用联系我。",), {"跟进状态": "拒绝"}),
    ("多轮信号纠正", ("昨天已经试听了，刚付完钱。", "我说错了，我还没试听，也还没付款。"),
     {"到课待核实": None, "付费待核实": None}),
)


@unittest.skipUnless(os.getenv("WECOM_TEST_30") == "1", "requires local Ollama and opt-in")
class SimulatedWeComTests(unittest.TestCase):
    def test_thirty_conversations_from_window_to_feishu(self):
        report = []
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(operator, LOCAL=root, PROFILES=root / "sessions.json"))
        config = Config(f"cli_{uuid4().hex}", "fixture", "baseA", "tblA")
        self.enterContext(patch.object(operator.Config, "load", return_value=config))
        table = TestTable()
        table.fields.update(FIELD_TYPES)
        self.enterContext(patch("lark_oapi.core.http.transport.requests.request", table.request))

        try:
            for index, (label, turns, expected) in enumerate(CASES, 1):
                result = {"轮次": index, "场景": label, "预期": expected, "通过": False}
                report.append(result)
                with self.subTest(round=index, scenario=label):
                    try:
                        self.run_conversation(index, turns, expected, table, result)
                    except Exception as error:
                        result["失败原因"] = str(error)
                        raise
        finally:
            if output := os.getenv("WECOM_TEST_30_REPORT"):
                Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def run_conversation(self, index, turns, expected, table, result):
        session = operator.Sessions.load().add(f"合成会话 {index:02}", customer=index % 2 == 0)
        baseline = [("employee", text) for text in ("你好，欢迎咨询。", "请问是给哪个年级的学生咨询？", "可以说说目前的情况。")]
        messages = baseline + [("customer", turn) if isinstance(turn, str) else turn for turn in turns]

        def capture(store, visible):
            snapshot = WindowSnapshot.model_validate({
                "header": [session.title],
                "messages": [{"speaker": speaker, "text": text} for speaker, text in visible],
            })
            return store.capture_window(lambda: snapshot, customer_pilot=session.customer)["新增消息"]

        with closing(Store(session.db)) as store:
            self.assertEqual(capture(store, baseline), 3)
            self.assertEqual(capture(store, messages), len(turns))
            self.assertEqual(capture(store, messages), 0)
            operator.classify_session(store)
            preview = operator.preview_fields(store)
            version = preview["同步版本"]
            operator.classify_session(store)
            self.assertEqual(operator.preview_fields(store)["同步版本"], version)
            previous_records = set(table.records)
            self.assertEqual(operator.sync_session(session, store)["新增"], 1)
            self.assertEqual(operator.sync_session(session, store)["已是最新"], 1)
            created = set(table.records) - previous_records
            self.assertEqual(len(created), 1)
            record = table.records[created.pop()]
            actual = {key: record.get(key) for key in expected}
            result.update({"实际": actual, "消息": len(messages), "同步": "新增后重复同步命中同一行",
                           "通过": actual == expected})
            self.assertEqual(actual, expected)
