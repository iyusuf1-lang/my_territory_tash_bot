#!/usr/bin/env python3
"""
Territory Tashkent — PWA Icon Generator
Barcha kerakli ikonlarni yaratadi (PNG formatida)
pip install Pillow --break-system-packages
"""

import os
import math

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

def create_icon(size: int, output_path: str):
    """Territory ikonkasini yaratish"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background — rounded square
    padding = size * 0.08
    bg_color = (13, 13, 26)  # #0d0d1a
    accent   = (233, 69, 96)  # #e94560

    radius = size * 0.2
    draw.rounded_rectangle(
        [padding, padding, size - padding, size - padding],
        radius=radius,
        fill=bg_color,
    )

    # Grid lines (mayda)
    grid_step = size // 8
    for i in range(0, size, grid_step):
        draw.line([(padding, i), (size - padding, i)], fill=(233, 69, 96, 20), width=1)
        draw.line([(i, padding), (i, size - padding)], fill=(233, 69, 96, 20), width=1)

    # Marker / location pin
    cx = size // 2
    cy = int(size * 0.42)
    r  = int(size * 0.22)

    # Pin circle
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        fill=accent,
    )

    # Inner white dot
    ir = int(r * 0.38)
    draw.ellipse(
        [cx - ir, cy - ir, cx + ir, cy + ir],
        fill=(255, 255, 255),
    )

    # Pin tail
    tail_w = int(r * 0.45)
    tail_h = int(size * 0.28)
    draw.polygon(
        [
            (cx - tail_w, cy + int(r * 0.7)),
            (cx + tail_w, cy + int(r * 0.7)),
            (cx, cy + r + tail_h),
        ],
        fill=accent,
    )

    # Bottom text "TT"
    if size >= 128:
        font_size = max(int(size * 0.13), 8)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        text = "TT"
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (size - tw) // 2
        ty = int(size * 0.76)
        draw.text((tx, ty), text, fill=accent, font=font)

    # Glow effect — subtle
    glow_r = int(r * 1.4)
    for i in range(3):
        alpha = 30 - i * 8
        overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        ov_draw.ellipse(
            [cx - glow_r - i*4, cy - glow_r - i*4,
             cx + glow_r + i*4, cy + glow_r + i*4],
            fill=(233, 69, 96, alpha),
        )
        img = Image.alpha_composite(img, overlay)

    img.save(output_path, "PNG")
    print(f"✅ Icon created: {output_path} ({size}x{size})")

def main():
    # icons/ papkasini yaratish
    os.makedirs("icons", exist_ok=True)

    sizes = [72, 96, 128, 192, 512]
    for size in sizes:
        create_icon(size, f"icons/icon-{size}.png")

    print("\n🎉 Barcha ikonlar tayyor!")
    print("📁 icons/ papkasini GitHub Pages ga yuklang")

if __name__ == "__main__":
    if not PIL_AVAILABLE:
        print("⚠️  Pillow yo'q. O'rnatish:")
        print("   pip install Pillow --break-system-packages")
        print("\nAlternativa — https://favicon.io/favicon-generator/ saytidan ikonka yarating")
        print("Nom: TT, Background: #0d0d1a, Text: #e94560")
    else:
        main()
