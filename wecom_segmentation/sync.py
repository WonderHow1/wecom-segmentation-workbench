from collections import Counter
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
from time import sleep

from .domain import (
    FOLLOW_UP_PAUSED, STUDY_PROFILE_FIELD, WECHAT_ID, WECHAT_ID_FIELD, follow_up_needs_sync,
    is_subject_selection, normalize_follow_up,
)
from .feishu import FIELD_TYPES, MANUAL_FIELD, SINGLE_SELECT_OPTIONS, FeishuError, Record
from .store import InputError, Store
from .window import CUSTOMER_PILOT_SCOPE, SAMPLE_SCOPE, WINDOW_SCOPE, normalize_scope


@contextmanager
def sync_lock(db_path: Path):
    with db_path.resolve().with_suffix(db_path.suffix + ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InputError("这份数据库已有飞书同步任务在运行") from None
        yield


def profile_hash(value):
    return sha256((value or "").encode()).hexdigest()


def preserve_manual_fields(fields, state, remote):
    if remote.has_wechat_id:
        fields.pop(WECHAT_ID_FIELD, None)
    last_hash = state["synced_study_profile_hash"] if state else None
    if ((last_hash is None and remote.study_profile is not None)
            or (last_hash is not None and profile_hash(remote.study_profile) != last_hash)):
        fields.pop(STUDY_PROFILE_FIELD, None)


def write_pending(api, store: Store, target: str, state, remote=None) -> Record:
    fields = normalize_follow_up(normalize_scope(json.loads(state["pending_fields"])))
    record_id = state["record_id"]
    if record_id:
        preserve_manual_fields(fields, state, remote)
        api.update(record_id, fields)
    else:
        record_id = api.create(fields, state["client_token"])
    version = fields["同步版本"]
    study_profile = fields.get(STUDY_PROFILE_FIELD, remote.study_profile if remote else None)
    store.confirm_sync(target, state["lead_id"], record_id, version,
                       study_profile_hash=profile_hash(study_profile) if STUDY_PROFILE_FIELD in fields else None,
                       follow_up=fields.get("跟进状态"))
    return Record(record_id, version, bool(fields.get(WECHAT_ID_FIELD) or (remote and remote.has_wechat_id)),
                  fields.get("跟进状态"), study_profile)


def sync_lead(api, store: Store, target: str, fields) -> str:
    lead_id = fields["线索 ID"]
    version = fields["同步版本"]
    state = store.sync_state(target, lead_id)
    remote = api.find(lead_id)
    if state and state["record_id"] and (remote is None or remote.record_id != state["record_id"]):
        raise FeishuError("已绑定的飞书记录无法按线索 ID 找到，需要对账")
    if state and remote and remote.version < state["confirmed_version"]:
        raise FeishuError("飞书同步版本低于上次确认的版本，需要对账")

    recovered = state is not None and state["pending_fields"] is not None
    if recovered:
        pending_fields = json.loads(state["pending_fields"])
        pending_version = pending_fields["同步版本"]
        if remote and remote.version >= pending_version:
            applied = (remote.version == pending_version and STUDY_PROFILE_FIELD in pending_fields
                       and remote.study_profile == pending_fields[STUDY_PROFILE_FIELD])
            store.confirm_sync(target, lead_id, remote.record_id, remote.version,
                               study_profile_hash=profile_hash(remote.study_profile) if applied else None,
                               follow_up=remote.follow_up)
        else:
            if remote and not state["record_id"]:
                raise FeishuError("创建结果待确认，飞书已出现另一版本的同名线索，需要对账")
            remote = write_pending(api, store, target, state, remote)
        state = store.sync_state(target, lead_id)

    if remote:
        fields = fields.copy()
        preserve_manual_fields(fields, state, remote)
    if remote and remote.version >= version:
        if remote.version > version:
            store.confirm_sync(target, lead_id, remote.record_id, remote.version, follow_up=remote.follow_up)
            return "远端较新"
        updates = {WECHAT_ID_FIELD: fields[WECHAT_ID_FIELD]} if WECHAT_ID_FIELD in fields else {}
        if follow_up_needs_sync(fields.get("跟进状态"), remote.follow_up):
            updates["跟进状态"] = FOLLOW_UP_PAUSED
        if updates:
            api.update(remote.record_id, updates)
        store.confirm_sync(target, lead_id, remote.record_id, remote.version,
                           follow_up=updates.get("跟进状态", remote.follow_up))
        if updates:
            return "更新"
        return "恢复" if recovered else "已是最新"

    state = store.begin_sync(target, fields, remote.record_id if remote else None)
    write_pending(api, store, target, state, remote)
    return "更新" if remote else "新增"


def sync_table(api, store: Store, target: str, *, setup=False, window_test=False, customer_pilot=False) -> dict:
    if window_test and customer_pilot:
        raise InputError("请指定一种企微窗口同步范围")
    requested_scope = None
    if customer_pilot:
        requested_scope = CUSTOMER_PILOT_SCOPE
    elif window_test:
        requested_scope = WINDOW_SCOPE
    if requested_scope != store.window_scope():
        raise InputError("同步范围与数据库不符，请为企微获客使用 --customer-pilot，为同事测试使用 --window-test")
    rows = store.preview()
    scope = requested_scope or SAMPLE_SCOPE
    if any(row.get("数据范围") != scope for row in rows):
        raise InputError("分层结果的数据范围与同步模式不符")
    if any("科目" in row and not is_subject_selection(row["科目"]) for row in rows):
        raise InputError("已有结果包含旧科目数据，请重新分层后再同步")
    if any(row.get(name) not in (None, *options)
           for row in rows for name, options in SINGLE_SELECT_OPTIONS.items()):
        raise InputError("已有结果包含固定选项以外的值，请重新分层后再同步")

    counts = dict.fromkeys(("新增", "更新", "已是最新", "远端较新", "恢复", "重试", "失败"), 0)
    counts["新增字段"] = api.ensure_fields(create=setup)
    errors = Counter()
    for row in rows:
        fields = {key: row[key] for key in FIELD_TYPES if key not in (MANUAL_FIELD, WECHAT_ID_FIELD) and key in row}
        wechat_id = row.get(WECHAT_ID_FIELD)
        if isinstance(wechat_id, str) and re.fullmatch(WECHAT_ID, wechat_id):
            fields[WECHAT_ID_FIELD] = wechat_id
        for attempt in range(3):
            try:
                outcome = sync_lead(api, store, target, fields)
            except FeishuError as error:
                if error.retryable and attempt < 2:
                    counts["重试"] += 1
                    sleep(0.5 * 2 ** attempt)
                    continue
                counts["失败"] += 1
                errors[str(error)] += 1
            else:
                counts[outcome] += 1
            break
    return {**counts, "失败原因": dict(errors)}
