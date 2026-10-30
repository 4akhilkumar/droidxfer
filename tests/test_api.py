import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from droidxfer.api.app import create_app
from droidxfer.core.context import AppContext

PORT = 8765
TOKEN = "test-token-123"


@pytest.fixture
def client(fake_adb, mac_home):
    ctx = AppContext(adb_port=fake_adb.port, local_root=mac_home)
    app = create_app(token=TOKEN, port=PORT, context=ctx, track=False)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        yield c


@pytest.fixture
def authed(client):
    r = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and "HttpOnly" in r.headers["set-cookie"]
    return client


def wait_job(c, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next(j for j in c.get("/api/transfers").json() if j["id"] == job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("transfer did not finish")


def test_requires_token(client):
    assert client.get("/api/session").status_code == 401
    assert client.get("/?token=wrong").status_code == 403
    assert client.get("/").status_code == 200  # the shell page itself is public


def test_rejects_bad_host_and_cross_site(authed):
    assert authed.get("/api/session", headers={"host": "evil.example:8765"}).status_code == 403
    assert authed.post("/api/local/mkdir", json={"parent": "~", "name": "x"}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert authed.post("/api/local/mkdir", json={"parent": "~", "name": "x"}, headers={"origin": "http://evil.example"}).status_code == 403


def test_session_and_browse(authed, mac_home):
    (mac_home / "hello.txt").write_text("hi")
    r = authed.get("/api/session")
    assert r.status_code == 200
    assert "content-security-policy" in r.headers
    body = r.json()
    assert body["devices"]["devices"][0]["id"] == "FAKE123"
    local = authed.get("/api/local/ls", params={"path": "~"}).json()
    assert [e["name"] for e in local["entries"]] == ["hello.txt"]
    remote = authed.get("/api/devices/FAKE123/ls", params={"path": "/sdcard"}).json()
    assert [e["name"] for e in remote["entries"]] == ["Download"]
    assert authed.get("/api/local/ls", params={"path": "/etc"}).status_code == 403
    assert authed.get("/api/devices/NOPE/ls").status_code == 404


def test_transfer_via_api(authed, mac_home, device_root):
    (mac_home / "doc.pdf").write_bytes(b"%PDF-1.4 test")
    job = authed.post(
        "/api/transfers",
        json={"device_id": "FAKE123", "direction": "push", "sources": [str(mac_home / "doc.pdf")], "dest_dir": "/sdcard/Download"},
    ).json()
    assert wait_job(authed, job["id"])["status"] == "completed"
    assert (device_root / "sdcard" / "Download" / "doc.pdf").read_bytes() == b"%PDF-1.4 test"


def test_upload_to_android(authed, device_root):
    upload = authed.post("/api/uploads").json()["id"]
    assert authed.put(f"/api/uploads/{upload}", params={"rel": "folder/a.txt", "mtime": 1_700_000_000_000}, content=b"abc").status_code == 200
    assert authed.put(f"/api/uploads/{upload}", params={"rel": "../escape.txt"}, content=b"x").status_code == 400
    res = authed.post(f"/api/uploads/{upload}/commit", json={"target": "android", "device_id": "FAKE123", "dest_dir": "/sdcard/Download"}).json()
    assert wait_job(authed, res["job"]["id"])["status"] == "completed"
    target = device_root / "sdcard" / "Download" / "folder" / "a.txt"
    assert target.read_bytes() == b"abc" and int(target.stat().st_mtime) == 1_700_000_000


def test_upload_to_mac(authed, mac_home):
    upload = authed.post("/api/uploads").json()["id"]
    authed.put(f"/api/uploads/{upload}", params={"rel": "x.txt"}, content=b"1")
    res = authed.post(f"/api/uploads/{upload}/commit", json={"target": "mac", "dest_dir": str(mac_home)}).json()
    assert res["placed"] == [str((mac_home / "x.txt").resolve())]


def test_preview_headers(authed, mac_home):
    (mac_home / "page.html").write_text("<script>alert(1)</script>")
    (mac_home / "pic.png").write_bytes(b"\x89PNG")
    html = authed.get("/api/local/file", params={"path": str(mac_home / "page.html")})
    assert html.headers["content-type"] == "application/octet-stream"
    assert html.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in html.headers["content-security-policy"]
    png = authed.get("/api/local/file", params={"path": str(mac_home / "pic.png")})
    assert png.headers["content-type"] == "image/png" and png.headers["content-disposition"].startswith("inline")


def test_settings_roundtrip(authed):
    r = authed.put("/api/settings", json={"default_conflict": "skip", "verify_checksum": True})
    assert r.json()["default_conflict"] == "skip"
    assert authed.put("/api/settings", json={"default_conflict": "bogus"}).status_code == 422


def test_websocket(authed):
    headers = {"host": f"127.0.0.1:{PORT}", "cookie": f"droidxfer_{PORT}={TOKEN}"}
    with authed.websocket_connect("/api/events", headers=headers) as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["devices"]["devices"][0]["id"] == "FAKE123"


def test_websocket_requires_cookie(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/events", headers={"host": f"127.0.0.1:{PORT}"}) as ws:
            ws.receive_json()
