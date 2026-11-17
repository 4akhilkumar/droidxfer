"""Phone log (logcat): parsing, plain-words notes, phone-side commands, search/paging/histogram, API, live tail."""

import asyncio
import re
from datetime import datetime

import pytest

from droidxfer.core import logs
from droidxfer.core.logs import LogLine, PhoneOptions, explain, mask_addresses, parse_line, query_from_params
from droidxfer.core.logviewer import LogQueryError
from droidxfer.core.remotefs import RemoteOpError

from test_api import PORT, TOKEN, authed, client  # noqa: F401  (fixtures)

WS_HEADERS = {"host": f"127.0.0.1:{PORT}", "cookie": f"droidxfer_{PORT}={TOKEN}"}
ALL = {"range": "all"}


def _line(message: str, tag: str = "Tag", level: str = "I") -> LogLine:
    return LogLine(1700000000.0, 1, 1, level, tag, message)


# parsing ------------------------------------------------------------------------------
def test_parse_line():
    line = parse_line("         1794927312.505  8716  8782 D libdsm  : Sending netbios packet: to 192.168.0.102")
    assert line == LogLine(1794927312.505, 8716, 8782, "D", "libdsm", "Sending netbios packet: to 192.168.0.102")
    assert parse_line("--------- beginning of main") is None
    assert parse_line("") is None
    assert parse_line("  1700000000.000     1     1 A DEBUG: abort").level == "F"  # assert -> fatal


def test_mask_addresses():
    assert mask_addresses("to AA:BB:CC:DD:EE:FF now") == "to XX:XX:XX:XX:EE:FF now"
    assert mask_addresses("aa:bb:cc:dd:f1:26STATE_CONNECTED") == "XX:XX:XX:XX:f1:26STATE_CONNECTED"
    assert mask_addresses("time 12:34:56") == "time 12:34:56"
    assert mask_addresses("XX:XX:XX:XX:F1:26 already") == "XX:XX:XX:XX:F1:26 already"


# plain-words notes --------------------------------------------------------------------
@pytest.mark.parametrize(
    "message, expected",
    [
        ("dispatchMediaKeyEvent, pkg=com.android.bluetooth pid=1, event=KeyEvent { action=ACTION_DOWN, "
         "keyCode=KEYCODE_MEDIA_PAUSE, scanCode=0 }", "Headphones (Bluetooth) sent Pause"),
        ("dispatchMediaKeyEvent, pkg=com.example.player pid=1, event=KeyEvent { action=ACTION_DOWN, "
         "keyCode=KEYCODE_MEDIA_NEXT }", "com.example.player sent Next track"),
        ("dispatchMediaKeyEvent, pkg=com.android.bluetooth pid=1, event=KeyEvent { action=ACTION_DOWN, "
         "keyCode=KEYCODE_HEADSETHOOK }", "Headphones (Bluetooth) sent the headset button"),
        ("Update PlaybackStatus 2 -> 1", "Playback paused → playing (as reported to the headphones)"),
        ("[From Service] HEADSET connection state changed: XX:XX:XX:XX:F1:26STATE_CONNECTED -> STATE_DISCONNECTING",
         "Calls/microphone disconnecting"),
        ("Active Device Changed: null for profile: A2DP", "Music audio: no active device"),
        ("requestAudioFocus() from uid/pid 1/2 AA=USAGE_MEDIA/CONTENT_TYPE_MUSIC clientId=x callingPack=org.videolan.vlc req=1",
         "org.videolan.vlc started playing media"),
        ("abandonAudioFocus() from uid/pid 1/2 clientId=x callingPack=com.android.systemui", "System sounds finished using audio"),
        ("ANR in com.example.game (com.example.game/.Main)", "com.example.game stopped responding (ANR)"),
        ("Process com.example.game (pid 123) has died: fg TOP", "com.example.game was stopped by Android"),
        ("nothing interesting here", None),
    ],
)
def test_explain(message, expected):
    assert explain(_line(message)) == expected


