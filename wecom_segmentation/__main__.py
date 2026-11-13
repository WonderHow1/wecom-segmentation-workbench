import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

from .classification import classify_pending
from .feishu import Config, Feishu, FeishuError
from .model_api import DEFAULT_CONFIG, ApiModel
from .store import InputError, Store
from .sync import sync_lock, sync_table
from .window import WindowError, read_window


def main(argv=None):
    parser = argparse.ArgumentParser(description="从企微私聊生成本地线索分层预览")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="导入 JSONL 样本")
    importer.add_argument("--file", type=Path, required=True)
    capture = commands.add_parser("capture-window", help="读取前台指定企微私聊")
    capture.add_argument("--title", required=True, help="聊天顶部显示的完整会话名")
    capture.add_argument("--reader", type=Path, default=Path(".local/wecom-window-reader"))
    capture.add_argument("--customer-pilot", action="store_true", help="采集指定客户私聊用于试跑")
    classification = commands.add_parser("classify", help="用本地 Ollama 或选定的模型 API 分层")
    classification.add_argument("--api", action="store_true", help="把待处理的聊天文字发送给配置的模型 API")
    classification.add_argument("--api-config", type=Path, default=DEFAULT_CONFIG)
    classification.add_argument("--reprocess", action="store_true", help="也重新处理已有分层结果的会话")
    commands.add_parser("status", help="查看本地处理进度")
    sync = commands.add_parser("sync", help="预览或同步线索到飞书测试表")
    sync_mode = sync.add_mutually_exclusive_group()
    sync_mode.add_argument("--dry-run", action="store_true")
    sync_mode.add_argument("--setup", action="store_true", help="先补齐缺失字段，再同步线索")
    sync.add_argument("--config", type=Path, default=Path(".local/feishu.env"))
    scope = sync.add_mutually_exclusive_group()
    scope.add_argument("--window-test", action="store_true", help="同步指定企微测试私聊的分层字段")
    scope.add_argument("--customer-pilot", action="store_true", help="同步指定客户私聊的试跑分层字段")
    args = parser.parse_args(argv)
    if args.db is None:
        if args.command == "capture-window" or (args.command == "sync" and (args.window_test or args.customer_pilot)):
            parser.error("企微窗口采集和同步需要用 --db 指定会话的本地数据库")
        args.db = Path(".local/segmentation.sqlite3")

    old_mask = os.umask(0o077)
    try:
        with closing(Store(args.db)) as store:
            match args.command:
                case "import":
                    result = store.import_file(args.file)
                case "capture-window":
                    result = store.capture_window(lambda: read_window(args.reader, args.title),
                                                  customer_pilot=args.customer_pilot)
                case "classify":
                    api = ApiModel.load(args.api_config) if args.api else None
                    result = classify_pending(store, api=api, reprocess=args.reprocess)
                case "status":
                    result = store.status()
                case "sync":
                    if args.dry_run:
                        result = store.preview()
                    else:
                        config = Config.load(args.config)
                        with sync_lock(args.db):
                            result = sync_table(Feishu(config), store, config.target,
                                                setup=args.setup, window_test=args.window_test,
                                                customer_pilot=args.customer_pilot)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.command == "classify":
                return int(bool(result["分层失败"] or (args.reprocess and result["失败原因"])))
            return int(args.command == "sync" and not args.dry_run and result["失败"] > 0)
    except (InputError, FeishuError, WindowError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("无法读写本地样本或数据库，请检查路径和权限", file=sys.stderr)
        return 1
    finally:
        os.umask(old_mask)


if __name__ == "__main__":
    raise SystemExit(main())
