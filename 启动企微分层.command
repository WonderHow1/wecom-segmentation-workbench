#!/bin/zsh
set -eu
cd -- "${0:A:h}"
umask 077

if [[ ! -x .local/venv/bin/python ]]; then
    print '请先按 README 的安装步骤创建 .local/venv 并安装依赖。'
    exit 1
fi
if [[ ! -x .local/wecom-window-reader || wecom_segmentation/read_window.swift -nt .local/wecom-window-reader || wecom_segmentation/read_window_main.swift -nt .local/wecom-window-reader ]]; then
    print '正在编译企微窗口读取程序…'
    xcrun swiftc -module-cache-path .local/swift-module-cache wecom_segmentation/read_window.swift wecom_segmentation/read_window_main.swift -o .local/wecom-window-reader
fi

export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-/etc/ssl/cert.pem}"
exec .local/venv/bin/python -m wecom_segmentation.operator
