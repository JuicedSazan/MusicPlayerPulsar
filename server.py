"""Pulsar Web v1 — multi-user web backend.

Each browser receives an opaque, HttpOnly session cookie. Yandex clients,
authentication state, jobs and audio cache are isolated per session.
"""
import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import secrets
import shutil
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory, make_response, abort, Response

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("PULSE_DATA_DIR", "/tmp/pulsar"))
CACHE_ROOT = DATA_ROOT / "cache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
SESSION_TTL = int(os.environ.get("PULSE_SESSION_TTL", "43200"))
COOKIE_NAME = "pulse_sid"
COOKIE_SECURE = os.environ.get("PULSE_COOKIE_SECURE", "1").lower() not in ("0", "false", "no")

app = Flask(__name__, static_folder=None)
_sessions = {}
_sessions_lock = threading.RLock()

try:
    from yandex_music import Client
except Exception:
    Client = None

try:
    from vkpymusic import Service as VKMusicService
except Exception:
    VKMusicService = None


def _new_session():
    return {
        "client": None, "auth": "idle", "auth_url": "", "auth_code": "",
        "auth_error": "", "auth_generation": 0, "vk_service": None, "vk_user_id": None,
        "vk_user_name": "", "vk_remixsid": None, "vk_cookie_p": None,
        "vk_user_agent": None, "vk_cookie_domain": "",
        "vk_token_expires": 0, "vk_refresh_lock": threading.Lock(),
        "vk_auth_error": "", "vk_remote_browser": None,
        "jobs": {}, "last_seen": time.time(),
        "lock": threading.RLock(), "cache_dir": None,
    }


def _get_session(create=True):
    sid = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[a-f0-9]{64}", sid):
        sid = ""
    with _sessions_lock:
        state = _sessions.get(sid) if sid else None
        if state is None and create:
            sid = secrets.token_hex(32)
            state = _new_session()
            state["cache_dir"] = CACHE_ROOT / sid
            state["cache_dir"].mkdir(parents=True, exist_ok=True)
            _sessions[sid] = state
        if state:
            state["last_seen"] = time.time()
    request.environ["pulse.sid"] = sid
    request.environ["pulse.state"] = state
    return state


@app.before_request
def bind_session():
    # State-changing calls are accepted only from this site's own origin.
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.netloc != request.host:
                abort(403)
    _get_session(create=True)


