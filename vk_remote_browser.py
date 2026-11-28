"""Headless VK browser that only shows the QR login.

Every login starts from a blank profile, like the Yandex Music sign-in: no VK
profile is remembered between visits. To keep memory low on a small hosting
instance the first browser only renders the login screen, takes a screenshot and
closes; a second, equally lean browser serves the QR refresh and the numeric
code entry while the visitor confirms the login on their phone.

Nothing here is exposed to the client except screenshots.
"""
import base64
import json
import os
import queue
import re
import tempfile
import threading
import time

# Render installs the browser with PLAYWRIGHT_BROWSERS_PATH=0 at build time, so
# the runtime must resolve it from Playwright's package-local directory too.
if os.environ.get("RENDER") and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
# Container images (for example Hugging Face Spaces) install the browser into a
# fixed directory and pass it through this variable.

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
def _route_login_request(route):
    """Login window requests: no cache, no ads/trackers, no heavy media."""
    request = route.request
    if _should_abort(request):
        route.abort()
        return
    headers = dict(request.headers)
    headers["Cache-Control"] = "no-cache, no-store, max-age=0"
    headers["Pragma"] = "no-cache"
    route.continue_(headers=headers)


LOGIN_URLS = ("https://vk.ru/", "https://vk.com/")
ABORT_TYPES = ("font", "media")
ABORT_HOST_PARTS = (
    "mc.yandex", "ad.mail", "an.yandex", "top-fwz1", "rs.mail", "adriver",
    "tns-counter", "google-analytics", "apptracer", "vk-portal", "weborama",
    "doubleclick", "googletagmanager", "serving-sys", "privacy-cs",
)
LAUNCH_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-software-rasterizer", "--no-zygote",
    "--disable-background-networking", "--disable-extensions",
    "--disable-sync", "--mute-audio", "--disable-default-apps",
    "--disable-translate", "--disable-features=Translate,BackForwardCache",
    "--renderer-process-limit=1", "--js-flags=--max-old-space-size=96",
    "--memory-pressure-off",
]

_SLOT = threading.BoundedSemaphore(1)
_SLOT_STATE = {"acquired_at": 0.0, "lock": threading.Lock()}
_SLOT_STALE_SECONDS = 180


def _acquire_slot():
    if _SLOT.acquire(blocking=False):
        with _SLOT_STATE["lock"]:
            _SLOT_STATE["acquired_at"] = time.monotonic()
        return True
    with _SLOT_STATE["lock"]:
        acquired_at = _SLOT_STATE["acquired_at"]
    if acquired_at and time.monotonic() - acquired_at > _SLOT_STALE_SECONDS:
        try:
            _SLOT.release()
        except ValueError:
            pass
        if _SLOT.acquire(blocking=False):
            with _SLOT_STATE["lock"]:
                _SLOT_STATE["acquired_at"] = time.monotonic()
            return True
    return False


def _free_slot():
    with _SLOT_STATE["lock"]:
        _SLOT_STATE["acquired_at"] = 0.0
    try:
        _SLOT.release()
    except ValueError:
        pass


def _should_abort(request):
    if request.resource_type in ABORT_TYPES:
        return True
    try:
        host = request.url.split("/")[2].lower()
    except Exception:
        return False
    return any(part in host for part in ABORT_HOST_PARTS)


