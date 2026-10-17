# -*- coding: utf-8 -*-
"""atlas 产物 vs psd-tools 产物：按各自画布原点对齐后 diff。"""
import json
from pathlib import Path

from PIL import Image, ImageChops

PLUGIN = Path(__file__).resolve().parent.parent
SAMPLES = PLUGIN / "tools" / "samples"

for emo in ("smile", "paint"):
    old_full = Image.open(SAMPLES / f"Noah_{emo}.png").convert("RGBA")
    meta = json.loads((PLUGIN / "asset" / "characters" / "Noah" / "meta.json")
                      .read_text(encoding="utf-8"))
    ox, oy = int(meta["x"]), int(meta["y"])
    new_img = Image.open(SAMPLES / f"Noah_atlas_{emo}.png").convert("RGBA")
    nx, ny = 0, 0  # atlas 统一并集 bbox 的画布原点（bench 输出 bbox=(0,0)）

    W, H = old_full.size
    n = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    n.paste(new_img, (nx, ny), new_img)

    diff = ImageChops.difference(old_full, n)
    bbox = diff.getbbox()
    print(f"{emo}: old webp 放置=({ox},{oy})  atlas 原点=({nx},{ny})")
    print(f"  diff bbox={bbox}")
    if bbox:
        region = diff.crop(bbox).convert("RGB")
        print(f"  diff 区 extrema={region.getextrema()}  "
              f"diff 区平均={[round(s / (region.width * region.height), 2) for s in region.resize((1, 1)).getpixel((0, 0))]}")

    h = 700
    pw = round(W * h / H)
    from build_cards import _label, _fit
    sheet = Image.new("RGB", (pw * 2 + 12, h + 28), (30, 30, 30))
    sheet.paste(_label(_fit(old_full, pw), "OLD (psd-tools)"), (0, 0))
    sheet.paste(_label(_fit(n, pw), "NEW (atlas)"), (pw + 12, 0))
    out = SAMPLES / f"Noah_compare_{emo}.png"
    sheet.save(str(out))
    print(f"  -> {out}")
