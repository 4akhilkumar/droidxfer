import pytest

from droidxfer.core.context import AppContext
from fake_adb import FakeAdb


@pytest.fixture(autouse=True)
def no_system_trash(monkeypatch):
    """Never let tests move files into the real macOS Trash."""
    monkeypatch.setattr("droidxfer.core.localfs.TRASH_TOOL", "/nonexistent/trash")


@pytest.fixture(autouse=True)
def no_real_usb(monkeypatch):
    """Never scan the machine's real USB bus (a phone may be plugged in while tests run)."""
    monkeypatch.setattr("droidxfer.core.mtpdevices.scan_usb", lambda: [])


@pytest.fixture(autouse=True)
def app_log_dir(tmp_path, monkeypatch):
    """droidxfer's own log goes to a per-test folder (never ~/Library/Logs, never the fake Mac home)."""
    from droidxfer.core import applog

    folder = tmp_path / "app-logs"
    monkeypatch.setattr(applog, "log_dir", lambda: folder)
    applog.teardown()
    yield folder
    applog.teardown()


@pytest.fixture
def device_root(tmp_path):
    root = tmp_path / "device"
    (root / "sdcard" / "Download").mkdir(parents=True)
    return root


@pytest.fixture
def fake_adb(device_root):
    server = FakeAdb(device_root).start()
    yield server
    server.stop()


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DROIDXFER_HOME", str(home / "Library" / "droidxfer"))
    return home


@pytest.fixture
async def ctx(fake_adb, mac_home):
    context = AppContext(adb_port=fake_adb.port, local_root=mac_home)
    await context.start(track=False)
    yield context
    await context.stop()
