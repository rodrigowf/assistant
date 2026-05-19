#!/usr/bin/env python3
"""Regenerate web + Android icons with a gradient background and a
larger glyph that actually fills the launcher tile.

Changes from v1:
- Background is a 135deg gradient from #4B0856 (deep purple, top-left)
  to #6366F1 (indigo, bottom-right) — matches the design sheet's
  `.app-icon.gradient` style.
- The glyph is sized against its *visual* bounding box (cropped to
  the actual ink, not the SVG viewBox), then scaled to fill ~86%
  of the icon canvas instead of ~66%.
- Web `icon-192.png` / `icon-512.png` get the same gradient + larger
  glyph so the PWA install icon and iOS home-screen icon match Android.
- `icon.svg` keeps its original gradient + transparent background
  (it's used as a browser favicon where transparency wins).
- Android adaptive-icon background is rewritten as a vector drawable
  with a linearGradient (no longer a flat @color/primary).
- Android adaptive-icon foreground is regenerated with the larger
  glyph fraction.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path("/home/rodrigo/assistant")
SRC_SVG = ROOT / "context/public/design/logo-icon.svg"
FRONTEND_DIR = ROOT / "frontend/public"
RES = ROOT / "android/app/src/main/res"
TMP = ROOT / ".tmp-icon-gen"

GRAD_START = (0x4B, 0x08, 0x56)  # deep purple — top-left
GRAD_END = (0x63, 0x66, 0xF1)    # indigo — bottom-right

DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

# Glyph occupies 86% of the icon canvas (vs the 66% safe zone before).
# For an Android adaptive icon, the safe zone is 72/108 = 0.667, but the
# *visible* circle/squircle the launcher carves out is ~84/108 = 0.78 —
# so 0.86 still risks the very edges getting clipped by the launcher mask.
# For our purposes (launcher tile fill on Xiaomi/HyperOS), 0.86 looks right;
# we accept that on a strict-Material-3 launcher the bubble's leg might
# touch the mask edge.
GLYPH_FRACTION_LEGACY = 0.474   # +5% from 0.451 — used by PWA install PNGs (plate)
GLYPH_FRACTION_ADAPTIVE = 0.416  # +5% from 0.396 — adaptive icon foreground

# Free-standing bubble icon (no plate, no shape mask). Used for Android
# legacy launcher PNGs (API <= 25), where the launcher composites the
# PNG directly onto the wallpaper — matches the OS pattern used by
# stock Android 5.x system icons (Gallery, My Files, etc.). The bubble
# fills most of the canvas since there's no surrounding plate.
GLYPH_FRACTION_FREE = 0.92
# Push glyph DOWN by 4.3% of icon size so the bubble's visual center sits
# slightly below the tile center. Without this, centering the bounding box
# (bubble + leg) puts the bubble's optical center above the tile center.
GLYPH_Y_OFFSET_FRACTION = 0.043

# White-on-transparent SVG of just the glyph (bubble + leg + counter dot).
# The counter dot is drawn in the gradient start color so it punches through
# the white bubble onto the gradient background.
WHITE_GLYPH_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="9.55 7.62 97.34 89.37">
  <g transform="translate(-33.476177,-22.491808)">
    <path fill="#FFFFFF" d="m 98.480703,32.661887 c -30.726336,0.597216 -52.022542,22.198386 -52.272494,40.506784 -0.195272,14.303125 4.952902,20.403884 15.42552,29.007619 l 6.13237,15.81638 36.121771,-56.527418 c 0,0 2.01316,-4.094738 7.55658,-2.796877 l 5.55754,37.287721 C 130.73463,86.140753 137.41091,74.628442 137.09133,64.102633 137.09126,46.849641 125.5835,32.1351 98.480703,32.661887 Z"/>
    <path fill="#FFFFFF" d="M 99.616178,84.759027 88.88798,104.69593 c 6.149885,0.14826 12.35754,-1.44339 18.37572,-3.94198 l -0.002,-15.889254 c -3e-5,-0.239854 -0.15952,-0.364115 -0.32136,-0.363057 l -6.983289,0.04567 c -0.13375,8.75e-4 -0.340873,0.07796 -0.340873,0.211716 z"/>
    <!-- Counter dot in the gradient start color (deep purple) so it reads
         as a "hole" through the bubble onto the background. -->
    <path fill="#4B0856" d="m 105.31412,74.805437 c -0.39064,0.179009 -2.54678,1.671779 -2.48303,3.153288 0.0176,0.429863 0.094,0.583056 0.27148,0.808729 0.60938,0.774968 2.67268,1.033285 3.81614,0.551729 0.27855,-0.117302 0.48494,-0.266759 0.54759,-0.71804 0.17447,-1.257075 -0.18199,-3.175538 -1.16659,-3.775813 -0.37248,-0.20411 -0.70766,-0.147255 -0.98559,-0.01989 z"/>
  </g>
</svg>
"""


