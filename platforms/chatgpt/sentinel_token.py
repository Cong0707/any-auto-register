"""Sentinel Token 生成器模块（Hybrid: Browser SDK + Python PoW）。"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import threading
import time
import uuid
from typing import Callable, Optional


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
DEFAULT_SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"

_SENTINEL_SDK_CACHE_TTL = 600.0
_SENTINEL_SDK_CACHE = {"url": "", "expires_at": 0.0}
_SENTINEL_SDK_CACHE_LOCK = threading.Lock()

_SENTINEL_TOKEN_CACHE_TTL = 45.0
_SENTINEL_TOKEN_CACHE: dict[tuple[str, str, str], dict[str, object]] = {}
_SENTINEL_TOKEN_CACHE_LOCK = threading.Lock()

_SENTINEL_SOURCE_EWMA: dict[tuple[str, str, str], float] = {}
_SENTINEL_SOURCE_EWMA_LOCK = threading.Lock()
_SENTINEL_EWMA_ALPHA = 0.25

_DEFAULT_STRICT_T_FLOWS = {"oauth_create_account"}


def _extract_sdk_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    full = re.search(r"https://sentinel\.openai\.com/sentinel/[a-z0-9]+/sdk\.js", text, re.I)
    if full:
        return full.group(0)

    relative = re.search(r"/sentinel/[a-z0-9]+/sdk\.js", text, re.I)
    if relative:
        return f"https://sentinel.openai.com{relative.group(0)}"
    return ""


def _normalize_flow(flow: str | None) -> str:
    value = str(flow or "authorize_continue").strip().lower()
    return value or "authorize_continue"


def _normalize_proxy(proxy: str | None) -> str:
    return str(proxy or "").strip().lower()


def _token_cache_key(device_id: str, flow: str, proxy: str | None) -> tuple[str, str, str]:
    return (str(device_id or "").strip(), _normalize_flow(flow), _normalize_proxy(proxy))


def _get_token_cache_ttl() -> float:
    raw = str(os.getenv("SENTINEL_TOKEN_CACHE_TTL_SECONDS", "") or "").strip()
    if not raw:
        return _SENTINEL_TOKEN_CACHE_TTL
    try:
        return max(1.0, min(float(raw), 300.0))
    except Exception:
        return _SENTINEL_TOKEN_CACHE_TTL


def _load_cached_token(device_id: str, flow: str, proxy: str | None) -> str:
    key = _token_cache_key(device_id, flow, proxy)
    now = time.time()
    with _SENTINEL_TOKEN_CACHE_LOCK:
        item = _SENTINEL_TOKEN_CACHE.get(key) or {}
        token = str(item.get("token") or "").strip()
        expires_at = float(item.get("expires_at") or 0.0)
        if token and expires_at > now:
            return token
        if item:
            _SENTINEL_TOKEN_CACHE.pop(key, None)
    return ""


def _save_cached_token(
    device_id: str,
    flow: str,
    proxy: str | None,
    token: str,
    *,
    score: float = 0.0,
    source: str = "",
) -> None:
    key = _token_cache_key(device_id, flow, proxy)
    with _SENTINEL_TOKEN_CACHE_LOCK:
        _SENTINEL_TOKEN_CACHE[key] = {
            "token": str(token or "").strip(),
            "score": float(score or 0.0),
            "source": str(source or "").strip(),
            "expires_at": time.time() + _get_token_cache_ttl(),
        }


def _source_key(flow: str, proxy: str | None, source: str) -> tuple[str, str, str]:
    return (_normalize_flow(flow), _normalize_proxy(proxy), str(source or "").strip().lower())


def _source_ewma(flow: str, proxy: str | None, source: str) -> float:
    key = _source_key(flow, proxy, source)
    with _SENTINEL_SOURCE_EWMA_LOCK:
        return float(_SENTINEL_SOURCE_EWMA.get(key, 0.5))


def _update_source_ewma(flow: str, proxy: str | None, source: str, success: bool) -> None:
    key = _source_key(flow, proxy, source)
    target = 1.0 if bool(success) else 0.0
    with _SENTINEL_SOURCE_EWMA_LOCK:
        base = float(_SENTINEL_SOURCE_EWMA.get(key, 0.5))
        _SENTINEL_SOURCE_EWMA[key] = base * (1.0 - _SENTINEL_EWMA_ALPHA) + target * _SENTINEL_EWMA_ALPHA


def _choose_source_order(flow: str, proxy: str | None, prefer_browser: bool) -> list[str]:
    forced = str(os.getenv("SENTINEL_FORCE_SOURCE", "") or "").strip().lower()
    if forced in {"browser", "python"}:
        return [forced, "python" if forced == "browser" else "browser"]

    browser_bias = 0.62 if prefer_browser else 0.45
    python_bias = 0.56 if not prefer_browser else 0.45

    browser_score = browser_bias + _source_ewma(flow, proxy, "browser") * 0.75
    python_score = python_bias + _source_ewma(flow, proxy, "python") * 0.75

    if browser_score >= python_score:
        return ["browser", "python"]
    return ["python", "browser"]


def _parse_token(token: str) -> dict:
    raw = str(token or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _strict_t_flows() -> set[str]:
    raw = str(os.getenv("SENTINEL_STRICT_T_FLOWS", "") or "").strip()
    if not raw:
        return set(_DEFAULT_STRICT_T_FLOWS)
    lowered = raw.lower()
    if lowered in {"0", "false", "no", "off", "none"}:
        return set()
    return {
        item.strip().lower()
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    }


def _evaluate_token_quality(
    token: str,
    *,
    expected_flow: str,
    expected_device_id: str,
    source: str,
) -> tuple[bool, float, str]:
    payload = _parse_token(token)
    if not payload:
        return False, 0.0, "invalid_json"

    p_value = str(payload.get("p") or "").strip()
    c_value = str(payload.get("c") or "").strip()
    t_value = str(payload.get("t") or "").strip()
    flow_value = _normalize_flow(str(payload.get("flow") or ""))
    id_value = str(payload.get("id") or "").strip()

    if not p_value:
        return False, 0.0, "missing_p"
    if not c_value:
        return False, 0.0, "missing_c"
    if not id_value:
        return False, 0.0, "missing_id"
    if not flow_value:
        return False, 0.0, "missing_flow"

    expected_flow_norm = _normalize_flow(expected_flow)
    expected_device = str(expected_device_id or "").strip()

    if expected_flow_norm and flow_value != expected_flow_norm:
        return False, 0.0, "flow_mismatch"
    if expected_device and id_value != expected_device:
        return False, 0.0, "device_mismatch"

    strict_t_required = expected_flow_norm in _strict_t_flows()
    if strict_t_required and str(source or "").strip().lower() != "python" and not t_value:
        return False, 0.0, "strict_t_required"

    score = 0.0
    if p_value:
        score += 0.35
        if p_value.startswith("gAAAAA"):
            score += 0.10
    if c_value:
        score += 0.25
    if id_value:
        score += 0.15
    if flow_value:
        score += 0.15
    if t_value:
        score += 0.10

    score = max(0.0, min(score, 1.0))
    return (score >= 0.65), score, ("ok" if score >= 0.65 else "low_score")


def resolve_sentinel_sdk_url(
    session=None,
    *,
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
    timeout: int = 15,
) -> str:
    override = str(os.getenv("SENTINEL_SDK_URL", "") or "").strip()
    if override:
        return override

    now = time.time()
    with _SENTINEL_SDK_CACHE_LOCK:
        cached_url = str(_SENTINEL_SDK_CACHE.get("url") or "").strip()
        cached_expire = float(_SENTINEL_SDK_CACHE.get("expires_at") or 0.0)
        if cached_url and cached_expire > now:
            return cached_url

    disabled = str(os.getenv("SENTINEL_SDK_RESOLVE_DISABLE", "") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"} or session is None:
        return DEFAULT_SENTINEL_SDK_URL

    headers = {
        "Accept": "text/html,*/*;q=0.8",
        "Referer": SENTINEL_REFERER,
        "User-Agent": user_agent or "Mozilla/5.0",
        "sec-ch-ua": sec_ch_ua
        or '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    kwargs = {"headers": headers, "timeout": max(int(timeout or 0), 5)}
    if impersonate:
        kwargs["impersonate"] = impersonate

    resolved = ""
    try:
        response = session.get(SENTINEL_REFERER, **kwargs)
        if int(getattr(response, "status_code", 0) or 0) < 500:
            resolved = _extract_sdk_url(getattr(response, "text", ""))
            if not resolved:
                resolved = _extract_sdk_url(str(getattr(response, "url", "") or ""))
    except Exception:
        resolved = ""

    if not resolved:
        resolved = DEFAULT_SENTINEL_SDK_URL

    with _SENTINEL_SDK_CACHE_LOCK:
        _SENTINEL_SDK_CACHE["url"] = resolved
        _SENTINEL_SDK_CACHE["expires_at"] = now + _SENTINEL_SDK_CACHE_TTL

    return resolved


class SentinelTokenGenerator:
    """Sentinel Token 纯 Python 生成器。"""

    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id=None, user_agent=None, sdk_url=None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        self.sdk_url = str(sdk_url or DEFAULT_SENTINEL_SDK_URL).strip() or DEFAULT_SENTINEL_SDK_URL
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text):
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice(
            [
                "vendorSub",
                "productSub",
                "vendor",
                "maxTouchPoints",
                "scheduling",
                "userActivation",
                "doNotTrack",
                "geolocation",
                "connection",
                "plugins",
                "mimeTypes",
                "pdfViewerEnabled",
                "webkitTemporaryStorage",
                "webkitPersistentStorage",
                "hardwareConcurrency",
                "cookieEnabled",
                "credentials",
                "mediaDevices",
                "permissions",
                "locks",
                "ink",
            ]
        )
        return [
            "1920x1080",
            date_str,
            4294705152,
            random.random(),
            self.user_agent,
            self.sdk_url,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            f"{nav_prop}−undefined",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data):
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        encoded = self._base64_encode(config)
        digest = self._fnv1a_32(seed + encoded)
        if digest[: len(difficulty)] <= difficulty:
            return encoded + "~S"
        return None

    def generate_token(self, seed=None, difficulty=None):
        seed = seed or self.requirements_seed
        difficulty = difficulty or "0"
        start_time = time.time()
        config = self._get_config()
        for nonce in range(self.MAX_ATTEMPTS):
            value = self._run_check(start_time, seed, difficulty, config, nonce)
            if value:
                return "gAAAAAB" + value
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self):
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def fetch_sentinel_challenge(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    request_p=None,
):
    flow = _normalize_flow(flow)
    sdk_url = resolve_sentinel_sdk_url(
        session,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        sdk_url=sdk_url,
    )

    req_body = {
        "p": str(request_p or "").strip() or generator.generate_requirements_token(),
        "id": str(device_id or "").strip(),
        "flow": flow,
    }
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": SENTINEL_REFERER,
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or "Mozilla/5.0",
        "sec-ch-ua": sec_ch_ua
        or '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    kwargs = {"data": json.dumps(req_body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate

    try:
        response = session.post(SENTINEL_REQ_URL, **kwargs)
        if int(getattr(response, "status_code", 0) or 0) == 200:
            payload = response.json()
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None
    return None


def _build_sentinel_token_python(
    session,
    device_id,
    *,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
):
    flow = _normalize_flow(flow)
    sdk_url = resolve_sentinel_sdk_url(
        session,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
    challenge = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
    if not challenge:
        return None

    c_value = str(challenge.get("token") or "").strip()
    if not c_value:
        return None

    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        sdk_url=sdk_url,
    )
    pow_data = challenge.get("proofofwork") if isinstance(challenge.get("proofofwork"), dict) else {}
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(
            seed=pow_data.get("seed"),
            difficulty=pow_data.get("difficulty", "0"),
        )
    else:
        p_value = generator.generate_requirements_token()

    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": c_value,
            "id": str(device_id or "").strip(),
            "flow": flow,
        },
        separators=(",", ":"),
    )


def _build_sentinel_token_browser(
    *,
    flow: str,
    proxy: str | None = None,
    page_url: str | None = None,
    headless: bool = True,
    device_id: str,
    user_agent: str | None = None,
    timeout_ms: int = 45000,
    log_fn: Callable[[str], None] | None = None,
) -> Optional[str]:
    try:
        from .sentinel_browser import get_sentinel_token_via_browser
    except Exception:
        return None

    return get_sentinel_token_via_browser(
        flow=_normalize_flow(flow),
        proxy=proxy,
        timeout_ms=max(int(timeout_ms or 0), 1000),
        page_url=page_url,
        headless=headless,
        device_id=str(device_id or "").strip(),
        user_agent=user_agent,
        log_fn=log_fn,
    )


def build_sentinel_token(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    *,
    proxy: str | None = None,
    page_url: str | None = None,
    headless: bool = True,
    prefer_browser: bool = True,
    use_cache: bool = True,
    log_fn: Callable[[str], None] | None = None,
    timeout_ms: int = 45000,
):
    """Hybrid Sentinel token 构造：Browser SDK + Python PoW。"""
    logger = log_fn or (lambda _msg: None)

    flow_norm = _normalize_flow(flow)
    device = str(device_id or "").strip()
    if not device:
        logger(f"Sentinel 跳过: 空 device_id, flow={flow_norm}")
        return None

    if use_cache:
        cached = _load_cached_token(device, flow_norm, proxy)
        if cached:
            ok, score, reason = _evaluate_token_quality(
                cached,
                expected_flow=flow_norm,
                expected_device_id=device,
                source="cache",
            )
            if ok:
                logger(f"Sentinel cache 命中: flow={flow_norm}, score={score:.2f}")
                return cached
            logger(f"Sentinel cache 丢弃: {reason}")

    order = _choose_source_order(flow_norm, proxy, prefer_browser=bool(prefer_browser))

    for source in order:
        token = None
        if source == "browser":
            token = _build_sentinel_token_browser(
                flow=flow_norm,
                proxy=proxy,
                page_url=page_url,
                headless=headless,
                device_id=device,
                user_agent=user_agent,
                timeout_ms=timeout_ms,
                log_fn=logger,
            )
        else:
            token = _build_sentinel_token_python(
                session,
                device,
                flow=flow_norm,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )

        ok, score, reason = _evaluate_token_quality(
            token or "",
            expected_flow=flow_norm,
            expected_device_id=device,
            source=source,
        )
        if (not ok) and source == "browser" and reason == "device_mismatch" and token:
            parsed = _parse_token(token)
            adopted_device = str(parsed.get("id") or "").strip()
            if adopted_device:
                adopt_ok, adopt_score, adopt_reason = _evaluate_token_quality(
                    token,
                    expected_flow=flow_norm,
                    expected_device_id=adopted_device,
                    source=source,
                )
                if adopt_ok:
                    logger(
                        f"Sentinel browser did 自适应成功: expected={device}, adopted={adopted_device}"
                    )
                    device = adopted_device
                    ok, score, reason = True, adopt_score, "device_adopted"
                else:
                    reason = f"device_mismatch:{adopt_reason}"
        _update_source_ewma(flow_norm, proxy, source, success=ok)

        if ok and token:
            logger(f"Sentinel {source} 成功: flow={flow_norm}, score={score:.2f}")
            if use_cache:
                _save_cached_token(device, flow_norm, proxy, token, score=score, source=source)
            return token

        logger(f"Sentinel {source} 失败: flow={flow_norm}, reason={reason}, score={score:.2f}")

    return None


def build_sentinel_token_vm_only(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
):
    """VM 分支专用构造器（保持兼容：仅 Python PoW）。"""
    return _build_sentinel_token_python(
        session,
        str(device_id or "").strip(),
        flow=_normalize_flow(flow),
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
