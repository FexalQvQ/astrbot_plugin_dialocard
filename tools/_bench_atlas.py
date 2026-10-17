# -*- coding: utf-8 -*-
"""计时验证：atlas 运行时合成 11 个 emotion 的速度 + 样张导出。"""
import json
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from PIL import Image

import atlas_render as ar

char_cfg = json.loads(
    (PLUGIN_DIR / "tools" / "characters" / "Noah.json").read_text(encoding="utf-8"))

t0 = time.perf_counter()
c = ar.get_character("Noah")
t_load = time.perf_counter() - t0
print(f"首次加载（manifest+2 页解码）: {t_load * 1000:.0f} ms")

sample_dir = PLUGIN_DIR / "tools" / "samples"
sample_dir.mkdir(parents=True, exist_ok=True)

times = []
first_composite = None
for emo, cfg in char_cfg["emotions"].items():
    t = time.perf_counter()
    img, x, y = c.render(cfg["on"])
    dt = (time.perf_counter() - t) * 1000
    times.append(dt)
    if emo == "smile":
        img.save(str(sample_dir / "Noah_atlas_smile.png"))
    if emo == "paint":
        img.save(str(sample_dir / "Noah_atlas_paint.png"))
    print(f"  {emo:<10} {dt:6.1f} ms  bbox=({x},{y}) size={img.size if img else None}")

print(f"\n合成+构图均值: {sum(times) / len(times):.1f} ms / "
      f"最大 {max(times):.1f} ms（首次含单元裁剪缓存预热）")

# 二次渲染（缓存全热，模拟 bot 稳态）
t = time.perf_counter()
for _ in range(10):
    c.render(char_cfg["emotions"]["smile"]["on"])
per = (time.perf_counter() - t) * 100
print(f"缓存全热稳态: {per:.1f} ms / 次（10 次均值）")
