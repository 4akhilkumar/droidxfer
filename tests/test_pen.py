import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from droidxfer.api.app import create_app
from droidxfer.core.context import AppContext
from droidxfer.core.handwriting import Attempt, Result
from droidxfer.core.notebooks import SNAPSHOT_EVERY, NoteError, NotebookStore, apply_op, blank_page
from droidxfer.core.penrender import PageRenderer, page_to_svg, pages_to_pdf, stroke_width

PORT = 8765
TOKEN = "test-token-123"


def stroke(sid, x=100, y=100, n=5, color="#112233", tool="pen"):
    return {"id": sid, "type": "stroke", "tool": tool, "color": color, "size": 3,
            "pts": [[x + i * 10, y + i * 5, 0.5] for i in range(n)]}


@pytest.fixture
def store(tmp_path):
    return NotebookStore(tmp_path / "Notes")


# ------------------------------------------------------------------ history model
def test_add_remove_undo_redo(store):
    nb = store.create("Ideas")
    page = nb.state["pages"][0]["id"]
    e1 = nb.apply({"op": "add", "page": page, "objs": [stroke("a"), stroke("b", 300)]}, actor="pad")
    assert e1.label == "Drew 2 strokes on page 1"
    assert nb.state["pages"][0]["order"] == ["a", "b"]
    nb.apply({"op": "remove", "page": page, "ids": ["a"]}, actor="pad")
    assert nb.state["pages"][0]["order"] == ["b"]

    undo = nb.undo("mac")
    assert undo.kind == "undo" and undo.label.startswith("Undo: Erased")
    assert nb.state["pages"][0]["order"] == ["a", "b"]  # z-order restored
    redo = nb.redo("mac")
    assert redo.kind == "redo"
    assert nb.state["pages"][0]["order"] == ["b"]
    # undo twice: back to empty page; a new change clears the redo stack
    nb.undo("mac")
    nb.undo("mac")
    assert nb.state["pages"][0]["order"] == []
    nb.apply({"op": "add", "page": page, "objs": [stroke("c")]}, actor="pad")
    assert nb.redo("mac") is None
    # every action is in the log, nothing rewritten
    assert [e.kind for e in nb.entries] == ["edit", "edit", "undo", "redo", "undo", "undo", "edit"]


def test_history_persists_and_reloads(store, tmp_path):
    nb = store.create("Diary")
    page = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": page, "objs": [stroke("a")]}, actor="pad")
    nb.apply({"op": "move", "page": page, "ids": ["a"], "dx": 10, "dy": -5}, actor="mac")
    nb.apply({"op": "update", "page": page, "changes": {"a": {"color": "#ff0000"}}}, actor="mac")
    again = NotebookStore(tmp_path / "Notes").get("Diary")
    obj = again.state["pages"][0]["objs"]["a"]
    assert obj["color"] == "#ff0000"
    assert obj["pts"][0][:2] == [110, 95]
    assert again.seq == nb.seq


def test_state_at_restore_and_snapshots(store, monkeypatch):
    monkeypatch.setattr("droidxfer.core.notebooks.SNAPSHOT_EVERY", 4)
    nb = store.create("Big")
    page = nb.state["pages"][0]["id"]
    for i in range(10):
        nb.apply({"op": "add", "page": page, "objs": [stroke(f"s{i}", x=i * 20)]}, actor="pad")
    assert (nb.folder / "snapshots" / "8.json").exists()
    old = nb.state_at(3)
    assert old["pages"][0]["order"] == ["s0", "s1", "s2"]
    assert nb.state_at(9)["pages"][0]["order"] == [f"s{i}" for i in range(9)]
    entry = nb.restore(3, page, "mac")
    assert entry.kind == "restore" and "#3" in entry.label
    assert nb.state["pages"][0]["order"] == ["s0", "s1", "s2"]
    # restoring is itself undoable
    nb.undo("mac")
    assert len(nb.state["pages"][0]["order"]) == 10