class QRLoginBrowser:
    """One VK login window: a lean Chromium that shows the QR and takes the code."""

    WIDTH = 1280
    HEIGHT = 800

    def __init__(self):
        if not _acquire_slot():
            raise RuntimeError(
                "Окно входа VK уже открыто. Повтори через минуту или закрой его."
            )
        self._slot_released = False
        self.timings = {}
        self.commands = queue.Queue()
        self.ready = threading.Event()
        self.start_error = None
        self.closed = False
        self.screenshot_data = None
        self.leaked_playwright = None
        self.serve_ready = threading.Event()
        self.last_error = ""
        self.code_stage = ""
        self.code_page_text = ""
        self.code_visible_fields = []
        self.trace = []
        self._page = None
        self.code_stage = ""
        self.code_stage = ""
        self.page_info = {}
        self.thread = threading.Thread(target=self._run, name="pulsar-vk-qr", daemon=True)
        self.thread.start()
        if not self.ready.wait(90):
            self.close()
            raise RuntimeError("Не удалось открыть окно входа VK: тайм-аут.")
        if self.start_error:
            self.close()
            raise RuntimeError(self.start_error)

    # ------------------------------------------------------------------ thread
    def _run(self):
        profile_dir = tempfile.mkdtemp(prefix="vk-qr-")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                started = time.monotonic()
                browser = p.chromium.launch(channel="chromium", headless=True, args=LAUNCH_ARGS)
                try:
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="ru-RU",
                        user_agent=USER_AGENT,
                        device_scale_factor=1,
                    )
                    context.route("**/*", _route_login_request)
                    page = context.new_page()
                    page.set_default_timeout(30000)
                    reached = False
                    for url in LOGIN_URLS:
                        try:
                            page.goto(url, wait_until="commit", timeout=45000)
                            if self._wait_for_qr(page, timeout_seconds=60):
                                reached = True
                                break
                        except Exception:
                            continue
                    self.timings["load_s"] = round(time.monotonic() - started, 1)
                    if not reached:
                        raise RuntimeError(
                            "Страница входа VK не отрисовала QR-код. Попробуй ещё раз через минуту."
                        )
                    self.screenshot_data = self._capture(page)
                    self.page_info = self._page_info(page)
                    self._trace("qr-shown", page)
                    self.timings["total_s"] = round(time.monotonic() - started, 1)
                    browser.close()
                    browser = None
                    # Keep the Playwright handle alive for the second session.
                    self.leaked_playwright = p
                    p = None
                    self.ready.set()
                    self._serve()
                    return
                finally:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
        except Exception as exc:  # noqa: BLE001 - surfaced through ready/start_error
            self.start_error = str(exc)[:300]
            self.ready.set()
        finally:
            self._release_slot()

    def _serve(self):
        """Answer QR refreshes and the numeric code while the visitor confirms."""
        p = self.leaked_playwright
        browser = None
        try:
            browser = p.chromium.launch(channel="chromium", headless=True, args=LAUNCH_ARGS)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                user_agent=USER_AGENT,
                device_scale_factor=1,
            )
            context.route("**/*", _route_login_request)
            page = context.new_page()
            page.set_default_timeout(30000)
            # Follow the live page that session 1 already loaded: opening another
            # login in a different context can be refused by VK as a new device.
            page.goto("https://vk.ru/", wait_until="commit", timeout=45000)
            self._wait_for_qr(page, timeout_seconds=45)
            self._page = page
            self.serve_ready.set()
            while True:
                command, args, result_queue = self.commands.get()
                try:
                    if command == "screenshot":
                        result_queue.put(self._capture(page))
                    elif command == "refresh":
                        result_queue.put(self._refresh(page))
                    elif command == "click":
                        page.mouse.click(max(0, min(1280, args["x"])),
                                         max(0, min(800, args["y"])))
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
                    elif command == "code":
                        result_queue.put(self._enter_code(page, context, args["code"]))
                    elif command == "cookies":
                        data = context.cookies([
                            "https://vk.ru/", "https://login.vk.ru/",
                            "https://vk.com/", "https://login.vk.com/",
                        ])
                        result_queue.put(json.dumps(data))
                    elif command == "close":
                        self.closed = True
                        result_queue.put(True)
                        break
                except Exception as exc:  # noqa: BLE001 - forwarded to the caller
                    self.last_error = (command + ": " + type(exc).__name__ + ": "
                                       + str(exc).replace("\n", " ")[:300])
                    result_queue.put(exc)
        except Exception as exc:  # noqa: BLE001
            self.start_error = str(exc).replace("\n", " ")[:300]
            self.last_error = "serve: " + self.start_error
            self.serve_ready.set()
            try:
                self.commands.put_nowait(("close", {}, queue.Queue()))
            except Exception:
                pass
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if self.leaked_playwright is not None:
                    self.leaked_playwright.stop()
            except Exception:
                pass
            self.leaked_playwright = None
            self._release_slot()

    @staticmethod
    def _wait_for_qr(page, timeout_seconds=40, hold_last=True):
        deadline = time.monotonic() + timeout_seconds
        seen_frame = False
        while time.monotonic() < deadline:
            for frame in page.frames:
                if "qr_auth" not in (frame.url or ""):
                    continue
                seen_frame = True
                try:
                    ok = frame.evaluate(
                        """() => {
                          const svgs = Array.from(document.querySelectorAll('svg'));
                          return svgs.some(el => {
                            const b = el.getBoundingClientRect();
                            return b.width >= 100 && b.height >= 100;
                          });
                        }"""
                    )
                except Exception:
                    ok = False
                if ok:
                    page.wait_for_timeout(1500)
                    return True
            page.wait_for_timeout(300)
        # The scrollable code page hides the QR frame: prefer the first
        # screenshot over a long poll when the page cannot be detected.
        if hold_last:
            page.wait_for_timeout(1500)
            return True
        return False

    @staticmethod
    def _capture(page):
        cdp = page.context.new_cdp_session(page)
        try:
            result = cdp.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": 65, "fromSurface": True,
            })
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
        return result["data"]

    @staticmethod
    def _page_info(page):
        """Whether the page is already logged in or showing a numeric code prompt."""
        text = ""
        try:
            text = page.evaluate("() => (document.body && document.body.innerText) || ''")
        except Exception:
            pass
        for frame in page.frames:
            if frame is page:
                continue
            try:
                text += "\n" + (frame.evaluate("() => (document.body && document.body.innerText) || ''") or "")
            except Exception:
                continue
        lowered = text.lower()
        code_prompt = "нового устройства" in lowered or "код" in lowered and "вход" in lowered
        logged_in = ("news" in lowered and "vk" in lowered) or "моя страница" in lowered or "войти" not in lowered
        return {"text_sample": text[:300], "code_prompt": bool(code_prompt), "logged_in": bool(logged_in)}

    def _refresh(self, page):
        """Reload the current VK page and capture it."""
        try:
            page.reload(wait_until="commit", timeout=45000)
        except Exception:
            page.goto("https://vk.ru/", wait_until="commit", timeout=45000)
        self._wait_for_qr(page, timeout_seconds=45)
        return self._capture(page)

    # Inputs that must never receive the code.
    _IGNORED_INPUTS = {"search", "hidden", "radio", "checkbox", "password", "file", "submit", "button"}

    @staticmethod
    def _usable_input(inputs):
        """Visible textual inputs (the code boxes or the code field)."""
        usable = []
        for item in inputs:
            try:
                kind = (item.get_attribute("type") or "text").lower()
                if kind in QRLoginBrowser._IGNORED_INPUTS:
                    continue
                if "search" in (item.get_attribute("inputmode") or "").lower():
                    continue
                if not item.is_visible():
                    continue
                usable.append(item)
            except Exception:
                continue
        return usable

    def _find_code_field(self, page, timeout_seconds=30):
        """Wait for a real code input; return (frame, [inputs], visible-field report)."""
        deadline = time.monotonic() + timeout_seconds
        report = []
        while time.monotonic() < deadline:
            for frame in page.frames:
                try:
                    inputs = frame.query_selector_all("input")
                except Exception:
                    continue
                if not inputs:
                    continue
                usable = self._usable_input(inputs)
                if usable:
                    # One wide field or several single-character boxes.
                    single = [item for item in usable if (item.get_attribute("maxlength") or "") == "1"]
                    return frame, (single or usable)
                for item in inputs:
                    try:
                        report.append({
                            "type": item.get_attribute("type"),
                            "name": item.get_attribute("name"),
                            "visible": bool(item.is_visible()),
                        })
                    except Exception:
                        continue
            page.wait_for_timeout(300)
        return None, [], report

    def _enter_code(self, page, context, code):
        self.code_stage = "find-field"
        try:
            return self._enter_code_impl(page, context, code)
        except Exception as exc:  # noqa: BLE001 - reported to the API caller
            self.last_error = ("code@" + str(getattr(self, "code_stage", "?")) + ": "
                               + type(exc).__name__ + ": " + str(exc).replace("\n", " ")[:240])
            raise

    def _enter_code_impl(self, page, context, code):
        text = re.sub(r"\D", "", str(code or ""))[:10]
        if not text:
            raise RuntimeError("пустой код")
        self._trace("code-requested", page)
        frame, fields, report = self._find_code_field(page, timeout_seconds=12)
        self._trace("code-field-search", page)
        self.code_visible_fields = report[:12]
        if frame is None or not fields:
            self.code_page_text = self._page_text(page) + " || " + self._frame_text(page)
            self.last_error = ("code@no-field: не нашёл поле кода. Видимые поля: "
                               + str(self.code_visible_fields)[:240])
            self.code_submitted = True
            return self._capture(page)
        self.code_stage = "fill"
        if len(fields) > 1:
            for index, char in enumerate(text):
                target = fields[index] if index < len(fields) else fields[-1]
                target.click(timeout=3000)
                target.type(char, delay=30)
        else:
            fields[0].click(timeout=3000)
            fields[0].fill(text)
        page.wait_for_timeout(400)
        self.code_stage = "submit"
        clicked = False
        try:
            container = fields[0].evaluate_handle("el => el.closest('form') || el.parentElement")
            element = container.as_element()
            if element is not None:
                for pattern in ("Продолжить", "Подтвердить", "Войти", "Далее", "Отправить", "Готово",
                                "Continue", "Confirm", "Next"):
                    try:
                        button = element.get_by_role("button", name=re.compile(pattern)).first
                        if button is not None and button.is_visible() and button.is_enabled():
                            button.click(timeout=4000)
                            clicked = True
                            break
                    except Exception:
                        continue
        except Exception:
            clicked = False
        if not clicked:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
        self._trace("code-submitted", page)
        self.code_stage = "wait-cookie"
        deadline = time.monotonic() + 10
        got_session = False
        while time.monotonic() < deadline:
            try:
                names = {item.get("name") for item in context.cookies(["https://vk.ru/"])}
            except Exception:
                names = set()
            if "remixsid" in names:
                got_session = True
                break
            page.wait_for_timeout(300)
        try:
            page.evaluate("() => window.stop()")
        except Exception:
            pass
        self.code_stage = "capture"
        self.code_page_text = " | ".join(
            part for part in (self._page_text(page), self._frame_text(page)) if part)[:600]
        if not got_session:
            self.last_error = "code@no-session: " + (self.code_page_text or "экран VK без текста")
        self.code_submitted = True
        return self._capture(page)

    @staticmethod
    def _page_text(page):
        """Visible text of the outer login page, compacted."""
        try:
            value = page.evaluate("() => (document.body && document.body.innerText) || ''")
        except Exception:
            value = ""
        return " ".join((value or "").split())[:300]

    @staticmethod
    def _frame_text(page):
        """Visible text of the VK ID frames, compacted."""
        chunks = []
        for frame in page.frames:
            if frame is page:
                continue
            try:
                value = frame.evaluate("() => (document.body && document.body.innerText) || ''")
                if value and value.strip():
                    chunks.append(" ".join(value.split())[:200])
            except Exception:
                continue
        return " | ".join(chunks)[:400]

    def _trace(self, stage, page=None):
        """Record what the window shows at a stage (bounded, never secrets)."""
        entry = {"t": round(time.time(), 1), "stage": stage}
        if page is not None:
            try:
                entry["page"] = self._page_text(page)[:160]
                entry["frame"] = self._frame_text(page)[:160]
                inputs = []
                for frame in page.frames:
                    try:
                        for item in frame.query_selector_all("input"):
                            inputs.append({
                                "type": item.get_attribute("type"),
                                "mode": item.get_attribute("inputmode"),
                                "max": item.get_attribute("maxlength"),
                                "visible": bool(item.is_visible()),
                            })
                    except Exception:
                        continue
                entry["inputs"] = inputs[:8]
            except Exception:
                entry["error"] = "trace failed"
        self.trace.append(entry)
        del self.trace[:-20]
        return entry

    def get_trace(self):
        return {"trace": list(self.trace), "stage": self.code_stage}

    def reload_screen(self):
        """Reload the login page and return a fresh screenshot (UI button)."""
        page = getattr(self, "_page", None)
        if page is None:
            self._call("screenshot")
            return base64.b64decode(self.screenshot_data or "")
        try:
            page.reload(wait_until="commit", timeout=45000)
        except Exception:
            try:
                page.goto("https://vk.ru/", wait_until="commit", timeout=45000)
            except Exception:
                pass
        self._wait_for_qr(page, timeout_seconds=25)
        return self._capture(page)

    # --------------------------------------------------------------- commands
    def _release_slot(self):
        if not self._slot_released:
            self._slot_released = True
            _free_slot()

    def _call(self, command, args=None, timeout=45):
        if self.closed:
            raise RuntimeError("Окно входа VK закрыто.")
        result_queue = queue.Queue(maxsize=1)
        self.commands.put((command, args or {}, result_queue))
        result = result_queue.get(timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result

    def screenshot(self):
        return base64.b64decode(self.screenshot_data or self._call("screenshot"))

    def refresh(self):
        return base64.b64decode(self._call("refresh", timeout=90))

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

    def enter_code(self, code):
        return base64.b64decode(self._call("code", {"code": str(code)}, timeout=40))

    def user_agent(self):
        return USER_AGENT

    def cookies(self):
        self.code_stage = "cookies"
        try:
            return json.loads(self._call("cookies", timeout=60))
        except Exception as exc:
            self.last_error = ("cookies: " + type(exc).__name__ + ": "
                               + str(exc).replace("\n", " ")[:260])
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.commands.put_nowait(("close", {}, queue.Queue()))
            except Exception:
                pass
        self._release_slot()
