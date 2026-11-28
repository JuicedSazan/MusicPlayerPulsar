"""Per-Pulsar-session headless VK browser for QR-code login.

The QR-code login flow needs no password and no browser extension: the server
opens a fresh Chromium profile on a VK login page that offers "scan the QR code
with the VK app", returns a screenshot of that page, and waits for the user to
confirm the login on their phone. Once VK sets the session cookies, the backend
exchanges them for a VK web token and closes the browser.

The browser profile and its cookies stay server-side and are never exposed to
the client; they live only as long as this Pulsar session.
"""
import base64
import os
import queue
import threading
import time

# Render may omit service-level env vars from the Gunicorn runtime even when
# they are present in render.yaml. Match the build command explicitly on Render:
# the browser is installed with PLAYWRIGHT_BROWSERS_PATH=0 there, while local
# development keeps Playwright's default browser cache.
if os.environ.get("RENDER") and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"


_BROWSER_SLOT = threading.BoundedSemaphore(1)


class VKRemoteBrowser:
    WIDTH = 1280
    HEIGHT = 800
    # A normal desktop UA: VK serves the QR login page to new devices, and the
    # same string must be reused for the web-token exchange and VK Music calls.
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self):
        if not _BROWSER_SLOT.acquire(blocking=False):
            raise RuntimeError("На бесплатном сервере доступно одно окно входа VK одновременно. Попробуй позже.")
        self._slot_released = False
        self.commands = queue.Queue()
        self.ready = threading.Event()
        self.start_error = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="pulsar-vk-qr", daemon=True)
        self.thread.start()
        if not self.ready.wait(80):
            self._release_slot()
            raise RuntimeError("Тайм-аут запуска браузера VK.")
        if self.start_error:
            self._release_slot()
            raise RuntimeError(self.start_error)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                executable = p.chromium.executable_path
                if not os.path.isfile(executable):
                    raise RuntimeError(
                        "Chromium не установлен в runtime. Ожидаемый файл: "
                        + str(executable) + "; PLAYWRIGHT_BROWSERS_PATH="
                        + repr(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
                    )
                browser = p.chromium.launch(
                    channel="chromium",
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                          "--no-zygote", "--disable-background-networking",
                          "--disable-extensions"],
                )
                context = browser.new_context(
                    viewport={"width": self.WIDTH, "height": self.HEIGHT},
                    device_scale_factor=1,
                    locale="ru-RU",
                    user_agent=self.USER_AGENT,
                )
                # Fonts and media are not needed for the QR code and slow the
                # page down on a small hosting instance.
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("font", "media")
                    else route.continue_(),
                )
                page = context.new_page()
                page.set_default_timeout(20000)
                page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1500)
                self.browser, self.context, self.page = browser, context, page
                self._wait_for_qr(page)
                self._prepare_screenshot(page)
                self.ready.set()
                while True:
                    command, args, result_queue = self.commands.get()
                    try:
                        if command == "screenshot":
                            result_queue.put(self._capture(page))
                        elif command == "refresh":
                            page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=90000)
                            page.wait_for_timeout(1500)
                            self._wait_for_qr(page)
                            result_queue.put(self._capture(page))
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
                            result_queue.put(context.cookies([
                                "https://vk.ru/", "https://login.vk.ru/",
                                "https://vk.com/", "https://login.vk.com/",
                            ]))
                        elif command == "user_agent":
                            result_queue.put(self.USER_AGENT)
                        elif command == "close":
                            context.close()
                            browser.close()
                            self.closed = True
                            result_queue.put(True)
                            break
                    except Exception as exc:  # noqa: BLE001 - forwarded to the caller
                        result_queue.put(exc)
        except Exception as exc:  # noqa: BLE001 - startup failures surface via ready
            self.start_error = str(exc)[:300]
            self.ready.set()

    @staticmethod
    def _wait_for_qr(page, timeout_seconds=25):
        """Wait until the VK ID QR widget inside its iframe has actually rendered.

        The QR code is an SVG drawn by a cross-origin iframe. A screenshot taken
        before that frame paints is an empty white card, which is exactly what a
        small hosting instance can produce while the frame is still loading.
        """
        deadline = time.monotonic() + timeout_seconds
        try:
            page.wait_for_selector("iframe[src*='qr_auth']", timeout=30000)
        except Exception:
            pass
        while time.monotonic() < deadline:
            for frame in page.frames:
                if "qr_auth" not in (frame.url or ""):
                    continue
                try:
                    rendered = frame.evaluate(
                        """() => {
                          const svgs = Array.from(document.querySelectorAll('svg'));
                          return svgs.some(item => {
                            const box = item.getBoundingClientRect();
                            return box.width >= 100 && box.height >= 100;
                          });
                        }"""
                    )
                except Exception:
                    rendered = False
                if rendered:
                    page.wait_for_timeout(600)
                    return True
            page.wait_for_timeout(400)
        return False

    @staticmethod
    def _prepare_screenshot(page):
        """Let layout settle; the QR readiness check does the real waiting."""
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    @staticmethod
    def _capture(page):
        cdp = page.context.new_cdp_session(page)
        try:
            result = cdp.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": 72, "fromSurface": True,
            })
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
        return result["data"]

    def _release_slot(self):
        if not self._slot_released:
            self._slot_released = True
            _BROWSER_SLOT.release()

    def _call(self, command, args=None, timeout=30):
        if self.closed:
            raise RuntimeError("Окно входа VK уже закрыто.")
        result_queue = queue.Queue(maxsize=1)
        self.commands.put((command, args or {}, result_queue))
        result = result_queue.get(timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result

    def screenshot(self):
        return base64.b64decode(self._call("screenshot"))

    def refresh(self):
        return base64.b64decode(self._call("refresh", timeout=100))

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
        return self.USER_AGENT

    def close(self):
        if not self.closed:
            try:
                self._call("close", timeout=20)
            except Exception:
                self.closed = True
            finally:
                self._release_slot()
