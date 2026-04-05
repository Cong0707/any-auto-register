import unittest
from unittest.mock import patch

from platforms.chatgpt.sentinel_browser import _apply_env_overrides
from platforms.chatgpt.sentinel_token import (
    DEFAULT_SENTINEL_SDK_URL,
    _SENTINEL_SDK_CACHE,
    _extract_sdk_url,
    resolve_sentinel_sdk_url,
)


class _FakeResponse:
    def __init__(self, text: str, url: str = "https://sentinel.openai.com/backend-api/sentinel/frame.html"):
        self.status_code = 200
        self.text = text
        self.url = url


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


class SentinelEnvTests(unittest.TestCase):
    def setUp(self):
        _SENTINEL_SDK_CACHE["url"] = ""
        _SENTINEL_SDK_CACHE["expires_at"] = 0.0

    def test_apply_env_overrides(self):
        with patch.dict(
            "os.environ",
            {
                "SENTINEL_BROWSER_FLOW": "password_verify",
                "SENTINEL_BROWSER_PROXY": "http://127.0.0.1:7890",
                "SENTINEL_BROWSER_PAGE_URL": "https://auth.openai.com/log-in/password",
                "SENTINEL_BROWSER_TIMEOUT_MS": "90000",
                "SENTINEL_BROWSER_UA": "Mozilla/5.0 UnitTest",
            },
            clear=False,
        ):
            flow, proxy, timeout_ms, page_url, ua = _apply_env_overrides(
                flow="authorize_continue",
                proxy=None,
                timeout_ms=45000,
                page_url="",
                user_agent=None,
            )

        self.assertEqual(flow, "password_verify")
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertEqual(timeout_ms, 90000)
        self.assertEqual(page_url, "https://auth.openai.com/log-in/password")
        self.assertEqual(ua, "Mozilla/5.0 UnitTest")

    def test_extract_sdk_url(self):
        html = '<script src="https://sentinel.openai.com/sentinel/20260401abcd/sdk.js"></script>'
        self.assertEqual(
            _extract_sdk_url(html),
            "https://sentinel.openai.com/sentinel/20260401abcd/sdk.js",
        )

    def test_resolve_sentinel_sdk_url_uses_session_html(self):
        session = _FakeSession(
            _FakeResponse('<script src="/sentinel/20260402dcba/sdk.js"></script>')
        )
        resolved = resolve_sentinel_sdk_url(session)
        self.assertEqual(
            resolved,
            "https://sentinel.openai.com/sentinel/20260402dcba/sdk.js",
        )

    def test_resolve_sentinel_sdk_url_falls_back_when_disabled(self):
        session = _FakeSession(_FakeResponse('<script src="/sentinel/ignored/sdk.js"></script>'))
        with patch.dict("os.environ", {"SENTINEL_SDK_RESOLVE_DISABLE": "1"}, clear=False):
            resolved = resolve_sentinel_sdk_url(session)
        self.assertEqual(resolved, DEFAULT_SENTINEL_SDK_URL)


if __name__ == "__main__":
    unittest.main()
