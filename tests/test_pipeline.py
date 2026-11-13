from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wecom_segmentation import ollama
from wecom_segmentation.__main__ import main
from wecom_segmentation.domain import STUDY_PROFILE_FIELD, Extraction, Fact, Message, StudyDetail


SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "conversations.jsonl"
EXPECTED = [
    ("资料领取", "待识别", "暂缓", None, None),
    ("待识别", "待识别", "暂缓", None, None),
    ("有效", "有效线索", "暂缓", None, None),
    ("有效", "已约试听", "推进中", None, None),
    ("有效", "有效线索", "暂缓", None, None),
    ("有效", "有效线索", "暂缓", "待核实", "待核实"),
    ("待识别", "待识别", "暂缓", None, None),
    ("有效", "有报名意向", "暂缓", None, None),
]


def message(message_id, text, contact_id="person-x", time="2026-11-12T09:00:00+08:00", **fields):
    return {"message_id": message_id, "contact_id": contact_id, "time": time,
            "speaker": "customer", "text": text, **fields}


def facts(*items):
    return Extraction(facts=[Fact(kind=kind, message_id=message_id, value=value) for kind, message_id, value in items])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / "state.sqlite3"

    def run_cli(self, *args, exit_code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--db", str(self.database), *args])
        self.assertEqual(result, exit_code, stderr.getvalue() or stdout.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else stderr.getvalue()

    def import_messages(self, name, *messages, exit_code=0):
        path = self.root / name
        path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in messages), encoding="utf-8")
        return self.run_cli("import", "--file", str(path), exit_code=exit_code)

    def test_late_grade_updates_same_lead_and_duplicate_keeps_version(self):
        initial = message("first", "数学跟不上，想补课", time="2026-11-12T09:05:00+08:00")
        self.import_messages("first.jsonl", initial)
        first_facts = facts(("need", "first", "跟课困难"))
        with patch.object(ollama, "extract", return_value=first_facts):
            self.run_cli("classify")
        first = self.run_cli("sync", "--dry-run")[0]
        self.assertEqual(first["有效线索判定"], "待识别")

        late_grade = message("late", "孩子高二")
        self.import_messages("late.jsonl", late_grade)
        self.assertEqual(self.run_cli("sync", "--dry-run"), [])
        second_facts = facts(("grade", "late", "高二"), ("need", "first", "跟课困难"))
        with patch.object(ollama, "extract", return_value=second_facts) as extract:
            self.run_cli("classify")
            self.assertEqual([item.message_id for item in extract.call_args.args[0]], ["late", "first"])
        second = self.run_cli("sync", "--dry-run")[0]
        self.assertEqual(first["线索 ID"], second["线索 ID"])
        self.assertEqual(second["同步版本"], 2)
        self.assertEqual(second["有效线索判定"], "有效")
        self.assertEqual(second["首次判定有效时间"], int(datetime.fromisoformat(initial["time"]).timestamp() * 1000))

        duplicate = self.import_messages("duplicate.jsonl", initial, late_grade)
        self.assertEqual(duplicate, {"导入消息": 0, "重复消息": 2, "待重算联系人": 0})
        with patch.object(ollama, "extract") as extract:
            self.run_cli("classify")
            extract.assert_not_called()
        self.assertEqual(self.run_cli("sync", "--dry-run"), [second])

    def test_preview_quotes_current_customer_evidence_and_omits_contact_details(self):
        self.import_messages(
            "quotes.jsonl",
            message("old", "我高一", time="2026-11-12T09:01:00+08:00"),
            message("staff", "建议先补函数基础", time="2026-11-12T09:02:00+08:00", speaker="employee"),
            message("new", "说错了，我现在上高二。函数图像跟不上，想补基础。我的微信号是 wx_example",
                    time="2026-11-12T09:03:00+08:00"),
        )
        extraction = facts(("grade", "old", "高一"), ("grade", "new", "高二"), ("need", "new", "补基础"))
        with patch.object(ollama, "extract", return_value=extraction):
            self.run_cli("classify")
        row, = self.run_cli("sync", "--dry-run")
        self.assertEqual(row["证据摘要"], "“我现在上高二”\n“函数图像跟不上”\n“想补基础”")
        self.assertEqual(row["微信ID"], "wx_example")

    def test_bad_or_conflicting_input_rolls_back_entire_file(self):
        original = message("original", "你好")
        self.import_messages("original.jsonl", original)
        result = self.import_messages("invalid.jsonl", message("other", "你好", contact_id="other"),
                                      message("original", "改写了原文"), exit_code=1)
        self.assertIn("第 2 行", result)
        self.assertEqual(self.run_cli("status")["联系人"], 1)
        result = self.import_messages("bad-date.jsonl", message("invalid-time", "你好", time="2026-11-12T09:00:00"),
                                      exit_code=1)
        self.assertIn("第 1 行", result)

    def test_recalled_payment_disappears_and_employee_evidence_is_rejected(self):
        self.import_messages("start.jsonl", message("paid", "我已付款"),
                             message("staff", "可以付款了", speaker="employee"))
        with patch.object(ollama, "extract", return_value=facts(("payment", "paid", None))):
            self.run_cli("classify")
        self.assertEqual(self.run_cli("sync", "--dry-run")[0]["付费待核实"], "待核实")

        self.import_messages("recall.jsonl", message("recall", None, kind="recall", target_message_id="paid"))
        for evidence in ("paid", "staff", "unknown"):
            with patch.object(ollama, "extract", return_value=facts(("payment", evidence, None))) as extract:
                result = self.run_cli("classify", exit_code=1)
                self.assertEqual([item.message_id for item in extract.call_args.args[0]], ["staff"])
            self.assertEqual(result["分层失败"], 1)
            self.assertEqual(self.run_cli("sync", "--dry-run"), [])
        with patch.object(ollama, "extract", return_value=facts()):
            self.run_cli("classify")
        self.assertIsNone(self.run_cli("sync", "--dry-run")[0]["付费待核实"])

    def test_moved_file_requires_reimport_before_replay(self):
        initial = message("a", "你好")
        self.import_messages("before.jsonl", initial)
        (self.root / "before.jsonl").rename(self.root / "after.jsonl")
        with patch.object(ollama, "extract", return_value=facts()) as extract:
            self.assertEqual(self.run_cli("classify", exit_code=1)["分层失败"], 1)
            extract.assert_not_called()
            self.run_cli("import", "--file", str(self.root / "after.jsonl"))
            self.assertEqual(self.run_cli("classify")["本次已分层"], 1)

    def test_import_during_inference_leaves_new_revision_pending(self):
        self.import_messages("before.jsonl", message("before", "我高二，想补基础"))

        def import_new_message(_):
            self.import_messages("new.jsonl", message("new", "说错了，我孩子现在读初三",
                                                      time="2026-11-12T09:01:00+08:00"))
            return facts(("grade", "before", "高二"), ("need", "before", "补基础"))

        with patch.object(ollama, "extract", side_effect=import_new_message):
            self.assertEqual(self.run_cli("classify")["待分层"], 1)
        self.assertEqual(self.run_cli("sync", "--dry-run"), [])
        with patch.object(ollama, "extract", return_value=facts(("grade", "new", "非高中"))):
            self.assertEqual(self.run_cli("classify")["本次已分层"], 1)
        current = self.run_cli("sync", "--dry-run")[0]
        self.assertEqual(current["同步版本"], 2)
        self.assertEqual(current["有效线索判定"], "明确不符合")

    def assert_sample_pipeline(self):
        sample = self.root / "with-selection.jsonl"
        sample.write_text(SAMPLES.read_text(encoding="utf-8").replace(
            "孩子高二，数学跟不上。", "孩子高二，数学跟不上。他已经确定选物理、化学和生物。",
        ), encoding="utf-8")
        self.run_cli("import", "--file", str(sample))
        self.assertEqual(self.run_cli("classify")["本次已分层"], len(EXPECTED))
        preview = self.run_cli("sync", "--dry-run")
        fields = ("有效线索判定", "当前阶段", "跟进状态", "到课待核实", "付费待核实")
        for index, (row, expected) in enumerate(zip(preview, EXPECTED, strict=True), 1):
            with self.subTest(conversation=index):
                self.assertEqual(tuple(row[field] for field in fields), expected)
        self.assertEqual(preview[2]["来源"], "小红书")
        self.assertEqual(preview[2]["科目"], ["物理", "化学", "生物"])
        self.assertEqual(preview[1]["科目"], [])
        self.assertEqual(preview[7]["来源"], "抖音")
        appointment = datetime.fromisoformat("2026-11-14T19:00:00+08:00")
        self.assertEqual(preview[3]["当前有效试听场次"], int(appointment.timestamp() * 1000))
        self.assertIsNotNone(preview[4]["首次确认预约时间"])
        self.assertIsNone(preview[4]["当前有效试听场次"])
        self.assertEqual(self.run_cli("import", "--file", str(sample))["重复消息"], 14)
        self.assertEqual(self.run_cli("classify")["本次已分层"], 0)
        self.assertEqual(self.run_cli("sync", "--dry-run"), preview)

    def test_sample_pipeline_with_fixed_extractions(self):
        appointment = "2026-11-14T19:00:00+08:00"
        extractions = [
            facts(("materials", "m01", None)),
            facts(("need", "m02", "跟课困难")),
            facts(("grade", "m03", "高二"), ("need", "m03", "跟课困难"), ("source", "m03", "小红书"),
                  ("subject_selection", "m03", "物理、化学、生物")),
            facts(("grade", "m04", "高一"), ("need", "m04", "补基础"), ("booking", "m06", appointment)),
            facts(("grade", "m07", "高三"), ("need", "m07", "备考"), ("booking", "m09", appointment),
                  ("cancel_booking", "m10", None)),
            facts(("grade", "m11", "高二"), ("need", "m11", "提高成绩"), ("attendance", "m11", None),
                  ("payment", "m11", None)),
            facts(("deny_attendance", "m13", None)),
            facts(("grade", "m14", "高三"), ("need", "m14", "备考"), ("enrollment_intent", "m14", None),
                  ("source", "m14", "抖音")),
        ]
        with patch.object(ollama, "extract", side_effect=extractions) as extract:
            self.assert_sample_pipeline()
            self.assertEqual(extract.call_count, len(EXPECTED))

    @unittest.skipUnless(os.environ.get("WECOM_TEST_OLLAMA") == "1", "requires local qwen3:4b")
    def test_real_ollama_on_synthetic_conversations(self):
        self.assert_sample_pipeline()


