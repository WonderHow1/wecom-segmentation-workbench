from pathlib import Path
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


WINDOW_CONTACT = "wecom-window-test"
WINDOW_SCOPE = "企微窗口测试"
LEGACY_CUSTOMER_PILOT_SCOPE = "企微客户试跑"
CUSTOMER_PILOT_SCOPE = "企微获客"
SAMPLE_SCOPE = "样本回放"
ANCHOR_LENGTH = 3
MIN_VISIBLE_MESSAGES = 2
MAX_VISIBLE_MESSAGES = 200


def normalize_scope(fields: dict) -> dict:
    if fields.get("数据范围") == LEGACY_CUSTOMER_PILOT_SCOPE:
        fields["数据范围"] = CUSTOMER_PILOT_SCOPE
    return fields


class WindowError(Exception):
    pass


class Bubble(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    speaker: Literal["customer", "employee"]
    text: str = Field(min_length=1)


class WindowSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header: list[str] = Field(min_length=1)
    messages: list[Bubble] = Field(max_length=MAX_VISIBLE_MESSAGES)


def read_window(reader: Path, title: str) -> WindowSnapshot:
    if not title.strip() or "\n" in title or "\r" in title:
        raise WindowError("请填写企微聊天顶部显示的完整会话名")
    try:
        result = subprocess.run([str(reader.resolve())], input=title + "\n",
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
    except FileNotFoundError:
        raise WindowError("请先按 README 编译企微窗口读取程序") from None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise WindowError("企微窗口读取未完成，请检查 macOS 权限和窗口状态") from None
    if result.returncode:
        raise WindowError(result.stderr.strip() or "无法读取当前企微窗口")
    try:
        snapshot = WindowSnapshot.model_validate_json(result.stdout)
    except ValidationError:
        raise WindowError("企微窗口读取结果格式有误") from None
    if snapshot.header[0] != title:
        raise WindowError("当前企微会话标题与指定标题不符")
    return snapshot


def new_bubbles(previous: list[Bubble], current: list[Bubble]) -> list[Bubble]:
    if len(current) < MIN_VISIBLE_MESSAGES:
        raise WindowError("企微窗口至少需要显示两条完整的文字消息")
    anchor = (previous or current)[-ANCHOR_LENGTH:]
    size = len(anchor)
    ends = [start + size for start in range(len(current) - size + 1)
            if current[start:start + size] == anchor]
    if len(ends) != 1:
        raise WindowError("无法唯一定位读取进度，请确认重叠消息仍在窗口内")
    if not previous:
        return current
    end = ends[0]
    overlap = min(len(previous), end)
    if previous[-overlap:] != current[end - overlap:end]:
        raise WindowError("与上次重叠的消息不一致，请核对企微窗口")
    return current[end:]
