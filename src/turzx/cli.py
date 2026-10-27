"""Command line entry point: `turzx show|test|brightness|sync`."""

from __future__ import annotations

import argparse
import logging
import sys

from PIL import Image, ImageDraw, ImageFont

from .screen import DEFAULT_ROTATION, HEIGHT, WIDTH, Screen

_FONT = "/System/Library/Fonts/Helvetica.ttc"


def test_pattern() -> Image.Image:
    """Landscape pattern that makes orientation obvious."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (200, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=(255, 255, 0), width=10)
    big = ImageFont.truetype(_FONT, 200)
    small = ImageFont.truetype(_FONT, 60)
    d.text((WIDTH // 2, HEIGHT // 2), "TEST", fill=(255, 255, 255), font=big, anchor="mm")
    d.text((30, 25), "LEFT TOP", fill=(255, 255, 255), font=small, anchor="la")
    d.text((WIDTH - 30, HEIGHT - 25), "RIGHT BOTTOM", fill=(255, 255, 255), font=small, anchor="rd")
    return img


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="turzx", description="TURZX 9.2\" (1920x462) screen")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--rotation", type=int, default=DEFAULT_ROTATION,
        help=f"degrees to rotate landscape images before sending (default {DEFAULT_ROTATION})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("show", help="display a landscape image (any size, scaled to fill 1920x462)")
    p.add_argument("path")
    p = sub.add_parser("show-raw", help="send a portrait 462x1920 image without rotation")
    p.add_argument("path")
    sub.add_parser("test", help="display an orientation test pattern")
    p = sub.add_parser("brightness", help="set brightness 0-100")
    p.add_argument("value", type=int)
    sub.add_parser("sync", help="handshake and print the device reply")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    try:
        screen = Screen(rotation=args.rotation)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.command == "show":
        ok = screen.show(args.path)
    elif args.command == "show-raw":
        ok = screen.show_raw(args.path)
    elif args.command == "test":
        ok = screen.show(test_pattern())
    elif args.command == "brightness":
        screen.brightness(args.value)
        ok = True
    else:
        reply = screen.sync()
        print(bytes(reply).rstrip(b"\0").hex(" ") if reply is not None else "no reply")
        ok = reply is not None
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