def render_glyph(size: int) -> Image.Image:
    """Render the white glyph SVG at `size` px, then crop to ink bbox."""
    TMP.mkdir(exist_ok=True)
    src = TMP / "glyph.svg"
    src.write_text(WHITE_GLYPH_SVG)
    raw = TMP / f"glyph_raw_{size}.png"
    # Render larger than needed so the crop has high precision, then crop.
    render_size = size * 2
    subprocess.run(
        [
            "inkscape",
            "--export-type=png",
            f"--export-filename={raw}",
            f"--export-width={render_size}",
            f"--export-height={render_size}",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    img = Image.open(raw).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img = img.resize((size, size * img.height // img.width), Image.LANCZOS) \
        if img.width >= img.height \
        else img.resize((size * img.width // img.height, size), Image.LANCZOS)
    return img


def make_gradient_bg(size: int) -> Image.Image:
    """135deg linear gradient from deep purple (top-left) to indigo (bottom-right)."""
    bg = Image.new("RGB", (size, size))
    px = bg.load()
    # Project (x, y) onto the 135deg axis: t = (x + y) / (2*(size-1))
    denom = max(2 * (size - 1), 1)
    for y in range(size):
        for x in range(size):
            t = (x + y) / denom
            r = int(GRAD_START[0] + (GRAD_END[0] - GRAD_START[0]) * t)
            g = int(GRAD_START[1] + (GRAD_END[1] - GRAD_START[1]) * t)
            b = int(GRAD_START[2] + (GRAD_END[2] - GRAD_START[2]) * t)
            px[x, y] = (r, g, b)
    return bg.convert("RGBA")


def composite_icon(size: int, glyph_fraction: float, shape: str) -> Image.Image:
    """Build a launcher icon: gradient background + centered glyph,
    masked to `shape` ('square' = rounded square, 'circle', 'none')."""
    bg = make_gradient_bg(size)

    target_long_side = int(size * glyph_fraction)
    raw_glyph = render_glyph(target_long_side)
    # raw_glyph is sized so its longest side = target_long_side; center
    # horizontally, then shift DOWN to compensate for the leg extending
    # below the visual center of mass.
    gx = (size - raw_glyph.width) // 2
    gy = (size - raw_glyph.height) // 2 + int(size * GLYPH_Y_OFFSET_FRACTION)
    bg.alpha_composite(raw_glyph, dest=(gx, gy))

    if shape == "circle":
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    elif shape == "square":
        mask = Image.new("L", (size, size), 0)
        radius = int(size * 0.18)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size - 1, size - 1), radius=radius, fill=255
        )
    else:
        return bg  # full square, no mask

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(bg, mask=mask)
    return out


# Flat-fill gradient bubble SVG for legacy launcher PNGs (API <= 25).
# Renders the bubble shape on a transparent background — NO plate, NO
# shape mask. Matches the iOS / Android-pre-Oreo pattern where the icon
# IS the visible shape, sitting directly on the launcher wallpaper.
# We bake the brand gradient into the path fills here since Inkscape's
# snap build doesn't reliably render <linearGradient> from SVG.
FREE_BUBBLE_SVG_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="9.55 7.62 97.34 89.37">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#4B0856"/>
      <stop offset="100%" stop-color="#6366F1"/>
    </linearGradient>
  </defs>
  <g transform="translate(-33.476177,-22.491808)">
    <!-- Bubble body -->
    <path fill="url(#g)" d="m 98.480703,32.661887 c -30.726336,0.597216 -52.022542,22.198386 -52.272494,40.506784 -0.195272,14.303125 4.952902,20.403884 15.42552,29.007619 l 6.13237,15.81638 36.121771,-56.527418 c 0,0 2.01316,-4.094738 7.55658,-2.796877 l 5.55754,37.287721 C 130.73463,86.140753 137.41091,74.628442 137.09133,64.102633 137.09126,46.849641 125.5835,32.1351 98.480703,32.661887 Z"/>
    <!-- Right leg -->
    <path fill="url(#g)" d="M 99.616178,84.759027 88.88798,104.69593 c 6.149885,0.14826 12.35754,-1.44339 18.37572,-3.94198 l -0.002,-15.889254 c -3e-5,-0.239854 -0.15952,-0.364115 -0.32136,-0.363057 l -6.983289,0.04567 c -0.13375,8.75e-4 -0.340873,0.07796 -0.340873,0.211716 z"/>
    <!-- Counter dot (slightly darker than bubble center to read as a hole) -->
    <path fill="#3a0644" d="m 105.31412,74.805437 c -0.39064,0.179009 -2.54678,1.671779 -2.48303,3.153288 0.0176,0.429863 0.094,0.583056 0.27148,0.808729 0.60938,0.774968 2.67268,1.033285 3.81614,0.551729 0.27855,-0.117302 0.48494,-0.266759 0.54759,-0.71804 0.17447,-1.257075 -0.18199,-3.175538 -1.16659,-3.775813 -0.37248,-0.20411 -0.70766,-0.147255 -0.98559,-0.01989 z"/>
  </g>
</svg>
"""


def render_free_bubble(canvas_size: int, glyph_fraction: float) -> Image.Image:
    """Render the gradient bubble onto a transparent canvas at `canvas_size`,
    sized to `glyph_fraction` of the canvas, with the same Y-offset rule
    used for the gradient-plate variant so the bubble's visual center
    sits slightly below the canvas center."""
    TMP.mkdir(exist_ok=True)
    src = TMP / "free_bubble.svg"
    src.write_text(FREE_BUBBLE_SVG_TEMPLATE)
    target_long_side = int(canvas_size * glyph_fraction)
    raw = TMP / f"free_bubble_{target_long_side}.png"
    subprocess.run(
        [
            "inkscape",
            "--export-type=png",
            f"--export-filename={raw}",
            f"--export-width={target_long_side * 2}",
            f"--export-height={target_long_side * 2}",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    bubble = Image.open(raw).convert("RGBA")
    bbox = bubble.getbbox()
    if bbox:
        bubble = bubble.crop(bbox)
    # Resize so the longest side equals target_long_side.
    if bubble.width >= bubble.height:
        bubble = bubble.resize(
            (target_long_side, target_long_side * bubble.height // bubble.width),
            Image.LANCZOS,
        )
    else:
        bubble = bubble.resize(
            (target_long_side * bubble.width // bubble.height, target_long_side),
            Image.LANCZOS,
        )

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    bx = (canvas_size - bubble.width) // 2
    by = (canvas_size - bubble.height) // 2 + int(canvas_size * GLYPH_Y_OFFSET_FRACTION)
    canvas.alpha_composite(bubble, dest=(bx, by))
    return canvas


# ---------------------------------------------------------------------------
# Android pieces
# ---------------------------------------------------------------------------

ADAPTIVE_BG_VECTOR = """<?xml version="1.0" encoding="utf-8"?>
<!--
  Adaptive icon background: 135deg gradient from deep purple to indigo,
  matching the design sheet's `.app-icon.gradient` treatment.
  Replaces the previous flat @color/primary.
-->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path android:pathData="M0,0 L108,0 L108,108 L0,108 Z">
        <aapt:attr xmlns:aapt="http://schemas.android.com/aapt" name="android:fillColor">
            <gradient
                android:type="linear"
                android:startX="0"
                android:startY="0"
                android:endX="108"
                android:endY="108">
                <item android:offset="0.0" android:color="#FF4B0856"/>
                <item android:offset="1.0" android:color="#FF6366F1"/>
            </gradient>
        </aapt:attr>
    </path>
</vector>
"""

# Foreground vector with the larger glyph fraction. Anchor math:
# scale = (108 * GLYPH_FRACTION_ADAPTIVE) / 97.34 ≈ 0.7767
# pad = (108 - 108*GLYPH_FRACTION_ADAPTIVE) / 2 = 108 * (1-frac) / 2
def adaptive_fg_vector() -> str:
    pad_x = 108 * (1 - GLYPH_FRACTION_ADAPTIVE) / 2
    pad_y = pad_x + 108 * GLYPH_Y_OFFSET_FRACTION
    scale = (108 * GLYPH_FRACTION_ADAPTIVE) / 97.34
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!--
  Adaptive icon foreground for Assistant.
  Source: context/public/design/logo-icon.svg
  Glyph fills {int(GLYPH_FRACTION_ADAPTIVE*100)}% of the 108x108 canvas (stays inside the 72x72 safe zone
  with a small margin). Counter dot drawn in gradient-start color so it
  punches through onto the background gradient.
-->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <group
        android:translateX="{pad_x:.4f}"
        android:translateY="{pad_y:.4f}"
        android:scaleX="{scale:.4f}"
        android:scaleY="{scale:.4f}">
        <group
            android:translateX="-9.55"
            android:translateY="-7.62">
            <group
                android:translateX="-33.476177"
                android:translateY="-22.491808">
                <!-- Main bubble body -->
                <path
                    android:fillColor="#FFFFFFFF"
                    android:pathData="M 98.480703,32.661887 C 67.754367,33.259103 46.458161,54.860273 46.208209,73.168671 46.012937,87.471796 51.161111,93.572555 61.633729,102.17629 L 67.766099,117.99267 103.88787,61.465252 C 103.88787,61.465252 105.90103,57.370514 111.44445,58.668375 L 117.00199,95.956096 C 130.73463,86.140753 137.41091,74.628442 137.09133,64.102633 137.09126,46.849641 125.5835,32.1351 98.480703,32.661887 Z"/>
                <!-- Right leg -->
                <path
                    android:fillColor="#FFFFFFFF"
                    android:pathData="M 99.616178,84.759027 88.88798,104.69593 C 95.037865,104.84419 101.24552,103.25254 107.2637,100.75395 L 107.2617,84.864696 C 107.26167,84.624842 107.10218,84.500581 106.94034,84.501639 L 99.957051,84.547309 C 99.823301,84.548184 99.616178,84.62545 99.616178,84.759027 Z"/>
                <!-- Counter dot in gradient-start color -->
                <path
                    android:fillColor="#FF4B0856"
                    android:pathData="M 105.31412,74.805437 C 104.92348,74.984446 102.76734,76.477216 102.83109,77.958725 102.84869,78.388588 102.92509,78.541781 103.10257,78.767454 103.71195,79.542422 105.77525,79.800739 106.91871,79.319183 107.19726,79.201881 107.40365,79.052424 107.4663,78.601143 107.64077,77.344068 107.28431,75.425605 106.29971,74.82533 105.92723,74.62122 105.59205,74.678075 105.31412,74.805437 Z"/>
            </group>
        </group>
    </group>
</vector>
"""

ADAPTIVE_ICON_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background"/>
    <foreground android:drawable="@drawable/ic_launcher_foreground"/>
</adaptive-icon>
"""


def main() -> None:
    TMP.mkdir(exist_ok=True)

    # 1. Web PNGs — gradient + larger glyph, full square (no mask, browsers
    #    apply their own corner radius on home-screen install).
    for size in (192, 512):
        out = FRONTEND_DIR / f"icon-{size}.png"
        img = composite_icon(size, GLYPH_FRACTION_LEGACY, shape="none")
        img.save(out, "PNG")
        print(f"wrote {out}")

    # 2. Android legacy launcher PNGs — free-standing gradient bubble on
    #    transparent background. No plate, no shape mask: the bubble's
    #    own outline IS the visible icon, matching the OS pattern for
    #    stock Android 5.x icons. Same image for both ic_launcher and
    #    ic_launcher_round since neither needs a shape mask.
    for density, size in DENSITIES.items():
        d = RES / f"mipmap-{density}"
        d.mkdir(parents=True, exist_ok=True)
        img = render_free_bubble(size, GLYPH_FRACTION_FREE)
        img.save(d / "ic_launcher.png", "PNG")
        img.save(d / "ic_launcher_round.png", "PNG")
        print(f"{density}: {size}x{size} -> {d}/ic_launcher{{,_round}}.png (free bubble)")

    # 3. Android adaptive-icon vector drawables.
    (RES / "drawable/ic_launcher_background.xml").write_text(ADAPTIVE_BG_VECTOR)
    print("wrote drawable/ic_launcher_background.xml")
    (RES / "drawable/ic_launcher_foreground.xml").write_text(adaptive_fg_vector())
    print("wrote drawable/ic_launcher_foreground.xml")

    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        (RES / "mipmap-anydpi-v26" / name).write_text(ADAPTIVE_ICON_XML)
        print(f"wrote mipmap-anydpi-v26/{name}")

    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
