"""Render tev-logo.svg to PNGs, favicon.ico and tev.icns. Needs: pip install cairosvg Pillow."""
import struct
from pathlib import Path

import cairosvg
from PIL import Image

D = Path(__file__).resolve().parents[2] / "noema" / "static" / "brand" / "tev"
svg = (D / "tev-logo.svg").read_bytes()
for s in (16, 32, 48, 64, 128, 180, 192, 256, 512, 1024):
    cairosvg.svg2png(bytestring=svg, write_to=str(D / f"tev-{s}.png"), output_width=s, output_height=s)
types = {b"icp4": 16, b"icp5": 32, b"ic07": 128, b"ic08": 256, b"ic09": 512, b"ic10": 1024,
         b"ic11": 32, b"ic12": 64, b"ic13": 256, b"ic14": 512}
body = b"".join(t + struct.pack(">I", len(d) + 8) + d for t, s in types.items()
                for d in [(D / f"tev-{s}.png").read_bytes()])
(D / "tev.icns").write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
Image.open(D / "tev-256.png").save(D / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
