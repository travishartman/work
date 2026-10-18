#!/usr/bin/env python3
"""Optimize moth photos for the /mothphotos gallery.

Reads full-res originals from photos/___selects/, writes web-optimized copies:
  mothphotos/img/moth-N.jpg    1800px wide, JPEG q88 (fullscreen view)
  mothphotos/thumb/moth-N.jpg   700px wide, JPEG q80 (grid)
All EXIF (incl. GPS) is stripped; only a copyright/artist tag is re-embedded.
Also emits mothphotos/photos.js with the ordered list + display dimensions.

Usage: python3 tools/build_photos.py [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import piexif
from PIL import Image, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "photos", "___selects")
OUT_DIR = os.path.join(ROOT, "mothphotos")
IMG_DIR = os.path.join(OUT_DIR, "img")
THUMB_DIR = os.path.join(OUT_DIR, "thumb")

FULL_WIDTH = 1800
THUMB_WIDTH = 700
FULL_QUALITY = 88
THUMB_QUALITY = 80

ARTIST = "Travis Hartman"
COPYRIGHT = "\u00a9 2026 Travis Hartman Photography. Not for use without express permission."


def natural_key(name: str) -> tuple:
    m = re.search(r"(\d+)", name)
    return (int(m.group(1)) if m else 0, name)


def copyright_exif() -> bytes:
    zeroth = {piexif.ImageIFD.Artist: ARTIST.encode("utf-8"),
              piexif.ImageIFD.Copyright: COPYRIGHT.encode("utf-8")}
    return piexif.dump({"0th": zeroth, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}})


def resize(img: Image.Image, target_w: int) -> Image.Image:
    if img.width <= target_w:
        return img.copy()
    h = round(img.height * target_w / img.width)
    return img.resize((target_w, h), Image.LANCZOS)


def save_jpeg(img: Image.Image, path: str, quality: int, exif: bytes) -> None:
    out = img
    if out.mode not in ("RGB", "L"):
        out = out.convert("RGB")
    out.save(path, "JPEG", quality=quality, optimize=True, progressive=True, exif=exif)


def needs_build(src: str, *outs: str, force: bool) -> bool:
    if force:
        return True
    if not all(os.path.exists(o) for o in outs):
        return True
    src_m = os.path.getmtime(src)
    return any(os.path.getmtime(o) < src_m for o in outs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(SRC_DIR):
        print(f"ERROR: source dir not found: {SRC_DIR}", file=sys.stderr)
        return 1
    os.makedirs(IMG_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)

    sources = sorted(
        (f for f in os.listdir(SRC_DIR) if f.lower().endswith((".jpg", ".jpeg"))),
        key=natural_key,
    )
    if not sources:
        print("ERROR: no source images found", file=sys.stderr)
        return 1

    exif = copyright_exif()
    manifest = []
    built = 0
    for name in sources:
        src = os.path.join(SRC_DIR, name)
        out_name = os.path.splitext(name)[0] + ".jpg"
        full_out = os.path.join(IMG_DIR, out_name)
        thumb_out = os.path.join(THUMB_DIR, out_name)

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)  # bake rotation, then drop EXIF
            full = resize(im, FULL_WIDTH)
            if needs_build(src, full_out, thumb_out, force=args.force):
                save_jpeg(full, full_out, FULL_QUALITY, exif)
                save_jpeg(resize(im, THUMB_WIDTH), thumb_out, THUMB_QUALITY, exif)
                built += 1
                print(f"  built {out_name}  ({full.width}x{full.height})")
            manifest.append({"f": out_name, "w": full.width, "h": full.height})

    js = "window.MOTH_PHOTOS = " + json.dumps(manifest, separators=(",", ":")) + ";\n"
    with open(os.path.join(OUT_DIR, "photos.js"), "w", encoding="utf-8") as fh:
        fh.write(js)

    print(f"\n{len(manifest)} photos in manifest, {built} (re)built. "
          f"photos.js written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
