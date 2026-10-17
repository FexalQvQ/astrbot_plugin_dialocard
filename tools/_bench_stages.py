# -*- coding: utf-8 -*-
"""分解 render 各阶段耗时。"""
import json
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import atlas_render as ar

c = ar.get_character("Noah")
on = json.loads((PLUGIN_DIR / "tools" / "characters" / "Noah.json")
                .read_text(encoding="utf-8"))["emotions"]["smile"]["on"]

# 预热单元缓存
c.composite(on)

N = 10
t = time.perf_counter()
for _ in range(N):
    comp = c.composite(on)
t_comp = (time.perf_counter() - t) / N

t = time.perf_counter()
for _ in range(N):
    trans = c.transform_to_canvas(comp)
t_trans = (time.perf_counter() - t) / N

t = time.perf_counter()
for _ in range(N):
    bbox = trans.getbbox()
    out = trans.crop(bbox)
t_crop = (time.perf_counter() - t) / N

print(f"composite（40 单元 alpha_composite/混合）: {t_comp*1000:6.1f} ms")
print(f"transform（resize LANCZOS + rotate BICUBIC）: {t_trans*1000:6.1f} ms")
print(f"getbbox+crop: {t_crop*1000:6.1f} ms")

# transform 再拆：resize vs rotate
from PIL import Image
w, h = comp.size
import math
rad = math.radians(ar.UNIFORM_ANGLE)
grow_h = w * math.sin(rad) + h * math.cos(rad)
scale = ar.UNIFORM_BBOX_H / grow_h
t = time.perf_counter()
for _ in range(N):
    r = comp.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
t_rs = (time.perf_counter() - t) / N
t = time.perf_counter()
for _ in range(N):
    rot = r.rotate(ar.UNIFORM_ANGLE, resample=Image.BICUBIC, expand=True)
t_rot = (time.perf_counter() - t) / N
print(f"  其中 resize LANCZOS {w}x{h}->{r.size}: {t_rs*1000:6.1f} ms")
print(f"  其中 rotate BICUBIC -> {rot.size}: {t_rot*1000:6.1f} ms")
