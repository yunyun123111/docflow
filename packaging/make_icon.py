"""生成 DocFlow 应用图标（packaging/app.ico）：纯 PIL 绘制，不依赖外部素材。

用法：python packaging/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZE = 1024
BG_TOP = (31, 41, 55)        # 侧栏深灰
ACCENT = (34, 211, 238)      # cyan-400
ACCENT_DARK = (14, 116, 144)
PAPER = (248, 250, 252)


def rounded(draw: ImageDraw.ImageDraw, box, radius, fill) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_icon(scale: int = 1) -> Image.Image:
    s = SIZE * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = lambda v: int(v * scale)  # noqa: E731

    # 底板：深色圆角方块
    rounded(d, (r(40), r(40), s - r(40), s - r(40)), r(180), BG_TOP + (255,))

    # 文件夹背板
    rounded(d, (r(150), r(300), r(880), r(840)), r(60), ACCENT_DARK + (255,))
    rounded(d, (r(150), r(250), r(470), r(340)), r(40), ACCENT_DARK + (255,))

    # 文档页（微微右上倾斜的两页，表现「流动」）
    for i, (dx, dy, tone) in enumerate(((r(60), r(-30), (226, 232, 240)),
                                        (r(20), r(0), PAPER))):
        box = (r(250) + dx, r(330) + dy, r(700) + dx, r(770) + dy)
        d.polygon([(box[0], box[1] + r(60)), (box[0] + r(60), box[1]),
                   (box[2], box[1]), (box[2], box[3]), (box[0], box[3])], fill=tone + (255,))
        for k in range(4):
            y = box[1] + r(150) + k * r(80)
            w = r(300) if k < 3 else r(180)
            d.rounded_rectangle((box[0] + r(50), y, box[0] + r(50) + w, y + r(34)),
                                radius=r(17), fill=(ACCENT_DARK[0], ACCENT_DARK[1], ACCENT_DARK[2], 190))

    # 流向箭头（右下角，压在文档上）
    d.ellipse((r(640), r(620), r(900), r(880)), fill=ACCENT + (255,))
    d.polygon([(r(720), r(790)), (r(830), r(700)), (r(830), r(750)), (r(790), r(790))],
              fill=BG_TOP + (255,))
    d.rounded_rectangle((r(712), r(770), r(838), r(812)), radius=r(21), fill=BG_TOP + (255,))
    return img


def main() -> None:
    base = draw_icon()
    sizes = [256, 128, 64, 48, 32, 16]
    out = HERE / "app.ico"
    base.resize((256, 256), Image.LANCZOS).save(
        out, format="ICO", sizes=[(n, n) for n in sizes], append_images=[
            base.resize((n, n), Image.LANCZOS) for n in sizes])
    base.save(HERE / "app.png")
    print(f"已生成 {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()