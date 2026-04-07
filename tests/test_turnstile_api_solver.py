import asyncio
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "turnstile_solver"))

from services.turnstile_solver.api_solver import TurnstileAPIServer, parse_args


class TurnstileAPIServerTests(unittest.TestCase):
    def _create_server(self) -> TurnstileAPIServer:
        return TurnstileAPIServer(
            headless=True,
            useragent=None,
            debug=False,
            browser_type="camoufox",
            thread=1,
            proxy_support=False,
        )

    def test_process_turnstile_returns_init_failed_before_request_validation(self):
        server = self._create_server()
        server.browser_init_error = "camoufox bootstrap failed"

        with patch("services.turnstile_solver.api_solver.request", SimpleNamespace(args={})):
            with patch("services.turnstile_solver.api_solver.jsonify", side_effect=lambda payload: payload):
                payload, status = asyncio.run(server.process_turnstile())

        self.assertEqual(status, 200)
        self.assertEqual(payload["errorCode"], "ERROR_SOLVER_INIT_FAILED")
        self.assertIn("camoufox bootstrap failed", payload["errorDescription"])

    def test_process_turnstile_returns_not_ready_when_pool_is_empty(self):
        server = self._create_server()

        with patch("services.turnstile_solver.api_solver.request", SimpleNamespace(args={})):
            with patch("services.turnstile_solver.api_solver.jsonify", side_effect=lambda payload: payload):
                payload, status = asyncio.run(server.process_turnstile())

        self.assertEqual(status, 200)
        self.assertEqual(payload["errorCode"], "ERROR_SOLVER_NOT_READY")

    def test_process_turnstile_keeps_missing_param_validation_after_ready(self):
        server = self._create_server()
        server.browser_pool.put_nowait((1, object(), {}))

        with patch("services.turnstile_solver.api_solver.request", SimpleNamespace(args={})):
            with patch("services.turnstile_solver.api_solver.jsonify", side_effect=lambda payload: payload):
                payload, status = asyncio.run(server.process_turnstile())

        self.assertEqual(status, 200)
        self.assertEqual(payload["errorCode"], "ERROR_WRONG_PAGEURL")

    def test_parse_args_reads_solver_thread_count_from_env(self):
        with patch.dict("os.environ", {"SOLVER_THREAD_COUNT": "3"}, clear=False):
            with patch("sys.argv", ["api_solver.py"]):
                args = parse_args()

        self.assertEqual(args.thread, 3)


if __name__ == "__main__":
    unittest.main()
