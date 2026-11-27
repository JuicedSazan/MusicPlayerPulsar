"""Dedicated Playwright worker for a persistent local VK browser session.

The worker records only request metadata and response structure. It never
stores or returns request values, cookies, authorization headers or audio URLs.
"""
import queue
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


class VKBrowserSession:
    def __init__(self, profile_dir):
        self.profile_dir = Path(profile_dir)
        self.commands = queue.Queue()
        self.ready = threading.Event()
        self.start_error = None
        self.thread = threading.Thread(target=self._run, name="pulsar-vk-browser", daemon=True)
        self.thread.start()
        if not self.ready.wait(30):
            raise RuntimeError("Тайм-аут запуска локального браузера VK.")
        if self.start_error:
            raise RuntimeError(self.start_error)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                try:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        channel="chrome",
                        headless=False,
                        args=["--new-window"],
                    )
                except Exception:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        headless=False,
                    )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=60000)
                self.context = context
                self.page = page
                self.ready.set()
                while True:
                    command, result_queue = self.commands.get()
                    try:
                        if command == "cookies":
                            result_queue.put(context.cookies(["https://vk.ru/", "https://login.vk.ru/"]))
                        elif command == "user_agent":
                            result_queue.put(page.evaluate("() => navigator.userAgent"))
                        elif command == "open":
                            if self.page.is_closed():
                                self.page = context.new_page()
                            self.page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=60000)
                            result_queue.put(True)
                        elif command == "inspect_audio":
                            result_queue.put(self._inspect_audio_requests())
                    except Exception as exc:
                        result_queue.put(exc)
        except Exception as exc:
            self.start_error = str(exc)
            self.ready.set()

    @staticmethod
    def _request_metadata(request):
        parsed = urlsplit(request.url)
        query_keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        post_keys = []
        try:
            post_data = request.post_data or ""
            if post_data:
                post_keys = sorted({key for key, _ in parse_qsl(post_data, keep_blank_values=True)})
        except Exception:
            pass
        return {
            "method": request.method,
            "host": parsed.hostname or "",
            "path": parsed.path or "/",
            "resource_type": request.resource_type,
            "query_keys": query_keys,
            "post_field_names": post_keys,
            "status": None,
            "content_type": "",
            "response_shape": None,
        }

    @staticmethod
    def _response_shape(response):
        try:
            payload = response.json()
        except Exception:
            return None
        shape = {"type": type(payload).__name__}
        if isinstance(payload, dict):
            shape["top_level_keys"] = sorted(str(key) for key in payload.keys())[:60]
            for key, value in payload.items():
                if isinstance(value, list):
                    shape.setdefault("arrays", {})[str(key)] = {
                        "count": len(value),
                        "first_item_keys": sorted(str(k) for k in value[0].keys())[:40]
                        if value and isinstance(value[0], dict) else [],
                    }
                elif isinstance(value, dict):
                    shape.setdefault("objects", {})[str(key)] = sorted(str(k) for k in value.keys())[:40]
        elif isinstance(payload, list):
            shape["count"] = len(payload)
            shape["first_item_keys"] = sorted(str(k) for k in payload[0].keys())[:40] if payload and isinstance(payload[0], dict) else []
        return shape

    def _inspect_audio_requests(self):
        captured = []
        by_request = {}

        def on_request(request):
            parsed = urlsplit(request.url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
            if not (host.endswith("vk.ru") or host.endswith("vk.com")):
                return
            if not any(term in path for term in ("audio", "al_audio", "catalog", "api")):
                return
            item = self._request_metadata(request)
            key = (item["method"], item["host"], item["path"], tuple(item["query_keys"]), tuple(item["post_field_names"]))
            if key not in by_request and len(captured) < 60:
                by_request[key] = item
                captured.append(item)

        def on_response(response):
            request = response.request
            parsed = urlsplit(request.url)
            item = self._request_metadata(request)
            key = (item["method"], item["host"], item["path"], tuple(item["query_keys"]), tuple(item["post_field_names"]))
            existing = by_request.get(key)
            if existing is None:
                return
            existing["status"] = response.status
            try:
                existing["content_type"] = response.headers.get("content-type", "")[:100]
            except Exception:
                pass
            if "json" in existing["content_type"].lower() or "javascript" in existing["content_type"].lower():
                existing["response_shape"] = self._response_shape(response)

        page = self.page
        page.on("request", on_request)
        page.on("response", on_response)
        navigation_error = None
        try:
            if page.is_closed():
                page = self.context.new_page()
                self.page = page
                page.on("request", on_request)
                page.on("response", on_response)
            page.goto("https://vk.ru/audio", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(12000)
        except Exception as exc:
            navigation_error = str(exc)[:220]
        finally:
            try:
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)
            except Exception:
                pass

        try:
            cookie_names = {cookie.get("name") for cookie in self.context.cookies(["https://vk.ru/"])}
            logged_in = "remixsid" in cookie_names
        except Exception:
            logged_in = False
        return {
            "ok": True,
            "page": "https://vk.ru/audio",
            "logged_in_session_detected": logged_in,
            "request_count": len(captured),
            "requests": captured,
            "navigation_error": navigation_error,
            "secrets_included": False,
        }

    def _call(self, command):
        result_queue = queue.Queue(maxsize=1)
        self.commands.put((command, result_queue))
        result = result_queue.get(timeout=90 if command == "inspect_audio" else 30)
        if isinstance(result, Exception):
            raise result
        return result

    def open(self):
        return self._call("open")

    def cookies(self):
        return self._call("cookies")

    def user_agent(self):
        return self._call("user_agent")

    def inspect_audio(self):
        return self._call("inspect_audio")
