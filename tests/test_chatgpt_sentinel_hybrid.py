import json
import unittest
from unittest.mock import patch

from platforms.chatgpt import sentinel_token


def _mk_token(*, flow: str, device_id: str, t: str = "tt") -> str:
    return json.dumps(
        {
            "p": "gAAAAAB-test",
            "t": t,
            "c": "challenge",
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


class SentinelHybridTests(unittest.TestCase):
    def setUp(self):
        sentinel_token._SENTINEL_TOKEN_CACHE.clear()
        sentinel_token._SENTINEL_SOURCE_EWMA.clear()

    def test_browser_success_and_cache_hit(self):
        flow = "authorize_continue"
        did = "dev-1"
        browser_token = _mk_token(flow=flow, device_id=did, t="ok")

        with patch.object(sentinel_token, "_build_sentinel_token_browser", return_value=browser_token) as browser_mock, \
             patch.object(sentinel_token, "_build_sentinel_token_python", return_value=None) as python_mock:
            first = sentinel_token.build_sentinel_token(
                session=object(),
                device_id=did,
                flow=flow,
                proxy="http://1.2.3.4:7890",
                use_cache=True,
            )
            second = sentinel_token.build_sentinel_token(
                session=object(),
                device_id=did,
                flow=flow,
                proxy="http://1.2.3.4:7890",
                use_cache=True,
            )

        self.assertEqual(first, browser_token)
        self.assertEqual(second, browser_token)
        self.assertEqual(browser_mock.call_count, 1)
        self.assertEqual(python_mock.call_count, 0)

    def test_strict_flow_browser_missing_t_fallback_python(self):
        flow = "oauth_create_account"
        did = "dev-2"
        browser_bad = _mk_token(flow=flow, device_id=did, t="")
        python_ok = _mk_token(flow=flow, device_id=did, t="")

        with patch.object(sentinel_token, "_build_sentinel_token_browser", return_value=browser_bad) as browser_mock, \
             patch.object(sentinel_token, "_build_sentinel_token_python", return_value=python_ok) as python_mock:
            token = sentinel_token.build_sentinel_token(
                session=object(),
                device_id=did,
                flow=flow,
                prefer_browser=True,
                use_cache=False,
            )

        self.assertEqual(token, python_ok)
        self.assertEqual(browser_mock.call_count, 1)
        self.assertEqual(python_mock.call_count, 1)

    def test_ewma_can_prioritize_python_before_browser(self):
        flow = "authorize_continue"
        did = "dev-3"
        proxy = "http://5.6.7.8:9000"
        norm_proxy = proxy.lower()
        sentinel_token._SENTINEL_SOURCE_EWMA[(flow, norm_proxy, "browser")] = 0.0
        sentinel_token._SENTINEL_SOURCE_EWMA[(flow, norm_proxy, "python")] = 1.0

        calls = []

        def _browser(*args, **kwargs):
            calls.append("browser")
            return _mk_token(flow=flow, device_id=did, t="ok")

        def _python(*args, **kwargs):
            calls.append("python")
            return _mk_token(flow=flow, device_id=did, t="")

        with patch.object(sentinel_token, "_build_sentinel_token_browser", side_effect=_browser), \
             patch.object(sentinel_token, "_build_sentinel_token_python", side_effect=_python):
            token = sentinel_token.build_sentinel_token(
                session=object(),
                device_id=did,
                flow=flow,
                proxy=proxy,
                prefer_browser=True,
                use_cache=False,
            )

        self.assertTrue(token)
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0], "python")

    def test_browser_device_mismatch_can_adopt_token_device_id(self):
        flow = "authorize_continue"
        did = "dev-4"
        browser_did = "dev-browser-4"
        browser_token = _mk_token(flow=flow, device_id=browser_did, t="ok")

        with patch.object(sentinel_token, "_build_sentinel_token_browser", return_value=browser_token) as browser_mock, \
             patch.object(sentinel_token, "_build_sentinel_token_python", return_value=None) as python_mock:
            token = sentinel_token.build_sentinel_token(
                session=object(),
                device_id=did,
                flow=flow,
                use_cache=False,
            )

        self.assertEqual(token, browser_token)
        self.assertEqual(browser_mock.call_count, 1)
        self.assertEqual(python_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main()
