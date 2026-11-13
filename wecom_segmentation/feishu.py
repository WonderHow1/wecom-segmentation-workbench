from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import lark_oapi as lark
from lark_oapi.core.exception import ObtainAccessTokenException
from requests.exceptions import ConnectionError, Timeout

from .domain import (
    AUDIENCE_TAG_FIELD, AUDIENCE_TAGS, FOLLOW_UP_BOOKING_PENDING, FOLLOW_UP_OPTIONS, STUDY_PROFILE_FIELD, SUBJECTS,
    VALUES, WECHAT_ID_FIELD,
)
from .store import InputError
from .window import CUSTOMER_PILOT_SCOPE, SAMPLE_SCOPE, WINDOW_SCOPE


TEXT, NUMBER, SINGLE_SELECT, MULTI_SELECT, DATE = 1, 2, 3, 4, 5
MANUAL_FIELD = "人工复核备注"
SINGLE_SELECT_OPTIONS = {
    "来源": (*VALUES["source"], "未知"),
    "来源依据": ("客户提及",),
    "有效线索判定": ("待识别", "资料领取", "有效", "明确不符合"),
    "当前阶段": ("待识别", "有效线索", "已约试听", "有报名意向"),
    "跟进状态": FOLLOW_UP_OPTIONS,
    "咨询人身份": VALUES["role"],
    "年级": VALUES["grade"],
    "学习目标": VALUES["need"],
    "主要卡点": VALUES["blocker"],
    "到课待核实": ("待核实",),
    "付费待核实": ("待核实",),
    "数据范围": (SAMPLE_SCOPE, WINDOW_SCOPE, CUSTOMER_PILOT_SCOPE),
}
MULTI_SELECT_OPTIONS = {"科目": SUBJECTS, AUDIENCE_TAG_FIELD: AUDIENCE_TAGS}
SELECT_OPTIONS = {**SINGLE_SELECT_OPTIONS, **MULTI_SELECT_OPTIONS}
FIELD_TYPES = {
    "线索 ID": TEXT, WECHAT_ID_FIELD: TEXT, STUDY_PROFILE_FIELD: TEXT, **dict.fromkeys(SINGLE_SELECT_OPTIONS, SINGLE_SELECT),
    **dict.fromkeys(MULTI_SELECT_OPTIONS, MULTI_SELECT),
    "首次纳入时间": DATE, "首次判定有效时间": DATE, "首次确认预约时间": DATE,
    "当前有效试听场次": DATE, "最后互动时间": DATE, "证据摘要": TEXT,
    "证据时间": DATE, "同步版本": NUMBER, MANUAL_FIELD: TEXT,
}
RETRY_CODES = {1254290, 1254291, 1255001, 1255002, 1255040, 1254607, 1254608}


class FeishuError(Exception):
    def __init__(self, message: str, *, retryable=False, code=None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str = field(repr=False)
    base_token: str
    table_id: str
    table_url: str | None = None

    @classmethod
    def load(cls, path: Path):
        try:
            values = {}
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, _, value = line.partition("=")
                if key.strip() in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TABLE_URL"):
                    values[key.strip()] = value.strip().strip("\"'")
            app_id = values["FEISHU_APP_ID"]
            secret = values["FEISHU_APP_SECRET"]
            url = urlsplit(values["FEISHU_TABLE_URL"])
        except (OSError, ValueError, KeyError):
            raise InputError("请在本地飞书配置文件填写 App ID、App Secret 和测试表链接，见 README") from None
        if not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id) or not secret:
            raise InputError("飞书 App ID 或 App Secret 未配置")
        host = url.hostname or ""
        base = re.fullmatch(r"/base/([A-Za-z0-9]+)/?", url.path)
        tables = parse_qs(url.query).get("table", [])
        if (url.scheme != "https" or not (host == "feishu.cn" or host.endswith(".feishu.cn"))
                or url.netloc != host or not base or len(tables) != 1 or not re.fullmatch(r"tbl[A-Za-z0-9]+", tables[0])):
            raise InputError("请填写飞书独立多维表格链接，路径含 /base/ 且参数含 table=tbl…")
        return cls(app_id, secret, base[1], tables[0], url.geturl())

    @property
    def target(self) -> str:
        return f"{self.base_token}/{self.table_id}"


