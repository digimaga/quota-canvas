#!/bin/bash
# Builds QuotaCanvasApp in release mode and assembles build/QuotaCanvas.app
# (menu-bar only app, ad-hoc signed). Requires only the Command Line Tools.
# Does not install or launch anything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
engine=true
case "${1:-}" in
    '') ;;
    --no-engine) engine=false ;;
    *) echo "Usage: $0 [--no-engine]" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo "Usage: $0 [--no-engine]" >&2; exit 2; }
cd "$ROOT"

swift build -c release

BIN_DIR="$(swift build -c release --show-bin-path)"
APP="$ROOT/build/QuotaCanvas.app"
CONTENTS="$APP/Contents"

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

cp "$BIN_DIR/QuotaCanvasApp" "$CONTENTS/MacOS/QuotaCanvasApp"
cp "$ROOT/Packaging/Info.plist" "$CONTENTS/Info.plist"
printf 'APPL????' > "$CONTENTS/PkgInfo"

# Localizations (ja = development language, keys are the Japanese text itself)
for lproj in "$ROOT"/Resources/*.lproj; do
    [ -d "$lproj" ] || continue
    cp -R "$lproj" "$CONTENTS/Resources/"
done

if "$engine"; then
    CACHE="$ROOT/build/cache"
    mkdir -p "$CACHE"

    # Download once and verify both new and cached archives before use.
    fetch_archive() {
        local name="$1" url="$2" digest="$3" temp="$CACHE/$1.download"
        if [ ! -f "$CACHE/$name" ]; then
            local cleanup
            printf -v cleanup 'rm -f -- %q' "$temp"
            trap "$cleanup" EXIT
            curl -fL --retry 3 "$url" -o "$temp"
            echo "$digest  $temp" | shasum -a 256 -c -
            mv "$temp" "$CACHE/$name"
            trap - EXIT
        fi
        echo "$digest  $CACHE/$name" | shasum -a 256 -c -
    }

    # Python runtime: pinned standalone CPython for Apple Silicon.
    VERSION=3.13.17
    RELEASE=20261021
    ARCHIVE="cpython-${VERSION}+${RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
    SHA256=9c985cbf327425873469896ce748c7134b6deca8edb81e93c589ad2fc5aff445
    fetch_archive "$ARCHIVE" "https://github.com/astral-sh/python-build-standalone/releases/download/$RELEASE/$ARCHIVE" "$SHA256"
    ENGINE="$CONTENTS/Resources/engine"
    mkdir -p "$ENGINE"
    tar -xzf "$CACHE/$ARCHIVE" -C "$ENGINE"
    PYTHON="$ENGINE/python/bin/python3"

    # Install the engine and its dependencies into the bundled Python.
    BUILD_SRC="$(mktemp -d "$ROOT/build/.engine-source.XXXXXX")"
    trap 'rm -rf "$BUILD_SRC"' EXIT
    cp "$REPO/pyproject.toml" "$BUILD_SRC/"
    cp -R "$REPO/src" "$BUILD_SRC/src"
    find "$BUILD_SRC/src" -type d \( -name __pycache__ -o -name '*.egg-info' \) -prune -exec rm -rf {} +
    "$PYTHON" -I -m pip install --no-cache-dir --no-compile "$BUILD_SRC"
    rm -rf "$BUILD_SRC"
    trap - EXIT
    rm -f "$ENGINE/python/lib/python3.13/site-packages/turzx-0.1.0.dist-info/direct_url.json"

    # Build libusb from its verified release for macOS 14 and arm64.
    LIBUSB_VERSION=1.0.30
    LIBUSB_ARCHIVE="libusb-$LIBUSB_VERSION.tar.bz2"
    LIBUSB_SHA256=fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf
    fetch_archive "$LIBUSB_ARCHIVE" "https://github.com/libusb/libusb/releases/download/v$LIBUSB_VERSION/$LIBUSB_ARCHIVE" "$LIBUSB_SHA256"
    LIBUSB_BUILD="$(mktemp -d "$ROOT/build/.libusb-source.XXXXXX")"
    trap 'rm -rf "$LIBUSB_BUILD"' EXIT
    tar -xjf "$CACHE/$LIBUSB_ARCHIVE" -C "$LIBUSB_BUILD"
    (
        cd "$LIBUSB_BUILD/libusb-$LIBUSB_VERSION"
        export MACOSX_DEPLOYMENT_TARGET=14.0
        export CC=/usr/bin/clang
        export CFLAGS='-arch arm64 -mmacosx-version-min=14.0'
        export LDFLAGS='-arch arm64 -mmacosx-version-min=14.0'
        # Libtool's conservative BSD limit avoids relying on sysctl access.
        lt_cv_sys_max_cmd_len=49152 ./configure --disable-static
        make -j 4
    )
    mkdir -p "$ENGINE/bin" "$ENGINE/lib"
    cp "$LIBUSB_BUILD/libusb-$LIBUSB_VERSION/libusb/.libs/libusb-1.0.0.dylib" "$ENGINE/lib/libusb-1.0.0.dylib"
    rm -rf "$LIBUSB_BUILD"
    trap - EXIT
    install_name_tool -id '@rpath/libusb-1.0.0.dylib' "$ENGINE/lib/libusb-1.0.0.dylib"
    cp "$REPO/config.example.toml" "$ENGINE/config.example.toml"

    # Launch with bundled paths, isolated Python, and no bytecode writes.
    cat > "$ENGINE/bin/quota-canvas" <<'SCRIPT'
#!/bin/bash
set -euo pipefail
ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
export TURZX_LIBUSB="$ENGINE/lib/libusb-1.0.0.dylib"
export QUOTA_CANVAS_EXAMPLE_CONFIG="$ENGINE/config.example.toml"
exec "$ENGINE/python/bin/python3" -I -B -c 'from quotacanvas.cli import main; raise SystemExit(main())' "$@"
SCRIPT
    chmod +x "$ENGINE/bin/quota-canvas"

    # Remove build tools and caches that the bundled engine does not need.
    rm -rf "$ENGINE/python/lib/python3.13/ensurepip" "$ENGINE/python/lib/python3.13/idlelib" "$ENGINE/python/lib/python3.13/tkinter" "$ENGINE/python/lib/python3.13/test" "$ENGINE/python/lib/python3.13/site-packages/pip" "$ENGINE/python/lib/python3.13/site-packages/pip-"*.dist-info
    rm -f "$ENGINE/python/bin/pip" "$ENGINE/python/bin/pip3" "$ENGINE/python/bin/pip3.13" \
        "$ENGINE/python/bin/idle3" "$ENGINE/python/bin/idle3.13" \
        "$ENGINE/python/bin/quota-canvas" "$ENGINE/python/bin/turzx" "$ENGINE/python/bin/turing-screen"
    find "$ENGINE/python" -type d -name __pycache__ -prune -exec rm -rf {} +

    # Sign each bundled Mach-O before sealing the app.
    while IFS= read -r -d '' file; do
        if file -b "$file" | grep -q 'Mach-O'; then codesign --force -s - "$file"; fi
    done < <(find "$ENGINE" -type f -print0)
fi

# Check every Mach-O slice against the minimum macOS version in Info.plist.
/usr/bin/python3 - "$APP" <<'PY'
import pathlib
import plistlib
import re
import subprocess
import sys

app = pathlib.Path(sys.argv[1])
with (app / "Contents/Info.plist").open("rb") as f:
    minimum = plistlib.load(f)["LSMinimumSystemVersion"]

def version(value):
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (3 - len(parts))

checked = 0
errors = []
for path in app.rglob("*"):
    if path.is_symlink() or not path.is_file():
        continue
    kind = subprocess.run(["/usr/bin/file", "-b", str(path)], check=True, capture_output=True, text=True).stdout
    if "Mach-O" not in kind:
        continue
    checked += 1
    architectures = subprocess.run(["/usr/bin/lipo", "-archs", str(path)], check=True, capture_output=True, text=True).stdout.split()
    for arch in architectures:
        result = subprocess.run(["/usr/bin/vtool", "-arch", arch, "-show-build", str(path)], capture_output=True, text=True)
        found = []
        for block in re.split(r"(?m)^Load command \d+\s*$", result.stdout):
            command = re.search(r"(?m)^\s*cmd\s+(LC_BUILD_VERSION|LC_VERSION_MIN_MACOSX)\s*$", block)
            if not command:
                continue
            key = "minos" if command[1] == "LC_BUILD_VERSION" else "version"
            value = re.search(r"(?m)^\s*" + key + r"\s+(\d+(?:\.\d+){1,2})\s*$", block)
            platform = re.search(r"(?m)^\s*platform\s+(\S+)\s*$", block)
            if value and (command[1] != "LC_BUILD_VERSION" or (platform and platform[1] == "MACOS")):
                found.append(value[1])
        if result.returncode or not found:
            errors.append(f"{path} ({arch}): missing or unreadable macOS minimum version")
        elif any(version(value) > version(minimum) for value in found):
            errors.append(f"{path} ({arch}): minimum {', '.join(found)} exceeds {minimum}")
if not checked:
    errors.append("No Mach-O files found in app")
if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)
print(f"Checked {checked} Mach-O files: minimum macOS <= {minimum}")
PY

# Signing: ad-hoc. The app holds no TCC grants that must survive rebuilds.
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"

echo
echo "Built: $APP"
codesign -dv "$APP" 2>&1 | grep -E "^(Identifier|Signature)=" || true