def test_restore_whole_notebook_and_deleted_page(store):
    nb = store.create("Pages")
    first = nb.state["pages"][0]["id"]
    second = blank_page("ruled")
    nb.apply({"op": "page_add", "page": second, "index": 1}, actor="pad")
    nb.apply({"op": "add", "page": second["id"], "objs": [stroke("x")]}, actor="pad")
    keep = nb.seq
    nb.apply({"op": "page_remove", "id": second["id"]}, actor="pad")
    assert [p["id"] for p in nb.state["pages"]] == [first]
    nb.restore(keep, second["id"], "mac")  # bring back a page that was deleted since
    assert [p["id"] for p in nb.state["pages"]] == [first, second["id"]]
    assert nb.state["pages"][1]["order"] == ["x"]
    nb.apply({"op": "page_remove", "id": first}, actor="pad")
    nb.restore(keep, None, "mac")
    assert [p["id"] for p in nb.state["pages"]] == [first, second["id"]]


def test_checkpoints_and_history_listing(store):
    nb = store.create("Plans")
    page = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": page, "objs": [stroke("a")]}, actor="pad")
    nb.checkpoint("Before redesign", "mac")
    nb.apply({"op": "add", "page": page, "objs": [stroke("b")]}, actor="pad")
    hist = nb.history()
    assert [e["kind"] for e in hist["entries"]] == ["edit", "checkpoint", "edit"]
    assert "op" not in hist["entries"][0]
    assert nb.checkpoints()[0]["name"] == "Before redesign"
    assert nb.undo("mac").label == "Undo: Drew a stroke on page 1"
    assert nb.undo("mac").label == "Undo: Drew a stroke on page 1"  # the checkpoint isn't an undo step


def test_rejects_bad_changes_without_half_applying(store):
    nb = store.create("Strict")
    page = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": page, "objs": [stroke("a")]}, actor="pad")
    before = json.dumps(nb.state)
    bad = [
        {"op": "add", "page": page, "objs": [stroke("b"), {**stroke("c"), "color": "red"}]},
        {"op": "add", "page": page, "objs": [stroke("a")]},
        {"op": "add", "page": "nope", "objs": [stroke("z")]},
        {"op": "remove", "page": page, "ids": ["missing"]},
        {"op": "update", "page": page, "changes": {"a": {"color": "#00ff00", "pts": []}}},
        {"op": "batch", "ops": [{"op": "remove", "page": page, "ids": ["a"]}, {"op": "explode"}]},
        {"op": "page_remove", "id": page},  # last page
        {"op": "add", "page": page, "objs": [{**stroke("d"), "pts": [[1, 2]]}]},
        {"op": "whatever"},
        {"op": "move", "page": page, "ids": "a", "dx": 1, "dy": 1},
    ]
    for op in bad:
        with pytest.raises(NoteError):
            nb.apply(op, actor="pad")
    assert json.dumps(nb.state) == before
    assert nb.seq == 1


def test_partial_erase_batch_is_one_undo_step(store):
    nb = store.create("Erase")
    page = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": page, "objs": [stroke("long", n=10)]}, actor="pad")
    left, right = stroke("l", n=3), stroke("r", x=180, n=3)
    entry = nb.apply({"op": "batch", "ops": [{"op": "remove", "page": page, "ids": ["long"]},
                                             {"op": "add", "page": page, "objs": [left, right]}]}, actor="pad")
    assert "Erased part of 1 stroke" in entry.label
    assert nb.state["pages"][0]["order"] == ["l", "r"]
    nb.undo("pad")
    assert nb.state["pages"][0]["order"] == ["long"]