@dataclass(frozen=True)
class Record:
    record_id: str
    version: int | None
    has_wechat_id: bool = False
    follow_up: str | None = None
    study_profile: str | None = None


class Feishu:
    def __init__(self, config: Config):
        self.config = config
        self.sdk = (lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
                    .domain("https://open.feishu.cn").timeout(12).log_level(lark.LogLevel.ERROR).build())

    def request(self, method, suffix, body=None, **query):
        request = (lark.BaseRequest.builder().http_method(method)
                   .uri(f"/open-apis/bitable/v1/apps/{self.config.base_token}/tables/{self.config.table_id}/{suffix}")
                   .token_types({lark.AccessTokenType.TENANT})
                   .headers({"Content-Type": "application/json; charset=utf-8"})
                   .body(body).build())
        for key, value in query.items():
            request.add_query(key, value)
        try:
            response = self.sdk.request(request)
        except ObtainAccessTokenException:
            raise FeishuError("飞书应用身份验证失败，请检查凭证及应用发布状态") from None
        except (ConnectionError, Timeout):
            raise FeishuError("飞书网络请求未完成", retryable=True) from None
        except (ValueError, TypeError):
            raise FeishuError("飞书返回了无法解析的响应", retryable=True) from None

        status, code = response.raw.status_code, response.code
        if not 200 <= status < 300 or code != 0:
            retry = status in (408, 429) or status >= 500 or code in RETRY_CODES
            raise FeishuError(f"飞书请求失败（HTTP {status}，业务码 {code}）", retryable=retry, code=code)
        try:
            data = json.loads(response.raw.content)["data"]
            if isinstance(data, dict):
                return data
        except (ValueError, KeyError, TypeError):
            pass
        raise FeishuError("飞书成功响应缺少数据，需重新查询确认", retryable=True)

    def items(self, method, suffix, body=None):
        query = {"page_size": 100}
        seen = set()
        while True:
            data = self.request(method, suffix, body, **query)
            items = data.get("items", [])
            if items is None and not data.get("total"):
                items = []
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise FeishuError("飞书分页返回的数据格式异常")
            if not items and data.get("total") and not seen:
                raise FeishuError("飞书分页返回的数据格式异常")
            yield from items
            if not data.get("has_more"):
                return
            token = data.get("page_token")
            if not token or token in seen:
                raise FeishuError("飞书分页游标异常")
            seen.add(token)
            query["page_token"] = token

    def ensure_fields(self, create=False) -> int:
        metadata = {item["field_name"]: item for item in self.items(lark.HttpMethod.GET, "fields")}
        existing = {name: item["type"] for name, item in metadata.items()}
        wrong = [name for name, kind in FIELD_TYPES.items() if name in existing and existing[name] != kind
                 and not (kind == SINGLE_SELECT and existing[name] == TEXT)]
        if wrong:
            raise FeishuError("测试表的同名字段类型不符，请检查「" + "、".join(wrong) + "」")
        missing = [name for name in FIELD_TYPES if name not in existing]
        if not create and any(name not in (AUDIENCE_TAG_FIELD, WECHAT_ID_FIELD, STUDY_PROFILE_FIELD) for name in missing):
            raise FeishuError("测试表缺少字段，请先执行 sync --setup")
        for name in missing:
            kind = FIELD_TYPES[name]
            body = {"field_name": name, "type": kind}
            if name in SELECT_OPTIONS:
                body["property"] = {"options": [{"name": option} for option in SELECT_OPTIONS[name]]}
            if kind == DATE:
                body["property"] = {"date_formatter": "yyyy/MM/dd HH:mm"}
            self.request(lark.HttpMethod.POST, "fields", body)
        if existing.get("跟进状态") == SINGLE_SELECT:
            field = metadata["跟进状态"]
            options = (field.get("property") or {}).get("options")
            if not isinstance(options, list) or not all(isinstance(item, dict) and "name" in item for item in options):
                raise FeishuError("无法读取飞书跟进状态选项，请检查字段配置")
            if not any(item["name"] == FOLLOW_UP_BOOKING_PENDING for item in options):
                kept = [{key: option[key] for key in ("id", "name", "color") if key in option}
                        for option in options]
                self.request(lark.HttpMethod.PUT, f"fields/{field['field_id']}", {
                    "field_name": "跟进状态", "type": SINGLE_SELECT,
                    "property": {"options": [*kept, {"name": FOLLOW_UP_BOOKING_PENDING}]},
                })
        return len(missing)

    def find(self, lead_id: str) -> Record | None:
        body = {"field_names": ["线索 ID", "同步版本", WECHAT_ID_FIELD, "跟进状态", STUDY_PROFILE_FIELD],
                "filter": {"conjunction": "and", "conditions": [
            {"field_name": "线索 ID", "operator": "is", "value": [lead_id]},
        ]}}
        records = list(self.items(lark.HttpMethod.POST, "records/search", body))
        if len(records) > 1:
            raise FeishuError("同一线索 ID 已存在多行，需要对账")
        if not records:
            return None
        row = records[0]
        fields = row.get("fields")
        if not isinstance(fields, dict):
            raise FeishuError("飞书记录缺少字段，需要对账")
        identity = fields.get("线索 ID")
        if isinstance(identity, list) and all(isinstance(part, dict) and isinstance(part.get("text"), str)
                                              for part in identity):
            identity = "".join(part["text"] for part in identity)
        version = fields.get("同步版本")
        if (identity != lead_id or not valid_record_id(row.get("record_id"))
                or type(version) not in (int, float) or version < 1 or version % 1 != 0):
            raise FeishuError("飞书记录的线索 ID 或同步版本异常，需要对账")
        # Any populated cell takes precedence, including an unexpected response shape.
        has_wechat_id = fields.get(WECHAT_ID_FIELD) not in (None, "", [])
        follow_up = fields.get("跟进状态")
        if isinstance(follow_up, list):
            follow_up = "".join(part["text"] for part in follow_up
                                if isinstance(part, dict) and isinstance(part.get("text"), str))
        study_profile = fields.get(STUDY_PROFILE_FIELD)
        if isinstance(study_profile, list) and all(isinstance(part, dict) and part.get("type") == "text"
                                                   and isinstance(part.get("text"), str) for part in study_profile):
            study_profile = "".join(part["text"] for part in study_profile)
        if study_profile is not None and not isinstance(study_profile, str):
            raise FeishuError("飞书用户基本情况字段格式异常，需要对账")
        return Record(row["record_id"], int(version), has_wechat_id, follow_up or None, study_profile or None)

    def create(self, fields, token) -> str:
        data = self.request(lark.HttpMethod.POST, "records", {"fields": fields}, client_token=token)
        return written_record_id(data)

    def update(self, record_id, fields):
        if not valid_record_id(record_id):
            raise FeishuError("飞书记录 ID 异常，需要对账")
        data = self.request(lark.HttpMethod.PUT, f"records/{record_id}", {"fields": fields})
        if written_record_id(data) != record_id:
            raise FeishuError("飞书返回了不同的记录 ID，需要对账")


def valid_record_id(value):
    return isinstance(value, str) and re.fullmatch(r"rec[A-Za-z0-9]+", value) is not None


def written_record_id(data) -> str:
    value = (data.get("record") or {}).get("record_id")
    if not valid_record_id(value):
        raise FeishuError("飞书写入响应缺少记录 ID，需重新查询确认", retryable=True)
    return value
