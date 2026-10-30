import pytest

from droidxfer.core.context import AppContext
from fake_adb import FakeAdb


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
