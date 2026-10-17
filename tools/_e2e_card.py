# -*- coding: utf-8 -*-
"""端到端：atlas 角色层按 (x,y) 贴 background -> 整卡目检。"""
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from PIL import Image
import atlas_render as ar

emotions = json.loads((PLUGIN / "asset" / "characters" / "Noah" / "emotions.json")
                      .read_text(encoding="utf-8"))["emotions"]
bg = Image.open(PLUGIN / "asset" / "background" / "background.webp").convert("RGBA")
if bg.size != (1920, 1200):
    bg = bg.resize((1920, 1200), Image.LANCZOS)

for emo in ("smile", "paint"):
    on = emotions[emo]["on"]
    img, x, y = ar.get_character("Noah").render(on)
    canvas = bg.copy()
    canvas.paste(img, (x, y), img)
    out = PLUGIN / "tools" / "samples" / f"Noah_atlascard_{emo}.png"
    canvas.save(str(out))
    print(f"{emo}: pos=({x},{y}) size={img.size} -> {out.name}")
