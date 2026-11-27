"""Per-Pulsar-session headless VK browser with screenshot/input control.

The browser profile and cookies stay server-side and are never exposed to the
client. This is a prototype: profiles live only as long as the Render instance.
"""
import base64
import queue
import threading


_BROWSER_SLOT = threading.BoundedSemaphore(1)


class VKRemoteBrowser:
    WIDTH = 1280
    HEIGHT = 800

    def __init__(self):
        if not _BROWSER_SLOT.acquire(blocking=False):
            raise RuntimeError("На бесплатном сервере доступно одно окно авторизации VK одновременно. Попробуй позже.")
        self._slot_released = False
        self.commands = queue.Queue()
        self.ready = threading.Event()
        self.start_error = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="pulsar-vk-remote", daemon=True)
        self.thread.start()
        if not self.ready.wait(75):
            self._release_slot()
            raise RuntimeError("Тайм-аут запуска удалённого браузера VK.")
        if self.start_error:
            self._release_slot()
            raise RuntimeError(self.start_error)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                # Explicit channel uses the full Chromium binary in headless mode,
                # not Playwright's separate chromium_headless_shell executable.
                browser = p.chromium.launch(
                    channel="chromium",
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                          "--no-zygote", "--disable-background-networking"],
                )
                context = browser.new_context(
                    viewport={"width": self.WIDTH, "height": self.HEIGHT},
                    device_scale_factor=1,
                    locale="ru-RU",
                )
                page = context.new_page()
                page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=60000)
                self.browser, self.context, self.page = browser, context, page
                self.ready.set()
                while True:
                    command, args, result_queue = self.commands.get()
                    try:
                        if command == "screenshot":
                            data = page.screenshot(type="jpeg", quality=65, timeout=15000)
                            result_queue.put(base64.b64encode(data).decode("ascii"))
                        elif command == "click":
                            page.mouse.click(max(0, min(self.WIDTH, args["x"])),
                                             max(0, min(self.HEIGHT, args["y"])))
                            result_queue.put(True)
                        elif command == "type":
                            page.keyboard.insert_text(args["text"][:2000])
                            result_queue.put(True)
                        elif command == "press":
                            page.keyboard.press(args["key"][:30])
                            result_queue.put(True)
                        elif command == "scroll":
                            page.mouse.wheel(0, max(-900, min(900, args["delta"])))
                            result_queue.put(True)
                        elif command == "cookies":
                            result_queue.put(context.cookies(["https://vk.ru/", "https://login.vk.ru/"]))
                        elif command == "user_agent":
                            result_queue.put(page.evaluate("() => navigator.userAgent"))
                        elif command == "close":
                            context.close()
                            browser.close()
                            self.closed = True
                            result_queue.put(True)
                            break
                    except Exception as exc:
                        result_queue.put(exc)
        except Exception as exc:
            self.start_error = str(exc)[:300]
            self.ready.set()

    def _release_slot(self):
        if not self._slot_released:
            self._slot_released = True
            _BROWSER_SLOT.release()

    def _call(self, command, args=None, timeout=25):
        if self.closed:
            raise RuntimeError("Окно авторизации VK уже закрыто.")
        result_queue = queue.Queue(maxsize=1)
        self.commands.put((command, args or {}, result_queue))
        result = result_queue.get(timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result

    def screenshot(self):
        return self._call("screenshot", timeout=30)

    def click(self, x, y):
        return self._call("click", {"x": int(x), "y": int(y)})

    def type_text(self, value):
        return self._call("type", {"text": str(value)})

    def press(self, key):
        allowed = {"Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp",
                   "ArrowDown", "ArrowLeft", "ArrowRight", "Space"}
        if key not in allowed:
            raise ValueError("Недопустимая клавиша.")
        return self._call("press", {"key": key})

    def scroll(self, delta):
        return self._call("scroll", {"delta": int(delta)})

    def cookies(self):
        return self._call("cookies")

    def user_agent(self):
        return self._call("user_agent")

    def close(self):
        if not self.closed:
            try:
                self._call("close", timeout=15)
            except Exception:
                self.closed = True
            finally:
                self._release_slot()