def test_explain_toggle_warns_about_inversion():
    note = explain(_line("dispatchMediaKeyEvent, pkg=com.google.android.googlequicksearchbox pid=6682, "
                         "event=KeyEvent { action=ACTION_DOWN, keyCode=KEYCODE_MEDIA_PLAY_PAUSE }"))
    assert note.startswith("Google app (Assistant) sent a Play/Pause toggle")
    assert "inverts" in note


def test_key_release_and_crash_lines():
    assert explain(_line("dispatchMediaKeyEvent, pkg=com.android.bluetooth event=KeyEvent { action=ACTION_UP, "
                         "keyCode=KEYCODE_MEDIA_PAUSE }")) is None
    assert explain(_line("FATAL EXCEPTION: main", tag="AndroidRuntime")).startswith("An app crashed")
    assert explain(_line("Process: com.example.game, PID: 12345", tag="AndroidRuntime")) == "Crashed app: com.example.game"


# options and phone commands -------------------------------------------------------------
@pytest.mark.parametrize("kwargs", [{"preset": "nope"}, {"app": "not a package; rm -rf /"}])
def test_options_reject_bad_input(kwargs):
    with pytest.raises(LogQueryError):
        PhoneOptions(**kwargs)


@pytest.mark.parametrize("params", [{"range": "2y"}, {"range": "custom"}, {"q": "/[unclosed/"}])
def test_query_rejects_bad_input(params):
    with pytest.raises(LogQueryError):
        query_from_params(params)


def test_query_levels_are_phone_levels():
    assert query_from_params({"levels": "W,E,INFO,X"}).levels == ["W", "E"]
    assert query_from_params({}).range_key == logs.DEFAULT_RANGE


def test_phone_commands_are_quoted():
    opts = PhoneOptions(preset="headphones")
    command = opts.logcat_command(10364, dump=True, since="1700000000.000", levels=["E", "W"])
    assert command == ("logcat -d -v threadtime -v epoch -b main,system,crash --uid=10364 "
                       "-T \"1700000000.000\" '*:W'")  # filterspec (lowest level) after the options
    grep = opts.phone_grep(["it's here"], case_sensitive=False)
    assert grep.startswith(" | grep -F -e opcode=Opcode::PASS_THROUGH -e 'Update PlaybackStatus' ")
    assert grep.endswith(""" | grep -iF -e 'it'"'"'s here'""")
    assert PhoneOptions().phone_grep([], True) == ""
    assert PhoneOptions().phone_grep(["Exact"], True) == " | grep -F -e Exact"


@pytest.mark.parametrize("preset", list(logs.PRESETS))
def test_preset_terms_cover_the_regex(preset):
    """The phone-side fixed strings must let through every line the preset's regex would match."""
    p = logs.PRESETS[preset]
    samples = {
        "headphones": ["opcode=Opcode::PASS_THROUGH", "Update PlaybackStatus 1 -> 2", "dispatchMediaKeyEvent, pkg=x",
                       "A2DP connection state changed: a -> b", "Active Device Changed: x for profile: A2DP"],
        "bluetooth": ["Bond state changed", "bondState=12", "bond_state", "BondState", "Bondstate", "bond_State",
                      "ACL disconnected for x", "ACL connected"],
        "crashes": ["FATAL EXCEPTION: main", "AndroidRuntime: x", "ANR in com.x", "Fatal signal 11", "has died: fg"],
        "audio": ["requestAudioFocus() from uid", "abandonAudioFocus() from uid"],
    }[preset]
    for text in samples:
        assert re.search(p.grep, text), text
        assert any(t in text for t in p.terms), text


