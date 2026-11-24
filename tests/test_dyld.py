"""DYLD_LIBRARY_PATH=/opt/homebrew/lib makes Apple Vision crash Python: droidxfer must restart without it."""

import sys

import pytest

from droidxfer import cli
from droidxfer.core import macbridge


def test_vision_refuses_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/opt/homebrew/lib")
    assert macbridge.dyld_overrides() == ["DYLD_LIBRARY_PATH"]
    assert macbridge.vision_available() is False
    if sys.platform == "darwin":
        with pytest.raises(macbridge.MacBridgeError, match="DYLD_LIBRARY_PATH"):
            macbridge.vision_recognize(b"not even a png")


def test_main_restarts_without_dyld_overrides(monkeypatch, tmp_path):
    script = tmp_path / "droidxfer"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", [str(script), "serve"])
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/opt/homebrew/lib")
    monkeypatch.setenv("DYLD_FRAMEWORK_PATH", "/opt/homebrew/Frameworks")
    calls = []

    def fake_execv(path, args):
        calls.append((path, args, {k: v for k, v in cli.os.environ.items() if k.startswith("DYLD_")}))
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == [(str(script), [str(script), "serve"], {})]


def test_main_does_not_restart_when_clean(monkeypatch):
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("DYLD_FRAMEWORK_PATH", raising=False)
    monkeypatch.setattr(cli.os, "execv", lambda *a: pytest.fail("restarted"))
    monkeypatch.setattr(cli, "app", lambda: None)
    cli.main()
