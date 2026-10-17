"""DialoCard 词卡插件"""
import os
import re
import json
import math
import random
import hashlib
import colorsys
import zipfile
import sys
import time
import threading
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
from PIL import Image, ImageDraw, ImageFont

def _fresh_import(name: str):
    """卫星模块（atlas_render/webui_api）以裸名/包内名驻留 sys.modules；
    插件热重载只替换主模块时旧实例会存活（旧代码+旧缓存），表现为
    「改了代码、实际渲染没变化」。导入前强制清掉，保证拿到磁盘最新代码。"""
    import importlib
    for key in [k for k in sys.modules if k == name or k.endswith("." + name)]:
        del sys.modules[key]
    try:  # 包形式加载走相对导入；顶层脚本形式回退裸名
        return importlib.import_module(f".{name}", __package__ or None)
    except (ImportError, TypeError, ValueError):
        return importlib.import_module(name)


try:
    atlas_render = _fresh_import("atlas_render")
except ImportError:  # 打包环境缺依赖时回退预烘焙 webp
    atlas_render = None

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

# ========== 模块级插件实例单例 ==========
_plugin_instance: Optional["DialoCardPlugin"] = None

EMOJI_LIST = [
    "wronged", "surprise", "smile", "paint",
    "flushed", "embarrass", "cry", "confused",
    "angry", "afraid"
]

TRIGGER_PROBABILITY = 0.20
FORCE_KEYWORDS = ["词卡"]

COLOR_WHITE = (255, 255, 255)
COLOR_BLACK = (0, 0, 0)

_h, _s, _v = 341 / 360, 38 / 100, 94 / 100
_r, _g, _b = colorsys.hsv_to_rgb(_h, _s, _v)
COLOR_PINK = (int(_r * 255), int(_g * 255), int(_b * 255))

STROKE_WIDTH = 4
FONT_SIZE_NORMAL = 96
FONT_SIZE_HIGHLIGHT = int(FONT_SIZE_NORMAL * 1.2)
TILT_ANGLE = 3

TEXTBOX_LEFT = 1000
TEXTBOX_WIDTH = 860
LINE_SPACING = 170

CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 1200

# 最大正文长度（字）
MAX_BODY_LENGTH = 30

