"""OAuth 流签名上下文与头部快照工具。"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass


def _u64_decimal(seed: str) -> str:
    raw = hashlib.sha256(seed.encode("utf-8")).digest()
    value = int.from_bytes(raw[:8], byteorder="big", signed=False)
    value = value or 1
    return str(value)


def _norm(value: str) -> str:
    text = str(value or "").strip()
    return text or "-"


@dataclass(frozen=True)
class FlowSignatureContext:
    flow_id: str
    device_id: str
    oauth_state: str
    auth_session_logging_id: str
    trace_id: str

    @classmethod
    def create(
        cls,
        *,
        device_id: str,
        oauth_state: str = "",
        flow_id: str | None = None,
        auth_session_logging_id: str | None = None,
    ) -> "FlowSignatureContext":
        flow_id = str(flow_id or uuid.uuid4())
        auth_session_logging_id = str(auth_session_logging_id or uuid.uuid4())
        trace_seed = "|".join(
            [
                _norm(flow_id),
                _norm(device_id),
                _norm(oauth_state),
                _norm(auth_session_logging_id),
            ]
        )
        trace_id = _u64_decimal(trace_seed)
        return cls(
            flow_id=flow_id,
            device_id=str(device_id or "").strip(),
            oauth_state=str(oauth_state or "").strip(),
            auth_session_logging_id=auth_session_logging_id,
            trace_id=trace_id,
        )

    def build_authorize_params(self, base_params: dict | None = None) -> dict:
        params = dict(base_params or {})
        if self.device_id:
            params["ext-oai-did"] = self.device_id
        params["auth_session_logging_id"] = self.auth_session_logging_id
        return params

    def datadog_headers(self, *, step: str, attempt: int = 1) -> dict[str, str]:
        step_key = _norm(step)
        attempt_num = max(1, int(attempt or 1))
        # 同步时间片参与 parent_id 派生，避免同一步骤并发完全一致。
        tick = int(time.monotonic_ns() // 1_000_000)
        parent_seed = "|".join([
            self.trace_id,
            step_key,
            str(attempt_num),
            str(tick),
        ])
        parent_id = _u64_decimal(parent_seed)
        trace_hex = format(int(self.trace_id), "016x")
        parent_hex = format(int(parent_id), "016x")
        return {
            "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-parent-id": parent_id,
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": self.trace_id,
        }


class HeaderSnapshotStore:
    """按步骤缓存首包头部，重放时只覆盖动态头。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, str]] = {}

    def capture(self, step: str, headers: dict) -> dict[str, str]:
        key = _norm(step)
        normalized = {str(k): str(v) for k, v in dict(headers or {}).items() if v is not None}
        if key not in self._snapshots:
            self._snapshots[key] = dict(normalized)
        return dict(self._snapshots[key])

    def get_for_replay(self, step: str, dynamic_overrides: dict | None = None) -> dict[str, str]:
        key = _norm(step)
        base = dict(self._snapshots.get(key) or {})
        if dynamic_overrides:
            for k, v in dynamic_overrides.items():
                if v is None:
                    continue
                base[str(k)] = str(v)
        return base

    def clear(self) -> None:
        self._snapshots.clear()
