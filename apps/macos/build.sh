#!/bin/bash
# Rebuild the icon + zip TEV.app. Usage: apps/macos/build.sh [out.zip]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; OUT="${1:-$HERE/TEV-mac.zip}"
python3 "$HERE/render_icons.py"
cp "$HERE/../../noema/static/brand/tev/tev.icns" "$HERE/TEV.app/Contents/Resources/icon.icns"
chmod +x "$HERE/TEV.app/Contents/MacOS/TEV"
rm -f "$OUT"
python3 - "$HERE" "$OUT" <<'PY'
import os, sys, zipfile
here, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(os.path.join(here, "TEV.app")):
        for f in files:
            p = os.path.join(root, f); info = zipfile.ZipInfo(os.path.relpath(p, here))
            info.external_attr = (os.stat(p).st_mode & 0xFFFF) << 16; info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, open(p, "rb").read())
    z.write(os.path.join(here, "README-INSTALL.txt"), "README-INSTALL.txt")
PY
echo "built $OUT"
