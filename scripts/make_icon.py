#!/usr/bin/env python3
"""Render the plugin icon (512x512 PNG) — a portable compressor cooler on a frost-blue plate.

Drawn at 4x and downsampled for anti-aliasing. Dev-only: needs Pillow (pip install pillow).

    .venv/bin/python scripts/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = (
    Path(__file__).resolve().parents[1]
    / "Dometic CFX.indigoPlugin"
    / "Contents"
    / "Resources"
)
SIZE = 512
S = 4  # supersampling factor
W = SIZE * S


def rgba(r: int, g: int, b: int, a: int = 255) -> tuple[int, int, int, int]:
    return (r, g, b, a)


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        for x in range(size):
            px[x, y] = c
    return img


def rounded_mask(size: int, box: tuple, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def shadow(size: int, box: tuple, radius: int, blur: int, alpha: int) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=radius, fill=(0, 0, 0, alpha))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def main() -> None:
    # --- plate: rounded square with a frost-blue gradient and a soft inner highlight
    plate = vertical_gradient(W, (70, 150, 205), (22, 74, 128))
    plate_mask = rounded_mask(W, (0, 0, W - 1, W - 1), radius=int(W * 0.22))
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    canvas.paste(plate, (0, 0), plate_mask)

    # inner glow near the top edge
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(
        (W * 0.03, W * 0.03, W * 0.97, W * 0.55),
        radius=int(W * 0.2),
        fill=(255, 255, 255, 60),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(W * 0.06))
    glow.putalpha(ImageChops.multiply(glow.getchannel("A"), plate_mask))
    canvas = Image.alpha_composite(canvas, glow)

    # --- cooler body geometry (front view, slightly wide chest)
    bx0, by0, bx1, by1 = W * 0.16, W * 0.30, W * 0.84, W * 0.80
    lid_h = W * 0.13
    body_r = int(W * 0.06)

    # drop shadow under the cooler
    canvas = Image.alpha_composite(
        canvas,
        shadow(
            W,
            (bx0 + W * 0.02, by0 + W * 0.06, bx1 + W * 0.02, by1 + W * 0.05),
            body_r,
            int(W * 0.03),
            110,
        ),
    )

    body = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    # lower body: ice white with a faint vertical gradient
    body_grad = vertical_gradient(W, (246, 249, 252), (214, 226, 238))
    body_mask = rounded_mask(W, (bx0, by0, bx1, by1), body_r)
    body.paste(body_grad, (0, 0), body_mask)
    # lid: a slightly darker cap with its own rounded top
    lid = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lid)
    ld.rounded_rectangle(
        (bx0, by0, bx1, by0 + lid_h), radius=body_r, fill=(228, 236, 245, 255)
    )
    ld.rectangle((bx0, by0 + lid_h * 0.55, bx1, by0 + lid_h), fill=(228, 236, 245, 255))
    body = Image.alpha_composite(body, lid)
    bd = ImageDraw.Draw(body)
    # lid seam
    bd.line(
        (bx0 + W * 0.01, by0 + lid_h, bx1 - W * 0.01, by0 + lid_h),
        fill=(160, 180, 200, 255),
        width=int(W * 0.008),
    )
    # lid latch/handle recess in the middle of the lid
    lw = W * 0.14
    bd.rounded_rectangle(
        ((W - lw) / 2, by0 + lid_h * 0.30, (W + lw) / 2, by0 + lid_h * 0.72),
        radius=int(W * 0.02),
        fill=(198, 212, 226, 255),
    )
    # side handles (folded-down aluminium handles)
    hh = W * 0.09
    for x0, x1 in (
        (bx0 - W * 0.05, bx0 + W * 0.015),
        (bx1 - W * 0.015, bx1 + W * 0.05),
    ):
        bd.rounded_rectangle(
            (x0, by0 + lid_h + W * 0.05, x1, by0 + lid_h + W * 0.05 + hh),
            radius=int(W * 0.025),
            fill=(120, 140, 160, 255),
        )
        bd.rounded_rectangle(
            (
                x0 + W * 0.012,
                by0 + lid_h + W * 0.062,
                x1 - W * 0.012,
                by0 + lid_h + W * 0.038 + hh,
            ),
            radius=int(W * 0.018),
            fill=(196, 208, 220, 255),
        )
    # feet
    for fx in (bx0 + W * 0.08, bx1 - W * 0.14):
        bd.rounded_rectangle(
            (fx, by1 - W * 0.005, fx + W * 0.06, by1 + W * 0.025),
            radius=int(W * 0.01),
            fill=(90, 105, 120, 255),
        )
    # display panel on the front (dark, with a blue-lit digit block)
    pw, ph = W * 0.24, W * 0.085
    px0, py0 = bx1 - W * 0.06 - pw, by1 - W * 0.06 - ph
    bd.rounded_rectangle(
        (px0, py0, px0 + pw, py0 + ph), radius=int(W * 0.015), fill=(28, 36, 46, 255)
    )
    bd.rounded_rectangle(
        (px0 + W * 0.02, py0 + ph * 0.28, px0 + pw - W * 0.02, py0 + ph * 0.72),
        radius=int(W * 0.006),
        fill=(120, 205, 255, 255),
    )

    canvas = Image.alpha_composite(canvas, body)

    # --- snowflake badge on the body, lower left
    flake = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    fd = ImageDraw.Draw(flake)
    cx, cy, R = bx0 + W * 0.20, by0 + lid_h + W * 0.20, W * 0.105
    blue = (31, 111, 174, 255)
    wline = int(W * 0.018)
    for k in range(6):
        a = math.radians(k * 60 + 90)
        ex, ey = cx + R * math.cos(a), cy + R * math.sin(a)
        fd.line((cx, cy, ex, ey), fill=blue, width=wline)
        fd.ellipse(
            (
                ex - wline * 0.55,
                ey - wline * 0.55,
                ex + wline * 0.55,
                ey + wline * 0.55,
            ),
            fill=blue,
        )
        for frac in (0.52, 0.78):
            bxp, byp = cx + R * frac * math.cos(a), cy + R * frac * math.sin(a)
            for side in (-1, 1):
                b = a + side * math.radians(55)
                L = R * 0.30
                fd.line(
                    (bxp, byp, bxp + L * math.cos(b), byp + L * math.sin(b)),
                    fill=blue,
                    width=int(wline * 0.8),
                )
    fd.ellipse(
        (cx - wline * 0.9, cy - wline * 0.9, cx + wline * 0.9, cy + wline * 0.9),
        fill=blue,
    )
    canvas = Image.alpha_composite(canvas, flake)

    # --- subtle top-edge highlight on the plate (glassy look like the other plugin icons)
    hl = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    hd.rounded_rectangle(
        (W * 0.02, W * 0.015, W * 0.98, W * 0.08),
        radius=int(W * 0.04),
        fill=(255, 255, 255, 70),
    )
    hl = hl.filter(ImageFilter.GaussianBlur(W * 0.012))
    hl.putalpha(ImageChops.multiply(hl.getchannel("A"), plate_mask))
    canvas = Image.alpha_composite(canvas, hl)

    out = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.mkdir(parents=True, exist_ok=True)
    out.save(OUT / "icon.png", optimize=True)
    print(f"wrote {OUT / 'icon.png'} ({SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
