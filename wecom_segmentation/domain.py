from datetime import datetime
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


RULES_VERSION = 12
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
FOLLOW_UP_ACTIVE, FOLLOW_UP_PAUSED, FOLLOW_UP_REFUSED = "推进中", "暂缓", "拒绝"
FOLLOW_UP_BOOKING_PENDING = "预约时间待确认"
FOLLOW_UP_OPTIONS = (FOLLOW_UP_ACTIVE, FOLLOW_UP_PAUSED, FOLLOW_UP_REFUSED, FOLLOW_UP_BOOKING_PENDING)
WECHAT_ID_FIELD = "微信ID"
STUDY_PROFILE_FIELD = "用户基本情况"
STUDY_DETAILS = ("region", "grade", "recent_score", "weak_subject", "difficulty")
MIDDLE_SCHOOL_GRADES = ("初一", "初二", "初三", "初中")
NON_HIGH_SCHOOL_GRADES = (*MIDDLE_SCHOOL_GRADES, "非高中")
AUDIENCE_TAG_FIELD = "人群标签"
MIDDLE_SCHOOL_TAG = "初中生"
PARENT_TAG = "家长"
AUDIENCE_TAGS = (MIDDLE_SCHOOL_TAG, PARENT_TAG)
SUBJECTS = ("物理", "化学", "生物", "政治", "历史", "地理")
SUBJECT_COUNT = 3
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
WECHAT_ID = r"[A-Za-z][A-Za-z0-9_-]{5,19}"
WECHAT_MENTION = re.compile(
    r"(?:^|[，。；！？：,;!\n\s])\s*"
    r"(?:(?:我(?:本人)?的?\s*微信\s*(?:号|ID|账号)|(?:加|添加)我(?:的)?\s*微信(?:号|ID)?)"
    r"\s*(?:是|为|[:：])?|微信\s*(?:号|ID)\s*[:：])"
    rf"\s*[「“【(（]?(?P<value>{WECHAT_ID})(?![A-Za-z0-9_@.+-])",
    re.IGNORECASE,
)
WECHAT_QUESTION = re.compile(
    r"(?:你的|您的|你|您)?\s*微信\s*(?:号|ID|账号).{0,12}(?:多少|什么|发|提供|留)"
    r"|(?:发|提供|留).{0,10}微信\s*(?:号|ID|账号)", re.IGNORECASE
)


def stated_wechat_id(messages: list["Message"]) -> str | None:
    candidates = set()
    previous = None
    for message in messages:
        text = message.text or ""
        if message.speaker == "customer":
            candidates.update(match["value"] for match in WECHAT_MENTION.finditer(text))
            if previous and previous.speaker == "employee" and WECHAT_QUESTION.search(previous.text or ""):
                answer = text.strip().strip("“”「」。，")
                if re.fullmatch(WECHAT_ID, answer):
                    candidates.add(answer)
        previous = message
    return next(iter(candidates)) if len(candidates) == 1 else None


def is_subject_selection(subjects):
    return isinstance(subjects, list) and (
        not subjects or (len(subjects) == SUBJECT_COUNT
                         and subjects == [item for item in SUBJECTS if item in subjects])
    )


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: Identifier
    contact_id: Identifier
    time: AwareDatetime
    speaker: Literal["customer", "employee"]
    kind: Literal["text", "recall"] = "text"
    text: str | None = None
    target_message_id: Identifier | None = None

    @model_validator(mode="after")
    def check_payload(self):
        if self.kind == "text" and self.text and self.text.strip() and self.target_message_id is None:
            return self
        if self.kind == "recall" and self.text is None and self.target_message_id not in (None, self.message_id):
            return self
        raise ValueError("invalid message payload")


VALUES = {
    "grade": ("高一", "高二", "高三", "高中", *NON_HIGH_SCHOOL_GRADES),
    "role": ("学生", "家长"),
    "source": ("抖音", "小红书", "其他"),
    "need": ("跟课困难", "补基础", "提高成绩", "备考", "课程咨询", "其他学习需求"),
    "blocker": ("家长决策", "预算", "时间安排", "课程匹配"),
}

FactKind = Literal[
    "grade", "subject_selection", "role", "source", "need", "blocker", "materials", "booking",
    "booking_pending", "cancel_booking", "enrollment_intent", "attendance", "deny_attendance", "payment",
    "deny_payment", "pause", "refuse", "study_detail",
]

StudyDetailKind = Literal[*STUDY_DETAILS]


class StudyDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: StudyDetailKind
    excerpt: Annotated[str, Field(min_length=1, max_length=72)]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    message_id: Identifier
    value: str | None = None
    study_detail: StudyDetail | None = None

    @model_validator(mode="after")
    def check_value(self):
        if self.kind == "study_detail" and self.value is None and self.study_detail is not None:
            return self
        if self.kind == "study_detail" or self.study_detail is not None:
            raise ValueError("invalid study detail")
        if self.kind in VALUES and self.value in VALUES[self.kind]:
            return self
        if self.kind == "subject_selection" and self.value is not None:
            subjects = self.value.split("、") if self.value else []
            if is_subject_selection(subjects):
                return self
        if self.kind == "booking" and self.value:
            appointment = datetime.fromisoformat(self.value)
            if appointment.utcoffset() is not None:
                return self
        if self.kind not in VALUES and self.kind not in ("booking", "subject_selection") and self.value is None:
            return self
        raise ValueError("invalid fact value")


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[Fact]


EVIDENCE_GROUPS = {
    "booking": "appointment", "booking_pending": "appointment", "cancel_booking": "appointment",
    "deny_attendance": "attendance", "deny_payment": "payment",
    "pause": "follow_up", "refuse": "follow_up",
}
EVIDENCE_CUES = {
    "grade": r"高[一二三中]|初[一二三中]|小学|大学",
    "subject_selection": r"选[科课考]|选了|改选|改了|[物化生政史地]{3}|没定",
    "role": r"家长|学生|孩子|儿子|女儿|妈妈|爸爸",
    "source": r"抖音|小红书|推荐|介绍|视频号|公众号",
    "need": r"跟不上|听不懂|丢分|失分|基础|成绩|提分|备考|补课|想.{0,6}(?:课|了解)",
    "blocker": r"预算|贵|商量|时间|教材|进度|匹配",
    "materials": r"资料|讲义|题库",
    "booking": r"确认|确定|这个时间|预约|试听|^可以$|^好$",
    "booking_pending": r"确认|确定|这个(?:时间|点)|就约|预约|试听|^(?:可以|好(?:的)?|行|没问题)$",
    "cancel_booking": r"改期|重约|取消|去不了",
    "enrollment_intent": r"报名|报班|报课|付款|缴费",
    "attendance": r"试听|到课|上完",
    "deny_attendance": r"试听|到课",
    "payment": r"付款|付钱|缴费|交费|转账|付完|支付",
    "deny_payment": r"付款|付钱|缴费|交费|转账|支付",
    "pause": r"暂缓|缓一缓|考虑|再说",
    "refuse": r"不需要|别联系|不用联系|不要再?联系|拒绝",
}
PRIVATE_CLAUSE = re.compile(
    r"微信|手机|电话|身份证|收件|住址|地址|姓名|我叫|孩子叫|姓氏|账号|银行卡|"
    r"https?://|www\.|@|[A-Za-z0-9_-]{6,}", re.IGNORECASE
)
STUDY_CLAUSE = re.compile(r"([^，。；！？,;!?\n]+)([，。；！？,;!?\n]*)")
STUDY_UNCONFIRMED = re.compile(
    r"假如|假设|如果|要是|的话|比如|举例|听说|可能|不确定|还不知道|不是|算错|说错|我同学|我朋友|"
    r"要不要|能不能|是不是|是否|多少分|几分|[吗么]\s*$"
)
STUDY_PRIVATE = re.compile(
    r"家庭地址|详细地址|身份证|校名|附中|中学|学校|校区|小区|单元|楼栋|门牌|街道|巷|"
    r"[一二三四五六七八九十]+[班中](?:$|\s)|(?:路|号楼|栋|室|班)(?:\d|$)|"
    r"\d+\s*(?:号|栋|楼|室|单元|班)|\d{3,}\s*(?:[-－—\s]\s*\d+)+"
)
STUDY_CUES = {
    "region": r"省|市|自治区|北京|上海|天津|重庆|[\u4e00-\u9fff]{2,6}人$|(?:在|来自)[\u4e00-\u9fff]{2,6}$",
    "grade": EVIDENCE_CUES["grade"],
    "recent_score": r"\d{1,3}(?:\.\d+)?\s*分",
    "weak_subject": r"语文|数学|英语|物理|化学|生物|政治|历史|地理|信息技术|日语",
    "difficulty": r"听不懂|听不明白|跟不上|基础|困难|不会|丢分|失分|做不|不懂|不熟|不稳|不太会|记不住|薄弱|想补|想提",
}
STUDY_GRADE_CUE = re.compile(r"(?:^|现(?:在|读)|目前|今年|在读|读|上|是)(?:高[一二三中]|初[一二三中]|小学|大学)")
STUDY_SCORE_NON_RESULT = re.compile(r"想|目标|满分|希望|打算|争取|预计|估计|至少|要求|要考|能考|没考|未考|考到多少")
STUDY_SUBJECT_DIFFICULTY = re.compile(r"弱|差|吃力|拖后腿|不太行|不好|低|跟不上|听不懂|难|丢分|失分|不会|做不|最怕")
STUDY_SCORE = re.compile(
    r"(?:(?:语文|数学|英语|物理|化学|生物|政治|历史|地理|总分)?(?:月考|期中|期末|联考|模考|一模|二模|三模|周测|单元测验)"
    r"(?:实际)?(?:只|才)?(?:考了|得了|考到|拿了)?|(?:考了|得了|拿了))\s{0,4}\d{1,3}(?:\.\d{1,2})?\s{0,4}分"
)
STUDY_REGION = re.compile(
    r"(?:^|[，。；\s])(?:我|我们|孩子)?(?:目前|现在)?(?:在|来自|住在)\s*"
    r"(?P<place>北京(?:市)?|上海(?:市)?|天津(?:市)?|重庆(?:市)?|[\u4e00-\u9fff]{2,7}?(?:省|市|自治区))"
    r"(?=$|[，。；！,;!\s])"
)


