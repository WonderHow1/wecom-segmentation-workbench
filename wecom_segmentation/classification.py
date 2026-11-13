from collections import Counter
from datetime import datetime

from . import ollama
from .domain import RULES_VERSION, classify
from .model_api import ApiModel
from .store import InputError, Store
from .window import SAMPLE_SCOPE


def classify_pending(store: Store, *, api: ApiModel | None = None, reprocess=False):
    saved = 0
    failures = Counter()
    pending = store.pending(reprocess=reprocess)
    window_scope = store.window_scope()
    model = f"api:{api.url.netloc}/{api.model}" if api else ollama.MODEL
    for lead in pending:
        try:
            messages = store.conversation(lead["contact_id"])
            options = {"allow_relative_booking": False} if window_scope else {}
            if api:
                options["observer"] = api.observe
            extraction = ollama.extract(messages, **options)
            fields = classify(messages, extraction, datetime.fromisoformat(lead["first_seen"]))
        except (InputError, ollama.ExtractionError, ValueError) as error:
            reason = "提取结果包含无效的消息证据" if isinstance(error, ValueError) else str(error)
            store.record_failure(lead, reason)
            failures[reason] += 1
            continue
        if window_scope:
            for field in ("首次判定有效时间", "首次确认预约时间", "最后互动时间", "证据时间"):
                fields[field] = None
        fields.update({"线索 ID": lead["lead_id"], "数据范围": window_scope or SAMPLE_SCOPE})
        last_customer = next((message for message in reversed(messages) if message.speaker == "customer"), None)
        fields["_last_customer_at"] = last_customer.time.isoformat() if last_customer else None
        if store.save(lead, fields, extraction, model, ollama.PROMPT_VERSION, RULES_VERSION, reprocess=reprocess):
            saved += 1
        else:
            failures["会话处理进度已变化，请重新查看分层"] += 1
    return {"本次已分层": saved, **store.status(), "失败原因": dict(failures)}
