"""Playwright 执行器 - 支持 headless/headed 模式"""

import logging
from typing import Any

from ..base_executor import BaseExecutor, Response
from ..browser_runtime import ensure_browser_display_available, resolve_browser_headless
from ..proxy_utils import build_playwright_proxy_config


logger = logging.getLogger(__name__)


class PlaywrightExecutor(BaseExecutor):
    def __init__(self, proxy: str | None = None, headless: bool = True):
        super().__init__(proxy or "")
        self.headless = headless
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._init()

    def _init(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        headless, reason = resolve_browser_headless(self.headless)
        ensure_browser_display_available(headless)
        logger.info(
            "PlaywrightExecutor 浏览器模式: %s (%s)",
            "headless" if headless else "headed",
            reason,
        )

        launch_opts: dict[str, Any] = {"headless": headless}
        if self.proxy:
            proxy_cfg = build_playwright_proxy_config(self.proxy)
            if proxy_cfg:
                launch_opts["proxy"] = proxy_cfg
        self._browser = self._pw.chromium.launch(**launch_opts)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()

    def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError("Playwright page 未初始化")
        return self._page

    def _require_context(self) -> Any:
        if self._context is None:
            raise RuntimeError("Playwright context 未初始化")
        return self._context

    @property
    def page(self) -> Any:
        """兼容平台插件直接访问 executor.page 的用法。"""
        return self._require_page()

    @property
    def context(self) -> Any:
        """兼容平台插件直接访问 executor.context 的用法。"""
        return self._require_context()

    def get(self, url, *, headers=None, params=None) -> Response:
        import urllib.parse

        page = self._require_page()
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        if headers:
            page.set_extra_http_headers(headers)
        resp = page.goto(url)
        if resp is None:
            raise RuntimeError(f"Playwright 导航失败: {url}")
        return Response(
            status_code=resp.status,
            text=page.content(),
            headers=dict(resp.headers),
            cookies=self.get_cookies(),
        )

    def post(self, url, *, headers=None, params=None, data=None, json=None) -> Response:
        import json as _json
        import urllib.parse

        page = self._require_page()
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        post_data = None
        content_type = "application/x-www-form-urlencoded"
        if json is not None:
            post_data = _json.dumps(json)
            content_type = "application/json"
        elif data:
            post_data = urllib.parse.urlencode(data)
        h = {"Content-Type": content_type}
        if headers:
            h.update(headers)
        resp = page.request.post(url, headers=h, data=post_data)
        return Response(
            status_code=resp.status,
            text=resp.text(),
            headers=dict(resp.headers),
            cookies=self.get_cookies(),
        )

    def get_cookies(self) -> dict:
        context = self._require_context()
        return {c["name"]: c["value"] for c in context.cookies()}

    def set_cookies(self, cookies: dict, domain: str = ".example.com") -> None:
        context = self._require_context()
        page = self._require_page()
        page_url = page.url
        if page_url and page_url.startswith("http"):
            context.add_cookies(
                [{"name": k, "value": v, "url": page_url} for k, v in cookies.items()]
            )
        else:
            context.add_cookies(
                [
                    {"name": k, "value": v, "domain": domain, "path": "/"}
                    for k, v in cookies.items()
                ]
            )

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
