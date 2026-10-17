# DialoCard 词卡插件

将 AstrBot 的 LLM 文本回复以 **20% 概率**渲染为精美词卡图片。

## 特性

- **多角色差分立绘**: 角色立绘来自 PSD 差分图层（眼神/嘴型/脸颊/手臂），离线管线按情绪合成
- **分层渲染**: 背景 + 透明角色层（WebP）运行时合成，切换角色零成本
- **心情驱动**: 10 种心情（wronged、surprise、smile、paint、flushed、embarrass、cry、confused、angry、afraid）+ default
- **Keyword 高亮**: LLM 自动选择关键词进行粉色高亮，字号放大 1.2 倍
- **倾斜排版**: 文字整体倾斜 3 度 + 黑色描边
- **角色全量自动下载**: 首次启动按 `asset_url` 配置自动下载全部角色包，之后随时在 WebUI 切换
- **优雅降级**: 角色包缺失 → 旧版整卡图 → 纯文字，逐级兜底

## 目录结构

```
astrbot_plugin_dialocard/
├── main.py                  # 插件主代码
├── metadata.yaml
├── _conf_schema.json        # WebUI 配置（character / asset_url）
├── asset/
│   ├── background/          # 背景图（background.webp）
│   ├── characters/          # 角色层（内置 Noah，其余自动下载）
│   │   └── Noah/
│   │       ├── default.webp ... afraid.webp   # 透明角色层（已含缩放/旋转/定位）
│   │       └── meta.json                      # 在 1920×1200 卡上的粘贴坐标
│   └── font/                # 子集化字体（GB2312 全汉字 + ASCII，2.6MB）
├── temp/                    # 生成缓存（运行时）
└── tools/                   # ★开发机专用，不影响插件运行
    ├── build_cards.py       # PSD → 角色层 离线构建管线
    ├── subset_font.py       # 字体子集化
    └── characters/*.json    # 每角色的情绪→差分图层映射与构图参数
```

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `character` | `Noah` | 当前角色（asset/characters/ 下的目录名） |
| `asset_url` | Releases 直链 | 全量角色包 zip（13 角色）；上架包仅内置 Noah，首次启动自动下载全部角色 |
| `font_url` | Releases 直链 | 完整版思源宋体（含「欸」等 GB2312 外口语字）；上架包只带子集，首次启动自动下载。渲染字体写死完整版优先、子集兜底 |

## 角色资产制作（开发机）

```bash
py -m pip install psd-tools pillow fonttools
# 1. 把 PSD 放入 asset/chracter/，在 tools/characters/{角色}.json 里
#    标定情绪→差分图层映射与构图参数（crop/scale/anchor/angle）
py tools/build_cards.py contact {角色} "ArmL"     # 某组差分联络表（挑手臂姿势用）
py tools/build_cards.py sample {角色}             # 整卡样张 -> tools/samples/
py tools/build_cards.py render {角色}             # 正式产出 -> asset/characters/{角色}/
# 2. 字体子集化（首次）
py tools/subset_font.py
```

差分合成输出为 1920×1200 卡坐标下的透明角色层（裁剪→缩放→旋转3°→定位），
`meta.json` 记录 union bbox 左上角坐标，运行时直接 paste。

## 工作原理

1. **on_llm_request**: 以 20% 概率标记会话，注入临时格式协议（正文|心情 + {关键词}）
2. **on_llm_response**: 解析输出并渲染词卡（背景 + 角色层 + 倾斜文字），失败逐级回退
3. **on_decorating_result**: 将消息链替换为纯图片

## 依赖

- Pillow（运行时）
- psd-tools、fonttools（仅离线工具链）
- AstrBot >= 4.16

## License

MIT