@app.after_request
def set_session_cookie(response):
    sid = request.environ.get("pulse.sid")
    if sid:
        response.set_cookie(
            COOKIE_NAME, sid, max_age=SESSION_TTL, httponly=True,
            secure=COOKIE_SECURE, samesite="Lax", path="/"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def state():
    return request.environ["pulse.state"]


def api_error(message, status=400):
    return jsonify({"error": str(message)[:500]}), status


def _cover_url(tr):
    albums = getattr(tr, "albums", []) or []
    artists = getattr(tr, "artists", []) or []
    uri = (getattr(albums[0], "cover_uri", None) if albums else None)
    if not uri and artists:
        artist_cover = getattr(artists[0], "cover", None)
        uri = getattr(artist_cover, "uri", None) or getattr(artists[0], "cover_uri", None)
    if not uri:
        return ""
    return "https://" + str(uri).removeprefix("https://").removeprefix("http://").replace("%%", "400x400")


def _track_item(tr):
    albums = getattr(tr, "albums", []) or []
    album_id = (getattr(albums[0], "id", None) if albums else None) or getattr(tr, "album_id", None)
    track_id = getattr(tr, "id", None) or getattr(tr, "track_id", None)
    if not track_id or not album_id:
        return None
    artists = getattr(tr, "artists", []) or []
    artist = ", ".join(getattr(a, "name", "") for a in artists if getattr(a, "name", None)) or "Исполнитель не определён"
    cover = _cover_url(tr)
    return {
        "id": f"{track_id}:{album_id}", "track_id": str(track_id), "album_id": str(album_id),
        "title": getattr(tr, "title", None) or "Неизвестный трек", "artist": artist,
        "duration": getattr(tr, "duration_ms", 0) or 0, "cover": cover,
        "url": f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
    }

def _expand_playlist_tracks(client, raw, diagnostics=None):
    """Resolve Yandex TrackShort entries into full Track objects.

    Prefer batched client.tracks() calls, then use TrackShort.fetch_track()
    for entries that the batch response did not resolve.
    """
    started = time.perf_counter()
    entries = [getattr(entry, "track", None) or entry for entry in (raw or [])]
    if diagnostics is not None:
        diagnostics.update({"raw_entries": len(entries), "unique_track_ids": 0, "batch_count": 0, "batch_seconds": 0.0, "batch_resolved": 0, "fallback_count": 0, "fallback_seconds": 0.0})
    expanded = {}
    requested_by_key = {}
    keys_by_entry = []

    for entry in entries:
        # TrackShort.track_id already contains "id:album_id". Keep the
        # numeric/string track id and album id separate to avoid building
        # the invalid "id:album_id:album_id" request key.
        tid = getattr(entry, "id", None)
        albums = getattr(entry, "albums", None) or []
        aid = getattr(entry, "album_id", None)
        if not tid:
            raw_track_id = getattr(entry, "track_id", None)
            if raw_track_id:
                tid = str(raw_track_id).split(":", 1)[0]
        if not aid and albums:
            aid = getattr(albums[0], "id", None)
        tid = str(tid) if tid else ""
        aid = str(aid) if aid else ""
        keys_by_entry.append((tid, aid))
        if tid:
            key = f"{tid}:{aid}" if aid else tid
            requested_by_key.setdefault(key, (tid, aid))

        # Some playlist responses already contain a full Track object.
        if tid and aid and albums and getattr(entry, "artists", None):
            expanded[f"{tid}:{aid}"] = entry
            expanded.setdefault(tid, entry)

    requested = list(requested_by_key)
    if diagnostics is not None:
        diagnostics["unique_track_ids"] = len(requested)
    # Keep requests reasonably small; a very large likes collection can
    # otherwise make the API request fail as a whole.
    batch_started = time.perf_counter()
    for offset in range(0, len(requested), 50):
        batch = requested[offset:offset + 50]
        if diagnostics is not None:
            diagnostics["batch_count"] += 1
        try:
            full_tracks = client.tracks(batch) or []
        except Exception:
            full_tracks = []
        for track in full_tracks:
            tid = str(getattr(track, "id", "") or "")
            albums = getattr(track, "albums", []) or []
            aid = str(getattr(albums[0], "id", "") or "") if albums else ""
            if tid:
                expanded[tid] = track
                if aid:
                    expanded[f"{tid}:{aid}"] = track

    if diagnostics is not None:
        diagnostics["batch_seconds"] = round(time.perf_counter() - batch_started, 3)
        diagnostics["batch_resolved"] = sum(1 for tid, aid in keys_by_entry if (expanded.get(f"{tid}:{aid}") if tid and aid else None) or (expanded.get(tid) if tid else None))

    # Resolve only entries missed by the batch lookup using a bounded pool.
    missing = []
    result = []
    for entry, (tid, aid) in zip(entries, keys_by_entry):
        full = expanded.get(f"{tid}:{aid}") if tid and aid else None
        full = full or (expanded.get(tid) if tid else None)
        if full is not None:
            result.append(full)
        else:
            index = len(result)
            result.append(None)
            missing.append((index, entry))

    def fetch_one(item):
        index, entry = item
        fetch_track = getattr(entry, "fetch_track", None)
        if not callable(fetch_track):
            return index, entry
        try:
            candidate = fetch_track()
            return index, candidate or entry
        except Exception:
            return index, entry

    fallback_started = time.perf_counter()
    if missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for index, resolved in pool.map(fetch_one, missing):
                result[index] = resolved

    if diagnostics is not None:
        diagnostics["fallback_count"] = len(missing)
        diagnostics["fallback_seconds"] = round(time.perf_counter() - fallback_started, 3)
        diagnostics["expand_seconds"] = round(time.perf_counter() - started, 3)
    return [track or entry for track, entry in zip(result, entries)]


def _start_auth(s):
    if Client is None:
        with s["lock"]:
            s.update(auth="error", auth_error="Не установлена библиотека yandex-music. Сообщи владельцу сайта.")
        return
    with s["lock"]:
        if s["auth"] == "pending":
            return
        s["auth_generation"] += 1
        generation = s["auth_generation"]
        s.update(auth="pending", auth_url="", auth_code="", auth_error="", client=None)

    def on_code(code):
        with s["lock"]:
            if s["auth_generation"] != generation or s["auth"] != "pending":
                return
            s["auth_url"] = code.verification_url
            s["auth_code"] = code.user_code

    def worker():
        try:
            client = Client()
            token = client.device_auth(on_code=on_code)
            client = Client(token.access_token).init()
            with s["lock"]:
                if s["auth_generation"] != generation or s["auth"] != "pending":
                    return
                s["client"] = client
                s["auth"] = "ready"
                s["auth_error"] = ""
        except Exception as exc:
            with s["lock"]:
                if s["auth_generation"] != generation or s["auth"] != "pending":
                    return
                s["auth"] = "error"
                s["auth_error"] = str(exc)[:450]
    threading.Thread(target=worker, daemon=True).start()


def _get_client(s):
    with s["lock"]:
        return s["client"]


def _request_vk_web_token(remixsid, cookie_p, user_agent, domain="vk.ru"):
    """Exchange in-memory VK session cookies for a short-lived web token.

    Newer VK accounts live on vk.ru; older sessions only carry vk.com cookies,
    so the exchange endpoint and its Origin follow the cookie domain.
    """
    import urllib.parse
    site = "vk.com" if domain == "vk.com" else "vk.ru"
    form = urllib.parse.urlencode({"version": "1", "app_id": "6287487"}).encode()
    cookie_header = f"remixsid={remixsid}" + (f"; p={cookie_p}" if cookie_p else "")
    req = urllib.request.Request(
        f"https://login.{site}/?act=web_token", data=form,
        headers={
            "User-Agent": user_agent or "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"https://{site}", "Referer": f"https://{site}/",
            "Cookie": cookie_header,
        }, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("type") != "okay" or not isinstance(data.get("data"), dict):
        reason = (data.get("error_info") or data.get("type")) if isinstance(data, dict) else "invalid response"
        raise RuntimeError("VK не принял сессию: " + str(reason)[:160])
    token_data = data["data"]
    if not token_data.get("access_token"):
        raise RuntimeError("VK вернул ответ без web-токена.")
    return token_data


def _ensure_vk_service(s):
    """Refresh the VK web token shortly before expiry using session cookies in RAM."""
    with s["lock"]:
        service = s.get("vk_service")
        expires = int(s.get("vk_token_expires") or 0)
        remixsid = s.get("vk_remixsid")
        cookie_p = s.get("vk_cookie_p") or ""
        user_agent = s.get("vk_user_agent") or "Mozilla/5.0"
        user_id = s.get("vk_user_id")
        cookie_domain = s.get("vk_cookie_domain") or "vk.ru"
    if not service:
        return None, "VK Music не подключена в этой сессии."
    if not remixsid:
        return service, ""
    if expires and expires - int(time.time()) > 120:
        return service, ""
    if not s["vk_refresh_lock"].acquire(blocking=False):
        with s["lock"]:
            current = s.get("vk_service")
            current_expiry = int(s.get("vk_token_expires") or 0)
        if current and (not current_expiry or current_expiry > int(time.time())):
            return current, ""
        return None, "Обновляю подключение VK. Повтори запрос через несколько секунд."
    try:
        token_data = _request_vk_web_token(remixsid, cookie_p, user_agent, cookie_domain)
        token = str(token_data.get("access_token") or "")
        new_user_id = token_data.get("user_id") or user_id
        new_expires = int(token_data.get("expires") or 0)
        if not token or not new_user_id:
            raise RuntimeError("VK не вернул токен или ID пользователя.")
        refreshed_service = VKMusicService(user_agent, token)
        refreshed_service.search_songs_by_text("Daft Punk", count=1, offset=0)
        with s["lock"]:
            s["vk_service"] = refreshed_service
            s["vk_user_id"] = int(new_user_id)
            s["vk_token_expires"] = new_expires
            s["vk_auth_error"] = ""
        return refreshed_service, ""
    except Exception as exc:
        message = str(exc)[:220]
        with s["lock"]:
            s["vk_auth_error"] = message
            current = s.get("vk_service")
            current_expiry = int(s.get("vk_token_expires") or 0)
        if current and current_expiry > int(time.time()):
            return current, ""
        return None, "Не удалось обновить сессию VK. Возможно, потребуется войти заново."
    finally:
        s["vk_refresh_lock"].release()


def _vk_clean_cookie_value(raw):
    """Accept only a bare cookie value; reject headers, separators and junk."""
    value = str(raw or "").strip()
    if not value or len(value) > 10000:
        return ""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or ";" in value or " " in value:
        return ""
    return value


def _vk_read_profile(service, fallback_user_id=None):
    """Return (user_id, display name) for a VKpyMusic service, best effort."""
    user_id = fallback_user_id
    name = ""
    try:
        info = service.get_user_info()
        info_id = getattr(info, "userid", None)
        if info_id:
            user_id = info_id
        name = " ".join(filter(None, [
            str(getattr(info, "first_name", "") or "").strip(),
            str(getattr(info, "last_name", "") or "").strip(),
        ])).strip()
    except Exception:
        pass
    try:
        user_id = int(user_id) if user_id else None
    except (TypeError, ValueError):
        user_id = None
    return user_id, name or ("ID " + str(user_id) if user_id else "VK")


def _vk_store_connection(s, service, user_id, name, remixsid=None, cookie_p=None,
                         user_agent=None, cookie_domain="", expires=0):
    """Keep the connected VK client and its refresh material in server memory only."""
    with s["lock"]:
        s["vk_service"] = service
        s["vk_user_id"] = user_id
        s["vk_user_name"] = name
        s["vk_remixsid"] = remixsid
        s["vk_cookie_p"] = cookie_p
        s["vk_user_agent"] = user_agent
        s["vk_cookie_domain"] = cookie_domain if remixsid else ""
        s["vk_token_expires"] = int(expires or 0)
        s["vk_auth_error"] = ""


def _vk_make_service(user_agent, token):
    service = VKMusicService(user_agent, token)
    songs = service.search_songs_by_text("Daft Punk", count=1, offset=0)
    if not songs:
        raise RuntimeError("VK ответил без результатов поиска музыки.")
    return service


def _vk_session_snapshot(s):
    with s["lock"]:
        connected = s.get("vk_service") is not None
        expires = int(s.get("vk_token_expires") or 0)
        return {
            "connected": connected,
            "user_id": s.get("vk_user_id"),
            "user_name": s.get("vk_user_name") or "",
            "expires_in_seconds": max(0, expires - int(time.time())) if expires else None,
            "refreshable": bool(s.get("vk_remixsid")),
            "auth_error": s.get("vk_auth_error") or "",
        }


def _vk_cookie_connection(remixsid, cookie_p, user_agent, domain):
    """Mint a web token from browser session cookies and verify VK Music."""
    if VKMusicService is None:
        raise RuntimeError("VKpyMusic не установлена на сервере.")
    token_data = _request_vk_web_token(remixsid, cookie_p, user_agent, domain)
    token = str(token_data.get("access_token") or "")
    if not token:
        raise RuntimeError("VK не вернул access_token.")
    service = _vk_make_service(user_agent, token)
    user_id, name = _vk_read_profile(service, token_data.get("user_id"))
    return {
        "service": service,
        "user_id": user_id,
        "user_name": name,
        "expires": int(token_data.get("expires") or 0),
    }


@app.post("/api/vk/session/connect")
def api_vk_session_connect():
    """Connect VK Music from data the browser already owns.

    Two callers are supported: the Pulsar VK Bridge extension sends the live VK
    session cookies (which also enable silent token refresh in memory), while the
    bookmarklet path sends only a freshly minted short-lived web token.
    """
    if VKMusicService is None:
        return api_error("VKpyMusic не установлена на сервере.", 503)
    payload = request.get_json(silent=True) or {}
    user_agent = str(payload.get("user_agent") or "").strip()
    if not user_agent or len(user_agent) > 1500:
        user_agent = request.headers.get("User-Agent", "Mozilla/5.0")
    remixsid = _vk_clean_cookie_value(payload.get("remixsid"))
    cookie_p = _vk_clean_cookie_value(payload.get("p"))
    remixsid_vkcom = _vk_clean_cookie_value(payload.get("remixsidVkCom"))
    cookie_p_vkcom = _vk_clean_cookie_value(payload.get("pVkCom"))
    access_token = str(payload.get("access_token") or "").strip()
    if len(access_token) > 12000:
        return api_error("Некорректный токен VK.")
    domain = "vk.ru"
    if not remixsid and remixsid_vkcom:
        remixsid, cookie_p, domain = remixsid_vkcom, cookie_p_vkcom, "vk.com"
    s = state()
    try:
        if remixsid:
            connection = _vk_cookie_connection(remixsid, cookie_p, user_agent, domain)
            _vk_store_connection(
                s, connection["service"], connection["user_id"], connection["user_name"],
                remixsid=remixsid, cookie_p=cookie_p, user_agent=user_agent,
                cookie_domain=domain, expires=connection["expires"],
            )
        elif access_token:
            service = _vk_make_service(user_agent, access_token)
            user_id, name = _vk_read_profile(service, payload.get("user_id"))
            expires = int(payload.get("expires") or 0)
            _vk_store_connection(s, service, user_id, name, user_agent=user_agent, expires=expires)
        else:
            return api_error("В браузере не найдена авторизация VK. Войди на vk.ru и повтори.", 401)
    except Exception as exc:
        return api_error("Не удалось подключить VK Музыку: " + str(exc)[:300], 401)
    snapshot = _vk_session_snapshot(s)
    snapshot.update(ok=True, message="VK Музыка подключена.")
    return jsonify(snapshot)


@app.get("/api/vk/session/status")
def api_vk_session_status():
    return jsonify({"ok": True, **_vk_session_snapshot(state())})


@app.post("/api/vk/session/refresh")
def api_vk_session_refresh():
    """Re-mint the web token from the cookies kept in this session's memory."""
    if VKMusicService is None:
        return api_error("VKpyMusic не установлена на сервере.", 503)
    s = state()
    with s["lock"]:
        remixsid = s.get("vk_remixsid")
        cookie_p = s.get("vk_cookie_p") or ""
        user_agent = s.get("vk_user_agent") or "Mozilla/5.0"
        domain = s.get("vk_cookie_domain") or "vk.ru"
    if not remixsid:
        return api_error("Для автообновления нужна сессия VK из расширения. Войди через VK снова.", 401)
    try:
        connection = _vk_cookie_connection(remixsid, cookie_p, user_agent, domain)
        _vk_store_connection(
            s, connection["service"], connection["user_id"], connection["user_name"],
            remixsid=remixsid, cookie_p=cookie_p, user_agent=user_agent,
            cookie_domain=domain, expires=connection["expires"],
        )
    except Exception as exc:
        with s["lock"]:
            s["vk_auth_error"] = str(exc)[:220]
        return api_error("Не удалось обновить доступ к VK: " + str(exc)[:260], 401)
    return jsonify({"ok": True, **_vk_session_snapshot(s)})


def _valid_yandex_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and (parsed.hostname or "").lower() in ("music.yandex.ru", "music.yandex.com", "music.yandex.by", "music.yandex.kz")
    except Exception:
        return False



# One VK login window per instance. A window abandoned by a closed tab must not
# lock the feature forever on a single-process deployment, so ownership is kept
# here with a freshness timestamp and reclaimed after a short idle period.
_vk_login_guard = threading.Lock()
_vk_login_global = {"browser": None, "sid": "", "last_used": 0.0}
_VK_LOGIN_STALE_SECONDS = 90.0


def _vk_login_touch(browser):
    with _vk_login_guard:
        if _vk_login_global["browser"] is browser:
            _vk_login_global["last_used"] = time.time()


def _vk_login_release(browser=None):
    """Forget the login window and close it when it is the current one."""
    with _vk_login_guard:
        current = _vk_login_global["browser"]
        if browser is None or current is browser:
            _vk_login_global.update(browser=None, sid="", last_used=0.0)
        else:
            current = None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    elif current is not None:
        try:
            current.close()
        except Exception:
            pass


def _vk_login_prepare(sid):
    """Allow a new login for this session, closing an abandoned window first."""
    stale = None
    with _vk_login_guard:
        current = _vk_login_global["browser"]
        owner = _vk_login_global["sid"]
        last_used = _vk_login_global["last_used"]
        if current is None:
            _vk_login_global.update(sid=sid, last_used=time.time())
            return True, ""
        if owner == sid or time.time() - last_used > _VK_LOGIN_STALE_SECONDS:
            _vk_login_global.update(browser=None, sid=sid, last_used=time.time())
            stale = current
        else:
            return False, ("Окно входа VK уже открыто в другой вкладке. Закройте его там "
                           "или повторите через минуту.")
    if stale is not None:
        try:
            stale.close()
        except Exception:
            pass
    return True, ""


# Hosted VK login prototype. Browser and cookies are isolated per Pulsar session.
@app.post("/api/vk/remote/start")
def api_vk_remote_start():
    s = state()
    sid = request.environ.get("pulse.sid") or ""
    with s["lock"]:
        existing = s.get("vk_remote_browser")
        s["vk_remote_browser"] = None
    if existing:
        _vk_login_release(existing)
    allowed, error_text = _vk_login_prepare(sid)
    if not allowed:
        return api_error(error_text, 409)
    try:
        from vk_remote_browser import VKRemoteBrowser
        import base64
        browser = VKRemoteBrowser()
    except Exception as exc:
        _vk_login_release()
        return api_error("Не удалось запустить окно входа VK: " + str(exc)[:220], 503)
    with _vk_login_guard:
        _vk_login_global.update(browser=browser, sid=sid, last_used=time.time())
    with s["lock"]:
        s["vk_remote_browser"] = browser
    try:
        image = base64.b64encode(browser.screenshot()).decode("ascii")
    except Exception as exc:
        _vk_login_release(browser)
        with s["lock"]:
            s["vk_remote_browser"] = None
        return api_error("Окно VK открылось, но не отдало QR-код: " + str(exc)[:200], 502)
    return jsonify({"ok": True, "image": image, "width": browser.WIDTH, "height": browser.HEIGHT,
                    "timings": getattr(browser, "timings", {})})


@app.post("/api/vk/remote/refresh")
def api_vk_remote_refresh():
    """Reload the QR login page and return a fresh screenshot."""
    s = state()
    with s["lock"]:
        browser = s.get("vk_remote_browser")
    if not browser:
        return api_error("Окно входа VK не запущено.", 404)
    _vk_login_touch(browser)
    try:
        import base64
        image = base64.b64encode(browser.refresh()).decode("ascii")
        return jsonify({"ok": True, "image": image, "width": browser.WIDTH, "height": browser.HEIGHT})
    except Exception as exc:
        return api_error("Не удалось обновить QR-код VK: " + str(exc)[:180], 502)


@app.post("/api/vk/remote/screen")
def api_vk_remote_screen():
    s = state()
    with s["lock"]:
        browser = s.get("vk_remote_browser")
    if not browser:
        return api_error("Сначала запусти окно авторизации VK.", 404)
    _vk_login_touch(browser)
    try:
        return jsonify({"ok": True, "image": __import__("base64").b64encode(browser.screenshot()).decode("ascii"),
                        "width": browser.WIDTH, "height": browser.HEIGHT})
    except Exception as exc:
        return api_error("Не удалось получить изображение VK: " + str(exc)[:180], 502)


@app.post("/api/vk/remote/input")
def api_vk_remote_input():
    s = state()
    with s["lock"]:
        browser = s.get("vk_remote_browser")
    if not browser:
        return api_error("Окно авторизации VK не запущено.", 404)
    _vk_login_touch(browser)
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    try:
        if action == "click":
            x, y = payload.get("x"), payload.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return api_error("Некорректные координаты.")
            browser.click(x, y)
        elif action == "type":
            value = payload.get("text")
            if not isinstance(value, str) or len(value) > 2000:
                return api_error("Некорректный ввод.")
            browser.type_text(value)
        elif action == "press":
            browser.press(str(payload.get("key") or ""))
        elif action == "scroll":
            browser.scroll(payload.get("delta", 0))
        else:
            return api_error("Неизвестное действие.")
        return jsonify({"ok": True})
    except Exception as exc:
        return api_error("Не удалось передать действие браузеру: " + str(exc)[:160], 502)


@app.post("/api/vk/remote/finish")
def api_vk_remote_finish():
    """Poll the QR login: as soon as VK sets the session cookies, connect VK Music."""
    s = state()
    with s["lock"]:
        browser = s.get("vk_remote_browser")
    if not browser:
        return api_error("Окно входа VK не запущено.", 404)
    _vk_login_touch(browser)
    if VKMusicService is None:
        return api_error("VKpyMusic не установлена на сервере.", 503)
    try:
        cookies = browser.cookies()
    except Exception as exc:
        return api_error("Не удалось прочитать сессию VK: " + str(exc)[:160], 502)
    remixsid = ""
    cookie_p = ""
    domain = "vk.ru"
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        cookie_domain = str(cookie.get("domain") or "")
        if not value or name not in ("remixsid", "p"):
            continue
        if cookie_domain.endswith("vk.ru"):
            if name == "remixsid" and not remixsid:
                remixsid = value
                domain = "vk.ru"
            elif name == "p" and not cookie_p:
                cookie_p = value
        elif cookie_domain.endswith("vk.com") and not remixsid:
            if name == "remixsid":
                domain = "vk.com"
    if not remixsid:
        return api_error("QR-код ещё не подтверждён в приложении VK.", 401)
    user_agent = browser.user_agent()
    try:
        connection = _vk_cookie_connection(remixsid, cookie_p, user_agent, domain)
        _vk_store_connection(
            s, connection["service"], connection["user_id"], connection["user_name"],
            remixsid=remixsid, cookie_p=cookie_p, user_agent=user_agent,
            cookie_domain=domain, expires=connection["expires"],
        )
    except Exception as exc:
        return api_error("VK принял вход, но подключить музыку не удалось: " + str(exc)[:200], 502)
    with s["lock"]:
        s["vk_remote_browser"] = None
    _vk_login_release(browser)
    snapshot = _vk_session_snapshot(s)
    snapshot.update(ok=True, connected=True, message="VK Музыка подключена.")
    return jsonify(snapshot)


@app.post("/api/vk/remote/close")
def api_vk_remote_close():
    s = state()
    with s["lock"]:
        browser = s.get("vk_remote_browser")
        s["vk_remote_browser"] = None
    if browser:
        _vk_login_release(browser)
    return jsonify({"ok": True})


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "Pulsar Web"})


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    # Audio is never served from a public, guessable path.
    if filename.startswith("cache/") or filename.startswith("api/") or filename.startswith("."):
        abort(404)
    return send_from_directory(ROOT, filename)