class OllamaTests(unittest.TestCase):
    def test_explicit_repeated_score_loss_makes_high_school_lead_effective(self):
        from wecom_segmentation.domain import classify

        sample = Message.model_validate(message(
            "student", "我在南京市，我高二，选科是政史地，数学月考72分，函数题容易丢分，预算有限。"))
        extraction = ollama.extract([sample], observer=lambda *_: ollama.Observation())
        row = classify([sample], extraction, sample.time)
        self.assertEqual((row["有效线索判定"], row["学习目标"]), ("有效", "提高成绩"))
        self.assertIn("“函数题容易丢分”", row["证据摘要"])

    def test_relative_trial_waits_for_date_without_rolling_over_to_paused(self):
        from wecom_segmentation.domain import classify, current_follow_up

        turns = [("employee", "明晚7点可以试听。"), ("customer", "好，就约这个时间。")]

        def result(turns):
            messages = [Message.model_validate({**message(str(index), text), "speaker": speaker})
                        for index, (speaker, text) in enumerate(turns)]
            extraction = ollama.extract(messages, observer=lambda *_: ollama.Observation(), allow_relative_booking=False)
            row = classify(messages, extraction, messages[0].time)
            current_follow_up(row, messages[-1].time, datetime.fromisoformat("2026-11-13T00:05:00+08:00"))
            return row

        row = result(turns)
        self.assertEqual(row["跟进状态"], "预约时间待确认")
        self.assertIsNone(row["当前有效试听场次"])
        self.assertIn("“就约这个时间”", row["证据摘要"])
        self.assertEqual(result(turns + [("customer", "取消这次试听。")])["跟进状态"], "暂缓")
        confirmed = result(turns + [("employee", "2026年11月14日 19:00可以试听。"), ("customer", "可以。")])
        self.assertEqual((confirmed["跟进状态"], confirmed["当前阶段"]), ("推进中", "已约试听"))
        for proposal, reply in [
            (turns[0][1], "好，我明天再确认试听。"),
            (turns[0][1], "可以约明晚7点试听吗？"),
            ("明晚7点给你发资料。", "可以。"),
        ]:
            with self.subTest(proposal=proposal, reply=reply):
                self.assertEqual(result([("employee", proposal), ("customer", reply)])["跟进状态"], "暂缓")

    def test_study_profile_uses_latest_explicit_customer_details(self):
        from wecom_segmentation.domain import classify

        turns = [
            ("customer", "我在南京市，高二，最近数学月考72分，函数题容易丢分，微信号是wx_example"),
            ("employee", "你上次期中是不是80分？"),
            ("customer", "如果考到95分我就不用补课了，想提高到100分"),
            ("customer", "数学期中实际考了85分，我的手机号是13800138000"),
        ]
        messages = [Message.model_validate({**message(str(i), text), "speaker": speaker})
                    for i, (speaker, text) in enumerate(turns)]
        predictions = {
            messages[0].text: [("region", "南京市"), ("grade", "高二"), ("recent_score", "最近数学月考72分"),
                               ("difficulty", "函数题容易丢分"), ("weak_subject", "物理很差")],
            messages[1].text: [("recent_score", "期中是不是80分")],
            messages[2].text: [("recent_score", "考到95分"), ("recent_score", "提高到100分")],
            messages[3].text: [("recent_score", "数学期中实际考了85分"),
                               ("recent_score", "我的手机号是13800138000")],
        }

        def observe(turn, context):
            return ollama.Observation(study_details=[StudyDetail(category=kind, excerpt=excerpt)
                                                     for kind, excerpt in predictions[turn["text"]]])

        extraction = ollama.extract(messages, observer=observe)
        row = classify(messages, extraction, messages[0].time)
        self.assertEqual(row[STUDY_PROFILE_FIELD], "南京市；高二；数学期中实际考了85分；函数题容易丢分")
        self.assertEqual(row["证据摘要"], "“函数题容易丢分”")

    def test_study_profile_leaves_material_requests_and_unconfirmed_details_empty(self):
        from wecom_segmentation.domain import classify

        sample = Message.model_validate(message("one", "发我高二的资料，想考90分，数学月考72分？函数难吗？"))
        observation = ollama.Observation(study_details=[
            StudyDetail(category="grade", excerpt="高二"),
            StudyDetail(category="recent_score", excerpt="90分"),
            StudyDetail(category="recent_score", excerpt="数学月考72分"),
            StudyDetail(category="difficulty", excerpt="函数难"),
        ])
        extraction = ollama.extract([sample], observer=lambda *_: observation)
        self.assertIsNone(classify([sample], extraction, sample.time)[STUDY_PROFILE_FIELD])

    def test_leading_standalone_grade_survives_missing_model_grade(self):
        from wecom_segmentation.domain import classify

        sample = Message.model_validate(message("one", "高二，物理、化学和生物都跟不上，想找课。"))
        extraction = ollama.extract([sample], observer=lambda *_: ollama.Observation())
        row = classify([sample], extraction, sample.time)
        self.assertEqual((row["年级"], row["有效线索判定"], row["科目"]), ("高二", "有效", []))

    @unittest.skipUnless(os.environ.get("WECOM_TEST_OLLAMA") == "1", "requires local qwen3:4b")
    def test_real_ollama_extracts_and_refreshes_study_profile(self):
        from wecom_segmentation.domain import classify

        first = Message.model_validate(message(
            "first", "我在南京市，我高二，最近数学月考72分，函数题容易丢分，微信号是wx_example"))
        updated = Message.model_validate(message("updated", "数学期中考了85分"))
        row = classify([first], ollama.extract([first]), first.time)
        self.assertIn("72分", row[STUDY_PROFILE_FIELD])
        self.assertIn("高二", row[STUDY_PROFILE_FIELD])
        self.assertNotIn("wx_example", row[STUDY_PROFILE_FIELD])
        messages = [first, updated]
        row = classify(messages, ollama.extract(messages), first.time)
        self.assertIn("85分", row[STUDY_PROFILE_FIELD])
        self.assertIn("函数", row[STUDY_PROFILE_FIELD])
        self.assertNotIn("72分", row[STUDY_PROFILE_FIELD])

    def test_wechat_id_requires_clear_customer_evidence(self):
        from wecom_segmentation.domain import classify

        cases = (
            ([("customer", "您好，我的微信号是 wx_example，谢谢")], "wx_example"),
            ([("customer", "微信ID：Ab123_789")], "Ab123_789"),
            ([("employee", "方便留一下您的微信号吗？"), ("customer", "wx_reply_demo。")], "wx_reply_demo"),
            ([("employee", "方便留一下您的微信号吗？"), ("customer", "13800138000")], None),
            ([("customer", "wx_unprompted_demo")], None),
            ([("employee", "我的微信号是 wx_employee"), ("customer", "收到")], None),
            ([("customer", "老师的微信号是 wx_teacher"), ("customer", "电话是 13800138000")], None),
            ([("customer", "我的微信号是 example@example.com")], None),
            ([("customer", "我的微信号是 wx_first"), ("customer", "我的微信号是 wx_second")], None),
        )
        for turns, expected in cases:
            with self.subTest(turns=turns):
                messages = [Message.model_validate(message(str(index), text, speaker=speaker))
                            for index, (speaker, text) in enumerate(turns)]
                self.assertEqual(classify(messages, Extraction(facts=[]), messages[0].time)["微信ID"], expected)

    def test_source_withdrawal_and_budget_are_grounded_in_customer_words(self):
        from wecom_segmentation.domain import classify

        texts = ("我选了政史地，预算也有限。", "渠道先不登记。", "后来改了，第三门现在还没定。",
                 "我回去还要和家长商量。")
        messages = [Message.model_validate(message(str(index), text)) for index, text in enumerate(texts)]
        observations = iter((ollama.Observation(), ollama.Observation(source="其他"), ollama.Observation(),
                             ollama.Observation(blocker="家长决策")))
        extraction = ollama.extract(messages, observer=lambda *_: next(observations))
        row = classify(messages, extraction, messages[0].time)
        self.assertEqual((row["来源"], row["来源依据"], row["科目"], row["主要卡点"]),
                         ("未知", None, [], "预算"))

        referred = Message.model_validate(message("referral", "我是朋友介绍来的。"))
        extraction = ollama.extract([referred], observer=lambda *_: ollama.Observation())
        self.assertEqual(classify([referred], extraction, referred.time)["来源"], "其他")

    def test_grade_and_role_require_evidence_from_the_current_message(self):
        from wecom_segmentation.domain import classify

        cases = (
            ("我现在上初二，几何课有点跟不上。", "初二", "学生", ["初中生"]),
            ("我是学生家长，想问有没有补基础的课。", None, "家长", ["家长"]),
            ("更正一下，我家孩子已经是高一了。", "高一", "家长", ["家长"]),
            ("我想拿一份高三政史地资料。", None, "学生", []),
        )
        for text, grade, role, tags in cases:
            with self.subTest(text=text):
                sample = Message.model_validate(message("current", text))
                observation = ollama.Observation(grade="初三" if tags else "高三",
                                                 role="家长" if tags else None)
                extraction = ollama.extract([sample], observer=lambda *_: observation)
                row = classify([sample], extraction, sample.time)
                self.assertEqual((row["年级"], row["咨询人身份"], row["人群标签"]), (grade, role, tags))

    def test_confirmed_new_appointment_overrides_stale_model_status(self):
        from wecom_segmentation.domain import classify

        texts = (
            ("customer", "暂时不考虑，过段时间再说。"),
            ("employee", "改到 2026 年 11 月 23 日北京时间 19:30 试听。"),
            ("customer", "好，就约这个时间。"),
        )
        messages = [Message.model_validate(message(str(index), text, speaker=speaker))
                    for index, (speaker, text) in enumerate(texts)]
        observations = iter((ollama.Observation(follow_up="暂缓"),
                             ollama.Observation(cancel_booking=True, follow_up="暂缓")))
        extraction = ollama.extract(messages, observer=lambda *_: next(observations))
        row = classify(messages, extraction, messages[0].time)
        self.assertEqual((row["当前阶段"], row["跟进状态"]), ("已约试听", "推进中"))

    def test_role_and_refusal_require_explicit_customer_evidence(self):
        from wecom_segmentation.domain import classify

        for text, role, status in (
            ("想先看看课程安排。", "学生", "推进中"),
            ("家长还没决定，我先了解。", "学生", "推进中"),
            ("你以为我是家长吗？", "学生", "推进中"),
            ("我是孩子的家长。", "家长", "推进中"),
            ("不用再联系我。", "学生", "拒绝"),
        ):
            with self.subTest(text=text):
                msg = Message.model_validate(message("a", text))
                extraction = ollama.extract([msg], observer=lambda *_: ollama.Observation(
                    role="家长", follow_up="明确拒绝"))
                row = classify([msg], extraction, msg.time)
                self.assertEqual((row["咨询人身份"], row["跟进状态"]), (role, status))

    def test_audience_tags_distinguish_student_grade_and_speaker_role(self):
        from wecom_segmentation.domain import classify

        cases = (
            ("我家孩子现在上初三，想补基础。", None, "初三", ["初中生", "家长"], "明确不符合"),
            ("我读初中，想提高成绩。", "学生", "初中", ["初中生"], "明确不符合"),
            ("我是高二学生，物理跟不上，要和家长商量。", "家长", "高二", [], "有效"),
            ("我是家长。孩子高一，数学跟不上。", None, "高一", ["家长"], "有效"),
            ("我儿子上小学，想补基础。", None, "非高中", ["家长"], "明确不符合"),
            ("我是高二学生。", "学生", "高二", [], "待识别"),
        )
        for text, role, grade, tags, eligibility in cases:
            with self.subTest(text=text):
                sample = Message.model_validate(message("audience", text))
                observation = ollama.Observation(role=role)
                extraction = ollama.extract([sample], observer=lambda *_: observation)
                row = classify([sample], extraction, sample.time)
                self.assertEqual((row["年级"], row["人群标签"], row["有效线索判定"]), (grade, tags, eligibility))

    def test_explicit_shorthand_survives_partial_or_missing_model_result(self):
        from wecom_segmentation.domain import classify

        cases = (
            ("选课是政史地三门", ["政治"], ["政治", "历史", "地理"]),
            ("高三 政史地", ["政治"], ["政治", "历史", "地理"]),
            ("高三政史地资料", None, []),
            ("我选了地政史", None, ["政治", "历史", "地理"]),
            ("我确定选物化地", None, ["物理", "化学", "地理"]),
            ("如果我选了政史地会怎样？", None, []),
            ("我没选政史地", ["政治", "历史", "地理"], []),
            ("我选了政史地还是物化生？", ["政治", "历史", "地理"], []),
            ("我选了政政史", None, []),
            ("我选了政史地物", None, []),
        )
        for text, model_selection, expected in cases:
            with self.subTest(text=text):
                sample = Message.model_validate(message("a", text))
                observation = ollama.Observation(subject_selection=model_selection)
                extraction = ollama.extract([sample], observer=lambda *_: observation)
                self.assertEqual(classify([sample], extraction, sample.time)["科目"], expected)

    def test_confirmed_subject_selection_replaces_previous_combination(self):
        from wecom_segmentation.domain import classify

        messages = [Message.model_validate(message(str(index), text)) for index, text in enumerate((
            "数学和英语跟不上，想补课。",
            "我选了物化生。",
            "如果选政治、历史、地理会怎样？",
            "我现在改选历史、地理、政治。",
            "最后又改了，第三门还没定。",
        ))]
        observations = iter((
            ollama.Observation(need="跟课困难"),
            ollama.Observation(subject_selection=["生物", "物理", "化学"]),
            ollama.Observation(subject_selection=["政治", "历史", "地理"]),
            ollama.Observation(subject_selection=["地理", "政治", "历史"]),
            ollama.Observation(subject_selection=[]),
        ))
        extraction = ollama.extract(messages, observer=lambda *_: next(observations))
        expectations = ([], ["物理", "化学", "生物"], ["物理", "化学", "生物"],
                        ["政治", "历史", "地理"], [])
        for count, expected in enumerate(expectations, 1):
            visible = messages[:count]
            facts = Extraction(facts=[fact for fact in extraction.facts if int(fact.message_id) < count])
            self.assertEqual(classify(visible, facts, visible[0].time)["科目"], expected)

    def test_request_is_local_and_requires_structured_output(self):
        sample = Message.model_validate(message("a", "你好"))
        response = BytesIO(b'{"message":{"content":"{}"},"done":true}')
        response.status = 200
        with patch.object(ollama, "HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.return_value = response
            self.assertEqual(ollama.extract([sample]).facts, [])
            factory.assert_called_once_with("127.0.0.1", 11434, timeout=120)
            method, path = connection.request.call_args.args
            self.assertEqual((method, path), ("POST", "/api/chat"))
            body = json.loads(connection.request.call_args.kwargs["body"])
            self.assertEqual(body["model"], "qwen3:4b")
            self.assertEqual(body["format"], ollama.Observation.model_json_schema())
            self.assertNotIn("person-x", body["messages"][1]["content"])
            connection.close.assert_called_once()

    def test_invalid_fact_does_not_echo_personal_data(self):
        sample = Message.model_validate(message("a", "你好"))
        content = json.dumps({"grade": "电话123456"})
        response = BytesIO(json.dumps({"message": {"content": content}}).encode())
        response.status = 200
        with patch.object(ollama, "HTTPConnection") as factory:
            factory.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(ollama.ExtractionError, "无效的结构化结果") as error:
                ollama.extract([sample])
        self.assertNotIn("123456", str(error.exception))

    def test_timeout_reports_local_inference_failure(self):
        sample = Message.model_validate(message("a", "你好"))
        with patch.object(ollama, "HTTPConnection") as factory:
            factory.return_value.getresponse.side_effect = TimeoutError
            with self.assertRaisesRegex(ollama.ExtractionError, "推理超时"):
                ollama.extract([sample])

    def test_model_payment_and_enrollment_claims_require_matching_evidence(self):
        observation = ollama.Observation(enrollment=True, payment="已付款")
        conditional = Message.model_validate(message("conditional", "如果试听合适我再付款"))
        future = Message.model_validate(message("future", "我同意报名方案，明早付款"))
        completed = Message.model_validate(message("completed", "我刚刚也付过款"))
        self.assertEqual(ollama.evidenced_facts(conditional, observation), [])
        self.assertEqual([fact.kind for fact in ollama.evidenced_facts(future, observation)], ["enrollment_intent"])
        self.assertEqual([fact.kind for fact in ollama.evidenced_facts(completed, observation)], ["payment"])

    def test_booking_uses_confirmed_proposal_instead_of_message_timestamp(self):
        proposal = Message.model_validate(message("proposal", "试听时间是 2026 年 11 月 14 日北京时间 19:00，可以参加吗？",
                                                   speaker="employee"))
        confirmation = Message.model_validate(message("confirmed", "可以，就约这个时间。"))
        guessed_time = datetime.fromisoformat("2026-11-12T09:00:00+08:00")
        observed = ollama.Observation(booking=guessed_time)
        extracted = ollama.evidenced_facts(confirmation, observed, proposal)
        self.assertEqual([(fact.kind, fact.value) for fact in extracted], [("booking", "2026-11-14T19:00:00+08:00")])