# searching the (fake) phone -------------------------------------------------------------------
async def test_search_headphones_preset(ctx, fake_adb):
    res = await ctx.logs.search("FAKE123", ALL, PhoneOptions(preset="headphones"))
    notes = [e["note"] for e in res["entries"]]
    assert notes == [  # newest first
        "Music audio connected",
        "Google app (Assistant) sent a Play/Pause toggle: it flips whatever is happening, so a missed one inverts later ones",
        "Playback playing → paused (as reported to the headphones)",
        "Headphones (Bluetooth) sent Pause",  # the ACTION_UP twin is dropped
        "Headphones sent a button signal over Bluetooth",
    ]  # adbd echoing droidxfer's own grep command is not a match
    assert res["matched"] == 5 and res["next_cursor"] is None
    assert res["phone_time"] == fake_adb.log_now
    first = res["entries"][0]
    assert first["group"] == "com.android.bluetooth" and first["stream"] == "bluetooth"
    assert "XX:XX:XX:XX:F1:26STATE_CONNECTING" in first["message"]
    assert first["severity"] == "info" and first["raw"].startswith("2023-11-")
    assert res["histogram"]["total"] == 5 and res["histogram"]["by_level"]["I"] == 3
    assert {g["name"] for g in res["groups"]} == {"com.android.bluetooth", "system_server"}
    command = fake_adb.shell_log[-1]
    assert command.startswith("echo @@now") and "ps -A -o PID,NAME" in command and "| grep -F -e " in command


async def test_search_pattern_levels_groups_and_paging(ctx, fake_adb):
    opts = PhoneOptions(mask=False)
    res = await ctx.logs.search("FAKE123", ALL | {"levels": "E,W"}, opts)
    assert [e["level"] for e in res["entries"]] == ["W", "E", "E", "E"]
    assert res["entries"][0]["message"] == "connected to AA:BB:CC:DD:EE:FF"
    assert "'*:W'" in fake_adb.shell_log[-1]

    res = await ctx.logs.search("FAKE123", ALL | {"q": 'game -"FATAL"'}, opts)
    assert [e["message"] for e in res["entries"]] == ["Process: com.example.game, PID: 12345", "ANR in com.example.game"]
    assert res["highlight"] == [{"type": "text", "value": "game"}]
    assert "| grep -iF -e game" in fake_adb.shell_log[-1]  # plain required terms are pre-filtered on the phone

    res = await ctx.logs.search("FAKE123", ALL | {"groups": "com.example.game"}, opts)
    assert {e["group"] for e in res["entries"]} == {"com.example.game"}
    unknown = await ctx.logs.search("FAKE123", ALL | {"groups": "pid 3000"}, opts)
    assert [e["stream"] for e in unknown["entries"]] == ["WifiService"]  # not in ps: "pid N"

    page1 = await ctx.logs.search("FAKE123", ALL | {"limit": "4"}, opts)
    assert len(page1["entries"]) == 4 and page1["next_cursor"]
    calls = len(fake_adb.shell_log)
    page2 = await ctx.logs.search("FAKE123", ALL | {"limit": "4", "cursor": page1["next_cursor"]}, opts)
    assert len(fake_adb.shell_log) == calls  # served from the cached result
    assert page2["entries"][0]["id"] == 4 and "histogram" not in page2
    stale = await ctx.logs.search("FAKE123", ALL | {"limit": "4", "cursor": "deadbeef:4"}, opts)
    assert stale["cursor_reset"] is True


async def test_search_time_range_notes_only_and_app(ctx, fake_adb):
    start = datetime.fromtimestamp(1700000004).strftime("%Y-%m-%dT%H:%M:%S")
    end = datetime.fromtimestamp(1700000005).strftime("%Y-%m-%dT%H:%M:%S")
    res = await ctx.logs.search("FAKE123", {"range": "custom", "start": start, "end": end}, PhoneOptions())
    assert [e["message"] for e in res["entries"]] == [  # the end second is included up to .999
        "Process: com.example.game, PID: 12345", "FATAL EXCEPTION: main", "ANR in com.example.game"]
    assert '-T "1700000004.000"' in fake_adb.shell_log[-1]

    res = await ctx.logs.search("FAKE123", ALL, PhoneOptions(notes_only=True))
    assert res["entries"] and all(e["note"] for e in res["entries"])
    assert res["matched"] == 9

    await ctx.logs.search("FAKE123", ALL, PhoneOptions(app="com.example.game"))
    assert "--uid=1016" in fake_adb.shell_log[-1]
    with pytest.raises(RemoteOpError, match="not installed"):
        await ctx.logs.search("FAKE123", ALL, PhoneOptions(app="com.example.missing"))