def supported_study_detail(detail: StudyDetail, message: Message) -> bool:
    excerpt = detail.excerpt
    if excerpt != excerpt.strip() or PRIVATE_CLAUSE.search(excerpt) or STUDY_PRIVATE.search(excerpt):
        return False
    if not re.search(STUDY_CUES[detail.category], excerpt):
        return False
    for clause, punctuation in STUDY_CLAUSE.findall(message.text or ""):
        if excerpt not in clause or "？" in punctuation or "?" in punctuation or STUDY_UNCONFIRMED.search(clause.strip()):
            continue
        if detail.category == "region" and re.search(r"高中|小学|学院|大学|路|区|县|镇|村", excerpt.replace("自治区", "")):
            continue
        if detail.category == "grade" and not (
            STUDY_GRADE_CUE.search(excerpt) and not re.search(r"资料|讲义|题库|课程|课本|教材", clause)
        ):
            continue
        if detail.category == "recent_score" and STUDY_SCORE_NON_RESULT.search(clause):
            continue
        if detail.category == "weak_subject" and not STUDY_SUBJECT_DIFFICULTY.search(excerpt):
            continue
        return True
    return False


def stated_study_details(message: Message, proposed: list[StudyDetail]) -> list[StudyDetail]:
    text = message.text or ""
    explicit = [StudyDetail(category="recent_score", excerpt=match[0]) for match in STUDY_SCORE.finditer(text)]
    explicit.extend(StudyDetail(category="region", excerpt=match["place"]) for match in STUDY_REGION.finditer(text))
    details = {}
    for detail in [*explicit, *proposed]:
        if supported_study_detail(detail, message):
            details[detail.category] = detail
    return list(details.values())


def evidence_summary(facts: list[Fact], messages: list[Message], positions: dict[str, int],
                     blocker: str | None) -> str | None:
    latest = {}
    for fact in facts:
        if fact.kind == "study_detail" or (fact.kind == "blocker" and fact.value != blocker):
            continue
        latest[EVIDENCE_GROUPS.get(fact.kind, fact.kind)] = fact

    excerpts = {}
    for fact in latest.values():
        position = positions[fact.message_id]
        clauses = re.split(r"[，。；！？,;!?\n]+", messages[position].text or "")
        cue = EVIDENCE_CUES[fact.kind]
        if fact.kind == "grade" and fact.value != "非高中":
            cue = re.escape(fact.value)
        for index, clause in enumerate(clauses):
            clause = clause.strip(" \t“”")
            if (clause and len(clause) <= 72 and not PRIVATE_CLAUSE.search(clause)
                    and re.search(cue, clause)):
                excerpts[position, index] = clause
    recent = list(dict.fromkeys(excerpts[key] for key in sorted(excerpts, reverse=True)))[:6]
    return "\n".join(f"“{clause}”" for clause in reversed(recent)) or None


def milliseconds(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp() * 1000)


