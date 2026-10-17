# -*- coding: utf-8 -*-
"""DialoCard · 官方插件页 Web API 适配层。

通过 AstrBot 官方 Pages 机制挂载（无需独立 Web 服务）：
  · 页面：pages/console/index.html（仪表盘 → 插件 → 词卡 → Pages → console）
  · 前端桥：window.AstrBotPluginPage.apiGet/apiPost（bridge SDK 自动注入）
  · 路由：/{plugin_name}/page/<endpoint>，经 context.register_web_api 注册

返回信封：{"status": "ok", "data": ...} / {"status": "error", "message": ...}
预览端点直接返回 PNG 二进制 Response（_coerce_view_result 原样透传）。
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import traceback
from pathlib import Path
from typing import Any

from PIL import Image

try:
    import atlas_render
except ImportError:  # 插件以 data.plugins.* 包形式导入时，回退相对导入
    from . import atlas_render
from astrbot.api import logger
from astrbot.api.web import request

PLUGIN_DIR = Path(__file__).resolve().parent
CHAR_DIR = PLUGIN_DIR / "asset" / "characters"
PALETTE_PATH = PLUGIN_DIR / "asset" / "palette.json"
TRANSFORM_PATH = PLUGIN_DIR / "asset" / "transform.json"
DEFAULT_PALETTE = [{"name": "默认粉", "hex": "#FB92AE"}]


def _ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data}


def _err(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _guard(fn):
    """handler 异常落完整 traceback 到日志，回传可读 message。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ValueError as exc:
            logger.debug(f"[DialoCard] WebUI 接口 {fn.__name__} 输入错误: {exc}")
            return _err(str(exc))
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error(f"[DialoCard] WebUI 接口 {fn.__name__} 异常:\n{tb}")
            last = tb.strip().splitlines()[-1] if tb.strip() else str(exc)
            return _err(f"{type(exc).__name__}: {last}")

    return wrapper


def _list_characters() -> list:
    if not CHAR_DIR.exists():
        return []
    return sorted(d.name for d in CHAR_DIR.iterdir()
                  if d.is_dir() and (d / "atlas.json").exists())


def _read_emotions(char: str, builtin: dict = None) -> dict:
    emo_path = CHAR_DIR / char / "emotions.json"
    if emo_path.exists():
        try:
            data = json.loads(emo_path.read_text(encoding="utf-8")).get("emotions", {})
        except Exception:
            data = {}
        if data:
            return data
    # 文件缺失/被重置/损坏：回退代码内置组合（与渲染侧同款兜底），回显真实渲染状态；
    # 此时在 WebUI 点保存即把内置组合固化回 emotions.json，一键修复
    return dict(builtin or {}).get(char, {})


def _load_state(char: str, builtin: dict = None) -> dict:
    char = str(char or "").strip()
    if char not in _list_characters():
        raise ValueError(f"未知角色 {char}")
    atlas = json.loads((CHAR_DIR / char / "atlas.json").read_text(encoding="utf-8"))
    return {"layers": atlas.get("layers", []),
            "emotions": _read_emotions(char, builtin)}