def test_compact_keeps_content(store):
    nb = store.create("Old")
    page = nb.state["pages"][0]["id"]
    for i in range(3):
        nb.apply({"op": "add", "page": page, "objs": [stroke(f"s{i}")]}, actor="pad")
    for e in nb.entries[:2]:
        e.t -= 100 * 86400
    assert nb.compact(30) == 2
    assert nb.base_seq == 2
    with pytest.raises(NoteError):
        nb.state_at(1)
    reloaded = NotebookStore(nb.folder.parent).get("Old")
    assert reloaded.state["pages"][0]["order"] == ["s0", "s1", "s2"]
    assert reloaded.state_at(2)["pages"][0]["order"] == ["s0", "s1"]


def test_store_create_rename_delete(store, tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDXFER_HOME", str(tmp_path / "home"))
    store.create("One")
    with pytest.raises(FileExistsError):
        store.create("One")
    with pytest.raises(NoteError):
        store.create("../evil")
    renamed = store.rename("One", "Two")
    assert renamed.name == "Two" and (store.root / "Two").is_dir()
    store.delete("Two")
    assert not (store.root / "Two").exists()
    assert (tmp_path / "home" / "Trash" / "Two").is_dir()
    assert store.default().name == "My notes"


# ------------------------------------------------------------------ rendering
def test_render_png_svg_pdf(store):
    nb = store.create("Render")
    page = nb.state["pages"][0]
    nb.apply({"op": "add", "page": page["id"], "objs": [stroke("a", color="#ff0000"), stroke("h", y=300, tool="highlighter"),
                                                       {"id": "t", "type": "text", "text": "hello", "x": 50, "y": 500, "color": "#000000"}]}, actor="pad")
    page = nb.state["pages"][0]
    img = PageRenderer(nb.asset_path).render(page, scale=0.5)
    assert img.size == (500, 707)
    r, g, b, _ = img.getpixel((round(120 * 0.5), round(110 * 0.5)))
    assert r > 200 and g < 80  # the red stroke is there
    svg = page_to_svg(page)
    assert svg.count("<g ") == 2 and "hello" in svg
    pdf = pages_to_pdf(PageRenderer(), [page], scale=0.5)
    assert pdf.startswith(b"%PDF")
    assert stroke_width("pen", 4, 1.0) == 4 and stroke_width("highlighter", 4, 0.1) == 10


# ------------------------------------------------------------------ web API, security, live flow
@pytest.fixture
def client(fake_adb, mac_home):
    ctx = AppContext(adb_port=fake_adb.port, local_root=mac_home)
    app = create_app(token=TOKEN, port=PORT, context=ctx, track=False)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        c.ctx = ctx
        yield c


@pytest.fixture
def authed(client):
    assert client.get(f"/?token={TOKEN}", follow_redirects=False).status_code == 303
    return client


def test_pad_token_scope(authed, client, fake_adb):
    assert client.get("/pad?k=nope", follow_redirects=False).status_code == 403
    status = authed.post("/api/pen/connect", json={"device_id": "FAKE123"}).json()
    assert status["phone"]["serial"] == "FAKE123"
    assert fake_adb.reverses == {f"tcp:{PORT}": f"tcp:{PORT}"}
    assert any("am start -a android.intent.action.VIEW" in c and f"localhost:{PORT}/pad?k=" in c for c in fake_adb.shell_log)
    token = client.ctx.pen.pad_token

    pad = TestClient(client.app, base_url=f"http://localhost:{PORT}")
    nav = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    r = pad.get(f"/pad?k={token}", follow_redirects=False, headers=nav)  # Chrome opened by `am start`
    assert r.status_code == 303 and f"droidxfer_pad_{PORT}=" in r.headers["set-cookie"]
    cookie = f"droidxfer_pad_{PORT}={token}"
    headers = {"cookie": cookie, "host": f"localhost:{PORT}"}
    assert pad.get("/pad", headers=nav).status_code == 200
    # cross-site is still refused for anything else
    assert pad.get("/pen", headers=nav).status_code == 403
    assert pad.post("/pad", headers=nav).status_code == 403
    # the pad token opens the pad's own endpoints only
    assert pad.get("/api/session", headers=headers).status_code == 401
    assert pad.get("/api/local/ls", params={"path": "~"}, headers=headers).status_code == 401
    assert pad.get("/api/pen/notebooks", headers=headers).status_code == 401
    with pad.websocket_connect("/api/pen/pad/ws", headers=headers) as ws:
        assert ws.receive_json()["t"] == "state"

    authed.post("/api/pen/disconnect")
    assert fake_adb.reverses == {}
    with pytest.raises(Exception):
        with pad.websocket_connect("/api/pen/pad/ws", headers=headers) as ws:
            ws.receive_json()


def test_mtp_only_phone_cannot_open_pad(authed, monkeypatch):
    from droidxfer.core.devices import DeviceUnavailable

    def mtp_transport(device_id, kind=None):
        class T:
            kind = "mtp"
            serial = "mtp:1"
        return T()

    monkeypatch.setattr(authed.ctx.devices, "transport", mtp_transport)
    r = authed.post("/api/pen/connect", json={"device_id": "FAKE123"})
    assert r.status_code == 409 and "USB debugging" in r.json()["detail"]
    assert DeviceUnavailable


def recv(ws):
    """Next message, skipping presence updates."""
    while True:
        msg = ws.receive_json()
        if msg["t"] != "status":
            return msg


def ws_headers():
    return {"cookie": f"droidxfer_{PORT}={TOKEN}", "host": f"127.0.0.1:{PORT}"}



def test_delete_last_page_swaps_in_a_new_page(store):
    nb = store.create("Last page")
    old = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": old, "objs": [stroke("a")]}, actor="pad")
    fresh = blank_page("grid")
    entry = nb.apply({"op": "batch", "ops": [{"op": "page_add", "page": fresh, "index": 1},
                                             {"op": "page_remove", "id": old}]}, actor="mac")
    assert entry.label == "Deleted page 1 and started a new grid page"
    assert [(p["id"], p["template"], p["order"]) for p in nb.state["pages"]] == [(fresh["id"], "grid", [])]
    nb.undo("mac")  # one undo brings the old page back, with its drawing
    assert [(p["id"], p["order"]) for p in nb.state["pages"]] == [(old, ["a"])]
    with pytest.raises(NoteError):  # a plain delete of the only page is still refused
        nb.apply({"op": "page_remove", "id": old}, actor="mac")


def test_live_current_page_follows_last_page_swap(authed):
    with authed.websocket_connect("/api/pen/ws", headers=ws_headers()) as mac:
        old = mac.receive_json()["current"]["page"]
        fresh = blank_page("ruled")
        mac.send_json({"t": "op", "cid": "c1", "op": {"op": "batch", "ops": [
            {"op": "page_add", "page": fresh, "index": 1}, {"op": "page_remove", "id": old}]}})
        applied = recv(mac)
        assert applied["t"] == "applied" and applied["current"]["page"] == fresh["id"]

def test_live_flow_two_peers(authed):
    with authed.websocket_connect("/api/pen/ws", headers=ws_headers()) as mac, \
            authed.websocket_connect("/api/pen/ws", headers=ws_headers()) as pad:
        state = mac.receive_json()
        assert state["t"] == "state" and state["notebook"]["name"] == "My notes"
        page = state["current"]["page"]
        assert recv(pad)["t"] == "state"
        pad.send_json({"t": "live", "id": "s1", "page": page, "tool": "pen", "color": "#000000", "size": 3, "pts": [[1, 2, 0.5]]})
        assert recv(mac)["t"] == "live"
        pad.send_json({"t": "op", "cid": "c1", "op": {"op": "add", "page": page, "objs": [stroke("s1")]}})
        for ws in (mac, pad):
            applied = recv(ws)
            assert applied["t"] == "applied" and applied["entry"]["label"] == "Drew a stroke on page 1"
            assert "inv" not in applied["entry"]
        # a resend after a reconnect is acknowledged, not applied twice
        pad.send_json({"t": "op", "cid": "c1", "op": {"op": "add", "page": page, "objs": [stroke("s1")]}})
        assert recv(pad) == {"t": "ack", "cid": "c1", "seq": 1}
        pad.send_json({"t": "op", "cid": "c2", "op": {"op": "add", "page": "nope", "objs": [stroke("s2")]}})
        rejected = recv(pad)
        assert rejected["t"] == "rejected" and rejected["cid"] == "c2"
        mac.send_json({"t": "undo"})
        undo = recv(pad)
        assert undo["entry"]["kind"] == "undo" and undo["can_redo"] is True
        pad.send_json({"t": "action", "id": "a1", "name": "list_notebooks"})
        msg = recv(pad)
        while msg["t"] != "result":
            msg = recv(pad)
        assert msg["ok"] and msg["result"][0]["name"] == "My notes"


def test_notebook_rest_history_export(authed):
    nb = authed.post("/api/pen/notebooks", json={"name": "Work", "template": "grid"}).json()
    assert nb["name"] == "Work"
    snap = authed.get("/api/pen/notebooks/Work").json()
    page = snap["pages"][0]["id"]
    assert snap["pages"][0]["template"] == "grid"
    hub = authed.ctx.pen
    entry = hub.notebook().apply({"op": "add", "page": page, "objs": [stroke("a")]}, actor="pad")
    authed.post("/api/pen/notebooks/Work/checkpoint", json={"name": "v1"})
    hist = authed.get("/api/pen/notebooks/Work/history").json()
    assert hist["checkpoints"][0]["name"] == "v1" and hist["entries"][-1]["seq"] == entry.seq
    assert authed.get("/api/pen/notebooks/Work/state", params={"seq": 0}).json()["pages"][0]["order"] == []
    r = authed.post("/api/pen/notebooks/Work/restore", json={"seq": 0, "page": page})
    assert r.status_code == 200 and r.json()["kind"] == "restore"
    thumb = authed.get("/api/pen/notebooks/Work/thumb", params={"page": page, "seq": 1, "w": 100})
    assert thumb.headers["content-type"] == "image/png"
    png = authed.get("/api/pen/notebooks/Work/export", params={"format": "png", "page": page})
    assert png.content.startswith(b"\x89PNG") and "attachment" in png.headers["content-disposition"]
    assert authed.get("/api/pen/notebooks/Work/export", params={"format": "pdf"}).content.startswith(b"%PDF")
    assert b"<svg" in authed.get("/api/pen/notebooks/Work/export", params={"format": "svg", "page": page}).content
    gif = authed.get("/api/pen/notebooks/Work/export", params={"format": "gif", "page": page})
    assert gif.content.startswith(b"GIF8")
    ops = authed.get("/api/pen/notebooks/Work/ops", params={"page": page}).json()["ops"]
    assert [o["seq"] for o in ops] == [1, 3]


def test_pictures_annotate_and_save_copy(authed, mac_home):
    buf = io.BytesIO()
    Image.new("RGB", (400, 200), (10, 200, 30)).save(buf, "JPEG")
    (mac_home / "photo.jpg").write_bytes(buf.getvalue())
    res = authed.post("/api/pen/annotate", json={"source": "mac", "path": str(mac_home / "photo.jpg")}).json()
    nb = authed.ctx.pen.notebook()
    page = next(p for p in nb.state["pages"] if p["id"] == res["page"])
    assert page["bg"]["asset"] == res["asset"] and page["h"] == 500
    assert authed.get(f"/api/pen/notebooks/{nb.id}/asset/{res['asset']}").content == (nb.folder / "assets" / res["asset"]).read_bytes()
    nb.apply({"op": "add", "page": page["id"], "objs": [stroke("mark")]}, actor="pad")
    saved = authed.post("/api/pen/action", json={"name": "save_copy", "args": {"page": page["id"]}}).json()["result"]
    assert saved["where"] == "mac"
    out = mac_home / "photo (annotated).png"
    assert out.exists() and Image.open(out).size == (2000, 1000)
    # pictures uploaded from the Mac view
    ins = authed.post(f"/api/pen/notebooks/{nb.id}/pictures", params={"mode": "insert", "page": page["id"]},
                      content=buf.getvalue())
    assert ins.status_code == 200
    assert authed.post(f"/api/pen/notebooks/{nb.id}/pictures", content=b"not a picture").status_code == 400


def test_stickers_and_search(authed):
    hub = authed.ctx.pen
    nb = hub.notebook()
    page = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": page, "objs": [stroke("sig"), {"id": "t1", "type": "text", "text": "Quarterly budget",
                                                                  "x": 0, "y": 0}]}, actor="pad")
    hub.save_sticker("Signature", page, ["sig"])
    assert [s["name"] for s in authed.get("/api/pen/stickers").json()] == ["Signature"]
    hub.insert_sticker("Signature", page, "mac")
    assert nb.state["pages"][0]["objs"][nb.state["pages"][0]["order"][-1]]["type"] == "image"
    hits = authed.get("/api/pen/search", params={"q": "budget quarterly"}).json()
    assert hits and hits[0]["page_no"] == 1
    authed.patch(f"/api/pen/notebooks/{nb.id}", json={"exclude_search": True})
    assert authed.get("/api/pen/search", params={"q": "budget"}).json() == []


