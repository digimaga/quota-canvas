#!/usr/bin/env python3
"""Check the two .strings files have the same unique keys."""
import plistlib
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "Resources"

DECLARATION = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*=', re.MULTILINE)
SPECIFIER = re.compile(
    r'%%|%'                                # %% is a literal percent, not a specifier
    r'(?:(?P<pos>\d+)\$)?'                 # %n$ argument position
    r'[-+#0\' ]*'                          # flags
    r'(?P<width>\d+|\*(?:(?P<wpos>\d+)\$)?)?'  # width (*, *n$: takes an argument)
    r'(?:\.(?P<prec>\d+|\*(?:(?P<ppos>\d+)\$)?))?'  # precision (same forms)
    r'(?P<len>hh|ll|l|L|h|z|t|j|q)?'       # length modifier
    r'(?P<conv>[@diuxXoefFEgGaAcCsSpn])')  # conversion


def strip_comments(text):
    """Remove /* */ and // comments outside string literals only; quoted
    text (with \\ escapes) is kept verbatim."""
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith('//', i):
            i = text.find('\n', i)
            if i < 0:
                break
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def declared_keys(path):
    """Declaration keys with comments stripped (plutil accepts duplicate
    keys, so count declarations independently)."""
    return DECLARATION.findall(strip_comments(path.read_text()))


def argument_types(value):
    """Format specifiers by argument slot, e.g. {1: '@', 2: 'lld'}.

    Positional specifiers (%2$lld) map to their slot so a reordered
    translation still has to fill every argument with the same type.
    Returns None when one slot is claimed with conflicting types
    (e.g. "%1$lld %1$@"), which can never be a valid format string."""
    types = {}
    position = 0
    for match in SPECIFIER.finditer(value):
        if match.group(0) == '%%':
            continue
        for field, pgroup in (('width', 'wpos'), ('prec', 'ppos')):
            star = match.group(field)
            if star and star.startswith('*'):
                if match.group(pgroup):
                    slot = int(match.group(pgroup))
                else:
                    position += 1
                    slot = position
                # A * width/precision consumes a plain int, the same type
                # as %d / %i, so "%1$*1$d" (one int for both) is valid.
                if types.setdefault(slot, 'd') != 'd':
                    return None
        position += 1
        slot = int(match.group('pos') or position)
        conv = 'd' if match.group('conv') == 'i' else match.group('conv')  # %i == %d
        kind = (match.group('len') or '') + conv
        if types.setdefault(slot, kind) != kind:
            return None
    return types


def unescape(raw):
    return re.sub(r'\\(.)', r'\1', raw)


keys = {}
translations = {}
failed = False
for language in ("ja", "en"):
    path = root / f"{language}.lproj" / "Localizable.strings"
    result = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", "--", str(path)],
                            capture_output=True, check=True)
    values = plistlib.loads(result.stdout)
    keys[language] = set(values)
    translations[language] = values
    declared = declared_keys(path)
    duplicates = sorted(key for key, count in Counter(declared).items() if count > 1)
    for key in duplicates:
        print(f"{language}: duplicate key: {unescape(key)}")
        failed = True
    if not duplicates and len(declared) != len(values):
        print(f"{language}: {len(declared)} declarations but {len(values)} parsed keys")
        failed = True
    for key, value in sorted(values.items()):
        if not value.strip():
            print(f"{language}: empty translation: {key}")
            failed = True
if keys["ja"] != keys["en"]:
    for language, other in (("ja", "en"), ("en", "ja")):
        for key in sorted(keys[language] - keys[other]):
            print(f"{language} only: {key}")
    failed = True
for key in sorted(keys["ja"] & keys["en"]):
    expected = argument_types(key)
    for language in ("ja", "en"):
        actual = argument_types(translations[language][key])
        if expected is None or actual is None or actual != expected:
            print(f"{language} format mismatch: {key}")
            failed = True
if failed:
    sys.exit(1)
print(f"Localization keys match: {len(keys['ja'])} keys per language")
