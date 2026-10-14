"""make_icon - draw the Log Grabber icon (log cross-section) -> icon.ico.

Dev tool; regenerate with `py make_icon.py` (needs pillow).
"""

import math
import random

from PIL import Image, ImageDraw

SIZE = 512
CX = CY = SIZE / 2

BARK_DARK = (59, 38, 22)
BARK = (96, 62, 36)
WOOD = (212, 165, 106)
WOOD_LIGHT = (226, 188, 137)
RING = (176, 128, 76)
HEART = (168, 116, 64)


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Bark: irregular dark outer edge.
    random.seed(7)
    bark_pts = []
    for i in range(360):
        a = math.radians(i)
        r = 246 - random.uniform(0, 14)
        bark_pts.append((CX + r * math.cos(a), CY + r * math.sin(a)))
    d.polygon(bark_pts, fill=BARK_DARK)

    # Inner bark ring.
    d.ellipse((CX - 232, CY - 232, CX + 232, CY + 232), fill=BARK)
    # Wood face.
    d.ellipse((CX - 214, CY - 214, CX + 214, CY + 214), fill=WOOD)

    # Subtle radial grain patches.
    for _ in range(90):
        a = random.uniform(0, 2 * math.pi)
        r0 = random.uniform(20, 190)
        x0 = CX + r0 * math.cos(a)
        y0 = CY + r0 * math.sin(a)
        x1 = x0 + random.uniform(-26, 26)
        y1 = y0 + random.uniform(-26, 26)
        d.line((x0, y0, x1, y1), fill=WOOD_LIGHT, width=4)

    # Growth rings (slightly off-centre, wobbly).
    rx, ry = CX + 6, CY - 4
    for radius in (190, 158, 126, 94, 62):
        pts = []
        for i in range(360):
            a = math.radians(i)
            wob = 1 + 0.05 * math.sin(a * 5 + radius)
            pts.append((rx + radius * wob * math.cos(a),
                        ry + radius * wob * math.sin(a)))
        d.line(pts + [pts[0]], fill=RING, width=5)

    # Heartwood centre.
    d.ellipse((rx - 34, ry - 34, rx + 34, ry + 34), fill=HEART)

    # Soft shading: darker lower-right crescent on the wood face.
    shade = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.ellipse((CX - 214, CY - 214, CX + 214, CY + 214), fill=(60, 30, 8, 60))
    sd.ellipse((CX - 200, CY - 230, CX + 228, CY + 200), fill=(0, 0, 0, 0))
    img = Image.alpha_composite(img, shade)

    img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((256, 256)).save("icon.png")
    print("wrote icon.ico / icon.png")


if __name__ == "__main__":
    main()
