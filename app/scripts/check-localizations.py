#!/usr/bin/env python3
"""Check the two .strings files have the same unique keys."""
import plistlib
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "Resources"
keys = {}
for language in ("ja", "en"):
    path = root / f"{language}.lproj" / "Localizable.strings"
    result = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", "--", str(path)],
                            capture_output=True, check=True)
    values = plistlib.loads(result.stdout)
    keys[language] = set(values)
    # plutil accepts duplicate keys; count declarations independently.
    import re
    declared = re.findall(r'^"((?:[^"\\]|\\.)*)"\s*=', path.read_text(), re.MULTILINE)
    if len(declared) != len(values):
        sys.exit(f"{language}: duplicate or unparsed keys")
if keys["ja"] != keys["en"]:
    for language, other in (("ja", "en"), ("en", "ja")):
        for key in sorted(keys[language] - keys[other]):
            print(f"{language} only: {key}")
    sys.exit(1)
print(f"Localization keys match: {len(keys['ja'])} keys per language")
