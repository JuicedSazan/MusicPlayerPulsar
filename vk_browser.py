"""Dedicated Playwright worker for a persistent local VK browser session."""
import queue
import threading
from pathlib import Path


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
                    except Exception as exc:
                        result_queue.put(exc)
        except Exception as exc:
            self.start_error = str(exc)
            self.ready.set()

    def _call(self, command):
        result_queue = queue.Queue(maxsize=1)
        self.commands.put((command, result_queue))
        result = result_queue.get(timeout=30)
        if isinstance(result, Exception):
            raise result
        return result

    def open(self):
        return self._call("open")

    def cookies(self):
        return self._call("cookies")

    def user_agent(self):
        return self._call("user_agent")
