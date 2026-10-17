# -*- coding: utf-8 -*-
"""DialoCard 离线资产构建管线（开发机运行，不随插件包分发）。

把 asset/chracter/*.psd 的差分立绘按情绪合成 -> 裁上半身 -> 缩放 -> 旋转 ->
落到 1920x1200 卡坐标，输出透明角色层 WebP + meta.json（运行时与背景合成）。

用法:
  py tools/build_cards.py tree                     # 列出全部 PSD 的图层树（人工标注情绪映射用）
  py tools/build_cards.py bg                       # background 旋转3°+自动放大消黑边 -> background.webp
  py tools/build_cards.py contact Anan "ArmL"       # 某组差分的联络表（选 paint 手臂用）
  py tools/build_cards.py render Anan               # 渲染全部情绪 -> asset/characters/Anan/
  py tools/build_cards.py render Anan --emotions smile,angry
  py tools/build_cards.py sample Anan               # 整卡样张(含文字) -> tools/samples/
  py tools/build_cards.py calibrate Anan smile      # 与现役烤死卡并排对比（仅 Anan 有基准）
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from psd_tools import PSDImage

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
ASSET_DIR = PLUGIN_DIR / "asset"
PSD_DIR = ASSET_DIR / "character"
OUT_DIR = ASSET_DIR / "characters"
CONF_DIR = Path(__file__).resolve().parent / "characters"
SAMPLE_DIR = Path(__file__).resolve().parent / "samples"
PICTURE_DIR = ASSET_DIR / "pictures"  # 旧版整卡基准（Anan 专用）
FONT_PATH = ASSET_DIR / "font" / "SourceHanSerifCN-SemiBold-7.ttf"

CANVAS_W, CANVAS_H = 1920, 1200
EMOTIONS = [
    "default", "wronged", "surprise", "smile", "paint", "flushed",
    "embarrass", "cry", "confused", "angry", "afraid",
]

# ---- 统一构图标准（所有角色同一套规则，保证人物间不歪七扭八）----
# 1. 不裁内容：取原 PSD 全宽 x 上半 1/2
# 2. 统一缩放：上半区旋转后外接框高度统一为 UNIFORM_BBOX_H
# 3. 统一旋转 UNIFORM_ANGLE 度
# 4. 统一平移：上半区中心落在 (UNIFORM_ANCHOR_X, CANVAS_H/2)（背景左半边中心）
UNIFORM_ANGLE = 3
UNIFORM_BBOX_H = 1200
UNIFORM_ANCHOR_X = 480

# 与 main.py 保持一致的文字排版常量（样张预览用）
TEXTBOX_LEFT = 1000
TEXTBOX_WIDTH = 860
LINE_SPACING = 170
FONT_SIZE_NORMAL = 96
FONT_SIZE_HIGHLIGHT = int(FONT_SIZE_NORMAL * 1.2)
TILT_ANGLE = 3
STROKE_WIDTH = 4
COLOR_WHITE = (255, 255, 255)
COLOR_BLACK = (0, 0, 0)
COLOR_PINK = (246, 136, 187)

SAMPLE_TEXTS = {
    "default": ("今天{天气}真好，一起去{画画}吧", "default"),
    "wronged": ("呜……你也觉得{我}画得不好吗", "wronged"),
    "surprise": ("诶？！你什么时候来的", "surprise"),
    "smile": ("今天{天气}真好，一起去{画画}吧", "smile"),
    "paint": ("看！这是我的{新作品}哦", "paint"),
    "flushed": ("才、才不是特意为你画的呢", "flushed"),
    "embarrass": ("唔……被你看到了，好{害羞}", "embarrass"),
    "cry": ("呜哇——你把我的颜料弄洒了", "cry"),
    "confused": ("咦？这条线应该画在哪里呢", "confused"),
    "angry": ("哼！再乱动我的画笔要{生气}了", "angry"),
    "afraid": ("打、打雷了……可以陪你一会吗", "afraid"),
}

_psd_cache = {}


def load_conf(char: str) -> dict:
    path = CONF_DIR / f"{char}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def open_psd(conf: dict) -> PSDImage:
    key = conf["psd"]
    if key not in _psd_cache:
        _psd_cache[key] = PSDImage.open(str(PSD_DIR / key))
    return _psd_cache[key]


def iter_layers(psd: PSDImage):
    def walk(layer):
        yield layer
        if layer.is_group():
            for child in layer:
                yield from walk(child)
    for layer in psd:
        yield from walk(layer)


def _layer_ancestors(psd: PSDImage):
    """[(pixel_layer, [祖先组...])]，保持 PSD 原始层级顺序。"""
    out = []

    def walk(layer, chain):
        if layer.is_group():
            for child in layer:
                walk(child, chain + [layer])
        else:
            out.append((layer, chain))

    for top in psd:
        walk(top, [])
    return out


def _group_key(name: str):
    """差分互斥组 key：None = 不参与互斥。
    Effect_Front/Effect_Back/Middle 按前两段分组，其余按第一段。"""
    parts = name.split("_")
    if parts[0] == "Effect" and len(parts) > 1:
        return "_".join(parts[:2])
    if parts[0] in ("Pale", "Sweat", "Shadow", "Option", "ClippingMask"):
        return None
    return parts[0]


def _composite_with(psd: PSDImage, on_set: set, hard_off: set,
                    viewport=None) -> Image.Image:
    """最小干预合成：除指定差分外一律保持 PSD 原始可见性。
    · on_set 中的层 -> 开启（其祖先组同步开启）
    · 与 on 同互斥组的其他差分 -> 关闭
    · Pale* 强制关（psd-tools 不支持 fill-opacity，开它会糊脸）
    · hard_off 显式关闭（配置 "off" 字段，可选）
    · viewport 只合成该区域（预览提速用）"""
    keys_on = {_group_key(n) for n in on_set} - {None}
    for layer, ancestors in _layer_ancestors(psd):
        name = layer.name
        want = None
        if name in on_set:
            want = True
        else:
            key = _group_key(name)
            if key is not None and key in keys_on:
                want = False
            elif name.startswith("Pale"):
                want = False
        if name in hard_off:
            want = False
        if want is not None:
            layer.visible = want
        if name in on_set:
            for a in ancestors:
                a.visible = True
    return psd.composite(viewport=viewport).convert("RGBA")


def composite_emotion(psd: PSDImage, conf: dict, emotion: str) -> Image.Image:
    emo_conf = conf["emotions"][emotion]
    return _composite_with(psd, set(emo_conf["on"]), set(emo_conf.get("off", ())))


def transform_to_canvas(full: Image.Image) -> Image.Image:
    """统一标准变换（所有角色一致）：
    原 PSD 全宽 x 上半 1/2 -> 统一缩放（旋转后外接框高 = UNIFORM_BBOX_H）
    -> 统一旋转 UNIFORM_ANGLE 度 -> 上半区中心落点 (UNIFORM_ANCHOR_X, CANVAS_H/2)。"""
    w, h = full.size
    crop = full.crop((0, 0, w, h // 2))
    rad = math.radians(UNIFORM_ANGLE)
    grow_h = crop.width * math.sin(rad) + crop.height * math.cos(rad)
    scale = UNIFORM_BBOX_H / grow_h
    crop = crop.resize(
        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
        Image.LANCZOS)
    crop = crop.rotate(UNIFORM_ANGLE, resample=Image.BICUBIC, expand=True)
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    px = round(UNIFORM_ANCHOR_X - crop.width / 2)
    py = round(CANVAS_H / 2 - crop.height / 2)
    canvas.paste(crop, (px, py), crop)
    return canvas


def union_bbox(canvases) -> tuple:
    box = None
    for cv in canvases.values():
        b = cv.getchannel("A").getbbox()
        if b is None:
            continue
        box = b if box is None else (
            min(box[0], b[0]), min(box[1], b[1]),
            max(box[2], b[2]), max(box[3], b[3]))
    return box


def cmd_render(char: str, emotions, quality: int):
    conf = load_conf(char)
    psd = open_psd(conf)
    emotions = emotions or EMOTIONS
    for emo in emotions:
        if emo not in conf["emotions"]:
            sys.exit(f"[{char}] 配置缺少情绪 {emo}")
    canvases = {}
    for emo in emotions:
        print(f"[{char}] 合成 {emo} ...", flush=True)
        full = composite_emotion(psd, conf, emo)
        canvases[emo] = transform_to_canvas(full)
    box = union_bbox(canvases)
    if box is None:
        sys.exit(f"[{char}] 渲染结果全空，检查配置")
    out_dir = OUT_DIR / char
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "character": char,
        "x": box[0], "y": box[1],
        "w": box[2] - box[0], "h": box[3] - box[1],
        "canvas": [CANVAS_W, CANVAS_H],
        "emotions": emotions,
    }
    for emo, cv in canvases.items():
        part = cv.crop(box)
        path = out_dir / f"{emo}.webp"
        part.save(str(path), "WEBP", quality=quality, method=6)
        print(f"  -> {path.name} {part.size} "
              f"{path.stat().st_size // 1024}KB", flush=True)
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{char}] meta: {meta}")


def _fit(img: Image.Image, width: int) -> Image.Image:
    h = round(img.height * width / img.width)
    return img.resize((width, h), Image.LANCZOS)


def _label(img: Image.Image, text: str) -> Image.Image:
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 22], fill=(0, 0, 0))
    d.text((4, 4), text, fill=(255, 255, 255))
    return img


def cmd_contact(char: str, pattern: str):
    """联络表：default 配置去掉匹配 pattern 的层后，逐个加回该组每个差分渲染缩略图。"""
    conf = load_conf(char)
    psd = open_psd(conf)
    base = {n for n in conf["emotions"]["default"]["on"]
            if not re.search(pattern, n)}
    hard_off = set(conf.get("off", ()))
    names = [l.name for l in iter_layers(psd)
             if not l.is_group() and re.search(pattern, l.name)]
    print(f"[{char}] {len(names)} 个差分匹配 {pattern!r}")
    thumbs = []
    for n in names:
        print(f"  渲染 {n} ...", flush=True)
        full = _composite_with(psd, base | {n}, hard_off)
        cv = transform_to_canvas(full)
        box = cv.getchannel("A").getbbox() or (0, 0, CANVAS_W, CANVAS_H)
        thumbs.append(_label(_fit(cv.crop(box), 300), n))
    cols = 4
    rows = math.ceil(len(thumbs) / cols)
    cell_w = max(t.width for t in thumbs) + 8
    cell_h = max(t.height for t in thumbs) + 8
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (40, 40, 40))
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % cols) * cell_w + 4, (i // cols) * cell_h + 4))
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    out = SAMPLE_DIR / f"{char}_contact_{re.sub(r'[^A-Za-z0-9]', '_', pattern)}.png"
    sheet.save(str(out))
    print(f"联络表 -> {out}")


def draw_card_text(card: Image.Image, text: str, keywords):
    """样张文字：与 main.py 相同的排版（关键词粉色放大 + 黑描边 + 3° 旋转）。"""
    font_n = ImageFont.truetype(str(FONT_PATH), FONT_SIZE_NORMAL)
    font_h = ImageFont.truetype(str(FONT_PATH), FONT_SIZE_HIGHLIGHT)
    if keywords:
        pattern = "|".join(re.escape(k) for k in keywords)
        parts = re.split(f"({pattern})", text)
    else:
        parts = [text]
    parts = [p for p in parts if p]
    segments = [(p, p in keywords) for p in parts]

    # 简化换行：逐字符
    lines, cur, cur_w = [], [], 0
    max_w = TEXTBOX_WIDTH - 60
    for seg, is_kw in segments:
        font = font_h if is_kw else font_n
        for ch in seg:
            bbox = font.getbbox(ch)
            w = (bbox[2] - bbox[0]) if bbox else FONT_SIZE_NORMAL
            if cur and cur_w + w > max_w:
                lines.append(cur)
                cur, cur_w = [], 0
            cur.append((ch, is_kw, font, w))
            cur_w += w
    if cur:
        lines.append(cur)

    expand = 500
    layer = Image.new("RGBA", (CANVAS_W + expand * 2, CANVAS_H + expand * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    total_h = len(lines) * LINE_SPACING
    y = CANVAS_H // 2 - total_h // 2 + expand
    n_bbox = font_n.getbbox("Ay")
    n_desc = n_bbox[3] if n_bbox else FONT_SIZE_NORMAL * 0.2
    for line in lines:
        x = TEXTBOX_LEFT + expand
        for ch, is_kw, font, w in line:
            bbox = font.getbbox(ch)
            desc = bbox[3] if bbox else FONT_SIZE_NORMAL * 0.2
            draw_y = y + n_desc - desc
            for ang in range(0, 360, 15):
                rad = math.radians(ang)
                for r in range(1, STROKE_WIDTH + 1):
                    d.text((x + r * math.cos(rad), draw_y + r * math.sin(rad)),
                           ch, font=font, fill=COLOR_BLACK)
            d.text((x, draw_y), ch, font=font,
                   fill=COLOR_PINK if is_kw else COLOR_WHITE)
            x += w
        y += LINE_SPACING
    rotated = layer.rotate(TILT_ANGLE, resample=Image.BICUBIC, expand=False,
                           center=(expand + CANVAS_W // 2, expand + CANVAS_H // 2))
    rotated = rotated.crop((expand, expand, expand + CANVAS_W, expand + CANVAS_H))
    card.paste(rotated, (0, 0), rotated)


def build_background() -> Image.Image:
    webp = ASSET_DIR / "background" / "background.webp"
    src = webp if webp.exists() else ASSET_DIR / "background" / "background.png"
    bg = Image.open(str(src)).convert("RGBA")
    if bg.size != (CANVAS_W, CANVAS_H):
        bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    return bg


def _edge_has_black(img: Image.Image, inset: int = 2, thresh: int = 10) -> bool:
    """检查四边（重点上下边）是否还有黑色像素。
    源图四角/四边烤死了纯黑 (0,0,0) 黑边，内容最暗通道值 >=24，
    故 thresh=10 只会命中黑边与旋转露底。"""
    rgb = img.convert("RGB")
    w, h = img.size
    strips = ((0, 0, w, inset), (0, h - inset, w, h),
              (0, 0, inset, h), (w - inset, 0, w, h))
    for box in strips:
        r, g, b = rgb.crop(box).getextrema()
        if max(r[0], g[0], b[0]) <= thresh:
            return True
    return False


def cmd_bg(angle: float):
    """background 旋转 angle 度后逐级轻微放大，直到上下边内侧边角
    不再出现露底黑边，产出预旋转版 asset/background/background.webp。"""
    src_path = ASSET_DIR / "background" / "background.png"
    src = Image.open(str(src_path)).convert("RGBA")
    if src.size != (CANVAS_W, CANVAS_H):
        src = src.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    rad = math.radians(angle)
    scale = max(
        (CANVAS_W * math.cos(rad) + CANVAS_H * math.sin(rad)) / CANVAS_W,
        (CANVAS_W * math.sin(rad) + CANVAS_H * math.cos(rad)) / CANVAS_H)
    canvas = None
    while scale <= 1.35:
        big = src.resize((round(CANVAS_W * scale), round(CANVAS_H * scale)),
                         Image.LANCZOS)
        rot = big.rotate(angle, resample=Image.BICUBIC, expand=True)
        left = (rot.width - CANVAS_W) // 2
        top = (rot.height - CANVAS_H) // 2
        canvas = rot.crop((left, top, left + CANVAS_W, top + CANVAS_H))
        if not _edge_has_black(canvas):
            break
        scale += 0.005
    else:
        print("警告：放大到 1.35 仍有黑边，检查 background 源图四边")
    out = ASSET_DIR / "background" / "background.webp"
    canvas.convert("RGB").save(str(out), "WEBP", quality=90, method=6)
    print(f"background -> {out}  angle={angle}  scale={scale:.3f}")


def cmd_tree():
    """列出 asset/chracter/ 全部 PSD 的图层树，供人工标注情绪->差分映射。"""
    psds = sorted(PSD_DIR.glob("*.psd"))
    if not psds:
        sys.exit(f"{PSD_DIR} 下没有 PSD")
    lines = []

    def walk(layer, depth):
        pad = "    " * depth
        bbox = tuple(round(v) for v in layer.bbox)
        if layer.is_group():
            lines.append(f"{pad}[G] {layer.name}  vis={layer.visible}  bbox={bbox}")
            for child in layer:
                walk(child, depth + 1)
        else:
            lines.append(f"{pad}- {layer.name}  vis={layer.visible}  bbox={bbox}")

    for psd_path in psds:
        psd = PSDImage.open(str(psd_path))
        lines.append(f"===== {psd_path.name}  {psd.width}x{psd.height} =====")
        for layer in psd:
            walk(layer, 1)
        lines.append("")
    out_file = Path(__file__).resolve().parent / "layer_trees.txt"
    out_file.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"共 {len(psds)} 份 PSD，完整图层树 -> {out_file}")


def build_card(psd, conf, emotion) -> Image.Image:
    full = composite_emotion(psd, conf, emotion)
    char_cv = transform_to_canvas(full)
    card = build_background()
    card.paste(char_cv, (0, 0), char_cv)
    text, emo_key = SAMPLE_TEXTS.get(emotion, (SAMPLE_TEXTS["default"][0], emotion))
    keywords = re.findall(r"\{([^{}]+?)\}", text)
    plain = re.sub(r"\{([^{}]+?)\}", r"\1", text)
    draw_card_text(card, plain, keywords)
    return card


def cmd_sample(char: str, emotions):
    conf = load_conf(char)
    psd = open_psd(conf)
    emotions = emotions or EMOTIONS
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for emo in emotions:
        print(f"[{char}] 样张 {emo} ...", flush=True)
        card = build_card(psd, conf, emo)
        out = SAMPLE_DIR / f"{char}_{emo}.png"
        card.convert("RGB").save(str(out), "PNG")
        print(f"  -> {out}", flush=True)


def cmd_calibrate(char: str, emotion: str):
    """左：新管线样卡 / 右：现役烤死卡（Anan 基准）。"""
    conf = load_conf(char)
    psd = open_psd(conf)
    new_card = build_card(psd, conf, emotion).convert("RGB")
    ref_path = PICTURE_DIR / f"{emotion}.png"
    ref = Image.open(str(ref_path)).convert("RGB")
    if ref.size != (CANVAS_W, CANVAS_H):
        ref = ref.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    h = 700
    pair_w = round(CANVAS_W * h / CANVAS_H)
    sheet = Image.new("RGB", (pair_w * 2 + 12, h + 28), (30, 30, 30))
    sheet.paste(_label(_fit(new_card, pair_w), "NEW"), (0, 0))
    sheet.paste(_label(_fit(ref, pair_w), "REF (baked)"), (pair_w + 12, 0))
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    out = SAMPLE_DIR / f"{char}_calibrate_{emotion}.png"
    sheet.save(str(out))
    print(f"对比图 -> {out}")


# ---- 图集（atlas）导出：运行时自由合成的资产格式 ----
ATLAS_PAGE = 4096


def _clipping_units(psd: PSDImage):
    """把像素层按「基底 + 其上方连续 clipped 链」划分为合成单元。
    返回 [[(layer, ancestors), ...], ...]，保持自底向上 z 序。
    链内混合（MULTIPLY/OVERLAY 修饰）在导出时烧进单元，运行时纯贴图。"""
    flat = _layer_ancestors(psd)
    n = len(flat)
    owner = [0] * n
    base = 0
    for i in range(n):
        if not flat[i][0].clipping:
            base = i
        owner[i] = base
    groups = {}
    for i in range(n):
        groups.setdefault(owner[i], []).append(i)
    return [[flat[i] for i in idxs] for _, idxs in sorted(groups.items())]


def _render_only(psd: PSDImage, ids: set, ancestors_of, viewport):
    """只渲染 ids 里的层（其他全关），祖先组同步可见。"""
    for layer, _anc in _layer_ancestors(psd):
        layer.visible = id(layer) in ids
    for layer in (l for l, _ in _layer_ancestors(psd) if id(l) in ids):
        for a in ancestors_of[id(layer)]:
            a.visible = True
    return psd.composite(viewport=viewport).convert("RGBA")


def _pack_pages(sizes):
    """贪心 shelf packing -> [(page, x, y)]。"""
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    pages, placed = [], {}
    x = y = row_h = 0
    cur = 0
    for i in order:
        w, h = sizes[i]
        if x + w > ATLAS_PAGE:
            x, y, row_h = 0, y + row_h, 0
        if y + h > ATLAS_PAGE:
            pages.append(placed)
            placed, x, y, row_h = {}, 0, 0, 0
            cur += 1
        placed[i] = (cur, x, y)
        x += w
        row_h = max(row_h, h)
    pages.append(placed)
    return pages


def cmd_atlas(char: str, quality: int = 90):
    """导出角色图集：asset/characters/{char}/atlas_N.webp + atlas.json。
    上半身以外的部分不导出（与统一构图标准一致）。"""
    psd = open_psd({"psd": f"{char}.psd"})
    W, H = psd.width, psd.height
    top_h = H // 2
    crop = (0, 0, W, top_h)

    # 先记录原始可见性（写进 manifest 供 UI 默认勾选）
    pairs = _layer_ancestors(psd)
    default_vis = {l.name: bool(l.visible) for l, _ in pairs}
    ancestors_of = {id(l): anc for l, anc in pairs}

    units = _clipping_units(psd)
    # 剪贴蒙版拆分：宿主是差分单元（表情/手臂多选一）时，clipped 效果层不烧进基底，
    # 拆成独立 clip 单元（记录 clip_host）——运行时对当前激活宿主 alpha 裁剪叠加，
    # 效果可跟任意表情复用（如前发对眼睛的遮盖不再只限默认眼）。
    CLIP_HOST_PREFIX = ("Eyes", "Mouth", "ArmL", "ArmR", "Arms", "Effect_Front", "EffectB")

    def _render_clip_layer(layer, host, viewport):
        """剪贴层原始像素（topil 绕过宿主可见性），裁到宿主 bbox ∩ viewport。
        运行时再乘激活宿主的 alpha，故此处不做宿主裁剪。"""
        px = layer.topil()
        if px is None:
            return None
        px = px.convert("RGBA")
        lx, ly = layer.offset
        # 全画布坐标系下与 viewport 求交
        cx0, cy0 = max(lx, viewport[0]), max(ly, viewport[1])
        cx1, cy1 = min(lx + px.width, viewport[2]), min(ly + px.height, viewport[3])
        if cx1 <= cx0 or cy1 <= cy0:
            return None
        out = px.crop((cx0 - lx, cy0 - ly, cx1 - lx, cy1 - ly))
        return out, (cx0, cy0, cx1 - cx0, cy1 - cy0)

    metas, images = [], []
    for chain in units:
        base_l = chain[0][0]
        clips = [l for l, _ in chain[1:]]
        split = (len(chain) > 1 and base_l.name.startswith(CLIP_HOST_PREFIX)
                 and all(l.name.startswith("ClippingMask") for l in clips))
        if split:
            # 基底单元：只渲染基底（clipped 层拆走）
            b = base_l.bbox
            b = [x for x in (b,) if x and x[2] > x[0] and x[3] > x[1]]
            if not b:
                continue
            vx0, vy0 = max(b[0][0], crop[0]), max(b[0][1], crop[1])
            vx1, vy1 = min(b[0][2], crop[2]), min(b[0][3], crop[3])
            if vx1 <= vx0 or vy1 <= vy0:
                continue
            viewport = (vx0, vy0, vx1, vy1)
            img = _render_only(psd, {id(base_l)}, ancestors_of, viewport)
            metas.append({"names": [base_l.name], "blend": "NORMAL",
                          "canvas": [vx0, vy0, vx1 - vx0, vy1 - vy0],
                          "size": img.size, "img": img})
            images.append(img)
            # clip 单元：逐层独立出图，运行时按原 blend 叠加并裁到宿主 alpha
            host_view = (max(base_l.bbox[0], crop[0]), max(base_l.bbox[1], crop[1]),
                         min(base_l.bbox[2], crop[2]), min(base_l.bbox[3], crop[3]))
            for l in clips:
                r = _render_clip_layer(l, base_l, host_view)
                if r is None:
                    continue
                cimg, ccv = r
                metas.append({"names": [l.name], "blend": l.blend_mode.name,
                              "canvas": list(ccv), "size": cimg.size, "img": cimg,
                              "clip_host": base_l.name})
                images.append(cimg)
            continue
        boxes = [l.bbox for l, _ in chain]
        boxes = [b for b in boxes if b and b[2] > b[0] and b[3] > b[1]]
        if not boxes:
            continue
        x0 = max(0, min(b[0] for b in boxes))
        y0 = max(0, min(b[1] for b in boxes))
        x1 = min(W, max(b[2] for b in boxes))
        y1 = min(H, max(b[3] for b in boxes))
        # 与上半区求交
        vx0, vy0 = max(x0, crop[0]), max(y0, crop[1])
        vx1, vy1 = min(x1, crop[2]), min(y1, crop[3])
        if vx1 <= vx0 or vy1 <= vy0:
            continue
        viewport = (vx0, vy0, vx1, vy1)
        ids = {id(l) for l, _ in chain}
        img = _render_only(psd, ids, ancestors_of, viewport)
        names = [l.name for l, _ in chain]
        blend = "NORMAL"
        if len(chain) == 1:  # 独立修饰层：运行时按混合模式叠加
            blend = chain[0][0].blend_mode.name
        metas.append({"names": names, "blend": blend,
                      "canvas": [vx0, vy0, vx1 - vx0, vy1 - vy0],
                      "size": img.size, "img": img})
        images.append(img)

    names_seen = {}
    for m in metas:
        for n in m["names"]:
            names_seen[n] = names_seen.get(n, 0) + 1
    dup = [n for n, c in names_seen.items() if c > 1]
    if dup:
        print(f"警告：重名图层 {dup}，UI 勾选将影响所有同名层")

    pages = _pack_pages([m["size"] for m in metas])
    out_dir = OUT_DIR / char
    out_dir.mkdir(parents=True, exist_ok=True)
    page_files = []
    for pi, placed in enumerate(pages):
        page_img = Image.new("RGBA", (ATLAS_PAGE, ATLAS_PAGE), (0, 0, 0, 0))
        for i, (_cur, px, py) in placed.items():
            page_img.paste(images[i], (px, py))
        path = out_dir / f"atlas_{pi}.webp"
        page_img.save(str(path), "WEBP", quality=quality, method=6)
        page_files.append(path.name)
        print(f"  {path.name} {path.stat().st_size // 1024}KB")

    layer_list = []
    for i, m in enumerate(metas):
        if "clip_host" in m:
            continue  # 剪贴效果层随宿主自动叠加，UI 不单列
        n = m["names"][0]  # 单元基底层（clipped 成员随单元出图，UI 不单列）
        layer_list.append({
            "name": n, "unit": i, "visible": default_vis.get(n, False),
            "group_key": _group_key(n),
        })

    def _find(i):
        for p, placed in enumerate(pages):
            if i in placed:
                return placed[i]
        raise KeyError(i)

    manifest = {
        "character": char,
        "canvas": [W, top_h],
        "pages": page_files,
        "units": [{"z": i, "names": m["names"], "blend": m["blend"],
                   "canvas": m["canvas"],
                   "page": _find(i)[0],
                   "pos": list(_find(i)[1:]),
                   "size": list(m["size"]),
                   **({"clip_host": m["clip_host"]} if "clip_host" in m else {}),
                   "group_key": _group_key(m["names"][0])}
                  for i, m in enumerate(metas)],
        "layers": layer_list,
    }
    path = out_dir / "atlas.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    # 离线 emotion 配置转写为运行时持久化文件（WebUI 可再改，渲染热读）
    cfg_src = TOOLS_DIR / "characters" / f"{char}.json"
    if cfg_src.exists() and not (out_dir / "emotions.json").exists():
        cfg = json.loads(cfg_src.read_text(encoding="utf-8"))
        (out_dir / "emotions.json").write_text(
            json.dumps({"emotions": cfg.get("emotions", {})},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"  emotions.json <- {cfg_src.name}")
    total = sum(p.stat().st_size for p in out_dir.glob("atlas_*.webp"))
    print(f"[{char}] {len(metas)} 单元 / {len(layer_list)} 层 -> {path} "
          f"图集共 {total // 1024}KB")


def cmd_pack(out_name: str):
    """把 asset/characters/* 打成发布 zip（根级 {角色}/... + manifest.json）。
    manifest 是插件端"是否已下载过"的标记。"""
    import zipfile
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chars = sorted(d.name for d in OUT_DIR.iterdir()
                   if d.is_dir() and ((d / "atlas.json").exists()
                                      or (d / "meta.json").exists()))
    if not chars:
        sys.exit("asset/characters 下没有可用角色（先 render）")
    (OUT_DIR / "manifest.json").write_text(
        json.dumps({"version": 1, "characters": chars}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    out = PLUGIN_DIR / out_name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        zf.write(OUT_DIR / "manifest.json", "manifest.json")
        for char in chars:
            d = OUT_DIR / char
            has_atlas = (d / "atlas.json").exists()
            for f in sorted(d.glob("*")):
                if not f.is_file():
                    continue
                # atlas 角色：跳过预烘焙情绪图与 meta（运行时实时合成）
                if has_atlas and (f.name == "meta.json" or (
                        f.suffix == ".webp" and not f.name.startswith("atlas"))):
                    continue
                zf.write(f, f"{char}/{f.name}")
    print(f"角色包 -> {out} ({out.stat().st_size / 1048576:.1f} MB, {len(chars)} 角色)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tree")
    p = sub.add_parser("atlas")
    p.add_argument("char")
    p.add_argument("--quality", type=int, default=90)
    p = sub.add_parser("bg")
    p.add_argument("--angle", type=float, default=3)
    p = sub.add_parser("contact")
    p.add_argument("char")
    p.add_argument("pattern")
    p = sub.add_parser("render")
    p.add_argument("char")
    p.add_argument("--emotions", default="")
    p.add_argument("--quality", type=int, default=90)
    p = sub.add_parser("sample")
    p.add_argument("char")
    p.add_argument("--emotions", default="")
    p = sub.add_parser("calibrate")
    p.add_argument("char")
    p.add_argument("emotion")
    p = sub.add_parser("pack")
    p.add_argument("--out", default="dialocard_characters.zip")
    p = sub.add_parser("webui")
    p.add_argument("char", nargs="?", default=None)
    p.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.cmd == "webui":
        from webui import main as webui_main
        sys.argv = ["webui"] + ([args.char] if args.char else []) + \
            (["--port", str(args.port)] if args.port != 8765 else [])
        webui_main()
    elif args.cmd == "tree":
        cmd_tree()
    elif args.cmd == "atlas":
        cmd_atlas(args.char, args.quality)
    elif args.cmd == "bg":
        cmd_bg(args.angle)
    elif args.cmd == "contact":
        cmd_contact(args.char, args.pattern)
    elif args.cmd == "render":
        emos = [e for e in args.emotions.split(",") if e] or None
        cmd_render(args.char, emos, args.quality)
    elif args.cmd == "sample":
        emos = [e for e in args.emotions.split(",") if e] or None
        cmd_sample(args.char, emos)
    elif args.cmd == "calibrate":
        cmd_calibrate(args.char, args.emotion)
    elif args.cmd == "pack":
        cmd_pack(args.out)


if __name__ == "__main__":
    main()
