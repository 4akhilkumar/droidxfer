"""droidxfer's own activity log: file format, masking, rotation, the matrix_sols-style query engine and its API."""

import logging
import os
from datetime import datetime, timedelta

import pytest

from droidxfer.core import applog, logviewer
from droidxfer.core.logviewer import FilterPattern, LogQueryError, derive_group_and_stream

from test_api import authed, client  # noqa: F401  (fixtures)

log = logging.getLogger("droidxfer.core.transfer")


def _stamp(dt: datetime) -> str:
    return dt.strftime("%d-%b-%y %H:%M:%S")


def write_log(folder, lines):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / applog.LOG_NAME).write_text("".join(line + "\n" for line in lines), encoding="utf-8")


@pytest.fixture
def sample(app_log_dir):
    now = datetime.now().replace(microsecond=0)
    lines = [
        f"{_stamp(now - timedelta(hours=3))} [INFO] droidxfer/core/devices.py:192:_resolve - phone ready: Pixel",
        f"{_stamp(now - timedelta(minutes=50))} [WARNING] droidxfer/core/wireless.py:80:connect - Wi-Fi: connecting failed",
        f"{_stamp(now - timedelta(minutes=20))} [ERROR] droidxfer/core/transfer.py:426:_run - transfer abc failed",
        "Traceback (most recent call last):",
        '  File "transfer.py", line 1, in _run',
        "OSError: disk full",
        f"{_stamp(now - timedelta(minutes=5))} [INFO] droidxfer/core/transfer.py:402:_run - transfer def completed: 3 copied",
        f"{_stamp(now - timedelta(minutes=1))} [DEBUG] droidxfer/adb/client.py:120:shell - shell: ls /sdcard",
        f"{_stamp(now)} [INFO] uvicorn/server.py:84:startup - Application startup complete.",
    ]
    write_log(app_log_dir, lines)
    return app_log_dir


# writing ------------------------------------------------------------------------------
def test_writes_matrix_sols_format_and_masks_secrets(app_log_dir):
    applog.setup("INFO")
    applog.register_secret("s3cr3t-token-value")
    log.info("serving at http://127.0.0.1:1/?token=abc123&x=1 with s3cr3t-token-value")
    log.info("copied %s", os.path.expanduser("~/Pictures/trip.jpg"))
    log.debug("not written at INFO")
    try:
        raise OSError("disk full")
    except OSError:
        log.exception("transfer %s failed", "abc")
    text = (app_log_dir / applog.LOG_NAME).read_text()
    first = text.splitlines()[0]
    assert " [INFO] test_applog.py:" in first and ":test_writes_matrix_sols_format_and_masks_secrets - " in first
    assert "token=******&x=1" in text and "s3cr3t-token-value" not in text and "with ******" in text
    assert "copied ~/Pictures/trip.jpg" in text
    assert "not written" not in text
    assert "Traceback (most recent call last):" in text and "OSError: disk full" in text
    entries = logviewer.search({"range": "1h"})["entries"]
    assert entries[0]["level"] == "ERROR" and "OSError: disk full" in entries[0]["extra"]


def test_level_changes_and_setup_is_idempotent(app_log_dir):
    handler = applog.setup("WARNING")
    assert applog.setup("DEBUG") is handler
    assert sum(isinstance(h, applog.LockedRotatingFileHandler) for h in logging.getLogger().handlers) == 1
    log.debug("now visible")
    applog.set_level("bogus")  # falls back to INFO
    log.debug("hidden again")
    text = (app_log_dir / applog.LOG_NAME).read_text()
    assert "now visible" in text and "hidden again" not in text


def test_relative_source():
    root = applog._PACKAGE_ROOT
    assert applog.relative_source(f"{root}/droidxfer/core/transfer.py") == "droidxfer/core/transfer.py"
    assert applog.relative_source("/x/.venv/lib/python3.12/site-packages/uvicorn/server.py") == "uvicorn/server.py"
    assert applog.relative_source("/usr/lib/python3.12/asyncio/base_events.py") == "python/base_events.py"


def test_rotation_keeps_backups(app_log_dir):
    handler = applog.LockedRotatingFileHandler(app_log_dir / applog.LOG_NAME, max_bytes=400, backups=2)
    handler.setFormatter(applog.MaskingFormatter())
    for i in range(30):
        handler.emit(logging.LogRecord("droidxfer.x", logging.INFO, __file__, 1, f"line {i:02d} " + "x" * 40, None, None))
    names = sorted(p.name for p in app_log_dir.iterdir() if not p.name.endswith(".lock"))
    assert names == ["droidxfer.log", "droidxfer.log.1", "droidxfer.log.2"]
    assert all(p.stat().st_size <= 400 for p in app_log_dir.iterdir() if p.suffix != ".lock")
    assert "line 29" in (app_log_dir / "droidxfer.log").read_text()


