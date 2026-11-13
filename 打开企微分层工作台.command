#!/bin/zsh
set -eu
cd -- "${0:A:h}"
umask 077

if [[ ! -x .local/venv/bin/python ]]; then
    print '请先按 README 的安装步骤创建 .local/venv 并安装依赖。'
    exit 1
fi
app='.local/企微分层工作台.app'
binary="$app/Contents/MacOS/Workbench"
if [[ ! -x "$binary" || desktop/Workbench.swift -nt "$binary" || wecom_segmentation/read_window.swift -nt "$binary" || desktop/Info.plist -nt "$app/Contents/Info.plist" ]]; then
    print '正在构建企微分层工作台…'
    stage=$(mktemp -d "$PWD/.local/workbench-build.XXXXXX")
    trap 'rm -rf -- "$stage"' EXIT
    bundle="$stage/企微分层工作台.app"
    mkdir -p "$bundle/Contents/MacOS"
    cp desktop/Info.plist "$bundle/Contents/Info.plist"
    xcrun swiftc -sdk "$(xcrun --show-sdk-path)" -target "$(uname -m)-apple-macosx13.0" \
        -module-cache-path .local/swift-module-cache -parse-as-library desktop/Workbench.swift wecom_segmentation/read_window.swift \
        -o "$bundle/Contents/MacOS/Workbench"
    /usr/bin/codesign --force --sign - --identifier local.wecom.segmentation.workbench "$bundle"
    /usr/bin/ditto "$bundle" "$app"
fi
if [[ "${1:-}" != '--build-only' ]]; then
    open "$app"
fi
