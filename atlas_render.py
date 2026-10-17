"""图集运行时合成管线：atlas_N.webp + atlas.json -> 角色画布 -> 词卡角色层。

资产格式（由 tools/build_cards.py atlas 子命令产出）：
  asset/characters/{角色}/atlas_0.webp ...   4096x4096 图集页
  asset/characters/{角色}/atlas.json         单元清单（z 序/混合/画布坐标/页内位置/互斥组/默认可见性）

每个 emotion 只需给出要"开"的图层名列表，合成规则与离线管线一致：
  - 命中 on 的单元可见，其祖先组在导出时已烧进单元，无需处理；
  - 与 on 同互斥组的单元自动关闭；Pale* 强制关闭；
  - 其余单元保持 PSD 默认可见性（记在 manifest.layers）。
角色页面图与单元裁剪在进程内缓存，单次合成毫秒级。
"""
from __future__ import annotations

import collections
import json
import math
import re
import threading
from pathlib import Path

from PIL import Image, ImageChops

PLUGIN_DIR = Path(__file__).resolve().parent
ASSET_DIR = PLUGIN_DIR / "asset" / "characters"  # 出厂内置（随包分发，只读）


def _resolve_data_dir() -> Path:
    """用户数据目录 data/plugin_data/astrbot_plugin_dialocard——插件更新/重装不覆盖。"""
    try:
        from astrbot.api import StarTools
        return Path(StarTools.get_data_dir("astrbot_plugin_dialocard"))
    except Exception:
        return PLUGIN_DIR.parents[1] / "plugin_data" / "astrbot_plugin_dialocard"


DATA_DIR = _resolve_data_dir()
# 角色目录搜索序：下载/用户数据优先，出厂内置回退（写入只进第一项）
CHAR_DIRS = [DATA_DIR / "characters", ASSET_DIR]


def find_char_dir(char: str):
    """角色目录解析：plugin_data 优先，出厂回退；无此角色返回 None。"""
    for d in CHAR_DIRS:
        if (d / char).is_dir():
            return d / char
    return None


def list_characters() -> list:
    out: list = []
    for d in CHAR_DIRS:
        if not d.is_dir():
            continue
        for c in sorted(d.iterdir()):
            if c.is_dir() and (c / "atlas.json").exists() and c.name not in out:
                out.append(c.name)
    return out


def clear_cache():
    """下载/解压角色后清空实例缓存，让新角色立即可见。"""
    with _LOCK:
        _CACHE.clear()

CANVAS_W, CANVAS_H = 1920, 1200
MODULE_REV = "20261016-clip-layout"  # 热重载新鲜度标记（main.py 初始化日志打印）
UNIFORM_ANGLE = 3
UNIFORM_BBOX_H = 1200
UNIFORM_ANCHOR_X = 480

# 全局构图：角度/缩放/平移，对全体角色生效；transform.json 持久化到 plugin_data
# （更新/重装不丢），出厂构图（随包 asset/transform.json）只读回退。
# 默认值 = WebUI 调定的最终构图（替换原 scale 1.0 / dy 0 的原作偏移参数）：
# 放大 1.34x、整体下移 265px；transform.json 缺失/损坏时回落到此默认。
TRANSFORM_PATH = DATA_DIR / "transform.json"
TRANSFORM_FALLBACK = ASSET_DIR.parent / "transform.json"
_TRANSFORM = {"angle": float(UNIFORM_ANGLE), "scale": 1.34, "dx": 0.0, "dy": 265.0}


def _load_transform():
    for path in (TRANSFORM_PATH, TRANSFORM_FALLBACK):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # 无文件/坏文件时试下一级，最后用默认构图
        for k in _TRANSFORM:
            v = data.get(k)
            if isinstance(v, (int, float)):
                _TRANSFORM[k] = float(v)
        return


_load_transform()

_BLEND_FN = {
    "MULTIPLY": ImageChops.multiply,
    "SCREEN": ImageChops.screen,
    "OVERLAY": ImageChops.overlay,
    "SOFT_LIGHT": ImageChops.soft_light,
    "HARD_LIGHT": ImageChops.hard_light,
    "DIFFERENCE": ImageChops.difference,
}


def _group_key(name: str):
    """差分互斥组：名字尾部编号剥离后取首段（ArmL01/ArmL04 -> ArmL 同组互斥，
    否则每个手部姿势各成一组，压不掉默认可见的另一只手）。"""
    parts = name.split("_")
    if parts[0] == "Effect" and len(parts) > 1:
        head = "_".join(parts[:2])
    else:
        head = re.sub(r"\d+$", "", parts[0])
        if head in ("Pale", "Sweat", "Shadow", "Option", "ClippingMask"):
            return None
    return re.sub(r"\d+$", "", head)


