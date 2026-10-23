#!/bin/bash
# Build the native TEV.app (Swift + WKWebView) and zip it.
# Usage: apps/macos/build.sh [out.zip]
#   macOS: compiles src/main.swift with swiftc (universal arm64+x86_64), ad-hoc signs, zips with ditto.
#   Linux/other: the Swift compile is SKIPPED (AppKit/WebKit only exist on macOS) and no zip is made;
#   grab TEV-mac.zip from the "TEV macOS app" GitHub Actions run instead.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; OUT="${1:-$HERE/TEV-mac.zip}"
ICNS="$HERE/../../noema/static/brand/tev/tev.icns"
BUILD="$HERE/build"; APP="$BUILD/TEV.app"

# Icons: re-render from tev-logo.svg when cairosvg is available, else use the committed tev.icns.
if python3 -c 'import cairosvg, PIL' 2>/dev/null; then python3 "$HERE/render_icons.py"; fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "SKIPPED: native TEV.app needs macOS to compile (swiftc + AppKit/WebKit). No zip built." >&2
  echo "         Run this script on a Mac, or download the TEV-mac artifact from the 'TEV macOS app' workflow." >&2
  exit 0
fi

rm -rf "$BUILD"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/TEV.app/Contents/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/TEV.app/Contents/Resources/tev-url.txt" "$APP/Contents/Resources/tev-url.txt"
cp "$ICNS" "$APP/Contents/Resources/icon.icns"

for ARCH in arm64 x86_64; do
  swiftc -O -target "$ARCH-apple-macos11.0" -framework Cocoa -framework WebKit \
    "$HERE/src/main.swift" -o "$BUILD/TEV-$ARCH"
done
lipo -create "$BUILD/TEV-arm64" "$BUILD/TEV-x86_64" -output "$APP/Contents/MacOS/TEV"
rm -f "$BUILD/TEV-arm64" "$BUILD/TEV-x86_64"
plutil -lint "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP"
codesign --verify --strict --verbose=2 "$APP"

rm -f "$OUT"
STAGE="$(mktemp -d)"; ditto "$APP" "$STAGE/TEV.app"; cp "$HERE/README-INSTALL.txt" "$STAGE/"
(cd "$STAGE" && ditto -c -k --sequesterRsrc . "$OUT")
rm -rf "$STAGE"
echo "built $OUT"
