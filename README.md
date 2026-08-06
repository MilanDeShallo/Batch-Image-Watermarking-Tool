# 批量图片水印工具

> 基于 Python + Pillow 的桌面端批量水印处理工具，面向图片版权保护与品牌标识场景。开箱即用，支持拖拽导入、批量处理、实时预览，可一键打包为 Windows 可执行文件。

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
| 颜色 | RGBA 四通道，含独立透明度 |
| 透明度 | 0–100 |
| 位置 | 九宫格预设 或 自定义坐标百分比 |
| 旋转 | -180° ~ 180° |
| 平铺 | 整图平铺，支持行间错位 |
| 边距 | 像素级边距 |

### 3. 图片 / Logo 水印

| 参数 | 说明 |
|------|------|
| Logo 来源 | 本地图片文件 |
| 缩放 | 按底图宽度比例（1%–50%） |
| 透明度 | 0–100 |
| 旋转 | -180° ~ 180° |
| 平铺 | 支持整图平铺 |
| 位置 | 九宫格 或 自定义坐标 |

### 4. 实时预览

所见即所得，所有参数调整即时反映在预览区，无需反复试错。

### 5. 批量处理 + 拖拽导入

- 支持文件拖拽导入与文件对话框两种方式
- 自动去重、格式过滤
- 文件名模板支持变量：`{original}`、`{date}`、`{time}`、`{seq}`

### 6. 预设系统

水印配置可保存为 JSON 预设，支持版本迁移与 Logo 路径失效容错，常用参数一键复用。

---

## 支持格式

| 类型 | 扩展名 |
|------|--------|
| 输入 | `.jpg` `.jpeg` `.png` `.bmp` `.webp` `.tiff` `.tif` |
| 输出 | 同输入，并支持 `auto` / `png` / `jpeg` 格式转换 |

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
pyinstaller>=6.21.0
```

> `pyinstaller` 仅打包时需要，运行时可省略。

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

`build.spec` 已处理 CustomTkinter、tkinterdnd2 的数据文件收集与 `presets` 目录打包，无需额外配置。

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
| `opacity` | `50` | 透明度 0–100 |
| `position` | `bottom-right` | 九宫格位置 |
| `position_mode` | `grid` | `grid` / `custom` |
| `custom_x` / `custom_y` | `50` / `50` | 自定义位置百分比 |
| `margin` | `20` | 边距 px |
| `angle` | `0.0` | 旋转角度 |
| `tile` | `False` | 是否平铺 |
| `spacing_x` / `spacing_y` | `100` / `100` | 平铺间距 |
| `stagger` | `False` | 平铺行间错位 |

### 图片水印

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `logo_path` | `""` | Logo 文件路径 |
| `scale_ratio` | `0.10` | Logo 相对底图宽度比例 |
| `logo_opacity` | `50` | Logo 透明度 |
| `logo_tile` | `False` | Logo 平铺 |
| `logo_spacing_x` / `logo_spacing_y` | `100` / `100` | 平铺间距 |
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
└── src/
    ├── config.py              # WatermarkConfig 数据类 + JSON 序列化
    ├── watermark_engine.py    # 水印引擎（无 GUI 依赖）
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

---

## 许可证

本项目采用 [MIT License](LICENSE)。

如需更换许可协议，请修改本声明并添加对应 LICENSE 文件。