class AtlasCharacter:
    def __init__(self, char: str):
        d = find_char_dir(char)
        if d is None:
            raise FileNotFoundError(f"角色目录不存在: {char}")
        self.char = char
        self.manifest = json.loads((d / "atlas.json").read_text(encoding="utf-8"))
        self.pages = [Image.open(d / p).convert("RGBA") for p in self.manifest["pages"]]
        self.units = sorted(self.manifest["units"], key=lambda u: u["z"])
        # 运行时重算互斥组：manifest 里的 group_key 是旧导出逻辑烧死的，
        # 把 ArmL01 整名当组名，导致手部姿势差分互相压不掉
        for u in self.units:
            u["group_key"] = _group_key(u["names"][0])
        self.w, self.h = self.manifest["canvas"]
        self.default_vis = {l["name"]: l["visible"] for l in self.manifest["layers"]}
        self._unit_cache: dict[int, Image.Image] = {}
        self._result_cache: collections.OrderedDict = collections.OrderedDict()
        self._ref_bbox = {}  # 惰性：全单元并集的统一裁剪框，按版式缓存（情绪切换人物不跳位）
        self._atlas_bbox = None  # 惰性：atlas 坐标系的并集框（preview 裁剪用）

    def unit_image(self, unit: dict) -> Image.Image:
        img = self._unit_cache.get(unit["z"])
        if img is None:
            x, y = unit["pos"]
            w, h = unit["size"]
            img = self.pages[unit["page"]].crop((x, y, x + w, y + h))
            self._unit_cache[unit["z"]] = img
        return img

    def composite(self, on, hard_off=(), overlays=()) -> Image.Image:
        """按 on（要开的图层名集合）合成角色画布，返回 RGBA。
        overlays 为遮罩/修饰层（Pale*/Sweat 等无互斥组的独立层）强制开：
        优先级高于 Pale* 强制关、互斥关与 PSD 默认可见性（hard_off 仍最优先）。
        同组多个显式 on 只保留名单最后一个（后勾选覆盖先勾选），防叠加打架。
        剪贴单元（clip_host，如前发遮盖眼睛的效果）随激活宿主自动叠加。"""
        picked = {}
        for n in on:
            gk = _group_key(n)
            if gk is not None:
                picked[gk] = n  # 同组后出现的覆盖先出现的
        on = [n for n in on if picked.get(_group_key(n), n) == n]
        keys_on = {_group_key(n) for n in on} - {None}
        on_set = set(on)
        over_set = set(overlays)
        # 手臂家族：Arms（双臂整体）与 ArmL/ArmR（单臂）互相压制；
        # 两个单臂之间可共存（左右手各选各的姿势）
        arm_solo = bool(keys_on & {"ArmL", "ArmR"})

        def _visible(u):
            name = u["names"][0]
            if name in hard_off:
                return False
            if name in on_set or name in over_set:
                return True
            gk = u["group_key"]
            if gk and gk in keys_on:
                return False
            if gk == "Arms" and arm_solo:
                return False
            if gk in ("ArmL", "ArmR") and "Arms" in keys_on:
                return False
            if name.startswith("Pale"):
                return False
            return self.default_vis.get(name, False)

        vis = {u["names"][0]: _visible(u) for u in self.units}
        # 剪贴单元跟随宿主组的激活单元（表情差分共享同一剪贴效果）；自身 hard_off 仍可强制关
        host_vis = {}
        for n2, v in vis.items():
            gk = _group_key(n2)
            if v and gk is not None:
                host_vis[gk] = True
        for u in self.units:
            hg = u.get("clip_host")
            if hg and u["names"][0] not in hard_off:
                gk = _group_key(hg)
                vis[u["names"][0]] = host_vis.get(gk, False) if gk is not None else vis.get(hg, False)

        canvas = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        for u in self.units:
            if not vis.get(u["names"][0]):
                continue
            img = self.unit_image(u)
            if u.get("clip_host"):
                img = self._clip_onto(img, u, vis)
                if img is None:
                    continue
            x, y = u["canvas"][0], u["canvas"][1]
            fn = _BLEND_FN.get(u["blend"])
            if fn is None:
                canvas.alpha_composite(img, (x, y))
            else:
                _blend_onto(canvas, img, (x, y), fn)
        return canvas

    def _clip_onto(self, img, u, vis):
        """剪贴单元：乘宿主组当前激活单元的 alpha（模拟 PSD 剪贴蒙版语义）。"""
        hg = _group_key(u["clip_host"])
        host = next((x for x in self.units
                     if x is not u and _group_key(x["names"][0]) == hg
                     and vis.get(x["names"][0])), None) if hg is not None else \
            next((x for x in self.units
                  if x is not u and x["names"][0] == u["clip_host"]
                  and vis.get(x["names"][0])), None)
        if host is None:
            return None
        host_img = self.unit_image(host)
        a = Image.new("L", img.size, 0)
        a.paste(host_img.getchannel("A"),
                (host["canvas"][0] - u["canvas"][0], host["canvas"][1] - u["canvas"][1]))
        out = img.copy()
        out.putalpha(ImageChops.multiply(img.getchannel("A"), a))
        return out

    def _scale_rotate(self, full: Image.Image, angle: float) -> Image.Image:
        """统一缩放到旋转后外接框高 UNIFORM_BBOX_H（乘全局 scale）-> 旋转。"""
        w, h = full.size
        rad = math.radians(angle)
        grow_h = w * abs(math.sin(rad)) + h * math.cos(rad)
        scale = UNIFORM_BBOX_H / grow_h * _TRANSFORM["scale"]
        full = full.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           Image.LANCZOS)
        return full.rotate(angle, resample=Image.BICUBIC, expand=True)

    def transform_to_canvas(self, full: Image.Image, layout: str = "left") -> Image.Image:
        """统一构图（三种版式，倾角基准 = 当前构图 angle，transform.json 微调全版式叠加）：
          left  = 画面倾 +angle，人物靠左（原作版式，文字在右）
          right = 画面倾 -angle，人物靠右（文字在左）
          none  = 不倾斜，人物靠右（文字在左）"""
        self._ensure_ref_bbox(layout)
        full = self._scale_rotate(full, self._angle_for(layout))
        px = round(self._anchor_for(layout) + _TRANSFORM["dx"] - full.width / 2)
        py = round(CANVAS_H / 2 + _TRANSFORM["dy"] - full.height / 2)
        canvas = Image.new("RGBA", (self._tW, self._tH), (0, 0, 0, 0))
        canvas.paste(full, (px - self._tsx, py - self._tsy), full)
        return canvas

    def render(self, on, hard_off=(), overlays=(), layout: str = "left"):
        """合成 + 统一构图，返回 (裁掉透明边的 RGBA, x, y)。
        与旧预烘焙 webp + meta.json 接口对齐，main.py 可无缝替换。
        结果按 (on, hard_off, overlays, layout) LRU 缓存——同一情绪重复渲染零开销。"""
        key = (tuple(sorted(on)), tuple(sorted(hard_off)), tuple(sorted(overlays)), layout)
        with _RESULT_LOCK:
            hit = self._result_cache.get(key)
            if hit is not None:
                self._result_cache.move_to_end(key)
                return hit
        img, x, y = self._render_uncached(on, hard_off, overlays, layout)
        with _RESULT_LOCK:
            self._result_cache[key] = (img, x, y)
            while len(self._result_cache) > _RESULT_CACHE_MAX:
                self._result_cache.popitem(last=False)
        return img, x, y

    def preview(self, on, hard_off=(), overlays=()) -> Image.Image:
        """WebUI 预览：仅合成并按 atlas 坐标系并集框裁剪，不做缩放旋转（~50ms）。
        注意 _ref_bbox 是构图变换画布坐标系，不能用来裁 atlas 坐标的合成图。"""
        self._ensure_ref_bbox()
        return self.composite(on, hard_off, overlays).crop(self._atlas_bbox)

    def _angle_for(self, layout: str) -> float:
        """版式倾角：left 正、right 负、none 不倾（基准 = 当前构图 angle）。"""
        return _TRANSFORM["angle"] if layout == "left" else (
            -_TRANSFORM["angle"] if layout == "right" else 0.0)

    def _anchor_for(self, layout: str) -> int:
        """版式人物中心落点：left 靠左，right/none 靠右。"""
        return UNIFORM_ANCHOR_X if layout == "left" else CANVAS_W - UNIFORM_ANCHOR_X

    def _ensure_ref_bbox(self, layout: str = "left"):
        """全单元并集矩形走一遍统一构图变换，实测其在词卡画布上的外接框。
        所有情绪共用该框裁剪，等价旧管线"全 emotion 并集 bbox"。
        外接框按版式独立缓存（left/right 人物落点与倾角不同）。
        同时得出构图画布的扩展尺寸与平移量（原 1920x1200 装不下宽人物）。"""
        if self._ref_bbox.get(layout) is not None:
            return
        boxes = [u["canvas"] for u in self.units]
        x0 = max(0, min(b[0] for b in boxes))
        y0 = max(0, min(b[1] for b in boxes))
        x1 = min(self.w, max(b[0] + b[2] for b in boxes))
        y1 = min(self.h, max(b[1] + b[3] for b in boxes))
        self._atlas_bbox = (x0, y0, x1, y1)
        marked = self._scale_rotate(
            Image.new("RGBA", (x1 - x0, y1 - y0), (255, 255, 255, 255)),
            self._angle_for(layout))
        px = round(self._anchor_for(layout) + _TRANSFORM["dx"] - marked.width / 2)
        py = round(CANVAS_H / 2 + _TRANSFORM["dy"] - marked.height / 2)
        # tsx/tsy：扩展画布相对原 (0,0) 的平移量（≤0）；任何情绪图都比并集框小，
        # 因此按并集框算出的画布尺寸对后续 transform_to_canvas 调用总是够用。
        self._tsx, self._tsy = min(0, px), min(0, py)
        self._tW = max(CANVAS_W, px + marked.width) - self._tsx
        self._tH = max(CANVAS_H, py + marked.height) - self._tsy
        canvas = Image.new("RGBA", (self._tW, self._tH), (0, 0, 0, 0))
        canvas.paste(marked, (px - self._tsx, py - self._tsy), marked)
        self._ref_bbox[layout] = canvas.getbbox()

    def _render_uncached(self, on, hard_off=(), overlays=(), layout="left"):
        self._ensure_ref_bbox(layout)
        canvas = self.transform_to_canvas(
            self.composite(on, hard_off, overlays), layout)
        if canvas.getbbox() is None:
            return None, 0, 0
        x0, y0, x1, y1 = self._ref_bbox[layout]
        # 扩展画布坐标 -> 原画布坐标（main.py 按原 1920x1200 坐标贴角色层）
        return canvas.crop((x0, y0, x1, y1)), x0 + self._tsx, y0 + self._tsy


