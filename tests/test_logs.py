"""Log viewer: logcat parsing, plain-words notes, filters, the phone commands, API and live stream."""

import asyncio

import pytest

from droidxfer.core import logs
from droidxfer.core.logs import LogFilter, LogLine, explain, mask_addresses, parse_line
from droidxfer.core.remotefs import RemoteOpError

from test_api import PORT, TOKEN, authed, client  # noqa: F401  (fixtures)

WS_HEADERS = {"host": f"127.0.0.1:{PORT}", "cookie": f"droidxfer_{PORT}={TOKEN}"}


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


def test_key_release_is_not_explained():
    assert explain(_line("dispatchMediaKeyEvent, pkg=com.android.bluetooth event=KeyEvent { action=ACTION_UP, "
                         "keyCode=KEYCODE_MEDIA_PAUSE }")) is None


def test_crash_lines():
    assert explain(_line("FATAL EXCEPTION: main", tag="AndroidRuntime")).startswith("An app crashed")
    assert explain(_line("Process: com.example.game, PID: 12345", tag="AndroidRuntime")) == "Crashed app: com.example.game"


# filters ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"preset": "nope"},
        {"min_level": "X"},
        {"app": "not a package; rm -rf /"},
        {"query": "two\nlines"},
        {"query": "x" * (logs.MAX_QUERY + 1)},
    ],
)
def test_filter_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        LogFilter(**kwargs)


def test_filter_defaults_and_matching():
    flt = LogFilter(preset="", min_level="w", query="  Wifi ")
    assert flt.preset is None and flt.min_level == "W" and flt.query == "Wifi"
    match = flt.make_matcher()
    assert match(_line("connected to wifi", tag="WifiService", level="W"))
    assert not match(_line("connected to wifi", tag="WifiService", level="I"))  # below the level
    assert not match(_line("something else", level="E"))  # search text missing


def test_phone_commands_are_quoted():
    flt = LogFilter(preset="headphones", min_level="W", query="it's here")
    command = flt.logcat_command(10364, dump=True, since="$(($(date +%s)-600)).000")
    assert command.startswith("logcat -d -v threadtime -v epoch -b main,system,crash --uid=10364 -T ")
    assert command.endswith("'*:W'")  # filterspec must come after the options
    grep = flt.phone_grep()
    assert grep.startswith(" | grep -E 'opcode=Opcode::PASS_THROUGH|")
    assert grep.endswith(""" | grep -iF -e 'it'"'"'s here'""")
    assert LogFilter().phone_grep() == ""


# reading from the (fake) phone ---------------------------------------------------------------
async def test_dump_headphones_preset(ctx, fake_adb):
    res = await ctx.logs.dump("FAKE123", LogFilter(preset="headphones"), minutes=10)
    notes = [line["note"] for line in res["lines"]]
    assert notes == [
        "Headphones sent a button signal over Bluetooth",
        "Headphones (Bluetooth) sent Pause",  # the ACTION_UP twin is dropped
        "Playback playing → paused (as reported to the headphones)",
        "Google app (Assistant) sent a Play/Pause toggle: it flips whatever is happening, so a missed one inverts later ones",
        "Music audio connected",
    ]  # adbd echoing droidxfer's own grep command is not a match
    assert res["phone_time"] == fake_adb.log_now
    assert res["matched"] == 5 and not res["truncated"]
    assert "XX:XX:XX:XX:F1:26STATE_CONNECTING" in res["lines"][-1]["message"]
    command = fake_adb.shell_log[-1]
    assert "-T \"$(($(date +%s)-600)).000\"" in command and "| grep -E " in command