# query engine (ported from matrix_sols) -------------------------------------------------------
@pytest.mark.parametrize(
    "pattern, text, expected",
    [
        ("error timeout", "ERROR: read timeout", True),
        ("error timeout", "error only", False),
        ('"disk full"', "OSError: disk full", True),
        ('"disk full"', "full disk", False),
        ("transfer -completed", "transfer failed", True),
        ("transfer -completed", "transfer completed", False),
        ("?wifi ?usb", "connected over USB", True),
        ("?wifi ?usb", "connected", False),
        (r"/transfer \w{3} failed/", "transfer abc failed", True),
        ("", "anything", True),
    ],
)
def test_filter_pattern(pattern, text, expected):
    assert FilterPattern(pattern).matches(text) is expected


def test_filter_pattern_case_and_errors():
    assert not FilterPattern("Error", case_sensitive=True).matches("error")
    assert FilterPattern("x -y ?z /r/").required_terms == ["x"]
    with pytest.raises(LogQueryError):
        FilterPattern("/[/")
    with pytest.raises(LogQueryError):
        FilterPattern("x" * 501)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("droidxfer/core/transfer.py", ("transfer", "core/transfer.py")),
        ("droidxfer/adb/client.py", ("adb", "adb/client.py")),
        ("droidxfer/api/routes.py", ("api", "api/routes.py")),
        ("droidxfer/cli.py", ("cli", "cli.py")),
        ("uvicorn/protocols/http/h11_impl.py", ("uvicorn", "uvicorn/protocols/http/h11_impl.py")),
        ("python/base_events.py", ("python", "python/base_events.py")),
    ],
)
def test_groups_and_streams(path, expected):
    assert derive_group_and_stream(path) == expected


def test_search_ranges_levels_groups_and_multiline(sample):
    res = logviewer.search({"range": "1h"})
    assert [e["message"][:20] for e in res["entries"]] == [
        "Application startup ", "shell: ls /sdcard", "transfer def complet", "transfer abc failed", "Wi-Fi: connecting fa"]
    assert res["entries"][3]["extra"].endswith("OSError: disk full") and res["next_cursor"] is None
    assert len(logviewer.search({"range": "all"})["entries"]) == 6
    assert [e["level"] for e in logviewer.search({"range": "all", "levels": "ERROR,WARNING"})["entries"]] == ["ERROR", "WARNING"]
    res = logviewer.search({"range": "all", "groups": "transfer", "q": "disk"})
    assert [e["message"] for e in res["entries"]] == ["transfer abc failed"]  # pattern also searches traceback lines
    assert res["highlight"] == [{"type": "text", "value": "disk"}]
    assert logviewer.search({"range": "all", "streams": "adb/client.py"})["entries"][0]["source"] == "adb/client.py:120"


def test_search_paging_with_cursor(sample):
    page1 = logviewer.search({"range": "all", "limit": "2"})
    assert len(page1["entries"]) == 2 and page1["next_cursor"]
    page2 = logviewer.search({"range": "all", "limit": "2", "cursor": page1["next_cursor"]})
    page3 = logviewer.search({"range": "all", "limit": "2", "cursor": page2["next_cursor"]})
    seen = [e["id"] for p in (page1, page2, page3) for e in p["entries"]]
    assert len(seen) == len(set(seen)) == 6
    stale = logviewer.search({"range": "all", "cursor": "0:5"})  # another inode: the file was rotated
    assert stale["cursor_reset"] is True
    with pytest.raises(LogQueryError):
        logviewer.search({"cursor": "garbage"})


@pytest.mark.parametrize("params", [{"range": "2y"}, {"range": "custom"},
                                    {"range": "custom", "start": "2026-01-02T00:00", "end": "2026-01-01T00:00"},
                                    {"limit": "many"}])
def test_bad_queries(params):
    with pytest.raises(LogQueryError):
        logviewer.search(params)


def test_custom_range_and_histogram(sample):
    now = datetime.now()
    start = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
    res = logviewer.search({"range": "custom", "start": start, "end": end})
    assert [e["level"] for e in res["entries"]] == ["INFO", "ERROR"]
    hist = logviewer.histogram({"range": "1h"})
    assert hist["total"] == 5
    assert hist["by_level"] == {"DEBUG": 1, "INFO": 2, "WARNING": 1, "ERROR": 1, "CRITICAL": 0}
    assert 30 <= len(hist["buckets"]) <= 62 and hist["step_seconds"] == 60
    assert sum(sum(b["counts"].values()) for b in hist["buckets"]) == 5