@app.get("/api/status")
def api_status():
    s = state()
    with s["lock"]:
        payload = {"auth": s["auth"], "auth_error": s["auth_error"]}
        # Device code is short-lived and is only useful while login is pending.
        if s["auth"] == "pending":
            payload.update(auth_url=s["auth_url"], auth_code=s["auth_code"])
        else:
            s["auth_url"] = ""
            s["auth_code"] = ""
        return jsonify(payload)


@app.post("/api/login")
def api_login():
    _start_auth(state())
    return jsonify({"ok": True})


@app.post("/api/logout")
def api_logout():
    s = state()
    with s["lock"]:
        remote_browser = s.get("vk_remote_browser")
        s["vk_remote_browser"] = None
        _vk_login_release(remote_browser)
        s.update(client=None, auth="idle", auth_url="", auth_code="", auth_error="", jobs={},
                 vk_service=None, vk_user_id=None, vk_user_name="", vk_remixsid=None,
                 vk_cookie_p=None, vk_user_agent=None, vk_cookie_domain="",
                 vk_token_expires=0, vk_auth_error="")
        s["auth_generation"] += 1
    if remote_browser:
        try:
            remote_browser.close()
        except Exception:
            pass
    # Remove only this browser's temporary cache.
    if s["cache_dir"]:
        shutil.rmtree(s["cache_dir"], ignore_errors=True)
        s["cache_dir"].mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True})


