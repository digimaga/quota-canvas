from pathlib import Path

import pytest

from quotacanvas import cli
from turzx import screen


def test_example_config_override_takes_priority(monkeypatch, tmp_path):
    config = tmp_path / "example.toml"
    config.write_text("example = true\n")
    monkeypatch.setenv("QUOTA_CANVAS_EXAMPLE_CONFIG", str(config))
    assert cli._find_example_file() == config
    config.unlink()
    assert cli._find_example_file() is None


def test_libusb_override_takes_priority(monkeypatch):
    monkeypatch.setenv("TURZX_LIBUSB", "/example/libusb.dylib")
    backend = object()
    seen = []
    def get_backend(find_library=None):
        seen.append(find_library("usb-1.0"))
        return backend
    monkeypatch.setattr(screen.usb.backend.libusb1, "get_backend", get_backend)
    assert screen._backend() is backend
    assert seen == ["/example/libusb.dylib"]


def test_libusb_override_failure_is_explicit(monkeypatch):
    monkeypatch.setenv("TURZX_LIBUSB", "/example/missing.dylib")
    monkeypatch.setattr(screen.usb.backend.libusb1, "get_backend", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="TURZX_LIBUSB"):
        screen._backend()