def classify(messages: list[Message], extraction: Extraction, first_seen: datetime) -> dict:
    """Reduce evidenced facts in message order to a Feishu field preview."""
    positions = {message.message_id: index for index, message in enumerate(messages)}
    for fact in extraction.facts:
        if fact.message_id not in positions or messages[positions[fact.message_id]].speaker != "customer":
            raise ValueError("fact must reference a visible customer message")

    attributes = {}
    study_details = {}
    subjects = []
    grade_since = need_since = first_booking = appointment = None
    materials = intent = attendance = payment = False
    follow_up = None
    evidence_time = None
    ordered_facts = sorted(extraction.facts, key=lambda item: positions[item.message_id])
    for fact in ordered_facts:
        message = messages[positions[fact.message_id]]
        if fact.kind == "study_detail":
            detail = fact.study_detail
            if supported_study_detail(detail, message):
                study_details[detail.category] = detail.excerpt
            continue
        evidence_time = message.time
        match fact.kind:
            case "grade":
                if fact.value in NON_HIGH_SCHOOL_GRADES:
                    grade_since = None
                elif grade_since is None:
                    grade_since = evidence_time
                attributes["grade"] = fact.value
            case "need":
                if need_since is None:
                    need_since = evidence_time
                attributes["need"] = fact.value
            case "subject_selection":
                subjects = fact.value.split("、") if fact.value else []
            case "role" | "source":
                attributes[fact.kind] = fact.value
            case "blocker":
                if attributes.get("blocker") != "预算" or fact.value == "预算":
                    attributes["blocker"] = fact.value
            case "materials":
                materials = True
            case "booking":
                appointment = datetime.fromisoformat(fact.value)
                if first_booking is None:
                    first_booking = evidence_time
                follow_up = None
            case "booking_pending":
                appointment = None
                follow_up = FOLLOW_UP_BOOKING_PENDING
            case "cancel_booking":
                appointment = None
                follow_up = FOLLOW_UP_PAUSED
            case "enrollment_intent":
                intent = True
            case "attendance" | "deny_attendance":
                attendance = fact.kind == "attendance"
            case "payment" | "deny_payment":
                payment = fact.kind == "payment"
            case "pause" | "refuse":
                follow_up = FOLLOW_UP_PAUSED if fact.kind == "pause" else FOLLOW_UP_REFUSED

    first_effective = max(grade_since, need_since) if grade_since and need_since else None
    eligibility = "待识别"
    if attributes.get("grade") in NON_HIGH_SCHOOL_GRADES:
        eligibility = "明确不符合"
    elif first_effective:
        eligibility = "有效"
    elif materials and not need_since:
        eligibility = "资料领取"

    stage = "有效线索" if first_effective else "待识别"
    if appointment:
        stage = "已约试听"
    if intent:
        stage = "有报名意向"

    tags = [tag for present, tag in (
        (attributes.get("grade") in MIDDLE_SCHOOL_GRADES, MIDDLE_SCHOOL_TAG),
        (attributes.get("role") == PARENT_TAG, PARENT_TAG),
    ) if present]
    return {
        WECHAT_ID_FIELD: stated_wechat_id(messages),
        STUDY_PROFILE_FIELD: "；".join(dict.fromkeys(
            study_details[category] for category in STUDY_DETAILS if category in study_details
        )) or None,
        "来源": attributes.get("source", "未知"),
        "来源依据": "客户提及" if attributes.get("source") else None,
        "有效线索判定": eligibility,
        "当前阶段": stage,
        "跟进状态": follow_up or FOLLOW_UP_ACTIVE,
        "咨询人身份": attributes.get("role", "学生"),
        AUDIENCE_TAG_FIELD: tags,
        "年级": attributes.get("grade"),
        "科目": subjects,
        "学习目标": attributes.get("need"),
        "主要卡点": attributes.get("blocker"),
        "首次纳入时间": milliseconds(first_seen),
        "首次判定有效时间": milliseconds(first_effective),
        "首次确认预约时间": milliseconds(first_booking),
        "当前有效试听场次": milliseconds(appointment),
        "最后互动时间": milliseconds(messages[-1].time) if messages else None,
        "到课待核实": "待核实" if attendance else None,
        "付费待核实": "待核实" if payment else None,
        "证据摘要": evidence_summary(ordered_facts, messages, positions, attributes.get("blocker")),
        "证据时间": milliseconds(evidence_time),
    }


def current_follow_up(fields: dict, last_customer_at: datetime | None, now: datetime) -> None:
    if (fields.get("跟进状态") == FOLLOW_UP_ACTIVE and not fields.get("当前有效试听场次")
            and last_customer_at is not None
            and now.astimezone(BUSINESS_TIMEZONE).date() > last_customer_at.astimezone(BUSINESS_TIMEZONE).date()):
        fields["跟进状态"] = FOLLOW_UP_PAUSED


def follow_up_needs_sync(current: str | None, confirmed: str | None) -> bool:
    return current == FOLLOW_UP_PAUSED and confirmed in (None, FOLLOW_UP_ACTIVE)


def normalize_follow_up(fields: dict) -> dict:
    if fields.get("跟进状态") in ("待改期", "明确拒绝"):
        fields["跟进状态"] = FOLLOW_UP_REFUSED if fields["跟进状态"] == "明确拒绝" else FOLLOW_UP_PAUSED
    return fields