@app.get("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"results": []})
    s = state(); client = _get_client(s)
    if not client:
        return api_error("Сначала войди в Яндекс через вкладку авторизации.", 401)
    try:
        started = time.perf_counter()
        result = None; last_error = None; attempts = 0
        for attempt in range(3):
            attempts = attempt + 1
            try:
                result = client.search(query, type_="track", page=0); break
            except Exception as exc:
                last_error = exc
                if attempt < 2: time.sleep(.7 * (attempt + 1))
        if result is None: raise last_error or RuntimeError("Яндекс не ответил на поисковый запрос.")
        section = getattr(result, "tracks", None)
        items = [item for tr in (getattr(section, "results", []) or [])[:20] if (item := _track_item(tr))]
        return jsonify({"results": items, "timings": {
            "search_total_ms": round((time.perf_counter() - started) * 1000),
            "attempts": attempts, "results": len(items)
        }})
    except Exception as exc:
        return api_error("Поиск не удался: " + str(exc)[:350], 502)


@app.post("/api/vk/id-probe")
def api_vk_id_probe():
    """Experimental: test whether a VK ID OAuth token also grants VK Audio API access."""
    if VKMusicService is None:
        return api_error("VKpyMusic не установлена на сервере.", 503)
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("access_token") or "").strip()
    user_agent = str(payload.get("user_agent") or "").strip()
    supplied_user_id = payload.get("user_id")
    if not token or not user_agent:
        return api_error("Не хватает VK ID access token или User-Agent.")
    if len(token) > 12000 or len(user_agent) > 1500:
        return api_error("Некорректная длина параметров.")
    result = {"token_received": True, "user_info": False, "audio_search": False,
              "recommendations": False, "details": []}
    try:
        service = VKMusicService(user_agent, token)
        try:
            info = service.get_user_info()
            user_id = getattr(info, "userid", None)
            result["user_info"] = bool(user_id)
            result["user_id"] = int(user_id) if user_id else None
            result["display_name"] = " ".join(filter(None, [
                getattr(info, "first_name", ""), getattr(info, "last_name", "")
            ])) or "VK"
        except Exception as exc:
            result["details"].append("get_user_info: " + str(exc)[:220])
            user_id = None
        try:
            found = service.search_songs_by_text("Daft Punk", count=1, offset=0) or []
            result["audio_search"] = bool(found)
            result["audio_search_count"] = len(found)
        except Exception as exc:
            result["details"].append("audio search: " + str(exc)[:220])
        if not user_id and supplied_user_id:
            try:
                user_id = int(supplied_user_id)
            except (TypeError, ValueError):
                user_id = None
        if user_id:
            try:
                recommended = service.get_recommendations(user_id=int(user_id), count=1, offset=0) or []
                result["recommendations"] = bool(recommended)
                result["recommendations_count"] = len(recommended)
            except Exception as exc:
                result["details"].append("recommendations: " + str(exc)[:220])
        if result["audio_search"] and user_id:
            session = state()
            with session["lock"]:
                session["vk_service"] = service
                session["vk_user_id"] = int(user_id)
            result["connected"] = True
        else:
            result["connected"] = False
        return jsonify(result)
    except Exception as exc:
        result["connected"] = False
        result["details"].append("Service init: " + str(exc)[:220])
        return jsonify(result), 200


# Local-only VK browser session. Playwright runs in a dedicated thread because
# its synchronous API must stay on the thread where it was initialized.
_vk_browser = None
_vk_browser_lock = threading.Lock()


def _get_vk_browser():
    global _vk_browser
    with _vk_browser_lock:
        if _vk_browser is None:
            from vk_browser import VKBrowserSession
            _vk_browser = VKBrowserSession(DATA_ROOT / "vk-browser-profile")
        return _vk_browser


@app.post("/api/vk/browser-login")
def api_vk_browser_login():
    if os.environ.get("PULSE_LOCAL_VK_BROWSER", "0") != "1":
        return api_error("Автоматическая авторизация VK доступна только в локальной версии.", 403)
    try:
        _get_vk_browser().open()
        return jsonify({"ok": True, "message": "Окно VK открыто. Войди в аккаунт и затем нажми «Проверить сессию VK»."})
    except Exception as exc:
        return api_error("Не удалось открыть браузер VK: " + str(exc)[:220], 500)


@app.post("/api/vk/inspect-audio")
def api_vk_inspect_audio():
    """Inspect official VK Music web requests using the user's local browser session."""
    if os.environ.get("PULSE_LOCAL_VK_BROWSER", "0") != "1":
        return api_error("Исследование официального веб-клиента доступно только в локальной версии.", 403)
    try:
        result = _get_vk_browser().inspect_audio()
        return jsonify(result)
    except Exception as exc:
        return api_error("Не удалось исследовать VK Музыку: " + str(exc)[:250], 500)


