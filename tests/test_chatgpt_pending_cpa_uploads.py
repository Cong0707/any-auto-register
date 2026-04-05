import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platforms.chatgpt import cpa_upload


class PendingCpaUploadTests(unittest.TestCase):
    def test_upload_fail_will_queue_pending_file(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"CHATGPT_PENDING_CPA_UPLOAD_DIR": td}, clear=False
        ):
            token_data = {
                "email": "demo@example.com",
                "access_token": "at",
                "refresh_token": "rt",
            }
            with patch.object(cpa_upload, "_upload_to_cpa_once", return_value=(False, "HTTP 503")):
                ok, msg = cpa_upload.upload_to_cpa(
                    token_data,
                    api_url="https://example.invalid",
                    api_key="k",
                    queue_on_fail=True,
                )

            self.assertFalse(ok)
            self.assertIn("待重传队列", msg)
            files = list(Path(td).glob("pending_cpa_*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload.get("token_data", {}).get("email"), "demo@example.com")

    def test_retry_pending_success_removes_file(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"CHATGPT_PENDING_CPA_UPLOAD_DIR": td}, clear=False
        ):
            queued = cpa_upload._queue_pending_cpa_upload(
                {
                    "email": "ok@example.com",
                    "access_token": "at",
                    "refresh_token": "rt",
                },
                api_url="https://example.invalid",
                error_message="fail",
            )
            self.assertTrue(Path(queued).exists())

            with patch.object(cpa_upload, "_upload_to_cpa_once", return_value=(True, "上传成功")):
                summary = cpa_upload.retry_pending_cpa_uploads(api_key="k", max_items=10)

            self.assertEqual(summary["success"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["remaining"], 0)

    def test_retry_pending_fail_updates_attempts(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"CHATGPT_PENDING_CPA_UPLOAD_DIR": td}, clear=False
        ):
            queued = cpa_upload._queue_pending_cpa_upload(
                {
                    "email": "bad@example.com",
                    "access_token": "at",
                    "refresh_token": "rt",
                },
                api_url="https://example.invalid",
                error_message="fail",
                attempts=1,
            )
            with patch.object(cpa_upload, "_upload_to_cpa_once", return_value=(False, "HTTP 500")):
                summary = cpa_upload.retry_pending_cpa_uploads(api_key="k", max_items=10)

            self.assertEqual(summary["success"], 0)
            self.assertEqual(summary["failed"], 1)
            payload = json.loads(Path(queued).read_text(encoding="utf-8"))
            self.assertEqual(int(payload.get("attempts") or 0), 2)
            self.assertIn("HTTP 500", str(payload.get("last_error") or ""))


if __name__ == "__main__":
    unittest.main()
