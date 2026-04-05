#!/usr/bin/env python3
"""离线批量刷新 token JSON 目录。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platforms.chatgpt.token_json_refresh import (  # noqa: E402
    refresh_token_json_directory,
    resolve_default_token_dir,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh ChatGPT token JSON directory")
    parser.add_argument(
        "token_dir",
        nargs="?",
        default="",
        help="token JSON 目录（默认读取 TOKEN_JSON_DIR / ConfigStore / ./codex_tokens）",
    )
    parser.add_argument("--recursive", action="store_true", help="递归刷新子目录")
    parser.add_argument("--proxy", default="", help="刷新时使用的代理")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写文件")
    parser.add_argument("--no-backup", action="store_true", help="覆盖写入时不保留 .bak")
    parser.add_argument("--fail-fast", action="store_true", help="遇到第一个失败立即停止")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    token_dir = Path(args.token_dir).expanduser() if str(args.token_dir).strip() else resolve_default_token_dir()

    try:
        summary = refresh_token_json_directory(
            token_dir,
            recursive=bool(args.recursive),
            proxy_url=str(args.proxy or "").strip() or None,
            dry_run=bool(args.dry_run),
            backup=not bool(args.no_backup),
            fail_fast=bool(args.fail_fast),
            log_fn=print,
        )
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "token_dir": str(token_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