def test_tail_follows_appends_partial_lines_and_rotation(sample):
    first = logviewer.tail({})
    assert len(first["entries"]) == 6 and first["offset"] == (sample / applog.LOG_NAME).stat().st_size
    path = sample / applog.LOG_NAME
    now = _stamp(datetime.now())
    with open(path, "a") as fh:
        fh.write(f"{now} [INFO] droidxfer/cli.py:1:f - new entry\n{now} [INFO] droidxfer/cli.py:1:f - half")
    nxt = logviewer.tail({"offset": str(first["offset"]), "inode": first["inode"], "q": "entry"})
    assert [e["message"] for e in nxt["entries"]] == ["new entry"]  # the unfinished line waits for the next poll
    with open(path, "a") as fh:
        fh.write(" done\n")
    nxt2 = logviewer.tail({"offset": str(nxt["offset"]), "inode": nxt["inode"]})
    assert [e["message"] for e in nxt2["entries"]] == ["half done"]
    os.replace(path, sample / "droidxfer.log.1")
    write_log(sample, [f"{now} [ERROR] droidxfer/cli.py:2:g - after rotation"])
    rotated = logviewer.tail({"offset": str(nxt2["offset"]), "inode": nxt2["inode"]})
    assert rotated["rotated"] and [e["message"] for e in rotated["entries"]] == ["after rotation"]


def test_download_and_meta(sample):
    text = "".join(logviewer.iter_download({"range": "all", "levels": "ERROR"}))
    assert text.startswith(text.split("\n")[0]) and "[ERROR]" in text and "OSError: disk full\n" in text
    meta = logviewer.meta()
    groups = {g["name"]: g for g in meta["groups"]}
    assert set(groups) == {"devices", "wireless", "transfer", "adb", "uvicorn"}
    assert groups["transfer"]["count"] == 2 and groups["transfer"]["streams"][0]["name"] == "core/transfer.py"
    assert meta["file"]["name"] == "droidxfer.log" and [lv["id"] for lv in meta["levels"]] == logviewer.LEVELS


def test_no_log_file_yet(app_log_dir):
    assert logviewer.search({})["entries"] == [] and logviewer.meta()["file"] is None
    assert logviewer.tail({})["offset"] == 0 and list(logviewer.iter_download({})) == []
    assert logviewer.histogram({})["total"] == 0


# API + activity entries -----------------------------------------------------------------------
def test_api_app_logs(authed, sample):
    meta = authed.get("/api/logs/app", params={"action": "meta"}).json()
    assert meta["log_level"] == "INFO" and meta["default_range"] == "1h"
    assert len(authed.get("/api/logs/app", params={"range": "1h"}).json()["entries"]) >= 5
    assert authed.get("/api/logs/app", params={"action": "histogram", "range": "1h"}).json()["total"] >= 5
    tail = authed.get("/api/logs/app", params={"action": "tail"}).json()
    assert tail["inode"] and tail["entries"]
    r = authed.get("/api/logs/app", params={"action": "download", "range": "all", "levels": "ERROR"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "disk full" in r.text
    assert authed.get("/api/logs/app", params={"range": "forever"}).status_code == 400
    assert authed.get("/api/logs/app", params={"action": "download", "range": "forever"}).status_code == 400
    assert authed.get("/api/logs/app", params={"action": "nope"}).status_code == 400


def test_server_records_activity(authed, app_log_dir):
    authed.put("/api/settings", json={"log_level": "DEBUG"})
    authed.post("/api/devices/FAKE123/mkdir", json={"parent": "/sdcard", "name": "Trip"})
    authed.get("/api/devices/NOPE/ls")
    assert authed.put("/api/settings", json={"log_level": "LOUD"}).status_code == 422
    text = (app_log_dir / applog.LOG_NAME).read_text()
    assert "web server starting" in text
    assert "settings changed: log_level=DEBUG" in text
    assert "phone FAKE123: created folder /sdcard/Trip" in text
    assert "GET /api/devices/NOPE/ls -> 404 DeviceNotFound" in text
    assert "test-token-123" not in text


async def test_transfer_summary_is_logged(ctx, app_log_dir, mac_home):
    applog.setup("INFO")
    (mac_home / "a.txt").write_text("hello")
    job = ctx.engine.submit("FAKE123", "push", [str(mac_home / "a.txt")], "/sdcard/Download")
    await ctx.engine.wait(job.id)
    text = (app_log_dir / applog.LOG_NAME).read_text()
    assert f"transfer {job.id} queued (Mac -> phone, FAKE123): a.txt -> /sdcard/Download" in text
    assert f"transfer {job.id} completed: 1 copied, 0 skipped, 0 failed, 5 B in" in text


def test_config_rejects_bad_level(tmp_path):
    from droidxfer.core.config import ConfigStore

    store = ConfigStore(tmp_path / "config.json")
    assert store.settings.log_level == "INFO"
    with pytest.raises(ValueError):
        store.update({"log_level": "LOUD"})
    store.update({"log_level": "DEBUG"})
    (tmp_path / "config.json").write_text('{"log_level": "nonsense"}')
    assert ConfigStore(tmp_path / "config.json").settings.log_level == "INFO"