def _save_emotions(char: str, emotions: dict) -> dict:
    """清洗后落盘 asset/characters/{char}/emotions.json（渲染侧 mtime 热读）。
    图层名只放行 atlas 里真实存在的，防止 WebUI 之外的垃圾数据写坏渲染。"""
    char = str(char or "").strip()
    if char not in _list_characters():
        raise ValueError(f"未知角色 {char}")
    if not isinstance(emotions, dict):
        raise ValueError("emotions 必须是对象")
    atlas = json.loads((CHAR_DIR / char / "atlas.json").read_text(encoding="utf-8"))
    valid = {str(l.get("name", "")) for l in atlas.get("layers", [])}
    clean = {}
    for name, cfg in emotions.items():
        name = str(name).strip()
        if not name:
            continue
        cfg = cfg or {}

        def _names(key):
            return [str(n).strip() for n in (cfg.get(key) or [])
                    if str(n).strip() and str(n).strip() in valid]

        clean[name] = {"on": _names("on"), "off": _names("off"),
                       "overlays": _names("overlays")}
    path = CHAR_DIR / char / "emotions.json"
    if path.exists():
        # 保存是前端内存状态的全量覆盖，先留档上一版（.bak），误保存/覆盖可找回
        path.replace(path.parent / (path.name + ".bak"))
    path.write_text(json.dumps({"emotions": clean}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return clean


def _load_palette() -> list:
    """PSD 图层色卡（asset/palette.json），缺失时回退默认粉。"""
    try:
        data = json.loads(PALETTE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return list(DEFAULT_PALETTE)
    out = []
    for c in data if isinstance(data, list) else []:
        hexv = str((c or {}).get("hex", "")).strip()
        if hexv.startswith("#") and len(hexv) in (4, 7):
            out.append({"name": str((c or {}).get("name", "")).strip(), "hex": hexv})
    return out or list(DEFAULT_PALETTE)


def _render_preview(char: str, on: list, off: list, ov: list, final: bool, bg=None) -> str:
    """渲染并返回 data URL（iframe 为 sandbox 无 allow-same-origin，
    直连 extensions API 的 <img> 带不上 cookie，须走 bridge JSON 通道）。
    final=True 时叠上真实背景，输出与发卡消息一致的整卡画面。"""
    c = atlas_render.get_character(char)
    if final:
        img, x, y = c.render(on, hard_off=off, overlays=ov)
        if bg is not None:
            canvas = bg.copy()
            if img is not None:
                canvas.paste(img, (int(x), int(y)), img)
            img = canvas
        elif img is None:
            img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    else:
        img = c.preview(on, hard_off=off, overlays=ov)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    import base64
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


_TRANSFORM_LIMITS = {
    "angle": (-30.0, 30.0), "scale": (0.5, 1.6),
    "dx": (-800.0, 800.0), "dy": (-600.0, 600.0),
}


def _apply_transform(body: dict, save: bool) -> dict:
    """校验并应用全局构图；save=True 时持久化到 asset/transform.json。"""
    cur = atlas_render.get_transform()
    clean = {}
    for k, (lo, hi) in _TRANSFORM_LIMITS.items():
        v = float(body.get(k, cur[k]))
        if not lo <= v <= hi:
            raise ValueError(f"{k} 超出范围 [{lo}, {hi}]")
        clean[k] = round(v, 2)
    t = atlas_render.set_transform(**clean)
    if save:
        TRANSFORM_PATH.write_text(
            json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
    return t


class DialoWebApi:
    """插件页 API：角色/图层/情绪差分读写、色卡、预览图。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        name = "词卡"
        try:
            import yaml
            meta_file = Path(__file__).parent / "metadata.yaml"
            meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
            name = str(meta.get("name") or name)
        except Exception:
            pass
        self.prefix = f"/{name}/page"

    def register(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("chars", self.get_chars, ["GET"], "词卡 · 角色列表"),
            ("state", self.get_state, ["GET"], "词卡 · 图层与情绪"),
            ("save", self.save, ["POST"], "词卡 · 保存情绪差分"),
            ("palette", self.get_palette, ["GET"], "词卡 · 主题色卡"),
            ("preview", self.preview, ["GET"], "词卡 · 预览图"),
            ("transform", self.transform, ["GET", "POST"], "词卡 · 全局构图（读/写）"),
            ("active", self.active, ["GET", "POST"], "词卡 · 渲染角色（读/设）"),
            ("render_test", self.render_test, ["GET"], "词卡 · 试渲染（实际发卡管线）"),
        ]
        n = 0
        for sub, handler, methods, desc in routes:
            try:
                register(f"{self.prefix}/{sub}", handler, methods, desc)
                n += 1
            except Exception as e:
                logger.warning(f"[DialoCard] WebUI 路由 {sub} 注册跳过: {e}")
        if n:
            logger.info(
                f"[DialoCard] WebUI API 已注册 {n} 条路由"
                "（仪表盘 → 插件 → 词卡 → Pages → console）")

    # ── handlers ──

    @_guard
    async def get_chars(self, **_p):
        return _ok(await asyncio.to_thread(_list_characters))

    @_guard
    async def get_state(self, **_p):
        char = request.query.get("char", "")
        builtin = getattr(self.plugin, "builtin_emotions", None)
        return _ok(await asyncio.to_thread(_load_state, char, builtin))

    @_guard
    async def save(self, **_p):
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象")
        saved = await asyncio.to_thread(
            _save_emotions, str(body.get("char", "")), body.get("emotions", {}))
        return _ok({"emotions": saved})

    @_guard
    async def get_palette(self, **_p):
        return _ok(await asyncio.to_thread(_load_palette))

    @_guard
    async def preview(self, **_p):
        char = request.query.get("char", "")
        on = [n for n in (request.query.get("on", "") or "").split(",") if n]
        off = [n for n in (request.query.get("off", "") or "").split(",") if n]
        ov = [n for n in (request.query.get("ov", "") or "").split(",") if n]
        final = request.query.get("final", "0") == "1"
        if char not in _list_characters():
            raise ValueError(f"未知角色 {char}")
        bg = await asyncio.to_thread(self.plugin._load_background) if final else None
        src = await asyncio.to_thread(_render_preview, char, on, off, ov, final, bg)
        return _ok({"src": src})

    @_guard
    async def render_test(self, **_p):
        """试渲染：走 main._generate_card 真实发卡管线（emotions 解包 / 版式背景 /
        随机版式），与 QQ 端实际出图完全同路径。
        ?char=&emoji=&text=&layout=left|right|none|random -> {"src": dataURL}"""
        char = str(request.query.get("char", "")) or self.plugin._current_character()
        if char not in _list_characters():
            raise ValueError(f"未知角色 {char}")
        emoji = str(request.query.get("emoji", "smile")).strip().lower() or "smile"
        text = str(request.query.get("text", "")).strip() or "今天天气真好，一起去画画吧"
        layout = str(request.query.get("layout", "random")).strip().lower()
        path = await asyncio.to_thread(self.plugin._generate_card, emoji, text, [], layout)
        if not path:
            raise ValueError("渲染失败（查看控制台日志）")

        def _read() -> str:
            import base64
            with open(path, "rb") as f:
                return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

        return _ok({"src": await asyncio.to_thread(_read), "layout": layout})

    @_guard
    async def transform(self, **_p):
        """GET 读当前构图；POST {"angle","scale","dx","dy","save"} 写（save=落盘）。"""
        try:
            body = await request.json(default=None)
        except Exception:
            body = None  # GET 无请求体
        if not isinstance(body, dict) or not body:
            return _ok(await asyncio.to_thread(atlas_render.get_transform))
        save = bool(body.get("save", False))
        t = await asyncio.to_thread(_apply_transform, body, save)
        return _ok(t)

    @_guard
    async def active(self, **_p):
        """GET 读当前实际渲染角色；POST {"char"} 设为该角色（写插件 config 并持久化）。
        渲染侧每次发卡都动态读 config，设置后下一条消息即生效。"""
        if request.method == "POST":
            body = await request.json(default={}) or {}
            char = str(body.get("char", "")).strip()
            if char not in _list_characters():
                raise ValueError(f"未知角色 {char}")
            self.plugin.config["character"] = char
            sc = getattr(self.plugin.config, "save_config", None)
            if sc:
                await asyncio.to_thread(sc)  # AstrBotConfig 落盘；纯 dict 时仅内存生效
            return _ok({"char": char})
        return _ok({"char": self.plugin._current_character()})
