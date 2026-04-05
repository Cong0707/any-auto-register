import unittest

from platforms.chatgpt.http_replay import (
    is_retryable_http_status,
    run_http_step_with_replay,
)


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class ReplayTests(unittest.TestCase):
    def test_retryable_status(self):
        self.assertTrue(is_retryable_http_status(429))
        self.assertTrue(is_retryable_http_status(530, extra=[530]))
        self.assertFalse(is_retryable_http_status(400))

    def test_run_replay_succeeds_after_transient_error(self):
        calls = {"n": 0}

        def _request():
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(503, "busy")
            return _Resp(200, "ok")

        result = run_http_step_with_replay(
            step="token_exchange",
            request_fn=_request,
            is_success_fn=lambda resp: resp.status_code == 200,
            max_attempts=3,
            base_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)

    def test_run_replay_stops_on_non_retryable_status(self):
        calls = {"n": 0}

        def _request():
            calls["n"] += 1
            return _Resp(400, "bad request")

        result = run_http_step_with_replay(
            step="token_exchange",
            request_fn=_request,
            is_success_fn=lambda resp: resp.status_code == 200,
            max_attempts=5,
            base_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)


if __name__ == "__main__":
    unittest.main()
