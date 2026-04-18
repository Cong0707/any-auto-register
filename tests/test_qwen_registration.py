import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import Account, RegisterConfig
from platforms.qwen.core import QwenRegister, wait_for_activation_link
from platforms.qwen.cpa_upload import generate_token_json, upload_to_cpa
from platforms.qwen.plugin import QwenPlatform


class _DummyExecutor:
    page = object()


class _DummyExecutorContext:
    def __enter__(self):
        return _DummyExecutor()

    def __exit__(self, exc_type, exc, tb):
        return False


class _SequenceQwenRegister(QwenRegister):
    def __init__(self, results, logs):
        super().__init__(executor=_DummyExecutor(), log_fn=logs.append)
        self._results = list(results)
        self.calls = 0

    def _try_register(self, page, email, password, full_name):
        current = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return dict(current)


class _FakeCFWorkerMailbox:
    def __init__(self, mails):
        self._mails = mails

    def _get_mails(self, _email: str):
        return list(self._mails)


class _FakeCFWorkerMailboxWithDifferentCurrentEmail(_FakeCFWorkerMailbox):
    def get_email(self):
        return MailboxAccount(email="other@example.com", account_id="dummy")

    def get_current_ids(self, _account):
        return set()


class _FakeHttpResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class QwenRegistrationTests(unittest.TestCase):
    def test_get_platform_actions_contains_upload_cpa(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        actions = platform.get_platform_actions()
        action_ids = [item.get("id") for item in actions]
        self.assertIn("upload_cpa", action_ids)

    def test_register_stops_immediately_when_tokens_exist(self):
        logs = []
        reg = _SequenceQwenRegister(
            results=[
                {
                    "email": "demo@example.com",
                    "password": "Abc123!@#",
                    "full_name": "Demo",
                    "tokens": {"cookie:token": "tok_demo"},
                    "status": "success",
                }
            ],
            logs=logs,
        )

        result = reg.register(email="demo@example.com", password="Abc123!@#", full_name="Demo")

        self.assertEqual(reg.calls, 1)
        self.assertEqual(result.get("status"), "success")
        self.assertEqual(result.get("tokens", {}).get("cookie:token"), "tok_demo")
        self.assertTrue(any("first-attempt token hit" in msg for msg in logs))

    def test_register_returns_failed_after_retry_exhausted(self):
        logs = []
        reg = _SequenceQwenRegister(
            results=[
                {"status": "failed", "tokens": {}, "error": "attempt-1"},
                {"status": "failed", "tokens": {}, "error": "attempt-2"},
                {"status": "failed", "tokens": {}, "error": "attempt-3"},
            ],
            logs=logs,
        )

        with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
            result = reg.register(email="demo@example.com", password="Abc123!@#", full_name="Demo")

        self.assertEqual(reg.calls, 3)
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result.get("error"), "attempt-3")
        self.assertTrue(any("final failure reason(no token): attempt-3" in msg for msg in logs))

    def test_plugin_success_requires_non_empty_token(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {"cookie:token": "tok_ok"},
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                account = platform.register(email="demo@example.com", password="Abc123!@#")

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.token, "tok_ok")

    def test_plugin_raises_when_registration_status_failed(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "failed",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {},
            "error": "no token",
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                with self.assertRaises(RuntimeError):
                    platform.register(email="demo@example.com", password="Abc123!@#")

    def test_plugin_raises_when_success_but_token_missing(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {},
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                with self.assertRaises(RuntimeError):
                    platform.register(email="demo@example.com", password="Abc123!@#")

    def test_plugin_register_extracts_oauth_fields_from_raw_tokens(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {
                "cookie:token": "tok_ok",
                "oauth_payload": (
                    '{"oauth_access_token":"oa_demo",'
                    '"refreshToken":"rt_demo","resource_url":"portal.qwen.ai"}'
                ),
            },
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                account = platform.register(email="demo@example.com", password="Abc123!@#")

        self.assertEqual(account.token, "tok_ok")
        self.assertEqual((account.extra or {}).get("oauth_access_token"), "oa_demo")
        self.assertEqual((account.extra or {}).get("refresh_token"), "rt_demo")
        self.assertEqual((account.extra or {}).get("resource_url"), "portal.qwen.ai")

    def test_activate_action_can_bootstrap_mailbox_and_activate(self):
        platform = QwenPlatform(
            config=RegisterConfig(extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="",
        )
        fake_mailbox = _FakeCFWorkerMailbox(
            mails=[
                {
                    "id": 1,
                    "subject": "Activate your Qwen account",
                    "raw": (
                        "Click to activate: "
                        "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def"
                    ),
                }
            ]
        )

        with mock.patch("core.base_mailbox.create_mailbox", return_value=fake_mailbox):
            with mock.patch("platforms.qwen.core.call_activation_api", return_value={"ok": True}):
                with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
                    result = platform.execute_action("activate_account", account, {})

        self.assertTrue(result.get("ok"))

    def test_activate_action_reports_timeout_with_default_wait_seconds(self):
        platform = QwenPlatform(
            config=RegisterConfig(
                extra={
                    "mail_provider": "cfworker",
                    "mailbox_otp_timeout_seconds": 30,
                }
            ),
            mailbox=None,
        )
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="",
        )
        fake_mailbox = _FakeCFWorkerMailbox(mails=[])

        with mock.patch("core.base_mailbox.create_mailbox", return_value=fake_mailbox):
            with mock.patch("platforms.qwen.core.wait_for_activation_link", return_value=None):
                result = platform.execute_action("activate_account", account, {})

        self.assertFalse(result.get("ok"))
        self.assertIn("在 30s 内未找到激活邮件", str(result.get("error")))

    def test_activate_action_ignores_current_mailbox_email_for_cfworker_lookup(self):
        platform = QwenPlatform(
            config=RegisterConfig(extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )
        account = Account(
            platform="qwen",
            email="target@example.com",
            password="Abc123!@#",
            token="",
        )
        fake_mailbox = _FakeCFWorkerMailboxWithDifferentCurrentEmail(
            mails=[
                {
                    "id": 1,
                    "subject": "Activate",
                    "raw": "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def",
                }
            ]
        )

        with mock.patch("core.base_mailbox.create_mailbox", return_value=fake_mailbox):
            with mock.patch("platforms.qwen.core.call_activation_api", return_value={"ok": True}):
                with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
                    result = platform.execute_action("activate_account", account, {})

        self.assertTrue(result.get("ok"))

    def test_wait_for_activation_link_can_decode_base64_html_raw_mail(self):
        import base64

        html = (
            '<html><body>'
            '<a href="https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def">Activate</a>'
            "</body></html>"
        )
        b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            f"{b64}\r\n"
        )
        mailbox = _FakeCFWorkerMailbox(
            mails=[{"id": 1, "subject": "Activate", "raw": raw}]
        )

        with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
            link = wait_for_activation_link(
                mailbox,
                account_email="target@example.com",
                timeout=5,
            )

        self.assertEqual(
            link,
            "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def",
        )

    def test_get_user_info_fallbacks_to_chats_endpoint(self):
        # Header: {"alg":"HS256","typ":"JWT"}
        # Payload: {"id":"u1","exp":1778728455}
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6InUxIiwiZXhwIjoxNzc4NzI4NDU1fQ.sig"
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token=token,
        )

        responses = [
            _FakeHttpResp(404, {"detail": "Not Found"}),
            _FakeHttpResp(403, {"detail": "restricted"}),
            _FakeHttpResp(200, []),
        ]

        with mock.patch("curl_cffi.requests.get", side_effect=responses):
            result = platform.execute_action("get_user_info", account, {})

        self.assertTrue(result.get("ok"))
        data = result.get("data", {})
        self.assertEqual(data.get("来源"), "会话列表接口")
        self.assertEqual(data.get("会话数量"), 0)
        self.assertEqual(data.get("用户ID"), "u1")

    def test_upload_cpa_action_uses_qwen_uploader(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="qwen_token_abc",
            extra={
                "refresh_token": "qwen_refresh_token_xyz",
                "resource_url": "portal.qwen.ai",
            },
        )

        with mock.patch(
            "platforms.qwen.cpa_upload.generate_token_json",
            return_value={"email": "demo@example.com", "access_token": "qwen_token_abc"},
        ) as build_mock:
            with mock.patch(
                "platforms.qwen.cpa_upload.upload_to_cpa",
                return_value=(True, "上传成功"),
            ) as upload_mock:
                result = platform.execute_action(
                    "upload_cpa",
                    account,
                    {"api_url": "http://cpa.local", "api_key": "k"},
                )

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("data"), "上传成功")
        build_arg = build_mock.call_args.args[0]
        self.assertEqual(getattr(build_arg, "email", ""), "demo@example.com")
        self.assertEqual(getattr(build_arg, "access_token", ""), "qwen_token_abc")
        self.assertEqual(getattr(build_arg, "refresh_token", ""), "qwen_refresh_token_xyz")
        self.assertEqual(getattr(build_arg, "resource_url", ""), "portal.qwen.ai")
        upload_mock.assert_called_once_with(
            {"email": "demo@example.com", "access_token": "qwen_token_abc"},
            api_url="http://cpa.local",
            api_key="k",
        )

    def test_upload_cpa_action_reads_refresh_from_raw_tokens(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="qwen_token_abc",
            extra={
                "raw_tokens": {
                    "oauth_payload": '{"refreshToken":"rt_raw","resource_url":"portal.qwen.ai"}',
                }
            },
        )

        with mock.patch(
            "platforms.qwen.cpa_upload.generate_token_json",
            return_value={"email": "demo@example.com", "access_token": "qwen_token_abc"},
        ) as build_mock:
            with mock.patch(
                "platforms.qwen.cpa_upload.upload_to_cpa",
                return_value=(True, "上传成功"),
            ):
                result = platform.execute_action(
                    "upload_cpa",
                    account,
                    {"api_url": "http://cpa.local", "api_key": "k"},
                )

        self.assertTrue(result.get("ok"))
        build_arg = build_mock.call_args.args[0]
        self.assertEqual(getattr(build_arg, "refresh_token", ""), "rt_raw")
        self.assertEqual(getattr(build_arg, "resource_url", ""), "portal.qwen.ai")

    def test_upload_cpa_action_bootstraps_oauth_when_refresh_missing(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="web_token_only",
            extra={},
        )

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch(
                "platforms.qwen.core.obtain_qwen_oauth_tokens_with_login",
                return_value={
                    "oauth_access_token": "oauth_access_123",
                    "refresh_token": "oauth_refresh_456",
                    "resource_url": "portal.qwen.ai",
                },
            ):
                with mock.patch(
                    "platforms.qwen.cpa_upload.generate_token_json",
                    return_value={"email": "demo@example.com", "access_token": "oauth_access_123"},
                ) as build_mock:
                    with mock.patch(
                        "platforms.qwen.cpa_upload.upload_to_cpa",
                        return_value=(True, "上传成功"),
                    ):
                        result = platform.execute_action(
                            "upload_cpa",
                            account,
                            {"api_url": "http://cpa.local", "api_key": "k"},
                        )

        self.assertTrue(result.get("ok"))
        build_arg = build_mock.call_args.args[0]
        self.assertEqual(getattr(build_arg, "access_token", ""), "oauth_access_123")
        self.assertEqual(getattr(build_arg, "refresh_token", ""), "oauth_refresh_456")
        self.assertEqual(getattr(build_arg, "resource_url", ""), "portal.qwen.ai")
        self.assertEqual(
            (result.get("account_extra_patch") or {}).get("refresh_token"),
            "oauth_refresh_456",
        )

    def test_qwen_cpa_upload_requires_refresh_token(self):
        ok, msg = upload_to_cpa(
            {
                "type": "qwen",
                "email": "demo@example.com",
                "access_token": "token_only",
                "refresh_token": "",
            },
            api_url="http://cpa.local",
            api_key="k",
        )
        self.assertFalse(ok)
        self.assertIn("refresh_token", msg)

    def test_qwen_cpa_generate_token_json_contains_oauth_fields(self):
        class _A:
            pass

        a = _A()
        a.email = "demo@example.com"
        a.access_token = "token"
        a.refresh_token = "rt_demo"
        a.resource_url = "portal.qwen.ai"

        token_json = generate_token_json(a)
        self.assertEqual(token_json.get("type"), "qwen")
        self.assertEqual(token_json.get("provider"), "qwen")
        self.assertEqual(token_json.get("email"), "demo@example.com")
        self.assertEqual(token_json.get("access_token"), "token")
        self.assertEqual(token_json.get("refresh_token"), "rt_demo")
        self.assertEqual(token_json.get("resource_url"), "portal.qwen.ai")


if __name__ == "__main__":
    unittest.main()
