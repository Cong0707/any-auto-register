"""批量刷新本地 ChatGPT token JSON 文件。"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .token_refresh import TokenRefreshManager

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


@dataclass
class TokenFileRefreshResult:
    path: str
    status: str
    message: str = ""
    email: str = ""
    old_expired: str = ""
    new_expired: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "message": self.message,
            "email": self.email,
            "old_expired": self.old_expired,
            "new_expired": self.new_expired,
        }


@dataclass
class TokenDirectoryRefreshSummary:
    token_dir: str
    total_files: int = 0
    refreshed: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[TokenFileRefreshResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "token_dir": self.token_dir,
            "total_files": self.total_files,
            "refreshed": self.refreshed,
            "skipped": self.skipped,
            "failed": self.failed,
            "results": [item.to_dict() for item in self.results],
        }


class _TokenRecord:
    """用于复用 TokenRefreshManager.refresh_account 的鸭子对象。"""

    def __init__(self, payload: dict):
        self.email = str(payload.get("email") or "").strip()
        self.access_token = str(payload.get("access_token") or "").strip()
        self.refresh_token = str(payload.get("refresh_token") or "").strip()
        self.session_token = str(payload.get("session_token") or "").strip()
        self.client_id = str(payload.get("client_id") or "").strip()


def _decode_jwt_payload(token: str) -> dict:
    raw = str(token or "").strip()
    if not raw or raw.count(".") < 2:
        return {}
    payload = raw.split(".")[1]
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _token_expired_string(access_token: str) -> str:
    payload = _decode_jwt_payload(access_token)
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= 0:
        return ""
    dt = datetime.fromtimestamp(exp, tz=_CST)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _now_string() -> str:
    return datetime.now(tz=_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _iter_token_json_files(root: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*.json" if recursive else "*.json"
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        if path.name.lower().endswith(".bak"):
            continue
        yield path


def _atomic_write_json(path: Path, payload: dict, *, backup: bool) -> None:
    original_text = ""
    if path.exists():
        original_text = path.read_text(encoding="utf-8", errors="ignore")

    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(serialized, encoding="utf-8")

    if backup and original_text:
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(original_text, encoding="utf-8")

    temp_path.replace(path)


def refresh_token_json_file(
    path: str | Path,
    *,
    manager: TokenRefreshManager,
    dry_run: bool = False,
    backup: bool = True,
    log_fn: Callable[[str], None] | None = None,
) -> TokenFileRefreshResult:
    token_path = Path(path)
    log = log_fn or (lambda _msg: None)

    result = TokenFileRefreshResult(path=str(token_path), status="failed")

    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.message = f"JSON 解析失败: {exc}"
        return result

    if not isinstance(payload, dict):
        result.message = "文件 JSON 结构不是 object"
        return result

    record = _TokenRecord(payload)
    result.email = record.email
    result.old_expired = str(payload.get("expired") or "").strip()

    if not record.refresh_token and not record.session_token:
        result.status = "skipped"
        result.message = "缺少 refresh_token/session_token"
        return result

    try:
        refresh_result = manager.refresh_account(record)
    except Exception as exc:
        result.message = f"刷新异常: {exc}"
        return result
    if not refresh_result.success:
        result.message = refresh_result.error_message or "刷新失败"
        return result

    payload["access_token"] = refresh_result.access_token
    if refresh_result.refresh_token:
        payload["refresh_token"] = refresh_result.refresh_token

    expired = _token_expired_string(refresh_result.access_token)
    if expired:
        payload["expired"] = expired
        result.new_expired = expired
    else:
        result.new_expired = str(payload.get("expired") or "").strip()

    payload["last_refresh"] = _now_string()

    if not dry_run:
        _atomic_write_json(token_path, payload, backup=backup)
        log(f"已刷新: {token_path.name}")
    else:
        log(f"[dry-run] 预览刷新: {token_path.name}")

    result.status = "refreshed"
    result.message = "ok"
    return result


def resolve_default_token_dir() -> Path:
    env_candidates = [
        os.getenv("TOKEN_JSON_DIR", ""),
        os.getenv("CHATGPT_TOKEN_JSON_DIR", ""),
    ]

    config_candidates: list[str] = []
    try:
        from core.config_store import config_store

        for key in ("token_json_dir", "chatgpt_token_json_dir", "codex_token_json_dir"):
            value = str(config_store.get(key, "") or "").strip()
            if value:
                config_candidates.append(value)
    except Exception:
        pass

    for item in [*env_candidates, *config_candidates]:
        text = str(item or "").strip()
        if text:
            return Path(text).expanduser()

    return Path.cwd() / "codex_tokens"


def refresh_token_json_directory(
    token_dir: str | Path,
    *,
    recursive: bool = False,
    proxy_url: str | None = None,
    dry_run: bool = False,
    backup: bool = True,
    fail_fast: bool = False,
    manager_factory: Callable[[], TokenRefreshManager] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> TokenDirectoryRefreshSummary:
    root = Path(token_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"token 目录不存在: {root}")

    summary = TokenDirectoryRefreshSummary(token_dir=str(root))
    files = list(_iter_token_json_files(root, recursive=recursive))
    summary.total_files = len(files)

    if manager_factory is None:
        manager = TokenRefreshManager(proxy_url=proxy_url)
    else:
        manager = manager_factory()

    for token_file in files:
        item = refresh_token_json_file(
            token_file,
            manager=manager,
            dry_run=dry_run,
            backup=backup,
            log_fn=log_fn,
        )
        summary.results.append(item)

        if item.status == "refreshed":
            summary.refreshed += 1
        elif item.status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1
            logger.warning("刷新 token 文件失败: %s (%s)", token_file, item.message)
            if fail_fast:
                break

    return summary
