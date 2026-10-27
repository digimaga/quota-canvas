"""Low-level access to the TURZX 9.2" screen.

The device speaks the same USB protocol as the Turing Smart Screen 8.8" V1.1
(implemented by phstudy/turing-smart-screen-cli), only with PID 0x0092.
Images must be sent as portrait 462x1920 PNGs; this module takes landscape
1920x462 images (how the panel is mounted) and rotates them before sending.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Union

import usb.backend.libusb1
import usb.core
from PIL import Image
from turingscreencli import operations

VENDOR_ID = 0x1CBE
PRODUCT_ID = 0x0092

# Landscape size as the panel is mounted.
WIDTH = 1920
HEIGHT = 462

# Rotation (degrees, counter-clockwise as in PIL) applied to a landscape image
# to produce the portrait 462x1920 frame the device expects.
DEFAULT_ROTATION = 90

_LIBUSB_CANDIDATES = (
    "/opt/homebrew/lib/libusb-1.0.dylib",
    "/usr/local/lib/libusb-1.0.dylib",
)

# turingscreencli warns on every frame that isn't 480x1920; that is expected here.
logging.getLogger("turingscreencli.operations").addFilter(
    lambda record: "not 480x1920" not in record.getMessage()
)

ImageSource = Union[str, Path, Image.Image]


def _backend():
    override = os.environ.get("TURZX_LIBUSB")
    if override:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _name: override)
        if backend is None:
            raise RuntimeError(f"Cannot load TURZX_LIBUSB: {override}")
        return backend
    for path in _LIBUSB_CANDIDATES:
        if Path(path).exists():
            return usb.backend.libusb1.get_backend(find_library=lambda _name, p=path: p)
    backend = usb.backend.libusb1.get_backend()
    if backend is None:
        raise RuntimeError("libusb not found. Install it with: brew install libusb")
    return backend


class Screen:
    def __init__(self, rotation: int = DEFAULT_ROTATION):
        self.rotation = rotation
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID, backend=_backend())
        if self.dev is None:
            raise RuntimeError("TURZX screen (1cbe:0092) not found on USB")
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured

    def sync(self):
        return operations.send_sync_command(self.dev)

    def brightness(self, value: int) -> None:
        operations.send_brightness_command(self.dev, max(0, min(100, value)))

    def show(self, source: ImageSource) -> bool:
        """Display a landscape image (scaled to fill 1920x462)."""
        img = source if isinstance(source, Image.Image) else Image.open(source)
        img = img.convert("RGBA")
        if img.size != (WIDTH, HEIGHT):
            img = _cover(img, WIDTH, HEIGHT)
        frame = img.rotate(self.rotation, expand=True)
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        buf.seek(0)
        return operations.send_image(self.dev, buf)

    def show_raw(self, path: Union[str, Path]) -> bool:
        """Send a portrait 462x1920 image as-is, without rotation."""
        return operations.send_image(self.dev, str(path))


def _cover(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale to fill width x height, cropping the overflow around the center."""
    scale = max(width / img.width, height / img.height)
    resized = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))
