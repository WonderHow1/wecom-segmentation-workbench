#!/bin/zsh
set -eu
cd -- "${0:A:h}/.."
./打开企微分层工作台.command --build-only
evidence="$PWD/.local/workbench-controls-evidence"
mkdir -p "$evidence"
fixture=$(mktemp -d "${TMPDIR:-/tmp}/wecom-workbench-ui.XXXXXX")
trap 'rm -rf -- "$fixture"' EXIT
mkdir -p "$fixture/.local"
if [[ "${1:-}" == "--connection-timeout" ]]; then
    mkdir -p "$fixture/.local/venv/bin"
    cat > "$fixture/.local/venv/bin/python" <<'SH'
#!/bin/sh
IFS= read -r request || exit 1
exec /bin/sleep 60
SH
    chmod +x "$fixture/.local/venv/bin/python"
else
    /usr/bin/ditto "$PWD/.local/venv" "$fixture/.local/venv"
fi
/usr/bin/ditto "$PWD/wecom_segmentation" "$fixture/wecom_segmentation"
/usr/bin/ditto '.local/企微分层工作台.app' "$fixture/.local/企微分层工作台.app"
WECOM_UI_FIXTURE="$fixture" .local/venv/bin/python - <<'PY'
from contextlib import closing
import os
from unittest.mock import patch

from wecom_segmentation.classification import classify_pending
from wecom_segmentation.ollama import Observation, StudyDetail
from wecom_segmentation.operator import Sessions
from wecom_segmentation.store import Store
from wecom_segmentation.window import WindowSnapshot

os.chdir(os.environ["WECOM_UI_FIXTURE"])
sessions = Sessions.load()
sessions.add("未采集会话", customer=False)
session = sessions.add("待分层会话", customer=False)
window = WindowSnapshot.model_validate({
    "header": [session.title],
    "messages": [{"speaker": "customer", "text": text} for text in ("合成甲", "合成乙", "合成丙")],
})
with closing(Store(session.db)) as store:
    store.capture_window(lambda: window)
session = sessions.add("人群标签演示", customer=False)
window = WindowSnapshot.model_validate({
    "header": [session.title],
    "messages": [
        {"speaker": "employee", "text": "你好，想了解哪个年级的课程？"},
        {"speaker": "customer", "text": "我家孩子上初三，想补基础，函数题容易丢分。"},
        {"speaker": "employee", "text": "收到。"},
    ],
})
with closing(Store(session.db)) as store:
    store.capture_window(lambda: window)
    observation = Observation(study_details=[StudyDetail(category="difficulty", excerpt="函数题容易丢分")])
    with patch("wecom_segmentation.ollama.observe", return_value=observation):
        assert classify_pending(store)["本次已分层"] == 1
PY
xcrun swiftc -sdk "$(xcrun --show-sdk-path)" -target "$(uname -m)-apple-macosx13.0" \
    -module-cache-path .local/swift-module-cache tests/workbench_ui.swift -o "$evidence/ui-test"
if ! "$evidence/ui-test" "$fixture/.local/企微分层工作台.app" "$evidence" "${@}"; then
    if [[ -f "$fixture/.local/workbench.log" ]]; then
        cat "$fixture/.local/workbench.log"
    fi
    exit 1
fi