def test_pen_settings_validation(authed, monkeypatch):
    monkeypatch.setattr("droidxfer.core.handwriting.gemma_models", lambda timeout=2.0: ["gemma4:12b"])
    s = authed.get("/api/pen/settings").json()
    assert s["hw_mode"] == "auto" and "Any app" in s["shortcuts"]
    assert authed.put("/api/pen/settings", json={"hw_mode": "gemma"}).json()["hw_mode"] == "gemma"
    assert authed.put("/api/pen/settings", json={"hw_mode": "magic"}).status_code == 422
    assert authed.put("/api/pen/settings", json={"shortcuts": {"X": [["Bad", "cmd+nosuchkey"]]}}).status_code == 400
    assert authed.put("/api/pen/settings", json={"browser": "com.android.chrome; reboot"}).status_code == 400


def test_handwriting_fallback(monkeypatch):
    from droidxfer.core import handwriting, macbridge

    png = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(png, "PNG")
    calls = []
    monkeypatch.setattr(macbridge, "vision_recognize",
                        lambda data: calls.append("vision") or [macbridge.TextLine("he1lo", 0.3, (0, 0, 1, 1))])
    monkeypatch.setattr(handwriting, "_post", lambda path, body, timeout: calls.append(body["model"]) or {"response": "hello\n"})
    r = handwriting.recognize_sync(png.getvalue(), "auto", "gemma4:12b")
    assert r.text == "hello" and r.engine == "gemma:gemma4:12b" and calls == ["vision", "gemma4:12b"]
    calls.clear()
    r = handwriting.recognize_sync(png.getvalue(), "vision", "gemma4:12b")
    assert r.text == "he1lo" and calls == ["vision"]

    def down(path, body, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(handwriting, "_post", down)
    r = handwriting.recognize_sync(png.getvalue(), "auto", "gemma4:12b")
    assert r.text == "he1lo" and r.engine == "vision"  # keep the weak answer rather than nothing


def test_order_lines_reading_order():
    from droidxfer.core.macbridge import TextLine, order_lines

    lines = [TextLine("second", 1, (0.1, 0.5, 0.3, 0.1)), TextLine("right", 1, (0.6, 0.1, 0.3, 0.1)),
             TextLine("left", 1, (0.1, 0.12, 0.3, 0.1))]
    assert [l.text for l in order_lines(lines)] == ["left", "right", "second"]


def test_apply_op_inverse_roundtrip():
    state = {"pages": [blank_page()]}
    pid = state["pages"][0]["id"]
    ops = [
        {"op": "add", "page": pid, "objs": [stroke("a"), stroke("b")]},
        {"op": "move", "page": pid, "ids": ["a"], "dx": 5, "dy": 5},
        {"op": "update", "page": pid, "changes": {"b": {"color": "#00ff00", "size": 7}}},
        {"op": "page_update", "id": pid, "template": "dot"},
        {"op": "page_add", "page": blank_page(), "index": 0},
        {"op": "remove", "page": pid, "ids": ["a"]},
    ]
    snapshots = [json.dumps(state, sort_keys=True)]
    inverses = []
    for op in ops:
        inverses.append(apply_op(state, json.loads(json.dumps(op))))
        snapshots.append(json.dumps(state, sort_keys=True))
    for inv, snap in zip(reversed(inverses), reversed(snapshots[:-1])):
        apply_op(state, inv)
        assert json.dumps(state, sort_keys=True) == snap
    assert SNAPSHOT_EVERY == 500


def test_clean_model_text():
    from droidxfer.core.handwriting import clean_model_text

    raw = "```\nHi! I am Akhil.\n\nI am 25 Yrs old.\n\n~~Gen On Dec 3~~\non Dec 3rd $\\rightarrow$ **DOB**.\n```"
    assert clean_model_text(raw) == "Hi! I am Akhil.\nI am 25 Yrs old.\non Dec 3rd -> DOB."


def test_auto_asks_gemma_when_vision_is_only_fairly_sure(monkeypatch):
    from droidxfer.core import handwriting, macbridge

    png = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(png, "PNG")
    # real S Pen handwriting: Vision said 0.73 and was wrong on 8 of ~30 words
    monkeypatch.setattr(macbridge, "vision_recognize", lambda data: [macbridge.TextLine("Jam 25 Ys old", 0.73, (0, 0, 1, 1))])
    monkeypatch.setattr(handwriting, "_post", lambda path, body, timeout: {"response": "I am 25 Yrs old"})
    r = handwriting.recognize_sync(png.getvalue(), "auto", "gemma4:12b")
    assert r.engine == "gemma:gemma4:12b" and r.text == "I am 25 Yrs old"


# ------------------------------------------------------------------ background handwriting reading
@pytest.fixture
def reader(store, tmp_path, monkeypatch):
    """A hub whose readers are fakes: Vision misreads "budget", Gemma gets it right."""
    from droidxfer.core import handwriting, macbridge, penindex
    from droidxfer.core.penpad import PenHub, PenSettings

    calls = {"vision": 0, "gemma": 0}
    fake = {"vision": Attempt("vision", "bugdet plan", 0.5),
            "gemma": Attempt("gemma:gemma4:12b", "budget plan"), "power": True}

    def vision(png):
        calls["vision"] += 1
        return fake["vision"]

    def gemma(png, model):
        calls["gemma"] += 1
        return fake["gemma"]

    monkeypatch.setattr(handwriting, "read_vision", vision)
    monkeypatch.setattr(handwriting, "read_gemma", gemma)
    monkeypatch.setattr(macbridge, "on_ac_power", lambda: fake["power"])
    monkeypatch.setattr(penindex, "VISION_IDLE", 0.0)
    monkeypatch.setattr(penindex, "GEMMA_IDLE", 0.0)
    monkeypatch.setattr(penindex, "POWER_CHECK", 0.0)
    hub = PenHub(None, None, None, store, PenSettings(tmp_path / "pen.json"))  # type: ignore[arg-type]
    nb = store.create("Ideas")
    page = nb.state["pages"][0]["id"]
    nb.apply({"op": "add", "page": page, "objs": [stroke("a")]}, actor="pad")
    hub.index.changed(nb.id, [page])
    return hub, nb, page, calls, fake


async def test_index_reads_with_vision_then_gemma_and_searches_both(reader):
    hub, nb, page, calls, _ = reader
    await hub.index.step()
    assert calls == {"vision": 1, "gemma": 1}
    hit = hub.search("budget")
    assert hit and hit[0]["snippet"] == "budget plan"  # Gemma's reading is preferred for the snippet
    assert hub.search("bugdet")  # Vision's reading still matches
    await hub.index.step()
    assert calls == {"vision": 1, "gemma": 1}  # an unchanged page is never read twice
    nb.apply({"op": "add", "page": page, "objs": [stroke("b", y=300)]}, actor="pad")
    hub.index.changed(nb.id, [page])
    await hub.index.step()
    assert calls == {"vision": 2, "gemma": 2}


async def test_index_gemma_text_is_ignored_once_the_page_changes(reader):
    hub, nb, page, calls, fake = reader
    await hub.index.step()
    fake["power"] = False  # Gemma can't refresh its reading now
    nb.apply({"op": "add", "page": page, "objs": [stroke("b", y=300)]}, actor="pad")
    hub.index.changed(nb.id, [page])
    await hub.index.step()
    assert not hub.search("budget") and hub.search("bugdet")


async def test_index_follows_the_engine_setting(reader):
    hub, nb, page, calls, fake = reader
    hub.settings.update(hw_mode="vision")
    await hub.index.step()
    assert calls == {"vision": 1, "gemma": 0}
    fake["vision"] = Attempt("vision", "bugdet plan", 0.97)
    hub.settings.update(hw_mode="auto")
    hub.index.changed(nb.id, [page])
    hub.index._file(nb, page).unlink()
    await hub.index.step()
    assert calls == {"vision": 2, "gemma": 0}  # Auto: Vision was sure, so no Gemma
    hub.settings.update(hw_mode="gemma")
    hub.index.rescan_soon()
    await hub.index.step()
    assert calls == {"vision": 2, "gemma": 1}  # Gemma: every page gets a Gemma reading


async def test_index_gemma_waits_for_power_ollama_and_the_user(reader):
    hub, nb, page, calls, fake = reader
    fake["power"] = False
    await hub.index.step()
    assert calls["gemma"] == 0 and hub.index.status()["paused"] == "battery"
    assert hub.index.status()["gemma_pending"] == 1
    fake["power"] = True
    with hub.index.user_reading():
        await hub.index.step()
    assert calls["gemma"] == 0  # steps aside while the user reads a page
    fake["gemma"] = Attempt("gemma", "", None, 0, "Ollama isn't reachable on http://127.0.0.1:11434 (refused)")
    await hub.index.step()
    assert calls["gemma"] == 1 and hub.index.status() == {
        "mode": "auto", "gemma_pending": 1, "vision_pending": 0, "paused": "ollama"}
    await hub.index.step()
    assert calls["gemma"] == 1  # waits a while before trying Ollama again


async def test_index_keeps_what_the_user_read(reader):
    hub, nb, page, calls, _ = reader
    pg = nb.state["pages"][0]
    hub.index.remember(nb, pg, Result("budget plan", "gemma:gemma4:12b", None, 0, [
        Attempt("vision", "bugdet plan", 0.5), Attempt("gemma:gemma4:12b", "budget plan")]))
    hub.index.vision_queue.clear()
    await hub.index.step()
    assert calls == {"vision": 0, "gemma": 0} and hub.search("budget")


def test_index_reads_old_records(store, tmp_path):
    from droidxfer.core.penpad import PenHub, PenSettings

    hub = PenHub(None, None, None, store, PenSettings(tmp_path / "pen.json"))  # type: ignore[arg-type]
    nb = store.create("Old")
    page = nb.state["pages"][0]["id"]
    (nb.folder / "text").mkdir()
    (nb.folder / "text" / f"{page}.json").write_text(json.dumps({"seq": 1, "text": "shopping list"}))
    assert hub.search("shopping")[0]["page_no"] == 1
