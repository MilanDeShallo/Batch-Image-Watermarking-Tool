# 批量图片水印工具

> 基于 Python + Pillow 的桌面端批量水印处理工具，面向图片与 PDF 的版权保护与品牌标识场景。开箱即用，支持拖拽导入、批量处理、实时预览，可一键打包为 Windows 可执行文件。

<!--
## 界面预览

![界面预览](assets/screenshot.png)

如需在 README 显示界面截图：
1. 运行 `python main.py` 启动程序
2. 拖入一张测试图，设置水印参数做演示
3. 用截图工具截取界面，保存为 `assets/screenshot.png`
4. 删除上方 HTML 注释包裹的两行（`<!--` 和 `-->`），保存提交
-->

---

## 目录

- [功能亮点](#功能亮点)
- [支持格式](#支持格式)
- [快速开始](#快速开始)
- [从源码运行](#从源码运行)
- [打包为可执行文件](#打包为可执行文件)
- [命令行用法](#命令行用法)
- [配置参数详解](#配置参数详解)
- [预设系统](#预设系统)
- [技术设计](#技术设计)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 功能亮点

### 1. 双水印模式，可叠加

支持**文字水印**与**图片 / Logo 水印**两种模式，可单独使用，也可同时叠加（先文字后图片）。

### 2. 文字水印完整参数

| 参数 | 说明 |
|------|------|
| 文本内容 | 支持多行（`\n` 换行） |
| 字体 | 自动探测系统字体，支持手动指定 |
| 字号 | 按图片短边的百分比自适应（1%–20%） |
| 颜色 | RGBA 四通道（颜色选择器取色） |
| 不透明度 | 0–100（0 = 完全看不见，100 = 完全不透明） |
| 位置 | 九宫格预设 或 自定义坐标百分比 |
| 旋转 | -180° ~ 180° |
| 平铺 | 整图平铺，支持行间错位；间距按「看得见的边缘」计算 |
| 边距 | 像素级边距 |

### 3. 图片 / Logo 水印

| 参数 | 说明 |
|------|------|
| Logo 来源 | 本地图片文件 |
| 缩放 | 按底图宽度比例（1%–50%），以 Logo 实际可见内容为准（自动裁掉透明留白） |
| 不透明度 | 0–100 |
| 旋转 | -180° ~ 180° |
| 平铺 | 支持整图平铺与交错排列 |
| 位置 | 九宫格 或 自定义坐标 |

### 4. 实时预览

所见即所得，所有参数调整即时反映在预览区，无需反复试错。

拖入 PDF 时预览区底部会出现**翻页栏**（上一页 / 下一页 / 跳转），可逐页检查水印效果；每页按自身页面尺寸渲染，混合尺寸或带 `/Rotate` 的页面都能正确显示。

### 5. 批量处理 + 拖拽导入

- 支持文件拖拽导入与文件对话框两种方式
- 自动去重、格式过滤
- 文件名模板支持变量：`{original}`、`{date}`、`{time}`、`{seq}`

### 6. 预设系统

水印配置可保存为 JSON 预设，支持版本迁移与 Logo 路径失效容错，常用参数一键复用。

### 7. PDF 文件水印

除图片外，同样支持给 **PDF 逐页加水印**。

| 参数 | 说明 |
|------|------|
| 处理方式 | 覆盖层合并（**非**把页面转成图片），原文内容保留、文字仍可选可搜 |
| 参数复用 | 文字 / Logo 的全部参数与图片完全一致，预览所见即所得 |
| 页数显示 | 预览区显示 PDF 总页数与首页尺寸 |
| 尺寸适配 | 按各页原始页面尺寸生成覆盖层，混合页面尺寸的 PDF 同样正确 |
| 输出格式 | 强制输出为 `.pdf`，PNG / JPEG 格式选项对其无效 |

### 8. 输入值安全校验

所有数值输入（坐标百分比、间距、透明度、缩放、旋转角度）在进入引擎前统一校验：越界值自动夹取到合法区间、非法值回退默认值，并在状态栏提示修正结果，不会静默产生错误水印。

---

## 支持格式

| 类型 | 输入扩展名 | 输出 |
|------|-----------|------|
| 图片 | `.jpg` `.jpeg` `.png` `.bmp` `.webp` `.tiff` `.tif` | 同输入格式，并支持 `auto` / `png` / `jpeg` 格式转换 |
| PDF | `.pdf` | 固定输出 `.pdf`（逐页叠加水印） |

PDF 与图片可混合拖入同一批次，各自按对应引擎处理。

---

## 快速开始

### 方式一：使用打包好的 exe（推荐普通用户）

1. 下载发行版中的 `WatermarkTool` 压缩包并解压。
2. 运行 `批量图片水印工具.exe`。
3. 拖入图片或点击「添加文件」即可开始。

无需安装 Python 或任何依赖。

### 方式二：从源码运行

见下节。

---

## 从源码运行

### 环境要求

- Windows 10 / 11（已测试）
- Python 3.10 及以上

### 安装依赖

```bash
cd watermark_tool
pip install -r requirements.txt
```

依赖清单（`requirements.txt`）：

```
pillow>=12.3.0
customtkinter>=5.2.2
tkinterdnd2>=0.6.2
reportlab>=4.0.0      # PDF 覆盖层生成
pypdf>=4.0.0          # PDF 逐页合并
pypdfium2>=4.30.0     # PDF 预览渲染
pyinstaller>=6.21.0
```

> `pyinstaller` 仅打包时需要，运行时可省略。
> `reportlab` / `pypdf` / `pypdfium2` 仅为 PDF 功能所需；若只用图片水印，可不安装。

已验证版本组合：Pillow 12.3.0、customtkinter 6.0.0、tkinterdnd2 0.6.2、reportlab 5.0.1、pypdf 6.16.2、pypdfium2 5.13.0、PyInstaller 6.21.0（Python 3.14）。

### 启动

```bash
python main.py
```

或双击仓库中的 `启动水印工具.bat`。

---

## 打包为可执行文件

项目内置 PyInstaller 打包脚本，一键生成免环境运行的 exe。

### 一键打包

```bash
build_exe.bat
```

脚本会自动完成依赖检查、清理旧构建、打包、验证输出。

### 手动打包

```bash
python -m PyInstaller build.spec --noconfirm
```

### 输出

- 输出目录：`dist/WatermarkTool/`
- 主程序：`dist/WatermarkTool/批量图片水印工具.exe`
- 将整个 `WatermarkTool` 文件夹分发给用户即可

`build.spec` 已处理 CustomTkinter、tkinterdnd2 的数据文件收集、`presets` 目录打包，以及 PDF 所需的 `pypdfium2` 原生 DLL（`collect_dynamic_libs`）与 `reportlab` / `pypdf` 隐式导入，无需额外配置。

---

## 命令行用法

水印引擎（`watermark_engine.py`）与 GUI 解耦，可独立用于脚本化处理：

```bash
python -m src.watermark_engine <输入图片> <输出图片>
```

示例：

```bash
python -m src.watermark_engine photo.jpg output.jpg
```

默认会叠加「内部资料 / Confidential」平铺文字水印，便于快速验证引擎可用性。如需自定义参数，可在脚本中构造 `WatermarkConfig` 并调用 `apply_watermark()`。

### 核心引擎 API

```python
from src.config import WatermarkConfig
from src.watermark_engine import safe_open_image, apply_watermark, save_with_format

cfg = WatermarkConfig(
    watermark_type="both",       # "text" | "image" | "both"
    text="© 品牌方",
    color_rgba=(255, 255, 255, 128),
    opacity=50,
    font_ratio=0.04,
    angle=30,
    tile=True,
    spacing_x=150,
    spacing_y=150,
    stagger=True,
    logo_path="logo.png",
    scale_ratio=0.10,
    logo_opacity=80,
)

img = safe_open_image("input.jpg")
result = apply_watermark(img, cfg)
save_with_format(result, "output.jpg", jpeg_quality=90)
```

### PDF 引擎 API

PDF 使用独立入口 `apply_pdf_watermark()`，参数复用同一个 `WatermarkConfig`：

```python
from src.config import WatermarkConfig
from src.pdf_engine import apply_pdf_watermark, render_pdf_page, get_pdf_page_count

cfg = WatermarkConfig(
    watermark_type="text",
    text="内部资料",
    angle=30,
    tile=True,
    spacing_x=200,
    spacing_y=200,
)

pages = apply_pdf_watermark("input.pdf", cfg, "output.pdf")   # 返回处理页数
print(f"已处理 {pages} 页")

first_page = render_pdf_page("input.pdf", page_num=0, scale=2.0)   # 返回 PIL.Image
print(get_pdf_page_count("input.pdf"))
```

`apply_pdf_watermark()` 对加密 PDF 会抛 `ValueError`；若 PDF 可用空密码解锁，则自动解密后处理。

---

## 配置参数详解

`WatermarkConfig` 是 GUI 与引擎之间的单一数据契约，完整字段如下。

### 水印类型

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `watermark_type` | `text` | `text` / `image` / `both` |

### 文字水印

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `text` | `""` | 水印文字，支持 `\n` 多行 |
| `font_path` | `""` | 空=自动探测系统字体 |
| `font_ratio` | `0.04` | 字号相对图片短边百分比 |
| `color_rgba` | `(255,255,255,128)` | RGBA 颜色 |
| `opacity` | `50` | 不透明度 0–100（同时决定 `color_rgba` 的 alpha） |
| `position` | `bottom-right` | 九宫格位置 |
| `position_mode` | `grid` | `grid` / `custom` |
| `custom_x` / `custom_y` | `50` / `50` | 自定义位置百分比 |
| `margin` | `20` | 边距 px |
| `angle` | `0.0` | 旋转角度 |
| `tile` | `False` | 是否平铺 |
| `spacing_x` / `spacing_y` | `100` / `100` | 平铺间距，按可见边缘计算 |
| `stagger` | `False` | 平铺行间错位 |

### 图片水印

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `logo_path` | `""` | Logo 文件路径 |
| `scale_ratio` | `0.10` | Logo 相对底图宽度比例（以可见内容为准，自动裁掉透明留白） |
| `logo_opacity` | `50` | Logo 不透明度 0–100 |
| `logo_tile` | `False` | Logo 平铺 |
| `logo_spacing_x` / `logo_spacing_y` | `100` / `100` | 平铺间距，按可见边缘计算 |
| `logo_stagger` | `False` | Logo 平铺行间错位 |
| `logo_angle` | `0.0` | Logo 旋转 |
| `logo_position` | `bottom-right` | 九宫格位置 |
| `logo_position_mode` | `grid` | `grid` / `custom` |
| `logo_custom_x` / `logo_custom_y` | `50` / `50` | 自定义位置百分比 |
| `logo_margin` | `20` | 边距 |

### 输出

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `output_dir` | `""` | 空=源目录下 `watermarked_output/` |
| `output_format` | `auto` | `auto` / `png` / `jpeg` |
| `jpeg_quality` | `90` | JPEG 质量 60–100 |
| `filename_template` | `{original}_watermarked` | 文件名模板 |
| `overwrite_original` | `False` | 是否覆盖原图 |

### 九宫格位置取值

格式为 `{vertical}-{horizontal}`，或直接使用 `center`。

- vertical：`top` / `middle` / `bottom`
- horizontal：`left` / `center` / `right`

示例：`top-left`、`middle-center`、`bottom-right`。

---

## 预设系统

预设存储为 JSON 文件，位于项目根目录的 `presets/` 文件夹下，每个预设一个文件。

- **保存**：当前所有参数序列化为 `{预设名}.json`
- **加载**：反序列化时自动过滤未知字段、恢复 tuple 类型、按 `version` 字段做版本迁移
- **路径失效容错**：若预设绑定的 Logo 文件已不存在，自动清空 `logo_path` 并给出警告，不阻断流程
- **文件名安全化**：预设名中的非法字符自动替换为下划线

预设可直接通过 `PresetManager` 管理：

```python
from src.preset_manager import PresetManager
from src.config import WatermarkConfig

# 保存
PresetManager.save_preset("品牌水印", cfg)

# 加载
cfg, warnings = PresetManager.load_preset("品牌水印")

# 列出全部
names = PresetManager.list_presets()

# 删除
PresetManager.delete_preset("品牌水印")
```

---

## 技术设计

### 健壮性

1. **EXIF 方向自动校正**：调用 `ImageOps.exif_transpose()` 实际旋转像素，并清除 EXIF 中的 `Orientation` 标记为 `1`，防止支持 EXIF 的查看器对已旋转图片二次旋转。
2. **色彩空间归一化**：CMYK / P / La 等非标准模式统一转为 RGB，保证后续合成与保存的稳定性。
3. **大图支持**：模块级 `Image.MAX_IMAGE_PIXELS = None`，解除 Pillow 的 `DecompressionBombError` 限制，支持超高分辨率图片。
4. **字体多级回退**：用户指定路径 → 微软雅黑 → 黑体 → Pillow 内置字体，任一可用即停止，避免缺字崩溃。
5. **空字符串容错**：文字为空或纯空白时直接返回原图，不报错。
6. **路径清洗**：自动清除 Windows 拖拽传入的 `{}` 包裹与引号。
7. **文件句柄释放**：所有图片读取路径使用 `with Image.open(...)` 并在加载后返回脱离文件的对象副本。若在打开与加载之间抛异常，句柄会被一直占用，导致源文件无法重命名 / 删除 —— 这在 Windows 上是常见故障。
8. **输入值统一校验**：`src/validators.py` 对所有数值输入做解析与区间夹取（坐标百分比 0–100、间距 ≥ 1、透明度 0–100、缩放 1%–100%、旋转 -180°–180°），非法值回退默认值并在状态栏提示。同时兜底平铺步长，避免间距为 0 或负数时 `range()` 抛异常或静默返回空序列。

### PDF 水印实现原理

采用**覆盖层合并**方案，而非把 PDF 页面渲染成图片后再重新组卷 —— 后者会丢失文本可选中性与矢量清晰度。

```
Pillow 引擎在透明底图渲染水印 ──▶ 透明 PNG 覆盖层
        │
        ▼
reportlab 包成与页面同尺寸的覆盖 PDF
        │
        ▼
pypdf merge_page 逐页叠加到原 PDF（原内容保留）
```

- 覆盖层渲染倍率 `OVERLAY_SCALE = 2.0`（约 144 DPI），保证文字在 200% 缩放下依然清晰
- 预览（`pypdfium2` 渲染当前页 + Pillow 叠加）与导出（透明底图 + 同一 Pillow 引擎）使用相同的相对几何计算，所见即所得
- 覆盖层按「页面尺寸 + 旋转角」缓存，多页同规格时只生成一次
- `/Rotate` 页面按**显示方向**渲染覆盖层，再反向旋转同角度写入页面坐标系，与目标页的 `/Rotate` 合成后正好摆正。旧实现按未旋转 `mediabox` 创建覆盖层，90° 下横向 ×0.707、纵向 ×1.415，水印被拉伸约 2 倍

**已知限制**

| 限制 | 说明 |
|------|------|
| 覆盖层为整页位图 | A4 单页覆盖层约 1190×1684 RGBA，混合尺寸 PDF 会生成多份缓存，内存与输出体积会上升 |
| 加密 PDF | 仅支持空密码（自动解密）；有密码的 PDF 会明确报错 |
| 页级参数 | 目前对全部页面应用同一套参数，不支持按页指定 |

### 智能保存

- **JPEG**：`RGBA→RGB`（白底合成）+ 安全 EXIF
- **PNG**：保持 `RGBA`
- **TIFF / WebP**：支持 EXIF 透传
- **其他**：直接保存
- 自动创建输出目录

### 日志系统

- 日志路径：`%APPDATA%/WatermarkTool/app.log`
- 自动轮转：单文件超过 1MB 时归档，仅保留最近 5 个
- 线程安全，同时输出到控制台与文件

### 架构分层

```
GUI 层 (src/ui/*)  ──仅持有──▶  WatermarkConfig (src/config.py)
        │                              │
        └──── 调用 ────  watermark_engine.py ◀── 唯一图像处理入口
```

水印引擎不依赖任何 GUI 库，`WatermarkConfig` 作为 GUI 与引擎之间的单一数据契约，保证引擎可独立测试与命令行调用。

---

## 项目结构

```
watermark_tool/
├── main.py                    # 入口
├── requirements.txt           # 依赖
├── build.spec                 # PyInstaller 打包配置
├── build_exe.bat              # 一键打包脚本
├── 启动水印工具.bat            # 启动脚本
├── presets/                  # 预设存储目录（JSON）
├── assets/                    # 资源目录
├── hooks/                     # PyInstaller hooks
│   └── hook-tkinterdnd2.py
├── tests/                     # 单元测试
│   ├── test_core.py           # 配置 / 路径 / 预设 / 引擎
│   ├── test_pdf.py            # PDF 水印引擎
│   └── test_validators.py     # 输入校验 / 句柄释放 / 平铺兜底
└── src/
    ├── config.py              # WatermarkConfig 数据类 + JSON 序列化
    ├── watermark_engine.py    # 图片水印引擎（无 GUI 依赖）
    ├── pdf_engine.py          # PDF 水印引擎（覆盖层合并）
    ├── validators.py          # 输入值校验与归一化（无 GUI 依赖）
    ├── file_manager.py        # 文件收集 / 去重 / 输出路径生成
    ├── preset_manager.py      # 预设管理器
    ├── logger.py              # 日志系统（轮转）
    └── ui/
        ├── app_window.py          # 主窗口
        ├── drop_zone.py           # 拖拽区
        ├── file_list.py           # 文件列表
        ├── text_watermark_panel.py# 文字水印面板
        ├── image_watermark_panel.py# 图片水印面板
        └── preview_canvas.py      # 预览画布
```

### 运行测试

```bash
python -m pytest tests/ -q
```

测试覆盖配置序列化与版本迁移、输出路径与重名保护、预设增删改查、图片与 PDF 水印引擎、输入值校验、文件句柄释放、平铺步长兜底。

---

## 常见问题

**Q：打开图片后方向是倒的 / 二次旋转？**
A：引擎已通过 `exif_transpose` 实际旋转像素并清除 `Orientation` 标记，正常情况下不会出现二次旋转。若仍出现，请确认是否被其他工具重新写入了带旧 `Orientation` 的 EXIF。

**Q：中文显示为方框？**
A：系统缺少中文字体，字体回退链已用尽。请手动指定一个支持中文的 `.ttf` / `.ttc` 字体路径。

**Q：超大图打开报错？**
A：已解除像素上限，若仍失败通常是内存不足，建议分批处理。

**Q：打包后拖拽无反应？**
A：`tkinterdnd2` 的 Tcl 库需正确打包，`build.spec` 已通过 `collect_data_files` 处理。如自行修改 spec，请勿遗漏该步骤。

**Q：预设加载后 Logo 没了？**
A：预设绑定的 Logo 路径失效时会被自动清空并提示，重新指定 Logo 路径即可。预设设计为可跨机器迁移，路径不作为强依赖。

**Q：PDF 加水印后，原文还能复制文字吗？**
A：可以。水印以覆盖层形式叠加，原页面内容与文字层完整保留，可正常选中、复制与搜索。

**Q：加了 PDF 支持后输出文件变大了？**
A：覆盖层是整页位图（`OVERLAY_SCALE = 2.0`），体积会有明显增加，且页面尺寸种类越多、每个尺寸的覆盖层都会各缓存一份。若体积敏感，可调小 `src/pdf_engine.py` 中的 `OVERLAY_SCALE`。

**Q：为什么 PDF 输出格式选不了 PNG / JPEG？**
A：PDF 无法转换成图片格式而不损失内容。工具对 PDF 源文件强制输出 `.pdf`，格式选项仅对图片生效。

**Q：PDF 有密码打不开怎么办？**
A：仅支持空密码（自动解密）的 PDF。设有打开密码的 PDF 会明确报错，需要先解密再处理。

**Q：为什么图片处理完删不掉、或者 Logo 换不了？**
A：这是 Windows 下文件句柄未释放的典型症状。当前所有读取路径已用 `with Image.open(...)` 并在加载后返回脱离文件的对象副本，处理完成后源文件可正常重命名 / 删除。若使用旧版本，请升级到最新版。

**Q：间距输入框填了非数字 / 清空会怎样？**
A：不会崩溃。输入值统一经 `src/validators.py` 校验，非法值回退到默认值、越界值夹取到合法区间，并在状态栏显示修正说明。

---

## 许可证

本项目采用 [MIT License](LICENSE)。

第三方依赖许可证：Pillow（MIT-CMU）、customtkinter（MIT）、tkinterdnd2（MIT）、reportlab（BSD）、pypdf（BSD）、pypdfium2（Apache-2.0 / BSD），均与本项目的 MIT 协议兼容。

如需更换许可协议，请修改本声明并添加对应 LICENSE 文件。