async def test_crashes_preset_skips_memory_cleanup(ctx, fake_adb):
    fake_adb.logcat_lines.append(
        "         1700000008.000  2945  6948 I ActivityManager: Process com.example.cached (pid 9) has died: cch+99 SVC")
    res = await ctx.logs.search("FAKE123", ALL, PhoneOptions(preset="crashes"))
    assert [e["note"] for e in res["entries"]] == [
        "Crashed app: com.example.game",
        "An app crashed (the lines below show where)",
        "com.example.game stopped responding (ANR)",
    ]


async def test_download_uses_the_cached_search(ctx, fake_adb):
    res = await ctx.logs.search("FAKE123", ALL, PhoneOptions(preset="audio"))
    calls = len(fake_adb.shell_log)
    text = "".join(await ctx.logs.download("FAKE123", ALL | {"qid": res["qid"]}, PhoneOptions(preset="audio")))
    assert len(fake_adb.shell_log) == calls
    assert text.endswith("<-- System sounds played a notification sound, interrupting other audio\n")


async def test_stream_and_clear(ctx, fake_adb):
    seen = []

    async def follow():
        async for entry in ctx.logs.stream("FAKE123", {"levels": "E"}, PhoneOptions()):
            seen.append(entry)
            if len(seen) == 3:
                return

    await asyncio.wait_for(follow(), 5)
    assert [e["group"] for e in seen] == ["system_server", "com.example.game", "com.example.game"]
    assert fake_adb.shell_log[-1].startswith("logcat -v threadtime -v epoch")
    assert '-T "$(date +%s).000"' in fake_adb.shell_log[-1]

    await ctx.logs.clear("FAKE123")
    assert fake_adb.logcat_lines == []
    assert (await ctx.logs.search("FAKE123", ALL, PhoneOptions()))["entries"] == []


# API ----------------------------------------------------------------------------------
def test_api_meta(authed):
    body = authed.get("/api/logs/phone/meta").json()
    assert [p["id"] for p in body["presets"]] == ["headphones", "bluetooth", "crashes", "audio"]
    assert [lv["id"] for lv in body["levels"]] == list("VDIWEF")
    assert body["default_range"] == logs.DEFAULT_RANGE


def test_api_search_download_and_errors(authed):
    r = authed.get("/api/devices/FAKE123/logs", params={"preset": "audio", "range": "all"})
    assert r.status_code == 200
    body = r.json()
    assert body["entries"][0]["note"].startswith("System sounds") and body["histogram"]["total"] == 1
    r = authed.get("/api/devices/FAKE123/logs", params={"action": "download", "range": "all", "qid": body["qid"]})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "requestAudioFocus" in r.text
    assert authed.get("/api/devices/FAKE123/logs", params={"range": "nope"}).status_code == 400
    assert authed.get("/api/devices/FAKE123/logs", params={"app": "com.example.missing"}).status_code == 400
    assert authed.get("/api/devices/FAKE123/logs", params={"action": "explode"}).status_code == 400
    assert authed.get("/api/devices/NOPE/logs").status_code == 404


def test_api_clear(authed, fake_adb):
    assert authed.post("/api/devices/FAKE123/logs/clear").json() == {"ok": True}
    assert fake_adb.logcat_lines == []


def test_api_live_tail(authed):
    with authed.websocket_connect("/api/devices/FAKE123/logs/live?preset=headphones&q=pause", headers=WS_HEADERS) as ws:
        started = ws.receive_json()
        assert started["type"] == "started" and started["highlight"] == [{"type": "text", "value": "pause"}]
        msg = ws.receive_json()
        assert msg["type"] == "entries"
        assert msg["entries"][0]["note"] == "Headphones (Bluetooth) sent Pause"


def test_api_live_bad_filter(authed):
    with authed.websocket_connect("/api/devices/FAKE123/logs/live?preset=nope", headers=WS_HEADERS) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "nope" in msg["message"]


def test_api_logs_need_login(client):
    assert client.get("/api/devices/FAKE123/logs").status_code == 401
    assert client.get("/api/logs/app").status_code == 401
