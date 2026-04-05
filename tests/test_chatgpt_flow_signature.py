import unittest

from platforms.chatgpt.flow_signature import FlowSignatureContext, HeaderSnapshotStore


class FlowSignatureTests(unittest.TestCase):
    def test_create_is_deterministic_when_seed_is_fixed(self):
        ctx1 = FlowSignatureContext.create(
            device_id="did-1",
            oauth_state="state-a",
            flow_id="flow-fixed",
            auth_session_logging_id="asl-fixed",
        )
        ctx2 = FlowSignatureContext.create(
            device_id="did-1",
            oauth_state="state-a",
            flow_id="flow-fixed",
            auth_session_logging_id="asl-fixed",
        )

        self.assertEqual(ctx1.trace_id, ctx2.trace_id)
        self.assertEqual(ctx1.auth_session_logging_id, "asl-fixed")

    def test_build_authorize_params_injects_tracking_fields(self):
        ctx = FlowSignatureContext.create(device_id="did-2", oauth_state="state-b")
        payload = ctx.build_authorize_params({"prompt": "login"})

        self.assertEqual(payload["prompt"], "login")
        self.assertEqual(payload["ext-oai-did"], "did-2")
        self.assertEqual(payload["auth_session_logging_id"], ctx.auth_session_logging_id)

    def test_datadog_headers_contains_required_keys(self):
        ctx = FlowSignatureContext.create(device_id="did-3", oauth_state="state-c")
        headers_a = ctx.datadog_headers(step="authorize", attempt=1)
        headers_b = ctx.datadog_headers(step="authorize", attempt=2)

        self.assertIn("traceparent", headers_a)
        self.assertIn("x-datadog-trace-id", headers_a)
        self.assertIn("x-datadog-parent-id", headers_a)
        self.assertNotEqual(headers_a["x-datadog-parent-id"], headers_b["x-datadog-parent-id"])


class HeaderSnapshotStoreTests(unittest.TestCase):
    def test_capture_once_and_replay_with_dynamic_overrides(self):
        store = HeaderSnapshotStore()
        first = store.capture("token_exchange", {"a": "1", "b": "2"})
        second = store.capture("token_exchange", {"a": "changed", "c": "3"})

        self.assertEqual(first, {"a": "1", "b": "2"})
        self.assertEqual(second, {"a": "1", "b": "2"})

        replay = store.get_for_replay("token_exchange", {"b": "9", "x": "7"})
        self.assertEqual(replay, {"a": "1", "b": "9", "x": "7"})


if __name__ == "__main__":
    unittest.main()
