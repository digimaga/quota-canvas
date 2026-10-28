"""High-resolution desktop rendering and its file contract."""

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageFont, ImageStat

from quotacanvas import cli, desktop_frame
from quotacanvas.config import Config
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import CANVAS_H, CANVAS_W, PanelSpec, _S, render, render_error

NOW = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)


def cards():
    window = Window("codex", "weekly", "Weekly", 30, NOW + timedelta(days=4), timedelta(days=7))
    usage = ProviderUsage("codex", "Example", [window], None, NOW)
    return [(usage, compute_pace(window, [], NOW))]


@pytest.mark.parametrize("error", [False, True])
def test_scaled_render_matches_logical_layout(error):
    draw = (lambda s: render_error("Example error", NOW, scale=s)) if error else (
        lambda s: render(cards(), NOW, offset=(2, -2),
                         layout=[PanelSpec("codex:weekly"), PanelSpec("codex:weekly", size="small")],
                         recommendation="Example", scale=s))
    one, two = draw(1.0), draw(2.0)
    assert one.size == (CANVAS_W, CANVAS_H)
    assert two.size == (CANVAS_W * 2, CANVAS_H * 2)
    diff = ImageChops.difference(one, two.resize(one.size, Image.Resampling.LANCZOS))
    assert max(ImageStat.Stat(diff).mean) < 3.5
    default = render_error("Example error", NOW) if error else render(
        cards(), NOW, offset=(2, -2), layout=[PanelSpec("codex:weekly"),
        PanelSpec("codex:weekly", size="small")], recommendation="Example")
    assert ImageChops.difference(one, default).getbbox() is None


@pytest.mark.parametrize("payload, expected", [
    ({"pixel_width": 1}, 1), ({"pixel_width": 1920}, 1920),
    ({"pixel_width": 1921}, 1921), ({"pixel_width": 3840}, 3840),
    ({"pixel_width": 5000}, 3840), ({"pixel_width": 0}, None),
    ({"pixel_width": -1}, None), ({"pixel_width": True}, None),
    ({"pixel_width": 2000.5}, None), ({"pixel_width": "2000"}, None),
    ({}, None), ([], None),
])
def test_request_width(tmp_path, payload, expected):
    path = tmp_path / "desktop-request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert desktop_frame.requested_width(path) == expected


def test_missing_and_broken_request(tmp_path):
    path = tmp_path / "desktop-request.json"
    assert desktop_frame.requested_width(path) is None
    path.write_text("{", encoding="utf-8")
    assert desktop_frame.requested_width(path) is None


@pytest.mark.parametrize("age, active", [(86399, True), (86400, True), (86401, False)])
def test_request_expiry_and_removal(tmp_path, monkeypatch, age, active):
    now = 2_000_000_000
    request = tmp_path / "desktop-request.json"
    destination = tmp_path / "desktop.png"
    request.write_text('{"pixel_width": 2400}')
    os.utime(request, (now - age, now - age))
    monkeypatch.setattr(desktop_frame.time, "time", lambda: now)
    destination.write_bytes(b"old")
    calls = []
    def draw(scale):
        calls.append(scale)
        return Image.new("RGB", (1, 1))
    desktop_frame.update(draw, tmp_path)
    assert destination.exists() is active
    assert calls == ([1.25] if active else [])


def test_bitmap_font_without_variant_is_returned_unchanged():
    font = ImageFont.ImageFont()
    assert not hasattr(font, "font_variant")
    assert _S(Image.new("RGB", (10, 10)), (0, 0), 2.0)._font(font) is font


def test_error_frame_passes_scale_to_renderer(monkeypatch):
    calls = []
    def draw(message, now, scale=1.0):
        calls.append(scale)
        return Image.new("RGB", (1, 1))
    monkeypatch.setattr(cli, "render_error", draw)
    cli._render_frame([], NOW, Config(), None)
    cli._render_frame([], NOW, Config(), None, scale=2.0)
    assert calls == [1.0, 2.0]


def test_write_replace_and_remove(tmp_path):
    request = tmp_path / "desktop-request.json"
    destination = tmp_path / "desktop.png"
    request.write_text('{"pixel_width": 5000}')
    calls = []
    def draw(scale):
        calls.append(scale)
        return Image.new("RGB", (round(CANVAS_W * scale), round(CANVAS_H * scale)), "red")
    desktop_frame.update(draw, tmp_path)
    assert calls == [2.0]
    with Image.open(destination) as image:
        assert image.size == (3840, 924)
    assert not (tmp_path / "desktop.png.tmp").exists()
    request.write_text('{"pixel_width": 1920}')
    desktop_frame.update(draw, tmp_path)
    assert not destination.exists()
    assert calls == [2.0]
    destination.write_bytes(b"old")
    request.unlink()
    desktop_frame.update(draw, tmp_path)
    assert not destination.exists()


def test_failed_write_keeps_previous_frame_and_removes_temporary_file(tmp_path):
    (tmp_path / "desktop-request.json").write_text('{"pixel_width": 2400}')
    destination = tmp_path / "desktop.png"
    destination.write_bytes(b"previous frame")

    class BrokenImage:
        def save(self, handle, format):
            handle.write(b"partial frame")
            raise OSError("write failed")

    with pytest.raises(OSError, match="write failed"):
        desktop_frame.update(lambda scale: BrokenImage(), tmp_path)
    assert destination.read_bytes() == b"previous frame"
    assert not (tmp_path / "desktop.png.tmp").exists()


def test_app_write_rerenders_with_same_arguments(tmp_path, monkeypatch):
    (tmp_path / "desktop-request.json").write_text('{"pixel_width": 2400}')
    monkeypatch.setattr(cli, "write_status", lambda payload: None)
    monkeypatch.setattr(cli, "write_frame", lambda image: desktop_frame.write_frame(image, tmp_path / "frame.png"))
    monkeypatch.setattr(cli.desktop_frame, "update", lambda draw: desktop_update(draw, tmp_path))
    cfg = Config()
    layout = [PanelSpec("codex:weekly")]
    items = cards()
    recommendation = SimpleNamespace(text="Example recommendation", payload=lambda: {})
    original = cli._render_frame(items, NOW, cfg, layout, recommendation=recommendation.text, offset=(2, -2))
    cli._write_app_files(cfg, items, [], NOW, original, layout=layout,
                         recommendation=recommendation, offset=(2, -2))
    with Image.open(tmp_path / "desktop.png") as actual:
        expected = cli._render_frame(items, NOW, cfg, layout, recommendation=recommendation.text,
                                     offset=(2, -2), scale=1.25)
        assert ImageChops.difference(actual.convert("RGB"), expected).getbbox() is None


desktop_update = desktop_frame.update


def test_render_failure_does_not_stop_app_write(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli, "write_status", lambda payload: None)
    monkeypatch.setattr(cli, "write_frame", lambda image: None)
    def fail(draw):
        raise OSError("desktop test failure")
    monkeypatch.setattr(cli.desktop_frame, "update", fail)
    cli._write_app_files(Config(), [], [], NOW, Image.new("RGB", (1, 1)))
    assert "desktop test failure" in caplog.text
