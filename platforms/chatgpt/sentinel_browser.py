"""Playwright 版 Sentinel SDK token 获取辅助。"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.proxy_utils import build_playwright_proxy_config


def _flow_page_url(flow: str) -> str:
    flow_name = str(flow or "").strip().lower()
    mapping = {
        "authorize_continue": "https://auth.openai.com/create-account",
        "username_password_create": "https://auth.openai.com/create-account/password",
        "password_verify": "https://auth.openai.com/log-in/password",
        "email_otp_validate": "https://auth.openai.com/email-verification",
        "oauth_create_account": "https://auth.openai.com/about-you",
    }
    return mapping.get(flow_name, "https://auth.openai.com/about-you")


def _apply_env_overrides(
    *,
    flow: str,
    proxy: Optional[str],
    timeout_ms: int,
    page_url: Optional[str],
    user_agent: Optional[str],
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[str, Optional[str], int, str, Optional[str]]:
    logger = log_fn or (lambda _msg: None)

    env_flow = str(os.getenv("SENTINEL_BROWSER_FLOW", "") or "").strip()
    if env_flow:
        flow = env_flow

    env_proxy = str(os.getenv("SENTINEL_BROWSER_PROXY", "") or "").strip()
    if env_proxy:
        proxy = env_proxy

    env_page_url = str(os.getenv("SENTINEL_BROWSER_PAGE_URL", "") or "").strip()
    target_url = env_page_url or str(page_url or _flow_page_url(flow)).strip() or _flow_page_url(flow)

    env_timeout_ms = str(os.getenv("SENTINEL_BROWSER_TIMEOUT_MS", "") or "").strip()
    if env_timeout_ms:
        try:
            parsed_timeout = int(env_timeout_ms)
            if parsed_timeout > 0:
                timeout_ms = parsed_timeout
        except ValueError:
            logger(f"SENTINEL_BROWSER_TIMEOUT_MS 非法，忽略: {env_timeout_ms}")

    env_ua = str(os.getenv("SENTINEL_BROWSER_UA", "") or "").strip()
    if env_ua:
        user_agent = env_ua

    return flow, proxy, timeout_ms, target_url, user_agent


def _extract_token_device_id(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("id") or "").strip()


def _seed_openai_device_cookie(context, device_id: str, logger: Callable[[str], None]) -> None:
    did = str(device_id or "").strip()
    if not did:
        return
    cookie_payloads = [
        {
            "name": "oai-did",
            "value": did,
            "url": "https://auth.openai.com/",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "oai-did",
            "value": did,
            "url": "https://sentinel.openai.com/",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "oai-did",
            "value": did,
            "domain": ".openai.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        },
    ]
    for cookie in cookie_payloads:
        try:
            context.add_cookies([cookie])
        except Exception:
            continue
    logger(f"Sentinel Browser 预置 did cookie: {did}")


def _prime_page_device_id(page, device_id: str, logger: Callable[[str], None]) -> None:
    did = str(device_id or "").strip()
    if not did:
        return
    try:
        page.evaluate(
            """
            ({ did }) => {
                const value = encodeURIComponent(did);
                const directives = [
                    "path=/",
                    "domain=.openai.com",
                    "secure",
                    "samesite=lax",
                ].join("; ");
                document.cookie = `oai-did=${value}; ${directives}`;
                try { localStorage.setItem("oai-did", did); } catch (_) {}
                try { sessionStorage.setItem("oai-did", did); } catch (_) {}
                return {
                    cookie: document.cookie || "",
                    ls: (() => { try { return localStorage.getItem("oai-did") || ""; } catch (_) { return ""; } })(),
                    ss: (() => { try { return sessionStorage.getItem("oai-did") || ""; } catch (_) { return ""; } })(),
                };
            }
            """,
            {"did": did},
        )
        logger(f"Sentinel Browser 页面 did 已对齐: {did}")
    except Exception as exc:
        logger(f"Sentinel Browser 页面 did 注入失败: {exc}")


def _request_sentinel_token(page, flow: str) -> dict:
    return page.evaluate(
        """
        async ({ flow }) => {
            try {
                const token = await window.SentinelSDK.token(flow);
                let tokenId = "";
                try {
                    const parsed = JSON.parse(token || "{}");
                    tokenId = parsed && parsed.id ? String(parsed.id) : "";
                } catch (_) {}
                return { success: true, token, token_id: tokenId };
            } catch (e) {
                return {
                    success: false,
                    error: (e && (e.message || String(e))) || "unknown",
                };
            }
        }
        """,
        {"flow": flow},
    )


def get_sentinel_token_via_browser(
    *,
    flow: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 45000,
    page_url: Optional[str] = None,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """通过浏览器直接调用 SentinelSDK.token(flow) 获取完整 token。"""
    logger = log_fn or (lambda _msg: None)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger(f"Sentinel Browser 不可用: {e}")
        return None

    flow, proxy, timeout_ms, target_url, user_agent = _apply_env_overrides(
        flow=flow,
        proxy=proxy,
        timeout_ms=timeout_ms,
        page_url=page_url,
        user_agent=user_agent,
        log_fn=logger,
    )

    effective_user_agent = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.7103.92 Safari/537.36"
    )

    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    logger(
        f"Sentinel Browser 模式: {'headless' if effective_headless else 'headed'} ({reason})"
    )

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    proxy_config = build_playwright_proxy_config(proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    logger(
        "Sentinel Browser 启动: "
        f"flow={flow}, url={target_url}, timeout_ms={timeout_ms}, "
        f"proxy={'on' if proxy else 'off'}"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        try:
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=effective_user_agent,
                ignore_https_errors=True,
            )
            if device_id:
                _seed_openai_device_cookie(context, str(device_id or "").strip(), logger)

            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
            if device_id:
                _prime_page_device_id(page, str(device_id or "").strip(), logger)
            page.wait_for_function(
                "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
                timeout=min(timeout_ms, 15000),
            )

            result = _request_sentinel_token(page, flow)

            if not result or not result.get("success") or not result.get("token"):
                logger(
                    "Sentinel Browser 获取失败: "
                    + str((result or {}).get("error") or "no result")
                )
                return None

            token = str(result["token"] or "").strip()
            if not token:
                logger("Sentinel Browser 返回空 token")
                return None

            expected_did = str(device_id or "").strip()
            token_did = _extract_token_device_id(token)
            if expected_did and token_did and token_did != expected_did:
                logger(
                    f"Sentinel Browser did 不一致，重试对齐: expected={expected_did}, token={token_did}"
                )
                _seed_openai_device_cookie(context, expected_did, logger)
                _prime_page_device_id(page, expected_did, logger)
                retry_result = _request_sentinel_token(page, flow)
                if retry_result and retry_result.get("success") and retry_result.get("token"):
                    retry_token = str(retry_result.get("token") or "").strip()
                    retry_did = _extract_token_device_id(retry_token)
                    if retry_token:
                        token = retry_token
                        token_did = retry_did or token_did
                        if retry_did == expected_did:
                            logger("Sentinel Browser did 对齐成功")
                        else:
                            logger(
                                "Sentinel Browser did 二次对齐仍未匹配，保留最新 token"
                            )

            try:
                parsed = json.loads(token)
                logger(
                    "Sentinel Browser 成功: "
                    f"p={'✓' if parsed.get('p') else '✗'} "
                    f"t={'✓' if parsed.get('t') else '✗'} "
                    f"c={'✓' if parsed.get('c') else '✗'} "
                    f"id={'✓' if parsed.get('id') else '✗'}"
                )
            except Exception:
                logger(f"Sentinel Browser 成功: len={len(token)}")

            return token
        except Exception as e:
            logger(f"Sentinel Browser 异常: {e}")
            return None
        finally:
            browser.close()
