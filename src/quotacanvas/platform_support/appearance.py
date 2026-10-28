"""Existing macOS language query and font candidates."""

import subprocess

FONT_ROOT = "/System/Library/Fonts/"
TURZX_TEST_FONT = FONT_ROOT + "Helvetica.ttc"
FONT_FILES = {
    "w8": ("ヒラギノ角ゴシック W8.ttc", "ヒラギノ角ゴシック W7.ttc"),
    "w7": ("ヒラギノ角ゴシック W7.ttc", "ヒラギノ角ゴシック W6.ttc"),
    "w6": ("ヒラギノ角ゴシック W6.ttc", "ヒラギノ角ゴシック W3.ttc"),
    "w5": ("ヒラギノ角ゴシック W5.ttc", "ヒラギノ角ゴシック W3.ttc"),
    "w4": ("ヒラギノ角ゴシック W4.ttc", "ヒラギノ角ゴシック W3.ttc"),
    "w3": ("ヒラギノ角ゴシック W3.ttc", "ヒラギノ角ゴシック W5.ttc"),
}
FONT_FALLBACK = "ヒラギノ角ゴシック W3.ttc"


def language_output():
    return subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                          capture_output=True, text=True, timeout=2).stdout


def font_paths(weight):
    return tuple(FONT_ROOT + name for name in
                 FONT_FILES.get(weight, FONT_FILES["w6"]) + (FONT_FALLBACK,))
