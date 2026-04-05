import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from platforms.chatgpt.token_json_refresh import (
    refresh_token_json_directory,
    refresh_token_json_file,
    resolve_default_token_dir,
)


class _FakeRefreshResult:
    def __init__(self, *, success: bool, access_token: str = "", refresh_token: str = "", error_message: str = ""):
        self.success = success
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.error_message = error_message


class _FakeManager:
    def __init__(self, mapper):
        self._mapper = mapper

    def refresh_account(self, account):
        key = str(getattr(account, "refresh_token", "") or getattr(account, "session_token", ""))
        return self._mapper(key)


def _jwt_with_exp(exp: int) -> str:
    def _b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'exp': exp})}.sig"


class TokenJSONRefreshTests(unittest.TestCase):
    def test_refresh_token_json_file_updates_and_writes_backup(self):
        with tempfile.TemporaryDirectory() as td:
            token_path = Path(td) / "demo.json"
            token_path.write_text(
                json.dumps(
                    {
                        "email": "demo@example.com",
                        "access_token": "old-at",
                        "refresh_token": "rt-ok",
                        "expired": "2000-01-01T00:00:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            exp = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
            new_access = _jwt_with_exp(exp)
            manager = _FakeManager(
                lambda key: _FakeRefreshResult(
                    success=key == "rt-ok",
                    access_token=new_access,
                    refresh_token="rt-new",
                    error_message="bad token",
                )
            )

            result = refresh_token_json_file(token_path, manager=manager, dry_run=False, backup=True)
            self.assertEqual(result.status, "refreshed")

            payload = json.loads(token_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["access_token"], new_access)
            self.assertEqual(payload["refresh_token"], "rt-new")
            self.assertIn("last_refresh", payload)

            expected_cst = datetime.fromtimestamp(exp, tz=timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%dT%H:%M:%S+08:00"
            )
            self.assertEqual(payload["expired"], expected_cst)

            backup_path = Path(str(token_path) + ".bak")
            self.assertTrue(backup_path.exists())
            backup_payload = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(backup_payload["access_token"], "old-at")

    def test_refresh_token_json_file_skips_without_refresh_or_session_token(self):
        with tempfile.TemporaryDirectory() as td:
            token_path = Path(td) / "skip.json"
            token_path.write_text(
                json.dumps({"email": "skip@example.com", "access_token": "at"}, ensure_ascii=False),
                encoding="utf-8",
            )

            manager = _FakeManager(lambda _key: _FakeRefreshResult(success=True, access_token="new"))
            result = refresh_token_json_file(token_path, manager=manager)

            self.assertEqual(result.status, "skipped")
            self.assertIn("缺少 refresh_token/session_token", result.message)

    def test_refresh_token_json_directory_collects_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.json").write_text(
                json.dumps({"email": "a@example.com", "refresh_token": "rt-a"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "b.json").write_text(
                json.dumps({"email": "b@example.com", "refresh_token": "rt-b"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "bad.json").write_text("not-json", encoding="utf-8")

            def _factory():
                return _FakeManager(
                    lambda key: _FakeRefreshResult(
                        success=key == "rt-a",
                        access_token=_jwt_with_exp(int(datetime(2032, 1, 1, tzinfo=timezone.utc).timestamp())),
                        refresh_token=key,
                        error_message="refresh failed",
                    )
                )

            summary = refresh_token_json_directory(root, dry_run=True, manager_factory=_factory)
            self.assertEqual(summary.total_files, 3)
            self.assertEqual(summary.refreshed, 1)
            self.assertEqual(summary.failed, 2)
            self.assertEqual(summary.skipped, 0)

    def test_resolve_default_token_dir_prefers_env(self):
        with patch.dict("os.environ", {"TOKEN_JSON_DIR": r"D:\\tokens"}, clear=False):
            resolved = resolve_default_token_dir()
        self.assertEqual(str(resolved), r"D:\tokens")


if __name__ == "__main__":
    unittest.main()