@app.post("/api/vk/browser-token")
def api_vk_browser_token():
    """Obtain a local VK web token, then test the actual VK Audio API."""
    if os.environ.get("PULSE_LOCAL_VK_BROWSER", "0") != "1":
        return api_error("Автоматическая авторизация VK доступна только в локальной версии.", 403)
    if VKMusicService is None:
        return api_error("VKpyMusic не установлена.", 503)
    try:
        browser = _get_vk_browser()
        cookies = browser.cookies()
    except Exception as exc:
        return api_error("Не удалось прочитать локальную сессию VK: " + str(exc)[:220], 500)

    remixsid = next((x["value"] for x in cookies if x["name"] == "remixsid" and "vk.ru" in x["domain"]), "")
    cookie_p = next((x["value"] for x in cookies if x["name"] == "p" and "login.vk.ru" in x["domain"]), "")
    if not remixsid:
        return api_error("В окне VK пока нет remixsid. Войди в аккаунт и повтори проверку.", 401)

    import urllib.parse
    import urllib.request
    form = urllib.parse.urlencode({"version": "1", "app_id": "6287487"}).encode()
    cookie_header = f"remixsid={remixsid}" + (f"; p={cookie_p}" if cookie_p else "")
    req = urllib.request.Request(
        "https://login.vk.ru/?act=web_token", data=form,
        headers={
            "User-Agent": browser.user_agent(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://vk.ru", "Referer": "https://vk.ru/",
            "Cookie": cookie_header,
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return api_error("Ошибка запроса VK: " + str(exc)[:180], 502)
    if data.get("type") != "okay" or not isinstance(data.get("data"), dict):
        return jsonify({"ok": False, "error": "VK отклонил локальную сессию: " + str(data.get("error_info") or data.get("type") or "неизвестная ошибка")[:180]}), 401

    token_data = data["data"]
    token = str(token_data.get("access_token") or "")
    if not token:
        return api_error("VK ответил без access_token.", 502)
    user_id = token_data.get("user_id")
    expires = int(token_data.get("expires") or 0)
    result = {
        "ok": True, "user_id": user_id,
        "expires_in_seconds": max(0, expires - int(time.time())) if expires else None,
        "cookie_p_found": bool(cookie_p),
        "audio_search": False, "recommendations": False,
        "details": [],
    }
    try:
        service = VKMusicService(browser.user_agent(), token)
    except Exception as exc:
        result["details"].append("Создание VKMusicService: " + str(exc)[:220])
        service = None

    if service:
        try:
            songs = service.search_songs_by_text("Daft Punk", count=3, offset=0) or []
            result["audio_search"] = bool(songs)
            result["audio_search_count"] = len(songs)
        except Exception as exc:
            result["details"].append("Поиск аудио: " + str(exc)[:220])
        if user_id:
            try:
                recommended = service.get_recommendations(user_id=int(user_id), count=3, offset=0) or []
                result["recommendations"] = bool(recommended)
                result["recommendations_count"] = len(recommended)
            except Exception as exc:
                result["details"].append("Рекомендации: " + str(exc)[:220])

        if result["audio_search"]:
            session = state()
            with session["lock"]:
                session["vk_service"] = service
                session["vk_user_id"] = int(user_id) if user_id else None
                session["vk_token_expires"] = expires
            result["connected"] = True
        else:
            result["connected"] = False
    else:
        result["connected"] = False

    result["message"] = (
        "Веб-токен получен; поиск VK Audio API работает."
        if result["audio_search"] else
        "Веб-токен получен, но доступ к поиску VK Audio API не подтверждён."
    )
    return jsonify(result)


# Process-local guard against repeatedly hitting VK after an explicit rate limit.
_vk_web_probe_blocked_until = 0.0
_vk_web_probe_guard = threading.Lock()


@app.post("/api/vk/web-token-probe")
def api_vk_web_token_probe():
    """One-shot VK web-token diagnostic. Never log or persist cookies/tokens."""
    global _vk_web_probe_blocked_until
    started = time.monotonic()
    now = time.time()
    with _vk_web_probe_guard:
        blocked_for = max(0, int(_vk_web_probe_blocked_until - now))
    if blocked_for:
        return jsonify({
            "ok": False,
            "error": "Повторная проверка временно приостановлена после HTTP 429.",
            "diagnostics": {
                "stage": "rate_limit_guard", "http_status": 429,
                "retry_after_seconds": blocked_for,
                "note": "Защитная пауза действует на этом экземпляре сервера."
            }
        }), 429

    payload = request.get_json(silent=True) or {}
    raw_remixsid = str(payload.get("remixsid") or "").strip()
    raw_cookie_p = str(payload.get("p") or "").strip()

    def extract_cookie_value(raw, cookie_name):
        # Accept either a raw cookie value or a copied Cookie/Set-Cookie fragment.
        if not raw:
            return ""
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
            raise ValueError("В cookie обнаружены управляющие символы.")
        candidate = raw
        if candidate.lower().startswith("cookie:"):
            candidate = candidate.split(":", 1)[1].strip()
        parts = [part.strip() for part in candidate.split(";")]
        named = {}
        for part in parts:
            if "=" in part:
                name, value = part.split("=", 1)
                named[name.strip()] = value.strip()
        if cookie_name in named:
            candidate = named[cookie_name]
        elif len(parts) > 1 or (parts and parts[0].partition("=")[0].strip() in {"remixsid", "p"}):
            raise ValueError(f"Не найден параметр {cookie_name} в скопированной строке cookies.")
        if not candidate or ";" in candidate or len(candidate) > 10000:
            raise ValueError(f"Некорректное значение {cookie_name}.")
        return candidate

    try:
        remixsid = extract_cookie_value(raw_remixsid, "remixsid")
        cookie_p = extract_cookie_value(raw_cookie_p, "p")
        # If a complete Cookie header was pasted into the remixsid field,
        # reuse its p value unless the dedicated p field was filled separately.
        if not cookie_p and (";" in raw_remixsid or raw_remixsid.lower().startswith("cookie:")):
            header_text = raw_remixsid.split(":", 1)[1].strip() if raw_remixsid.lower().startswith("cookie:") else raw_remixsid
            for part in header_text.split(";"):
                name, separator, value = part.strip().partition("=")
                if separator and name.strip() == "p":
                    cookie_p = value.strip()
                    break
    except ValueError as exc:
        return api_error(str(exc))
    if not remixsid:
        return api_error("Нужна cookie remixsid с vk.ru.")

    import urllib.parse
    import email.utils

    form = urllib.parse.urlencode({"version": "1", "app_id": "6287487"}).encode()
    user_agent = request.headers.get("User-Agent", "Mozilla/5.0")
    cookie_header = f"remixsid={remixsid}" + (f"; p={cookie_p}" if cookie_p else "")
    diagnostics = {
        "stage": "request_web_token",
        "endpoint": "https://login.vk.ru/?act=web_token",
        "method": "POST",
        "cookie_p_provided": bool(cookie_p),
        "cookie_values_logged_or_saved": False,
        "request_user_agent_present": bool(user_agent),
        "request_origin": "https://vk.ru",
        "started_at_unix": int(now),
    }
    req = urllib.request.Request(
        "https://login.vk.ru/?act=web_token",
        data=form,
        headers={
            "User-Agent": user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://vk.ru",
            "Referer": "https://vk.ru/",
            "Cookie": cookie_header,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            http_status = response.status
            response_headers = response.headers
            raw_body = response.read()
        diagnostics["http_status"] = http_status
        diagnostics["response_content_type"] = response_headers.get("Content-Type", "")
        data = json.loads(raw_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        diagnostics["http_status"] = exc.code
        diagnostics["response_content_type"] = exc.headers.get("Content-Type", "") if exc.headers else ""
        retry_after = (exc.headers.get("Retry-After") if exc.headers else None)
        retry_seconds = None
        if retry_after:
            try:
                retry_seconds = max(1, int(retry_after))
            except ValueError:
                try:
                    retry_seconds = max(1, int(email.utils.parsedate_to_datetime(retry_after).timestamp() - time.time()))
                except Exception:
                    retry_seconds = None
        if exc.code == 429:
            retry_seconds = retry_seconds or 60
            with _vk_web_probe_guard:
                _vk_web_probe_blocked_until = max(_vk_web_probe_blocked_until, time.time() + retry_seconds)
            diagnostics.update({
                "stage": "vk_rate_limited", "retry_after_header": retry_after,
                "retry_after_seconds": retry_seconds,
                "note": "VK вернул HTTP 429. Значение 60 секунд — локальная защитная пауза, если VK не прислал Retry-After; фактический срок ограничения VK неизвестен."
            })
            return jsonify({
                "ok": False,
                "error": "VK ограничил частоту запросов (HTTP 429). Не повторяй проверку до окончания паузы.",
                "diagnostics": diagnostics
            }), 429
        diagnostics["stage"] = "vk_http_error"
        diagnostics["error_type"] = "HTTPError"
        diagnostics["error_message"] = str(exc.reason)[:180]
        diagnostics["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return jsonify({"ok": False, "error": f"VK ответил HTTP {exc.code}.", "diagnostics": diagnostics}), 502
    except Exception as exc:
        diagnostics.update({
            "stage": "network_or_response_error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:180],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
        return jsonify({"ok": False, "error": "Не удалось получить корректный ответ от VK.", "diagnostics": diagnostics}), 502

    if not isinstance(data, dict):
        diagnostics["stage"] = "invalid_response_json"
        return jsonify({"ok": False, "error": "VK вернул неожиданный формат ответа.", "diagnostics": diagnostics}), 502
    diagnostics["vk_response_type"] = str(data.get("type") or "missing")[:80]
    if data.get("type") != "okay" or not isinstance(data.get("data"), dict):
        diagnostics["stage"] = "vk_rejected_session"
        diagnostics["vk_error_info"] = str(data.get("error_info") or data.get("type") or "unknown")[:180]
        diagnostics["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return jsonify({
            "ok": False,
            "error": "VK не принял переданную сессию. Проверь актуальность cookies и домены, с которых они получены.",
            "diagnostics": diagnostics
        }), 401

    token_data = data["data"]
    token = str(token_data.get("access_token") or "")
    if not token:
        diagnostics["stage"] = "token_missing"
        return jsonify({"ok": False, "error": "VK ответил без access_token.", "diagnostics": diagnostics}), 502

    expires = int(token_data.get("expires") or 0)
    user_id = token_data.get("user_id")
    result = {
        "ok": True, "user_id": user_id,
        "expires_at": expires,
        "expires_in_seconds": max(0, expires - int(time.time())) if expires else None,
        "audio_search": False, "recommendations": False, "details": [],
        "diagnostics": {**diagnostics, "stage": "web_token_received"},
    }
    service = None
    if VKMusicService is None:
        result["details"].append("VKpyMusic не установлена на сервере.")
    else:
        try:
            service = VKMusicService(user_agent, token)
            result["diagnostics"]["stage"] = "vk_audio_search"
            songs = service.search_songs_by_text("Daft Punk", count=2, offset=0) or []
            result["audio_search"] = bool(songs)
            result["audio_search_count"] = len(songs)
        except Exception as exc:
            result["details"].append("Поиск VK Audio: " + str(exc)[:180])
            result["diagnostics"]["audio_search_error_type"] = type(exc).__name__
        if service and user_id:
            try:
                result["diagnostics"]["stage"] = "vk_mix_recommendations"
                recommendations = service.get_recommendations(user_id=int(user_id), count=2, offset=0) or []
                result["recommendations"] = bool(recommendations)
                result["recommendations_count"] = len(recommendations)
            except Exception as exc:
                result["details"].append("VK Mix: " + str(exc)[:180])
                result["diagnostics"]["recommendations_error_type"] = type(exc).__name__

    if result["audio_search"] and user_id:
        session = state()
        with session["lock"]:
            session["vk_service"] = service
            session["vk_user_id"] = int(user_id)
            session["vk_remixsid"] = remixsid
            session["vk_cookie_p"] = cookie_p
            session["vk_user_agent"] = user_agent
            session["vk_token_expires"] = expires
            session["vk_auth_error"] = ""
        result["connected"] = True
    else:
        result["connected"] = False
    result["diagnostics"].update({
        "stage": "complete",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "token_received": True,
        "credentials_stored_on_disk": False,
        "credentials_kept_in_server_memory": bool(result["connected"]),
    })
    result["message"] = (
        "VK Music подключена. Токен будет автоматически обновляться в памяти сервера."
        if result["connected"] else
        "Веб-токен получен, но подключить VK Music не удалось: поиск или ID пользователя не подтверждены."
    )
    return jsonify(result)


@app.post("/api/vk/connect")
def api_vk_connect():
    if VKMusicService is None:
        return api_error("VKpyMusic не установлена на сервере. Проверь зависимости.", 503)
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "").strip()
    user_agent = str(payload.get("user_agent") or "").strip()
    if not token or not user_agent:
        return api_error("Нужны токен VK Music и соответствующий User-Agent.")
    if len(token) > 12000 or len(user_agent) > 1500:
        return api_error("Проверь длину токена и User-Agent.")
    try:
        service = VKMusicService(user_agent, token)
        info = service.get_user_info()
        user_id = getattr(info, "userid", None)
        if not user_id:
            raise RuntimeError("VK не вернул ID пользователя.")
        s = state()
        with s["lock"]:
            s["vk_service"] = service
            s["vk_user_id"] = int(user_id)
            s["vk_user_name"] = " ".join(filter(None, [
                getattr(info, "first_name", ""), getattr(info, "last_name", "")
            ])) or "VK"
            s["vk_remixsid"] = None
            s["vk_cookie_p"] = None
            s["vk_user_agent"] = user_agent
            s["vk_cookie_domain"] = ""
            s["vk_token_expires"] = 0
            s["vk_auth_error"] = ""
        return jsonify({"ok": True, "user": {
            "id": int(user_id),
            "name": " ".join(filter(None, [getattr(info, "first_name", ""), getattr(info, "last_name", "")])) or "VK"
        }})
    except Exception as exc:
        return api_error("Не удалось подключить VK Music: " + str(exc)[:350], 401)


@app.post("/api/vk/disconnect")
def api_vk_disconnect():
    s = state()
    with s["lock"]:
        s["vk_service"] = None
        s["vk_user_id"] = None
        s["vk_user_name"] = ""
        s["vk_remixsid"] = None
        s["vk_cookie_p"] = None
        s["vk_user_agent"] = None
        s["vk_cookie_domain"] = ""
        s["vk_token_expires"] = 0
        s["vk_auth_error"] = ""
    return jsonify({"ok": True})


def _vk_track_item(song):
    title = getattr(song, "title", None) or "Неизвестный трек"
    artist = getattr(song, "artist", None) or "Исполнитель не определён"
    owner_id = str(getattr(song, "owner_id", "") or "")
    track_id = str(getattr(song, "track_id", "") or "")
    stream_url = str(getattr(song, "url", "") or "")
    if not stream_url:
        return None
    return {
        "id": "vk:" + owner_id + "_" + track_id,
        "track_id": track_id, "owner_id": owner_id,
        "title": title, "artist": artist,
        "duration": int(getattr(song, "duration", 0) or 0) * 1000,
        "cover": "", "url": "https://vk.com/audio" + owner_id + "_" + track_id,
        "stream_url": stream_url, "provider": "vk"
    }


@app.get("/api/vk/search")
def api_vk_search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"results": []})
    s = state()
    service, refresh_error = _ensure_vk_service(s)
    if not service:
        return api_error(refresh_error or "Сначала подключи VK Music в настройках Пульсара.", 401)
    try:
        started = time.perf_counter()
        songs = service.search_songs_by_text(query, count=30, offset=0) or []
        items = [item for song in songs if (item := _vk_track_item(song))]
        return jsonify({"results": items, "timings": {
            "search_total_ms": round((time.perf_counter() - started) * 1000),
            "results": len(items)
        }})
    except Exception as exc:
        return api_error("Поиск VK Music не удался: " + str(exc)[:350], 502)


@app.get("/api/vk/stream")
def api_vk_stream():
    owner_id = str(request.args.get("owner_id") or "").strip()
    track_id = str(request.args.get("track_id") or "").strip()
    if not re.fullmatch(r"-?\d+", owner_id) or not re.fullmatch(r"\d+", track_id):
        return api_error("Некорректный идентификатор VK-трека.")
    s = state()
    service, refresh_error = _ensure_vk_service(s)
    if not service:
        return api_error(refresh_error or "VK Music не подключена в этой сессии.", 401)
    try:
        songs = service.get_songs_by_id([owner_id + "_" + track_id]) or []
        item = next((_vk_track_item(song) for song in songs if getattr(song, "url", None)), None)
        if not item:
            return api_error("VK не вернул ссылку на аудио этого трека.", 404)
        return jsonify({"stream_url": item["stream_url"]})
    except Exception as exc:
        return api_error("Не удалось обновить ссылку на аудио VK: " + str(exc)[:350], 502)


@app.get("/api/vk/audio")
def api_vk_audio():
    """Proxy VK audio through localhost so browser playback is not blocked by CDN CORS."""
    owner_id = str(request.args.get("owner_id") or "").strip()
    track_id = str(request.args.get("track_id") or "").strip()
    if not re.fullmatch(r"-?\d+", owner_id) or not re.fullmatch(r"\d+", track_id):
        return api_error("Некорректный идентификатор VK-трека.")
    s = state()
    service, refresh_error = _ensure_vk_service(s)
    if not service:
        return api_error(refresh_error or "VK Music не подключена в этой сессии.", 401)
    try:
        songs = service.get_songs_by_id([owner_id + "_" + track_id]) or []
        song = next((x for x in songs if getattr(x, "url", None)), None)
        if not song:
            return api_error("VK не вернул ссылку на аудио этого трека.", 404)
        stream_url = str(song.url)
        parsed = urlparse(stream_url)
        host = (parsed.hostname or "").lower()
        allowed_hosts = ("okcdn.ru", "userapi.com", "vkuseraudio.net")
        if parsed.scheme != "https" or not any(host == domain or host.endswith("." + domain) for domain in allowed_hosts):
            return api_error("VK вернул неподдерживаемый аудио-хост.", 502)

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"}
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        upstream_req = urllib.request.Request(stream_url, headers=headers, method="GET")
        upstream = urllib.request.urlopen(upstream_req, timeout=30)
        response_headers = {}
        for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
            value = upstream.headers.get(name)
            if value:
                response_headers[name] = value
        response_headers["Cache-Control"] = "no-store"
        def generate():
            try:
                while True:
                    chunk = upstream.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()
        return Response(generate(), status=getattr(upstream, "status", 200),
                        headers=response_headers, direct_passthrough=True)
    except urllib.error.HTTPError as exc:
        return api_error("VK CDN отклонил запрос аудио: HTTP " + str(exc.code), 502)
    except Exception as exc:
        return api_error("Не удалось передать аудио VK: " + str(exc)[:250], 502)


@app.get("/api/vk/recommendations")
def api_vk_recommendations():
    s = state()
    service, refresh_error = _ensure_vk_service(s)
    with s["lock"]:
        user_id = s.get("vk_user_id")
    if not service or not user_id:
        return api_error(refresh_error or "Сначала подключи VK Music в настройках Пульсара.", 401)
    try:
        started = time.perf_counter()
        songs = service.get_recommendations(user_id=user_id, count=50, offset=0) or []
        items = [item for song in songs if (item := _vk_track_item(song))]
        return jsonify({"title": "Музыкальные рекомендации для вас", "tracks": items,
                        "timings": {"total_ms": round((time.perf_counter() - started) * 1000),
                                    "results": len(items)}})
    except Exception as exc:
        return api_error("Не удалось получить рекомендации VK: " + str(exc)[:350], 502)


@app.get("/api/playlist")
def api_playlist():
    url = request.args.get("url", "")
    if not _valid_yandex_url(url):
        return api_error("Принимаются только ссылки music.yandex.ru на плейлисты.")
    match = re.search(r"/playlists/([0-9a-fA-F-]+)", url)
    if not match: return api_error("Не распознал ссылку на плейлист Яндекс Музыки.")
    client = _get_client(state())
    if not client: return api_error("Сначала войди в Яндекс.", 401)
    playlist_id = match.group(1)
    try:
        playlist = None
        for call in (lambda: client.playlist(playlist_id), lambda: client.playlist(playlist_id, 0)):
            try:
                playlist = call()
                if playlist: break
            except Exception: pass
        if not playlist: raise RuntimeError("Яндекс не вернул плейлист по этой ссылке.")
        raw = getattr(playlist, "tracks", None)
        if hasattr(raw, "fetch_tracks"):
            try: raw = raw.fetch_tracks()
            except Exception: pass
        if not raw and hasattr(playlist, "fetch_tracks"): raw = playlist.fetch_tracks()
        items = []
        for entry in (raw or []):
            item = _track_item(getattr(entry, "track", None) or entry)
            if item: items.append(item)
        if not items: raise RuntimeError("Плейлист найден, но треки получить не удалось.")
        return jsonify({"title": getattr(playlist, "title", None) or "Плейлист Яндекс Музыки", "id": playlist_id, "tracks": items})
    except Exception as exc:
        return api_error("Не удалось загрузить плейлист: " + str(exc)[:350], 502)


@app.get("/api/user-playlists")
def api_user_playlists():
    s = state()
    client = _get_client(s)
    if not client: return api_error("Сначала войди в Яндекс.", 401)
    try:
        started = time.perf_counter()
        items = []
        for playlist in (client.users_playlists_list() or []):
            uid = getattr(playlist, "uid", None)
            kind = getattr(playlist, "kind", None)
            if uid is None or kind is None: continue
            items.append({
                "id": f"{uid}:{kind}", "uid": str(uid), "kind": str(kind),
                "title": getattr(playlist, "title", None) or "Плейлист",
                "track_count": int(getattr(playlist, "track_count", 0) or 0),
                "revision": getattr(playlist, "revision", None),
            })
        # The Likes collection is a special endpoint, not an ordinary user playlist.
        liked = client.users_likes_tracks()
        liked_tracks = getattr(liked, "tracks", None) or []
        items.insert(0, {"id": "likes", "uid": "", "kind": "", "title": "Мне нравится",
                         "track_count": len(liked_tracks), "is_likes": True})
        return jsonify({"playlists": items, "timings": {
            "total_ms": round((time.perf_counter() - started) * 1000)
        }})
    except Exception as exc:
        return api_error("Не удалось загрузить плейлисты Яндекс Музыки: " + str(exc)[:350], 502)


@app.get("/api/user-playlist")
def api_user_playlist():
    s = state()
    client = _get_client(s)
    if not client: return api_error("Сначала войди в Яндекс.", 401)
    playlist_id = request.args.get("id", "")
    diagnostics = {"playlist_fetch_seconds": 0.0, "serialization_seconds": 0.0}
    request_started = time.perf_counter()
    try:
        playlist_started = time.perf_counter()
        if playlist_id == "likes":
            liked = client.users_likes_tracks()
            raw = getattr(liked, "tracks", None) or []
            title = "Мне нравится"
        else:
            uid = request.args.get("uid", "")
            kind = request.args.get("kind", "")
            if not uid.isdigit() or not kind.isdigit():
                return api_error("Некорректный идентификатор плейлиста.")
            playlists = client.users_playlists_list() or []
            playlist = next((p for p in playlists
                if str(getattr(p, "uid", "")) == uid and str(getattr(p, "kind", "")) == kind), None)
            if playlist is None:
                return api_error("Плейлист не найден в этом аккаунте.", 404)
            raw = getattr(playlist, "tracks", None) or []
            if not raw and hasattr(playlist, "fetch_tracks"):
                raw = playlist.fetch_tracks() or []
            title = getattr(playlist, "title", None) or "Плейлист"
        diagnostics["playlist_fetch_seconds"] = round(time.perf_counter() - playlist_started, 3)
        expand_diagnostics = {}
        expanded_tracks = _expand_playlist_tracks(client, raw, expand_diagnostics)
        serialization_started = time.perf_counter()
        tracks = []
        for tr in expanded_tracks:
            item = _track_item(tr)
            if item: tracks.append(item)
        diagnostics.update(expand_diagnostics)
        diagnostics["serialization_seconds"] = round(time.perf_counter() - serialization_started, 3)
        diagnostics["total_seconds"] = round(time.perf_counter() - request_started, 3)
        diagnostics["serialized_tracks"] = len(tracks)
        if not tracks:
            return jsonify({"title": title, "tracks": [], "message": "В этом плейлисте пока нет доступных треков.", "diagnostics": diagnostics})
        return jsonify({"title": title, "tracks": tracks, "diagnostics": diagnostics})
    except Exception as exc:
        return api_error("Не удалось открыть плейлист: " + str(exc)[:350], 502)


@app.get("/api/my-wave")
def api_my_wave():
    client = _get_client(state())
    if not client: return api_error("Сначала войди в Яндекс.", 401)
    try:
        # Rotor returns a finite batch. Pass already played track IDs to request
        # the next batch instead of restarting the same recommendation sequence.
        played_ids = [
            value.strip() for value in request.args.get("queue", "").split(",")
            if value.strip().isdigit()
        ][:500]
        result = client.rotor_station_tracks(
            "user:onyourwave", settings2=True, queue=played_ids or None
        )
        items = []
        for entry in (getattr(result, "sequence", None) or []):
            tr = getattr(entry, "track", None)
            if tr:
                item = _track_item(tr)
                if item: items.append(item)
        return jsonify({"title": "Моя волна", "tracks": items, "batch_id": getattr(result, "batch_id", None)})
    except Exception as exc:
        return api_error("Не удалось получить «Мою волну»: " + str(exc)[:350], 502)


def _track_match(url):
    if not _valid_yandex_url(url): return None
    return re.search(r"/album/(\d+)/track/(\d+)", url)


@app.get("/api/resolve")
def api_resolve():
    url = request.args.get("url", "")
    match = _track_match(url)
    if not match: return api_error("Нужна ссылка music.yandex.ru на отдельный трек.")
    album, track = match.group(1), match.group(2)
    client = _get_client(state())
    if not client: return api_error("Сначала войди в Яндекс.", 401)
    try:
        tracks = client.tracks([f"{track}:{album}"])
        if not tracks: raise RuntimeError("Яндекс не вернул этот трек.")
        item = _track_item(tracks[0]) or {}
        return jsonify(item)
    except Exception as exc:
        return api_error("Не удалось получить данные трека: " + str(exc)[:350], 502)


@app.post("/api/track-reaction")
def api_track_reaction():
    payload = request.get_json(silent=True) or {}
    track_id = str(payload.get("track_id", "")).strip()
    action = payload.get("action")
    if not track_id.isdigit() or action not in ("like", "dislike"):
        return api_error("Некорректный идентификатор трека или действие.")
    client = _get_client(state())
    if not client:
        return api_error("Сначала войди в Яндекс.", 401)
    try:
        if action == "like":
            ok = client.users_likes_tracks_add(track_id)
        else:
            ok = client.users_dislikes_tracks_add(track_id)
        if not ok:
            raise RuntimeError("Яндекс не подтвердил изменение оценки.")
        return jsonify({"ok": True, "action": action})
    except Exception as exc:
        return api_error("Яндекс не принял оценку: " + str(exc)[:300], 502)


@app.get("/api/track")
def api_track():
    url = request.args.get("url", "")
    match = _track_match(url)
    if not match: return api_error("Нужна ссылка music.yandex.ru на отдельный трек.")
    album, track = match.group(1), match.group(2)
    s = state(); client = _get_client(s)
    if not client: return api_error("Сначала авторизуйся через «Войти в Яндекс».", 401)
    job_id = uuid.uuid4().hex
    with s["lock"]:
        s["jobs"][job_id] = {"status": "working", "progress": "Запрашиваю информацию о треке…", "result": None, "error": None, "updated": time.time(), "timings": {}}

    def work():
        last_error = None
        for attempt in range(1, 4):
            try:
                attempt_started = time.perf_counter()
                with s["lock"]:
                    s["jobs"][job_id].update(progress=f"Получаю аудио (попытка {attempt} из 3)…", updated=time.time())
                lookup_started = time.perf_counter()
                tracks = client.tracks([f"{track}:{album}"])
                lookup_ms = round((time.perf_counter() - lookup_started) * 1000)
                if not tracks: raise RuntimeError("Яндекс не вернул этот трек. Проверь ссылку и доступность трека.")
                metadata_started = time.perf_counter()
                full = tracks[0].fetch_track() if hasattr(tracks[0], "fetch_track") else tracks[0]
                metadata_ms = round((time.perf_counter() - metadata_started) * 1000)
                title = getattr(full, "title", None) or "Неизвестный трек"
                artists = getattr(full, "artists", []) or []
                artist = ", ".join(getattr(a, "name", "") for a in artists if getattr(a, "name", None)) or "Исполнитель не определён"
                duration = getattr(full, "duration_ms", 0) or 0
                filename = s["cache_dir"] / f"{track}_{album}.mp3"
                if filename.exists() and (filename.stat().st_size < 4096 or filename.read_bytes()[:100].lstrip().lower().startswith((b"<html", b"<!doctype", b'{"error'))): filename.unlink(missing_ok=True)
                cache_hit = filename.exists()
                download_started = time.perf_counter()
                if not cache_hit: full.download(str(filename), codec="mp3", bitrate_in_kbps=192)
                download_ms = round((time.perf_counter() - download_started) * 1000)
                if not filename.exists() or filename.stat().st_size < 4096:
                    filename.unlink(missing_ok=True); raise RuntimeError("Скачанный аудиофайл пустой или слишком маленький.")
                head = filename.read_bytes()[:4]
                if not (head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)):

                    filename.unlink(missing_ok=True); raise RuntimeError("Яндекс вернул файл не в MP3-формате.")
                timings = {
                    "track_lookup_ms": lookup_ms,
                    "metadata_ms": metadata_ms,
                    "download_ms": download_ms,
                    "attempt_total_ms": round((time.perf_counter() - attempt_started) * 1000),
                    "attempt": attempt,
                    "cache_hit": cache_hit,
                    "file_size_bytes": filename.stat().st_size,
                }
                with s["lock"]:
                    s["jobs"][job_id].update(status="done", progress="Трек готов", result={"title": title, "artist": artist, "duration": duration, "cover": _cover_url(full), "src": f"/api/audio/{track}_{album}.mp3", "id": f"{track}:{album}"}, timings=timings, updated=time.time())
                return
            except Exception as exc:
                last_error = str(exc)
                if attempt < 3: time.sleep(attempt * 1.5)
        with s["lock"]:
            job = s["jobs"].get(job_id)
            if job: job.update(status="error", progress="Не удалось добавить трек", error=(last_error or "Неизвестная ошибка")[:500], updated=time.time())
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/track-status")
def api_track_status():
    job_id = request.args.get("id", "")
    s = state()
    with s["lock"]: job = s["jobs"].get(job_id)
    if not job: return api_error("Задание не найдено. Попробуй добавить трек ещё раз.", 404)
    return jsonify({k: job.get(k) for k in ("status", "progress", "result", "error", "timings")})


@app.get("/api/audio/<path:filename>")
def api_audio(filename):
    # Never accept nested paths or arbitrary file names.
    if not re.fullmatch(r"\d+_\d+\.mp3", filename): abort(404)
    s = state()
    response = make_response(send_from_directory(s["cache_dir"], filename, mimetype="audio/mpeg", conditional=True))
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Accept-Ranges"] = "bytes"
    return response


def _cleanup_loop():
    while True:
        time.sleep(600)
        cutoff = time.time() - SESSION_TTL
        with _sessions_lock:
            expired = [(sid, s) for sid, s in _sessions.items() if s["last_seen"] < cutoff]
            for sid, _ in expired: _sessions.pop(sid, None)
        for sid, s in expired:
            with s["lock"]:
                remote_browser = s.get("vk_remote_browser")
                s["vk_remote_browser"] = None
                s.update(client=None, jobs={})
            if remote_browser:
                _vk_login_release(remote_browser)
            shutil.rmtree(CACHE_ROOT / sid, ignore_errors=True)


threading.Thread(target=_cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "443"))
    cert_file = os.environ.get("PULSE_SSL_CERT", str(ROOT / "localhost+2.pem"))
    key_file = os.environ.get("PULSE_SSL_KEY", str(ROOT / "localhost+2-key.pem"))
    ssl_context = (cert_file, key_file) if Path(cert_file).is_file() and Path(key_file).is_file() else None
    if os.environ.get("PULSE_REQUIRE_HTTPS", "0") == "1" and ssl_context is None:
        raise SystemExit("HTTPS certificate files are missing. Run run_local.bat to create them.")
    app.run(host="0.0.0.0", port=port, threaded=True, ssl_context=ssl_context)
