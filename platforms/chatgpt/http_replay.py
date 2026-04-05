"""HTTP 步骤回放（replay）辅助。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Iterable


RETRYABLE_HTTP_STATUS = {
    408,
    409,
    423,
    425,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    524,
}


@dataclass
class ReplayResult:
    ok: bool
    response: object | None
    attempts: int
    error: str = ""


def is_retryable_http_status(status_code: int, extra: Iterable[int] | None = None) -> bool:
    if int(status_code or 0) in RETRYABLE_HTTP_STATUS:
        return True
    if extra is None:
        return False
    try:
        return int(status_code or 0) in {int(item) for item in extra}
    except Exception:
        return False


def run_http_step_with_replay(
    *,
    step: str,
    request_fn: Callable[[], object],
    is_success_fn: Callable[[object], bool],
    max_attempts: int = 3,
    retryable_statuses: Iterable[int] | None = None,
    base_backoff_seconds: float = 0.6,
    max_backoff_seconds: float = 4.0,
    log_fn: Callable[[str], None] | None = None,
) -> ReplayResult:
    logger = log_fn or (lambda _msg: None)
    attempts = max(1, min(int(max_attempts or 1), 8))

    last_response = None
    last_error = ""

    for idx in range(attempts):
        attempt = idx + 1
        try:
            response = request_fn()
            last_response = response
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                delay = min(max_backoff_seconds, base_backoff_seconds * (2**idx) + random.uniform(0.05, 0.25))
                logger(f"{step} 异常，{delay:.2f}s 后重试 ({attempt}/{attempts}): {last_error}")
                time.sleep(delay)
                continue
            return ReplayResult(ok=False, response=None, attempts=attempt, error=last_error)

        if is_success_fn(response):
            return ReplayResult(ok=True, response=response, attempts=attempt)

        status = int(getattr(response, "status_code", 0) or 0)
        text = str(getattr(response, "text", "") or "")
        last_error = f"HTTP {status}: {text[:180]}"
        if attempt < attempts and is_retryable_http_status(status, extra=retryable_statuses):
            delay = min(max_backoff_seconds, base_backoff_seconds * (2**idx) + random.uniform(0.05, 0.25))
            logger(f"{step} 命中可重放状态 {status}，{delay:.2f}s 后重试 ({attempt}/{attempts})")
            time.sleep(delay)
            continue

        return ReplayResult(ok=False, response=response, attempts=attempt, error=last_error)

    return ReplayResult(ok=False, response=last_response, attempts=attempts, error=last_error)