async def test_dump_limit_mask_level_and_app(ctx, fake_adb):
    res = await ctx.logs.dump("FAKE123", LogFilter(min_level="W", mask=False), minutes=0, limit=2)
    assert [line["level"] for line in res["lines"]] == ["E", "W"]  # newest kept
    assert res["matched"] == 4 and res["truncated"]
    assert res["lines"][-1]["message"] == "connected to AA:BB:CC:DD:EE:FF"
    assert " -T " not in fake_adb.shell_log[-1]  # 0 minutes = everything stored

    await ctx.logs.dump("FAKE123", LogFilter(app="com.example.game"), minutes=5)
    assert "--uid=1016" in fake_adb.shell_log[-1]
    with pytest.raises(RemoteOpError, match="not installed"):
        await ctx.logs.dump("FAKE123", LogFilter(app="com.example.missing"))
    with pytest.raises(ValueError):
        await ctx.logs.dump("FAKE123", LogFilter(), minutes=-1)


async def test_crashes_preset(ctx, fake_adb):
    fake_adb.logcat_lines.append(
        "         1700000008.000  2945  6948 I ActivityManager: Process com.example.cached (pid 9) has died: cch+99 SVC")
    res = await ctx.logs.dump("FAKE123", LogFilter(preset="crashes"))
    assert [line["note"] for line in res["lines"]] == [
        "com.example.game stopped responding (ANR)",
        "An app crashed (the lines below show where)",
        "Crashed app: com.example.game",
    ]


async def test_stream_and_clear(ctx, fake_adb):
    seen = []

    async def follow():
        async for line in ctx.logs.stream("FAKE123", LogFilter(preset="audio")):
            seen.append(line)
            return

    await asyncio.wait_for(follow(), 5)
    assert seen[0].note == "System sounds played a notification sound, interrupting other audio"
    assert fake_adb.shell_log[-1].startswith("logcat -v threadtime -v epoch")
    assert '-T "$(date +%s).000"' in fake_adb.shell_log[-1]

    await ctx.logs.clear("FAKE123")
    assert fake_adb.logcat_lines == []
    assert (await ctx.logs.dump("FAKE123", LogFilter()))["lines"] == []


def test_to_text_marks_notes():
    line = _line("Update PlaybackStatus 1 -> 2")
    line.note = explain(line)
    text = logs.to_text([line], header="hdr")
    assert text.startswith("hdr\n") and text.rstrip().endswith("<-- Playback playing → paused (as reported to the headphones)")


# API ----------------------------------------------------------------------------------
def test_api_presets(authed):
    body = authed.get("/api/logs/presets").json()
    assert [p["id"] for p in body["presets"]] == ["headphones", "bluetooth", "crashes", "audio"]
    assert body["default_window"] == logs.DEFAULT_WINDOW


def test_api_dump_and_errors(authed):
    r = authed.get("/api/devices/FAKE123/logs", params={"preset": "audio", "minutes": 5})
    assert r.status_code == 200
    assert r.json()["lines"][0]["note"].startswith("System sounds")
    assert authed.get("/api/devices/FAKE123/logs", params={"level": "Z"}).status_code == 400
    assert authed.get("/api/devices/FAKE123/logs", params={"app": "com.example.missing"}).status_code == 400
    assert authed.get("/api/devices/FAKE123/logs", params={"minutes": -5}).status_code == 422
    assert authed.get("/api/devices/NOPE/logs").status_code == 404


def test_api_clear(authed, fake_adb):
    assert authed.post("/api/devices/FAKE123/logs/clear").json() == {"ok": True}
    assert fake_adb.logcat_lines == []


def test_api_live_stream(authed):
    with authed.websocket_connect("/api/devices/FAKE123/logs/live?preset=headphones&level=V", headers=WS_HEADERS) as ws:
        assert ws.receive_json() == {"type": "started"}
        msg = ws.receive_json()
        assert msg["type"] == "lines"
        assert msg["lines"][1]["note"] == "Headphones (Bluetooth) sent Pause"


def test_api_live_bad_filter(authed):
    with authed.websocket_connect("/api/devices/FAKE123/logs/live?level=Q", headers=WS_HEADERS) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "level" in msg["message"]


def test_api_logs_need_login(client):
    assert client.get("/api/devices/FAKE123/logs").status_code == 401