def _blend_onto(canvas: Image.Image, img: Image.Image, pos, fn):
    """把 img 以 PSD 混合模式叠到 canvas 局部。
    alpha_composite 对"B(Cb,Cs) 作为源色"的处理正好满足 PSD 公式：
    不透明底 -> lerp(Cb, blend, as)；透明底 -> 源色本身。
    透明底垫白（MULTIPLY 恒等）或中灰（OVERLAY/SOFT_LIGHT 恒等）。"""
    w, h = img.size
    x, y = pos
    region = canvas.crop((x, y, x + w, y + h))
    base_rgb = region.convert("RGB")
    base_a = region.getchannel("A")
    pad = 255 if fn is ImageChops.multiply else 128
    solid = Image.new("RGB", base_rgb.size, (pad, pad, pad))
    base_padded = Image.composite(base_rgb, solid, base_a)
    blended = fn(base_padded, img.convert("RGB"))
    layer = Image.merge("RGBA", (*blended.split(), img.getchannel("A")))
    region.alpha_composite(layer)
    canvas.paste(region, (x, y))


_CACHE: dict[str, AtlasCharacter] = {}
_LOCK = threading.Lock()

_RESULT_CACHE_MAX = 32  # 每角色缓存的渲染结果条数
_RESULT_LOCK = threading.Lock()


def get_character(char: str) -> AtlasCharacter:
    with _LOCK:
        c = _CACHE.get(char)
        if c is None:
            c = AtlasCharacter(char)
            _CACHE[char] = c
        return c


def has_atlas(char: str) -> bool:
    d = find_char_dir(char)
    return d is not None and (d / "atlas.json").exists()


def get_transform() -> dict:
    """当前全局构图（角度/缩放/平移），WebUI 读取用。"""
    return dict(_TRANSFORM)


def set_transform(angle=None, scale=None, dx=None, dy=None) -> dict:
    """更新全局构图（None 表示保持原值），并失效全部角色的构图缓存。
    落盘由调用方负责（webui_api 写 plugin_data/transform.json）。"""
    for k, v in (("angle", angle), ("scale", scale), ("dx", dx), ("dy", dy)):
        if v is not None:
            _TRANSFORM[k] = float(v)
    with _LOCK:
        chars = list(_CACHE.values())
    with _RESULT_LOCK:
        for c in chars:
            c._ref_bbox = {}
            c._result_cache.clear()
    return dict(_TRANSFORM)
