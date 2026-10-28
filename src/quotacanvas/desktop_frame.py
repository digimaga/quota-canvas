"""Optional high-resolution desktop frame, requested by the app."""

import json
import logging
import os
import time
from pathlib import Path

from .render import CANVAS_W
from .status import FRAME_PATH, write_frame

log = logging.getLogger(__name__)


def requested_width(path: Path):
    """Return a bounded width, or None when no valid request is available."""
    try:
        with path.open(encoding="utf-8") as handle:
            if time.time() - os.fstat(handle.fileno()).st_mtime > 24 * 60 * 60:
                return None
            request = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("desktop-request.json を読めません: %s", exc)
        return None
    width = request.get("pixel_width") if isinstance(request, dict) else None
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        log.warning("desktop-request.json の pixel_width が不正です")
        return None
    return min(width, 2 * CANVAS_W)


def update(render_scaled, directory: Path = FRAME_PATH.parent):
    """Atomically write the requested frame, or remove an unnecessary one."""
    width = requested_width(directory / "desktop-request.json")
    destination = directory / "desktop.png"
    if width is None or width <= CANVAS_W:
        destination.unlink(missing_ok=True)
        return
    write_frame(render_scaled(width / CANVAS_W), destination)
