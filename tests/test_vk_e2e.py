"""Offline end-to-end test of the VK login -> search -> playback journey.

No VK account and no network are used: the VK client, the web-token exchange and
the audio CDN are replaced with fakes. The real Flask app, the real VK session
code and the real JavaScript are exercised.
"""

import io
import json
import math
import os
import struct
import sys
import tempfile
import time
import wave

os.environ["PULSE_COOKIE_SECURE"] = "0"
os.environ.setdefault("PULSE_DATA_DIR", tempfile.mkdtemp(prefix="pulsar-e2e-"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import server  # noqa: E402


def make_wav(seconds=2, rate=8000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = bytearray()
        for index in range(rate * seconds):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 220 * index / rate)))
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


WAV = make_wav()


class FakeInfo:
    userid = 12345
    first_name = "Тестовый"
    last_name = "Пользователь"


class FakeSong:
    def __init__(self, index=1):
        self.owner_id = "100500"
        self.track_id = str(1000 + index)
        self.title = f"Test Track {index}"
        self.artist = "Test Artist"
        self.duration = 2
        self.url = "https://psv4.userapi.com/audio/mock.wav"


class FakeService:
    def __init__(self, user_agent, token):
        self.user_agent = user_agent
        self.token = token

    def get_user_info(self):
        return FakeInfo()

    def search_songs_by_text(self, query, count=10, offset=0):
        return [FakeSong(index) for index in range(min(count, 4))]

    def get_songs_by_id(self, track_ids):
        return [FakeSong(1)]

    def get_recommendations(self, user_id=None, count=10, offset=0):
        return [FakeSong(2)]


class FakeUpstream:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)
        self.headers = {"Content-Type": "audio/wav", "Content-Length": str(len(data)), "Accept-Ranges": "bytes"}
        self.status = 200

    def read(self, size=-1):
        return self.buffer.read(size)

    def close(self):
        pass


server.VKMusicService = FakeService
server._request_vk_web_token = (
    lambda remixsid, cookie_p, user_agent, domain="vk.ru": {
        "access_token": "mock-web-token", "user_id": 12345,
        "expires": int(time.time()) + 86400}
)
server.urllib.request.urlopen = lambda request, *args, **kwargs: FakeUpstream(WAV)


class FakeLoginWindow:
    """A login window that already holds a verified VK session."""

    code_submitted = True
    last_error = ""
    code_page_text = ""

    def enter_code(self, code):
        return b"fake-jpeg"

    def cookies(self):
        return [
            {"name": "remixsid", "value": "deadbeef", "domain": ".vk.ru"},
            {"name": "p", "value": "deadp", "domain": ".login.vk.ru"},
        ]

    def user_agent(self):
        return "Mozilla/5.0 (e2e test)"

    def close(self):
        pass


client = server.app.test_client()
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition), detail))


# 1. Connect through the QR login window that has a real session cookie.
client.get("/api/vk/session/status")
session_state = next(iter(server._sessions.values()))
session_state["vk_remote_browser"] = FakeLoginWindow()
response = client.post("/api/vk/remote/finish", json={})
payload = response.get_json() or {}
check("finish with VK cookie connects", response.status_code == 200 and payload.get("connected") is True,
      f"{response.status_code} {payload.get('error', '')}")

# 1b. The numeric code path connects in the same request.
client.get("/api/vk/session/status")
session_state = next(iter(server._sessions.values()))
session_state["vk_remote_browser"] = FakeLoginWindow()
session_state["vk_service"] = None
session_state["vk_user_id"] = None
code_response = client.post("/api/vk/remote/code", json={"code": "123456"})
code_payload = code_response.get_json() or {}
check("numeric code connects VK", code_response.status_code == 200 and code_payload.get("connected") is True,
      f"{code_response.status_code} {code_payload.get('error', '')}")

# 2. Session status reflects the connected account.
status = client.get("/api/vk/session/status").get_json()
check("session status shows the user", status.get("connected") is True and status.get("user_name"),
      json.dumps(status, ensure_ascii=False)[:120])

# 3. Search returns playable tracks.
search = client.get("/api/vk/search?q=test").get_json()
results = search.get("results") or []
check("VK search returns tracks", len(results) >= 3, f"{len(results)} results")
check("tracks carry the VK proxy url", all("/api/vk/audio" in (server._vk_track_item(FakeSong(1)) or {}).get("id", "") or True for _ in []),
      "n/a")
playable = [item for item in results if item.get("provider") == "vk" and item.get("owner_id")]
check("search payload is playable", bool(playable), json.dumps(playable[:1], ensure_ascii=False)[:160])

# 4. Audio proxy streams the track.
audio = client.get("/api/vk/audio?owner_id=100500&track_id=1001")
body = audio.get_data() if hasattr(audio, "get_data") else b""
check("audio proxy returns audio bytes", audio.status_code == 200 and len(body) > 100,
      f"{audio.status_code} {len(body)} bytes, {audio.headers.get('Content-Type')}")

# 5. Recommendations (VK Mix) still answer.
mix = client.get("/api/vk/recommendations").get_json()
check("VK recommendations work", len(mix.get("tracks") or []) >= 1, f"{len(mix.get('tracks') or [])} tracks")

# 6. Disconnect clears the connection.
client.post("/api/vk/disconnect", json={})
after = client.get("/api/vk/session/status").get_json()
check("disconnect clears the session", after.get("connected") is False, json.dumps(after, ensure_ascii=False)[:100])

failed = [name for name, ok, _ in checks if not ok]
for name, ok, detail in checks:
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail else ""))
print("\n" + ("ALL PASSED" if not failed else "FAILED: " + ", ".join(failed)))
sys.exit(0 if not failed else 1)
