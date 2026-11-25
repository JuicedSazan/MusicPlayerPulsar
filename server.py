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
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory, make_response, abort

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


def _new_session():
    return {
        "client": None, "auth": "idle", "auth_url": "", "auth_code": "",
        "auth_error": "", "auth_generation": 0, "jobs": {}, "last_seen": time.time(),
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
        tid = getattr(entry, "track_id", None) or getattr(entry, "id", None)
        albums = getattr(entry, "albums", None) or []
        aid = getattr(entry, "album_id", None)
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


def _valid_yandex_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and (parsed.hostname or "").lower() in ("music.yandex.ru", "music.yandex.com", "music.yandex.by", "music.yandex.kz")
    except Exception:
        return False


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
        s.update(client=None, auth="idle", auth_url="", auth_code="", auth_error="", jobs={})
        s["auth_generation"] += 1
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
            with s["lock"]: s.update(client=None, jobs={})
            shutil.rmtree(CACHE_ROOT / sid, ignore_errors=True)


threading.Thread(target=_cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8765")), threaded=True)