# 情绪差分组合内置兜底（2026-10-17 从 WebUI 保存版烤入）：emotions.json 被误重置/
# 缺失/损坏时，渲染回退到这里，不再掉回 PSD 默认可见性（粉色喷漆残留那类事故）。
# 文件正常时仍以文件为准（WebUI 编辑照常）；WebUI 打开也会回显内置组合，点保存即回写修复。
BUILTIN_EMOTIONS = {
    "Noah": {
        "default": {"on": ["Eyes_Normal_Open01", "Mouth_Normal_Closed", "ArmR01", "ArmL01"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
        "smile": {"on": ["ArmR01", "Mouth_Smile_Closed", "Eyes_Smile_Closed02", "ArmL04"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
        "angry": {"on": ["ArmR01", "ArmL01", "Mouth_Angry_Closed", "Eyes_Uncomfortable_Open"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
        "cry": {"on": ["Eyes_Cry_Open", "Mouth_Cry_Closed", "ArmR01", "ArmL01"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
        "afraid": {"on": ["Eyes_Surprised_Open01", "Mouth_Pensive_Open", "ArmR02", "ArmL06"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": ["Pale01"]},
        "surprise": {"on": ["Eyes_Surprised_Open01", "Mouth_Surprised_Open01", "ArmR01", "ArmL01"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
        "confused": {"on": ["ArmL01", "ArmR04", "Mouth_Normal_Closed", "Eyes_Uncomfortable_Open"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
        "flushed": {"on": ["Cheeks_Flushed", "ArmL02", "Mouth_Normal_Closed", "Eyes_Smile_Closed02", "ArmR02"],
                "off": ["Effect_Front_ArmL01"], "overlays": []},
        "embarrass": {"on": ["ArmR01", "ArmL01", "Mouth_Smile_Open01", "Eyes_Normal_Open01"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": ["Sweat01"]},
        "wronged": {"on": ["Mouth_Pensive_Open", "Eyes_Normal_Open02", "ArmR05", "ArmL02"],
                "off": ["Cheeks_Flushed", "Effect_Front_ArmL01", "ArmL01"], "overlays": []},
        "paint": {"on": ["Effect_Front_ArmR04", "ArmL05", "ArmR05", "Eyes_Smile_Closed01", "Mouth_Smile_Open01"],
                "off": ["Effect_Front_ArmL01", "Cheeks_Flushed"], "overlays": []},
    },
}

# 角色资产：asset/characters/{角色}/{情绪}.webp + meta.json
# （离线管线 tools/build_cards.py 从 PSD 预渲染；全角色包通过 asset_url 首次自动下载）
# 上架 zip ≤16MB 限制：全量角色包与完整字体托管在 GitHub Releases，首次启动自动下载
DEFAULT_ASSET_URL = ("https://github.com/FexalQvQ/astrbot_plugin_dialocard"
                     "/releases/download/v2.0.0/dialocard-characters-full.zip")
DEFAULT_FONT_URL = ("https://github.com/FexalQvQ/astrbot_plugin_dialocard"
                    "/releases/download/v2.0.0/SourceHanSerifCN-SemiBold-7.ttf")

# v1 协议碎片出口兜底：dialocard 情绪 + opiniocard 立场尾标白名单
_PROTO_TAIL_TOKENS = set(EMOJI_LIST) | {"agree", "disagree", "doubt"}
_PROTO_TAIL_RE = re.compile(
    r"\s*[|&]{1,2}\s*([A-Za-z_]{2,20})\s*[|&]{0,2}\s*$"
    r"|\r?\n\s*([A-Za-z_]{2,20})\s*$")
# ★句中协议碎片（2026-10-05 实证事故：模型输出「句子|flushed 句子」「句子|smile 句子」，
# 旧版正则只锚定行尾，句中碎片原样直出并被记忆/上下文回放，进一步教模型复读协议）。
# 竖线/and 符号引导的英文 token 在中文正文里即视为协议残留，句中一并剥除；
# &&word&&（emoji_sender 协议）由回调保留，不在误伤范围。
_PROTO_MID_BAR_RE = re.compile(r"\s*[|&]{1,2}\s*[A-Za-z_]{2,20}\s*[|&]{0,2}")
_PROTO_KEPT_MARKER = re.compile(r"^\s*&&[A-Za-z_]{2,20}&&\s*$")
# ★裸英文情绪 token：模型会自发输出白名单外的情绪词（实证：「……啊， misunderstood
# 清淡甜口的……」），仅当两侧紧邻中日文/中文标点、且词在情绪词表内时剥除，
# 避免误伤正文里的普通英文。
_PROTO_EXTRA_TOKENS = {
    "misunderstood", "understand", "understood", "thinking", "shy", "blush",
    "blushing", "pout", "pouting", "happy", "sad", "crying", "smiling",
    "embarrassed", "surprised", "confused", "angry", "afraid", "scared",
    "worried", "nervous", "excited", "proud", "lonely", "jealous", "annoyed",
    "upset", "curious", "serious", "smug", "teasing", "giggle", "sigh",
    "shocked", "startled", "sleepy", "tired", "bored",
}
_PROTO_ALL_TOKENS = _PROTO_TAIL_TOKENS | _PROTO_EXTRA_TOKENS
_PROTO_BARE_TOKEN_RE = re.compile(
    r"(?<=[\u4e00-\u9fff\u3040-\u30ff，。！？、；：…～—「」『』（）])"
    r"\s*([A-Za-z_]{2,20})\s*"
    r"(?=[\u4e00-\u9fff\u3040-\u30ff，。！？、；：…～—「」『』（）])")


def _strip_proto_text(text: str) -> str:
    """无词卡会话 / 出图失败时的协议兜底：把模型裸输出的卡面协议剥成纯文字。
    · 行尾 |情绪 / &x& / ||x|| / 换行裸 token（仅白名单 token）→ 剥除；
    · ★句中「正文|情绪 正文」碎片（不限白名单，竖线/&&引导的英文 token）→ 剥除；
    · ★夹在中日文标点间的裸英文情绪词（含白名单外自创词，如 misunderstood）→ 剥除；
    · 正文 ||词||（模型把 {} 高亮误写成双竖线）与 {词} → 展平；
    · 保留 [AT:qq] / [NOAHPAINT:] 与 &&emoji&&（emoji_sender priority=99999 自兜底）。"""
    if not text:
        return text
    kept: list = []

    def _keep(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = re.sub(r"\[\s*AT\s*:\s*\d+\s*\]|\[\s*NOAHPAINT:[^\]]+\]", _keep, text)
    text = re.sub(r"\|\|\s*([^|]{1,20}?)\s*\|\|", r"\1", text)

    def _tail(m):
        tok = m.group(1) or m.group(2)
        return "" if tok and tok.strip().lower() in _PROTO_ALL_TOKENS else m.group(0)

    text = _PROTO_TAIL_RE.sub(_tail, text)

    def _mid(m):
        # &&word&& 是 emoji_sender 协议，句中同样保留
        return m.group(0) if _PROTO_KEPT_MARKER.match(m.group(0)) else ""

    text = _PROTO_MID_BAR_RE.sub(_mid, text)

    def _bare(m):
        return "" if m.group(1).strip().lower() in _PROTO_ALL_TOKENS else m.group(0)

    text = _PROTO_BARE_TOKEN_RE.sub(_bare, text)
    text = re.sub(r"\{([^{}]{1,20}?)\}", r"\1", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\s+", " ", text).strip()
    for i, s in enumerate(kept):
        text = text.replace(f"\x00{i}\x00", s)
    return text.strip()


class DialoCardPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        global _plugin_instance
        _plugin_instance = self
        self.plugin_dir = Path(__file__).parent
        self.asset_dir = self.plugin_dir / "asset"
        self.pic_dir = self.asset_dir / "pictures"
        # 用户数据/下载资产全部落 data/plugin_data/<name>（更新/重装不覆盖）；
        # 出厂资产（背景/子集字体/内置诺亚）只读留在插件包内，读取时回退
        self.data_dir = atlas_render.DATA_DIR
        self.char_dirs = atlas_render.CHAR_DIRS  # [plugin_data/characters, 出厂]
        self.font_dirs = [self.data_dir / "fonts", self.asset_dir / "font"]
        self.temp_dir = self.data_dir / "temp"  # 渲染产物/下载临时
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or {}
        # 字体优先完整版（GB2312 外的口语字如「欸」只在完整版里），无完整版用子集兜底
        self.font_path = self._pick_font_path()
        self.builtin_emotions = BUILTIN_EMOTIONS  # WebUI 回显用（见 webui_api._read_emotions）
        self._card_sessions: Dict[str, Any] = {}
        self._font_normal = None
        self._font_highlight = None
        self._char_meta: Dict[str, Dict] = {}
        self._emotions_cache: Dict[str, Any] = {}
        self._load_fonts()
        self._ensure_character_assets()
        self._ensure_font_assets()
        try:
            self.webui_api = _fresh_import("webui_api").DialoWebApi(self)
            self.webui_api.register()
        except Exception as e:
            logger.warning(f"[DialoCard] WebUI 初始化跳过（旧版框架无 Pages 机制时属预期）: {e}")
        logger.info(f"[DialoCard] 初始化完成, 角色={self._current_character()}, "
                    f"atlas_render={getattr(atlas_render, 'MODULE_REV', None) or '缺依赖回退预烘焙webp'}")

    def _pick_font_path(self) -> Path:
        """写死字体优先级：完整版全集（含 GB2312 外口语字）优先、子集兜底。
        完整版下载落 plugin_data/fonts，出厂子集在插件包内。"""
        for name in ("SourceHanSerifCN-SemiBold-7.ttf",
                     "SourceHanSerifCN-SemiBold-subset.ttf"):
            for d in self.font_dirs:
                p = d / name
                if p.exists():
                    return p
        return self.font_dirs[-1] / "SourceHanSerifCN-SemiBold-subset.ttf"

    def _font_url(self) -> str:
        return str(self.config.get("font_url") or DEFAULT_FONT_URL or "").strip()

    def _ensure_font_assets(self):
        """上架 zip 只带子集（16MB 限制）；完整版缺失时后台下载全集并热切换
        渲染字体（同角色包下载机制）。任一搜索路径已有完整版则零下载。"""
        full = "SourceHanSerifCN-SemiBold-7.ttf"
        have = any((d / full).exists() for d in self.font_dirs)
        url = self._font_url()
        if have or not url:
            return
        threading.Thread(target=self._download_font, args=(url,), daemon=True).start()

    def _fetch_to(self, url: str, dest: Path, label: str):
        """流式下载到 dest：分块写盘 + 进度日志，失败抛出（含原因）。
        dest 用临时名落盘（带时间戳），避免热重载后新旧下载线程互抢同一文件。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{time.time():.0f}.part")
        last = 0.0
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done = tmp.stat().st_size / 1048576
                    if done - last >= 8:
                        last = done
                        logger.info(f"[DialoCard] {label}下载中: {done:.0f}MB"
                                    + (f"/{total / 1048576:.0f}MB" if total else ""))
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

    def _download_font(self, url: str):
        try:
            logger.info(f"[DialoCard] 开始下载完整字体包: {url}")
            tmp = self.temp_dir / "font_full.bin"
            self._fetch_to(url, tmp, "字体")
            full = self.font_dirs[0] / "SourceHanSerifCN-SemiBold-7.ttf"
            full.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(tmp) as zf:  # zip 包：取体积最大的 ttf/otf 条目
                    cand = max((i for i in zf.infolist()
                                if i.filename.lower().endswith((".ttf", ".otf"))),
                               key=lambda i: i.file_size)
                    inner = tmp.with_suffix(".ttf")
                    with zf.open(cand) as src, open(inner, "wb") as dst:
                        dst.write(src.read())
                    inner.replace(full)
            except zipfile.BadZipFile:
                tmp.replace(full)  # 直链裸 ttf/otf
            tmp.unlink(missing_ok=True)
            self.font_path = self._pick_font_path()
            self._load_fonts()
            logger.info(f"[DialoCard] 完整字体包就绪，渲染字体已切换: {full.name}")
        except Exception as e:
            logger.warning(f"[DialoCard] 完整字体下载失败（继续用子集渲染）: {e}")

    def _current_character(self) -> str:
        return str(self.config.get("character") or "Noah").strip()

    def _asset_url(self) -> str:
        url = str(self.config.get("asset_url") or DEFAULT_ASSET_URL or "").strip()
        return url

    # ========== 角色资产下载 ==========
    def _ensure_character_assets(self):
        """首次使用自动下载全量角色包到 plugin_data/characters（后台线程，不阻塞启动；
        失败静默——已内置/已下载的角色照常工作，最差回退纯文字/旧版整卡图）。
        manifest 存在但清单角色缺失（插件重装清空后）也会重新下载补齐。"""
        char_dir = self.char_dirs[0]
        char_dir.mkdir(parents=True, exist_ok=True)
        manifest = char_dir / "manifest.json"
        if manifest.exists():
            try:
                listed = json.loads(
                    manifest.read_text(encoding="utf-8")).get("characters", [])
                missing = [c for c in listed if atlas_render.find_char_dir(c) is None]
                if not missing:
                    return
                logger.info(f"[DialoCard] 角色缺失({', '.join(missing[:3])}"
                            f"{'...' if len(missing) > 3 else ''})，重新下载资产包")
            except Exception:
                pass
        url = self._asset_url()
        if not url:
            return
        threading.Thread(target=self._download_assets, args=(url,), daemon=True).start()

    def _download_assets(self, url: str):
        try:
            logger.info(f"[DialoCard] 开始下载角色资产包: {url}")
            tmp_zip = self.temp_dir / "character_assets.zip"
            self._fetch_to(url, tmp_zip, "角色包")
            char_dir = self.char_dirs[0]
            char_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp_zip) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # 防 zip-slip：只放行落在 char_dir 内的条目
                    target = (char_dir / info.filename).resolve()
                    if not str(target).startswith(str(char_dir.resolve())):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        dst.write(src.read())
            tmp_zip.unlink(missing_ok=True)
            self._char_meta.clear()
            atlas_render.clear_cache()
            logger.info(f"[DialoCard] 角色资产包下载完成")
        except Exception as e:
            logger.warning(f"[DialoCard] 角色资产下载失败（可用旧版图/纯文字兜底）: {e}",
                           exc_info=True)

    # ========== 角色层加载 ==========
    def _load_char_meta(self, char: str) -> Optional[Dict[str, Any]]:
        if char in self._char_meta:
            return self._char_meta[char]
        cdir = atlas_render.find_char_dir(char)
        meta_path = cdir / "meta.json" if cdir else None
        if meta_path is None or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[DialoCard] meta.json 解析失败({char}): {e}")
            return None
        self._char_meta[char] = meta
        return meta

    def _load_emotions(self, char: str) -> Dict[str, Any]:
        """emotions.json 热读（mtime 缓存）：WebUI 改完保存即生效。
        读取链：plugin_data（用户保存）→ 插件包出厂组合 → BUILTIN_EMOTIONS 兜底。"""
        for d in self.char_dirs:
            path = d / char / "emotions.json"
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue  # 该级无文件，看下一级
            cached = self._emotions_cache.get(char)
            if cached and cached[:2] == (str(path), mtime):
                return cached[2]
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"[DialoCard] emotions.json 解析失败({path})，回退内置组合: {e}")
                break
            # webui 落盘带 {"emotions": {...}} 包裹层，渲染侧统一解包（兼容旧平铺格式）；
            # 不解包会让 on/off 全部落空，实际渲染按 PSD 默认可见性出图（喷漆粉色残留根因）
            if isinstance(data, dict) and isinstance(data.get("emotions"), dict):
                data = data["emotions"]
            self._emotions_cache[char] = (str(path), mtime, data)
            return data
        return BUILTIN_EMOTIONS.get(char, {})

    def _load_character_layer(self, char: str, emoji: str, layout: str = "left"):
        """返回 (RGBA角色层 或 None, (x, y))。atlas 管线优先，回退预烘焙 webp。
        情绪缺图回退 default；atlas 下未知情绪按 PSD 默认可见性渲染。"""
        # 1) atlas 运行时管线（图集 + emotion 配置 -> 实时合成）
        if atlas_render is not None and atlas_render.has_atlas(char):
            try:
                emotions = self._load_emotions(char)
                cfg = emotions.get(emoji) or emotions.get("default")
                on = list((cfg or {}).get("on") or [])
                off = [str(n) for n in ((cfg or {}).get("off") or []) if str(n).strip()]
                ov = [str(n) for n in ((cfg or {}).get("overlays") or []) if str(n).strip()]
                img, x, y = atlas_render.get_character(char).render(
                    on, hard_off=off, overlays=ov, layout=layout)
                if img is not None:
                    return img, (x, y)
            except Exception as e:
                logger.error(f"[DialoCard] atlas 渲染失败({char}/{emoji}): {e}",
                             exc_info=True)
        # 2) 旧预烘焙管线：{情绪}.webp + meta.json 全局定位
        meta = self._load_char_meta(char)
        if meta is None:
            return None, (0, 0)
        cdir = atlas_render.find_char_dir(char)
        for name in (emoji, "default"):
            p = cdir / f"{name}.webp"
            if p.exists():
                try:
                    return (Image.open(str(p)).convert("RGBA"),
                            (int(meta.get("x", 0)), int(meta.get("y", 0))))
                except Exception as e:
                    logger.error(f"[DialoCard] 角色层加载失败({p.name}): {e}")
        return None, (int(meta.get("x", 0)), int(meta.get("y", 0)))

    # 各版式预旋转背景：left=原作 +3°（background.webp）；
    # right=-3° / none=0° 用 tools/build_cards.py bg 同款算法离线生成，
    # 保证人物/文字/背景三者倾角一致；新文件缺失时回退原版背景
    _BG_BY_LAYOUT = {
        "left": ("background.webp", "background.png"),
        "right": ("background_right.webp", "background.webp"),
        "none": ("background_none.webp", "background.webp"),
    }

    def _load_background(self, layout: str = "left") -> Optional[Image.Image]:
        for name in self._BG_BY_LAYOUT.get(layout, self._BG_BY_LAYOUT["left"]):
            p = self.asset_dir / "background" / name
            if p.exists():
                try:
                    bg = Image.open(str(p)).convert("RGBA")
                    if bg.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
                        bg = bg.resize((CANVAS_WIDTH, CANVAS_HEIGHT), Image.LANCZOS)
                    return bg
                except Exception as e:
                    logger.error(f"[DialoCard] 背景加载失败({name}): {e}")
        return None

    def _load_fonts(self):
        try:
            if self.font_path.exists():
                self._font_normal = ImageFont.truetype(str(self.font_path), FONT_SIZE_NORMAL)
                self._font_highlight = ImageFont.truetype(str(self.font_path), FONT_SIZE_HIGHLIGHT)
            else:
                self._font_normal = ImageFont.load_default()
                self._font_highlight = ImageFont.load_default()
        except Exception as e:
            logger.error(f"[DialoCard] 字体加载失败: {e}")
            self._font_normal = ImageFont.load_default()
            self._font_highlight = ImageFont.load_default()

    def _get_bg_path(self, emoji: str) -> Path:
        return self.pic_dir / f"{emoji}.png"

    def _check_bg_exists(self, emoji: str) -> bool:
        return self._get_bg_path(emoji).exists()

    def _check_asset_exists(self, emoji: str) -> bool:
        """atlas 管线 / 预烘焙角色层 / 旧版整卡图任一可用即可。"""
        char = self._current_character()
        cdir = atlas_render.find_char_dir(char)
        if cdir is not None:
            if (cdir / "atlas.json").exists():
                return True
            if (cdir / f"{emoji}.webp").exists() or (cdir / "default.webp").exists():
                return (cdir / "meta.json").exists()
        return self._check_bg_exists(emoji)

    # ========== on_llm_request ==========
    @filter.on_llm_request()
    async def on_llm_request(self, event=None, req=None):
        plugin = self
        # unbound 调用：self 其实是 event
        if hasattr(self, "unified_msg_origin") and not hasattr(event, "unified_msg_origin"):
            req, event = event, self
            plugin = _plugin_instance
        if plugin is None or event is None or req is None:
            return
        if not hasattr(event, "unified_msg_origin"):
            return

        umo = event.unified_msg_origin
        user_text = event.message_str or ""

        if getattr(event, "_opiniocard_marked", False) or getattr(event, "_noahpaint_marked", False):
            return

        if umo in plugin._card_sessions and not plugin._card_sessions[umo].get("consumed", False):
            return
        if umo in plugin._card_sessions and plugin._card_sessions[umo].get("consumed", False):
            del plugin._card_sessions[umo]

        force_trigger = any(kw in user_text for kw in FORCE_KEYWORDS)
        if not force_trigger and random.random() >= TRIGGER_PROBABILITY:
            return

        chosen_emoji = random.choice(EMOJI_LIST)
        plugin._card_sessions[umo] = {
            "emoji": chosen_emoji,
            "consumed": False,
            "force": force_trigger,
            "image_path": None,
            "sent": False,
        }
        event._dialocard_marked = True

        prompt_addition = f"""
[DIALOCARD_FORMAT_REQ]
输出格式：正文|心情
规则：
1){MAX_BODY_LENGTH}字内
2)1-3个关键词用{{}}高亮：把{{}}直接套在句子里那个词的两侧，禁止把关键词单独罗列在句尾；只标名词/动词/形容词等实词，禁止标语气词（哦啊呀呢吧啦）、助词、标点符号
3)心情选：{', '.join(EMOJI_LIST)}
4)只输出一份正文：禁止写「输出：」等字样、禁止把同一句话输出两遍；{{}}里放正文里真实出现的词，禁止放 name 之类占位符
5)末尾必须有|心情，禁止省略
示例：今天{{天气}}真好，一起去{{画画}}吧|smile
[/DIALOCARD_FORMAT_REQ]"""
        try:
            from astrbot.core.agent.message import TextPart
            # ★mark_as_temp：格式说明仅对本轮生效。旧版未标记临时，协议模板
            # （「正文|心情」「{关键词}」示例）随 extra part 写进框架会话历史，
            # 之后每一轮模型都能看到历史里的协议教材，是未触发词卡的回合也
            # 裸输出「正文|flushed」/自创情绪词（misunderstood）的主要来源
            part = TextPart(text=prompt_addition)
            if hasattr(part, "mark_as_temp"):
                part.mark_as_temp()
            req.extra_user_content_parts.append(part)
        except ImportError:
            req.prompt += "\n\n" + prompt_addition

        logger.info(f"[DialoCard] {'[强制]' if force_trigger else '[概率]'}已标记: {umo}")

    # ========== on_llm_response ==========
    @filter.on_llm_response(priority=-1)
    async def on_llm_response(self, event=None, resp=None):
        plugin = self
        # unbound 调用：self 其实是 event
        if hasattr(self, "unified_msg_origin") and not hasattr(event, "unified_msg_origin"):
            resp, event = event, self
            plugin = _plugin_instance
        if plugin is None or event is None or resp is None:
            return
        if not hasattr(event, "unified_msg_origin"):
            return

        umo = event.unified_msg_origin
        session_info = plugin._card_sessions.get(umo)
        if not session_info or session_info.get("consumed", False):
            # v1 无词卡会话兜底：本回合并未触发词卡，模型却自发模仿了协议
            # （「正文|flushed」「||土豆|| ||刀||」「{词}」「句子|smile 句子」「，misunderstood，」），
            # pipeline 已在 card_active=False 通道剥过一遍；此处作为最后一道保险，保证裸协议不直出
            try:
                t0 = getattr(resp, "completion_text", "") or ""
                if t0 and ("{" in t0 or "|" in t0 or "&" in t0
                           or _PROTO_BARE_TOKEN_RE.search(t0)):
                    cleaned = _strip_proto_text(t0)
                    if cleaned != t0:
                        resp.completion_text = cleaned
                        logger.warning(f"[DialoCard] 无会话协议碎片已清理: {repr(t0[-60:])}")
            except Exception as e:
                logger.error(f"[DialoCard] 无会话兜底清理失败: {e}")
            return

        # 跳过 chunk 模式
        if getattr(resp, "is_chunk", False):
            return

        # 优先使用 completion_text（可能已被前置 filter 插件处理，如 MessageReplacer / noah风格流水线）
        text = getattr(resp, "completion_text", "") or ""

        # 预检：如果 completion_text 中仍能解析出有效 emotion，说明标记未被破坏，直接使用
        _test_emoji, _, _ = plugin._parse_card_text(text)

        def _raw_content():
            raw = getattr(resp, "raw_completion", None)
            if raw is None:
                return ""
            try:
                return raw.choices[0].message.content or ""
            except (AttributeError, IndexError, TypeError):
                return ""

        if not text.strip():
            # completion 为空：维持原行为，用原始输出抢救（有尾标出卡片，无尾标也至少出文字）
            text = _raw_content()
        elif not _test_emoji:
            # completion 非空但无尾标：可能已被 noah 风格流水线加工。
            # 仅当原始输出带有效尾标（能抢救成卡片）时才回退 raw，
            # 否则保留当前文本走纯文字 fallback，避免抹掉风格化结果
            fallback = _raw_content()
            if fallback.strip():
                _raw_emoji, _, _ = plugin._parse_card_text(fallback)
                if _raw_emoji:
                    text = fallback
        
        # 最后回退：从 tool_calls 提取
        if not text or not text.strip():
            text = plugin._extract_text_from_s(resp)
        if not text:
            return

        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            # 模型只吐出裸花括号（无正文/无情绪尾标）= 不符合词卡格式，
            # 直接 fallback 纯文字，避免「{词}」原样发出（卫生事故兜底）
            pure = _strip_proto_text(stripped)
            at_markers = re.findall(r"\[\s*AT\s*:\s*\d+\s*\]", stripped)
            paint_markers = re.findall(r"\[\s*NOAHPAINT:[^\]]+\]", stripped)
            tail_mk = " ".join(at_markers + paint_markers)
            resp.completion_text = (pure + (" " + tail_mk if tail_mk else "")).strip()
            logger.warning(f"[DialoCard] 裸花括号输出，回退纯文字: {repr(stripped[-60:])}")
            plugin._cleanup_session(umo)
            return
        if re.search(r"\|[\s]*(?:agree|disagree|doubt)[\s]*$", stripped, re.IGNORECASE):
            logger.info(f"[DialoCard] 检测到立场卡格式，跳过处理")
            plugin._cleanup_session(umo)
            return
        if re.search(r"&&[a-zA-Z_]+&&", text):
            logger.info(f"[DialoCard] 检测到表情包标记，跳过处理")
            plugin._cleanup_session(umo)
            return

        session_info["consumed"] = True
        session_info["original_text"] = text
        logger.info(f"[DialoCard] LLM原始输出: {repr(text)}")

        original_text = text

        def _write_plain(pure: str):
            """回退纯文字：保留 @ / 发画标记，绝不允许脏的协议原文直出（completion_text
            此时可能仍是「正文|flushed」，见 on_decorating_result 纯图片替换逻辑）。
            ★NOAHPAINT 标记同样回贴：_parse_card_text 解析时已把它剥掉，不回贴会让
            词卡轮里附带发画的指令静默丢失（2026-10-06 00:42 实证：文字与画作都没送达）。"""
            at_markers = re.findall(r"\[\s*AT\s*:\s*\d+\s*\]", original_text)
            paint_markers = re.findall(r"\[\s*NOAHPAINT:[^\]]+\]", original_text)
            tail_mk = " ".join(at_markers + paint_markers)
            resp.completion_text = (pure + (" " + tail_mk if tail_mk else "")).strip()

        try:
            emoji, clean_text, keywords = plugin._parse_card_text(text)
            session_info["clean_text"] = clean_text
            session_info["keywords"] = keywords
            session_info["emoji_used"] = emoji

            # 不带 emotion（含字数超限）时 fallback 到文字输出
            if not emoji:
                logger.info(f"[DialoCard] 未检测到有效 emotion，fallback 到文字输出: {clean_text}")
                _write_plain(clean_text)
                plugin._cleanup_session(umo)
                return

            if not plugin._check_asset_exists(emoji):
                emoji = session_info.get("emoji", "smile")
            if not plugin._check_asset_exists(emoji):
                emoji = "smile"
            if not plugin._check_asset_exists(emoji):
                logger.error("[DialoCard] 底图不存在，回退纯文字")
                _write_plain(clean_text)
                plugin._cleanup_session(umo)
                return

            image_path = plugin._generate_card(emoji, clean_text, keywords)
            if not image_path or not os.path.exists(image_path):
                logger.error("[DialoCard] 生成失败，回退纯文字")
                _write_plain(clean_text)
                plugin._cleanup_session(umo)
                return

            _write_plain(clean_text)
            session_info["image_path"] = image_path

            logger.info(f"[DialoCard] 词卡已生成: emoji={emoji}, text={clean_text}")

        except Exception as e:
            logger.error(f"[DialoCard] 异常，回退纯文字: {e}", exc_info=True)
            # clean_text 可能尚未赋值（解析阶段异常），对原文做协议剥离兜底
            try:
                _write_plain(_strip_proto_text(original_text))
            except Exception as e2:
                logger.error(f"[DialoCard] 异常回退二次失败: {e2}")
            plugin._cleanup_session(umo)

    def _extract_text_from_s(self, resp: LLMResponse) -> str:
        try:
            if hasattr(resp, "raw_completion") and resp.raw_completion:
                raw = resp.raw_completion
                if hasattr(raw, "choices") and raw.choices:
                    choice = raw.choices[0]
                    if hasattr(choice, "message") and choice.message:
                        msg = choice.message
                        if hasattr(msg, "s") and msg.s:
                            for tc in msg.s:
                                if hasattr(tc, "function") and tc.function:
                                    func = tc.function
                                    if hasattr(func, "arguments") and func.arguments:
                                        args = json.loads(func.arguments)
                                        messages = args.get("messages", [])
                                        for m in messages:
                                            if m.get("type") == "plain":
                                                return m.get("text", "")
            if hasattr(resp, "s") and resp.s:
                for tc in resp.s:
                    if hasattr(tc, "function") and tc.function:
                        args = json.loads(tc.function.arguments)
                        messages = args.get("messages", [])
                        for m in messages:
                            if m.get("type") == "plain":
                                return m.get("text", "")
        except Exception:
            pass
        return ""

    def _parse_card_text(self, text: str) -> Tuple[str, str, List[str]]:
        text = text.strip()
        emoji = ""
        keywords = []

        # ★双份输出兜底（2026-10-11 17:46 实证：模型输出
        # 「A|confused 输出：B|confused」，末尾情绪tag被rsplit抓走后，
        # 正文里整段双份内容连同「输出：」一起渲染进卡片直出群聊）。
        # 「输出：」是协议泄漏不是台词——只保留最后一段「输出：」之后的成品
        _out = max(text.rfind("输出："), text.rfind("输出:"))
        if _out != -1:
            after = text[_out + 3:].strip()
            if after:
                text = after

        # 清理艾特标记
        text = re.sub(r"\[\s*AT\s*:\s*\d+\s*\]", "", text).strip()
        # ★字段名标签泄漏兜底（2026-10-11 23:14 实证：模型把格式说明
        # 「输出格式：正文|心情」里的字段名照抄进输出，「正文：嗯？妈妈为什么哭了
        # 呢？」直出词卡正文）——剥行首字段标签（可重复）
        text = re.sub(r"^(?:(?:正文|心情)[:：]\s*)+", "", text)
        # 清理 NoahPaint 画作标记
        text = re.sub(r"\[NOAHPAINT:[^\]]+\]", "", text).strip()

        # 从末尾提取 emoji
        emoji_patterns = [
            (r"[\|&]{2}\s*([a-zA-Z_]+)\s*[\|&]{2}\s*$", 1),
            (r"[\|&]\s*([a-zA-Z_]+)\s*[\|&]\s*$", 1),
            (r"[\|&]{3,}\s*([a-zA-Z_]+)\s*$", 1),
            (r"[\|&]{2}\s*([a-zA-Z_]+)\s*$", 1),
            (r"[\|&]\s*([a-zA-Z_]+)\s*$", 1),
            (r"\n\s*([a-zA-Z_]+)\s*$", 1),
        ]
        body = text
        for pattern, group_idx in emoji_patterns:
            match = re.search(pattern, text)
            if match:
                potential = match.group(group_idx).strip().lower()
                if potential in EMOJI_LIST:
                    emoji = potential
                    body = text[:match.start()].strip()
                    break

        # 标准 | 分隔符回退
        if not emoji and "|" in text:
            parts = text.rsplit("|", 1)
            if len(parts) == 2:
                body = parts[0].strip()
                emoji = parts[1].strip().lower()
                if emoji not in EMOJI_LIST:
                    emoji = ""

        # 清理正文
        if body:
            body = re.sub(r"[\|&\s]+$", "", body).strip()
            body = re.sub(r"\n{2,}", " ", body)
            body = re.sub(r"\n", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
            body = re.sub(r"[，,]+", "，", body)
            body = re.sub(r"[。.]+", "。", body)
            # ★句中协议碎片清洗：模型偶尔在正文中间再塞一个「|情绪」
            # （如「A|flushed B|smile」解析掉行尾 smile 后正文还剩「A|flushed B」），
            # 不剥会直接渲染进卡片/回退文字里
            body = _PROTO_MID_BAR_RE.sub("", body).strip()

        # 提取关键词
        keywords = re.findall(r"\{([^{}]+?)\}", body)
        keywords = [k.strip() for k in keywords if k and k.strip()]

        # 去掉关键词标记
        clean_text = re.sub(r"\{([^{}]+?)\}", r"\1", body)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        # 长度限制 —— 超限时不截断，返回空 emoji 触发 fallback 到文字输出
        if len(clean_text) > MAX_BODY_LENGTH:
            return "", clean_text, keywords

        return emoji, clean_text, keywords

    def _generate_card(self, emoji: str, text: str, keywords: List[str],
                       layout: str = "random") -> Optional[str]:
        try:
            canvas = None
            # 版式三选一（人物/文字镜像布局，倾角同乘当前构图 angle）：
            # left = 画面左倾、人物在左、文字在右（原作）；right = 右倾、人物右、文字左；
            # none = 不倾、人物右、文字左；layout 显式传入时用指定版式（WebUI 试渲染）
            if layout not in ("left", "right", "none"):
                layout = random.choice(("left", "right", "none"))
            # 1) 新管线：背景 + 透明角色层（atlas 实时合成或预烘焙 webp，已含缩放/旋转）
            char_img, pos = self._load_character_layer(self._current_character(), emoji, layout)
            if char_img is None:
                layout = "left"  # 旧兜底烤死卡人物固定在左，文字保持原版式
            if char_img is not None:
                bg = self._load_background(layout)
                if bg is not None:
                    canvas = bg.copy()
                    canvas.paste(char_img, pos, char_img)
            # 2) 旧版兜底：整张烤死卡（角色+背景一体）
            if canvas is None and self._check_bg_exists(emoji):
                bg_path = self._get_bg_path(emoji)
                bg = Image.open(str(bg_path)).convert("RGBA")
                if bg.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
                    bg = bg.resize((CANVAS_WIDTH, CANVAS_HEIGHT), Image.LANCZOS)
                canvas = bg.copy()
            if canvas is None:
                logger.error("[DialoCard] 无背景与角色资产")
                return None
            lines = self._layout_text(text, keywords)
            self._draw_text_rotated(canvas, lines, layout)
            text_hash = hashlib.md5(f"{emoji}_{text}_{'_'.join(keywords)}".encode()).hexdigest()[:12]
            output_path = self.temp_dir / f"dialocard_{emoji}_{text_hash}.png"
            canvas.save(str(output_path), "PNG")
            return str(output_path)
        except Exception as e:
            logger.error(f"[DialoCard] 渲染失败: {e}", exc_info=True)
            return None

    def _layout_text(self, text: str, keywords: List[str]) -> List[List[Tuple[str, bool]]]:
        if keywords:
            escaped = [re.escape(kw) for kw in keywords]
            pattern = "|".join(escaped)
            parts = re.split(f"({pattern})", text)
        else:
            parts = [text]
        parts = [p for p in parts if p]
        segments = [(p, p in keywords) for p in parts]
        return self._wrap_lines(segments)

    def _wrap_lines(self, segments: List[Tuple[str, bool]]) -> List[List[Tuple[str, bool]]]:
        lines = []
        current_line = []
        current_width = 0
        max_width = TEXTBOX_WIDTH - 60

        for seg_text, is_keyword in segments:
            font = self._font_highlight if is_keyword else self._font_normal
            if is_keyword:
                bbox = font.getbbox(seg_text)
                seg_width = bbox[2] - bbox[0] if bbox else len(seg_text) * FONT_SIZE_NORMAL
                if current_width + seg_width > max_width and current_line:
                    lines.append(current_line)
                    current_line = [(seg_text, is_keyword)]
                    current_width = seg_width
                else:
                    current_line.append((seg_text, is_keyword))
                    current_width += seg_width
            else:
                chars = list(seg_text)
                char_idx = 0
                while char_idx < len(chars):
                    char = chars[char_idx]
                    bbox = font.getbbox(char)
                    char_width = bbox[2] - bbox[0] if bbox else FONT_SIZE_NORMAL
                    if not current_line and char_width > max_width:
                        current_line.append((char, False))
                        current_width = char_width
                        char_idx += 1
                        continue
                    if current_width + char_width > max_width and current_line:
                        lines.append(current_line)
                        current_line = []
                        current_width = 0
                        continue
                    if current_line and not current_line[-1][1]:
                        current_line[-1] = (current_line[-1][0] + char, False)
                    else:
                        current_line.append((char, False))
                    current_width += char_width
                    char_idx += 1

        if current_line:
            lines.append(current_line)
        return lines

    def _draw_text_rotated(self, canvas: Image.Image, lines: List[List[Tuple[str, bool]]],
                           layout: str = "left"):
        expand = 500
        text_layer = Image.new("RGBA", (CANVAS_WIDTH + expand * 2, CANVAS_HEIGHT + expand * 2), (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer)
        offset_x = expand
        offset_y = expand

        text_left = (TEXTBOX_LEFT if layout == "left"
                     else CANVAS_WIDTH - TEXTBOX_LEFT - TEXTBOX_WIDTH) + offset_x
        tilt = (TILT_ANGLE if layout == "left"
                else -TILT_ANGLE if layout == "right" else 0)
        box_center_y = CANVAS_HEIGHT // 2
        total_height = len(lines) * LINE_SPACING
        start_y = box_center_y - total_height // 2 + offset_y

        normal_bbox = self._font_normal.getbbox("Ay")
        normal_descent = normal_bbox[3] if normal_bbox else FONT_SIZE_NORMAL * 0.2

        for line_idx, line_segments in enumerate(lines):
            y = start_y + line_idx * LINE_SPACING
            x = text_left
            for seg_text, is_keyword in line_segments:
                font = self._font_highlight if is_keyword else self._font_normal
                color = COLOR_PINK if is_keyword else COLOR_WHITE
                bbox = font.getbbox(seg_text)
                if bbox:
                    text_width = bbox[2] - bbox[0]
                    descent = bbox[3]
                else:
                    text_width = len(seg_text) * FONT_SIZE_NORMAL
                    descent = FONT_SIZE_NORMAL * 0.2
                draw_y = y + normal_descent - descent
                for angle in range(0, 360, 15):
                    rad = math.radians(angle)
                    for r in range(1, STROKE_WIDTH + 1):
                        dx = int(r * math.cos(rad))
                        dy = int(r * math.sin(rad))
                        text_draw.text((x + dx, draw_y + dy), seg_text, font=font, fill=COLOR_BLACK)
                text_draw.text((x, draw_y), seg_text, font=font, fill=color)
                x += text_width

        rotated = text_layer.rotate(
            tilt,
            resample=Image.BICUBIC,
            expand=False,
            center=(offset_x + CANVAS_WIDTH // 2, offset_y + CANVAS_HEIGHT // 2)
        )
        rotated = rotated.crop((offset_x, offset_y, offset_x + CANVAS_WIDTH, offset_y + CANVAS_HEIGHT))
        canvas.paste(rotated, (0, 0), rotated)

    # ========== on_decorating_result ==========
    @filter.on_decorating_result()
    async def on_decorating_result(self, event=None):
        plugin = self
        # unbound 调用：self 其实是 event
        if hasattr(self, "unified_msg_origin") and not hasattr(event, "unified_msg_origin"):
            event = self
            plugin = _plugin_instance
        if plugin is None or event is None:
            return
        if not hasattr(event, "unified_msg_origin"):
            return

        umo = event.unified_msg_origin
        session_info = plugin._card_sessions.get(umo)
        if not session_info:
            return

        image_path = session_info.get("image_path")
        if not image_path or not os.path.exists(image_path):
            plugin._cleanup_session(umo)
            return

        if session_info.get("sent"):
            plugin._cleanup_session(umo)
            return

        try:
            result = event.get_result()
            if result is None:
                plugin._cleanup_session(umo)
                return
            session_info["sent"] = True
            # 发送纯图片，不保留任何文本，不引用对方消息
            new_chain = []
            for comp in result.chain:
                comp_type = getattr(comp, "type", None) or \
                    getattr(comp, "component_type", None) or \
                    comp.__class__.__name__
                if comp_type in ("Plain", "plain"):
                    continue
                new_chain.append(comp)
            new_chain.append(Comp.Image.fromFileSystem(image_path))
            result.chain = new_chain
            logger.info(f"[DialoCard] 已发送纯图片")
        except Exception as e:
            logger.error(f"[DialoCard] 消息装饰失败: {e}", exc_info=True)
        finally:
            plugin._cleanup_session(umo)

    def _cleanup_session(self, umo: str):
        if umo in self._card_sessions:
            del self._card_sessions[umo]

    async def terminate(self):
        try:
            for f in self.temp_dir.glob("dialocard_*.png"):
                f.unlink(missing_ok=True)
            self._card_sessions.clear()
            logger.info("[DialoCard] 临时文件和会话已清理")
        except Exception as e:
            logger.error(f"[DialoCard] 清理失败: {e}")