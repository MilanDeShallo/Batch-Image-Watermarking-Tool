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

实际生效的类型由**内容**推导，而不是由「当前打开的是哪个 Tab」决定：

| 条件 | 实际类型 |
|------|----------|
| 文字非空 **且** 有 Logo | `both` |
| 只有 Logo | `image` |
| 其余 | `text` |

两个面板的参数会**合并成一份完整配置**提交，切换 Tab 只影响你正在编辑哪一组参数，不会把另一组静默关掉。所以「在文字 Tab 里调字号」不会丢掉已经选好的 Logo。

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

### 4. 实时预览（真·所见即所得）

所有参数调整即时反映在预览区，无需反复试错。

**预览不是「大致像」，而是与导出严格同构**：同一套参数、同一套几何算法，只是在缩小的底图上渲染。这是靠把「绝对像素量」一起按比例换算做到的 ——

| 参数类别 | 字段 | 缩放行为 |
|----------|------|----------|
| 相对量 | 字号占比 `font_ratio`、Logo 占比 `scale_ratio` | 天然等比，任何分辨率都一致 |
| **绝对像素量** | 间距 `spacing_x/y`、边距 `margin`、`logo_margin`、`logo_spacing_x/y` | 按 `scale = 预览分辨率 / 导出分辨率` **同步换算** |

引擎的 `apply_watermark(img, cfg, scale=1.0)` 多了一个 `scale` 参数：`scale=1.0` 就是导出（行为与历史版本逐字节一致），预览传小于 1 的值。

字号与 Logo 的**下限也一起缩放**（`font_size = max(12*scale, 短边*font_ratio)`），否则小图上预览字号会被 12px 下限顶住，比导出的相对字号大出好几倍。

导出的「参考分辨率」只有一处来源：图片是原图像素，PDF 是页面可视尺寸 × `OVERLAY_SCALE`，由 `pdf_engine.overlay_reference_size()` 统一给出，避免覆盖层生成与预览两处算法各写一遍而逐渐漂移。

拖入 PDF 时预览区底部会出现**翻页栏**（上一页 / 下一页 / 跳转），可逐页检查水印效果；每页按自身页面尺寸渲染，混合尺寸或带 `/Rotate` 的页面都能正确显示。

**流畅度**：三类重复劳动各自带缓存 —— 解码后的底图按 `(路径, 页, 目标尺寸)` 缓存、渲染结果只在参数变化时重做、显示图按 `(画布尺寸, 缩放, 渲染版本)` 缓存。缩放与拖动只重采样已渲染好的图，不再重跑引擎；渲染走后台线程且做了**单飞合并**（渲染进行中只登记待办标记，不堆线程）。

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
| 页数显示 | 预览区显示总页数与当前页尺寸，可上一页 / 下一页 / 跳到第 N 页逐页检查 |
| 尺寸适配 | 按各页原始页面尺寸生成覆盖层，混合页面尺寸的 PDF 同样正确 |
| 旋转页 | `/Rotate 90/180/270` 的页面按显示方向渲染并反向写回，水印不被拉伸 |
| 输出格式 | 强制输出为 `.pdf` |

### 8. 输入值安全校验

所有数值输入（坐标百分比、间距、透明度、缩放、旋转角度）在进入引擎前统一校验：越界值自动夹取到合法区间、非法值回退默认值，并在状态栏提示修正结果，不会静默产生错误水印。

### 9. 参数作用范围：不同文件、不同页可以用不同参数

右侧面板顶部选择当前这套参数记到哪一层：

| 作用范围 | 含义 |
|----------|------|
| 全部文件 | 作为**全局默认**，没单独设置过的文件/页都用它 |
| 仅本文件 | 只记给列表里选中的这个文件（列表行亮起「专属」） |
| 仅本页 | 只记给 PDF 当前预览页（翻页栏显示「专属页: 2, 5」） |

导出时按 **页 > 文件 > 全局** 逐层查找：

- **只记改动过的字段**：单独设置某文件后，没改过的字段仍跟随全局默认变化，不会被旧值冻住
- **自动定位**：选中带专属参数的文件/页时，作用范围自动跳到那一层，面板显示的就是该文件真正会用的参数
- **手动切回「全部文件」**编辑全局默认时，提示行会写明「本文件已有专属参数，当前修改不影响它」，避免误以为改了个寂寞
- 「清除专属」删除当前层的专属参数，该文件/页回落到上一层

### 10. 输出为 PDF

输出格式下拉新增 **PDF**：

| 选项 | 行为 |
|------|------|
| PDF（不勾选合并） | 每个输入产出一个 PDF：图片 → 单页 PDF（页面尺寸 = 像素 × 72/96，与图片同比例、不留白边）；PDF → 逐页加水印后输出 |
| PDF + 勾选「合并为单个 PDF」 | 整批按列表顺序合成**一个多页 PDF**（一图一页），默认文件名形如 `合并水印_20261012_174500.pdf`；中途取消不会留下半成品文件 |

每个输入都用**它自己那一层**的参数（文件层 / 页层），所以「A 打红字、B 打蓝字、C 的第 1 页不打」在合并输出里同样成立。

图片放进 PDF 时的编码按**是否存在真实透明像素**选择：有透明像素走无损 PNG（保住 alpha），完全不透明走 JPEG 并应用底部栏的质量设置。
注意判断依据是"真的有透明像素"而不是"有 alpha 通道" —— 加过水印的图片一律是 RGBA，只看通道会让质量设置彻底失效、体积还大得多。

---

## 支持格式

| 类型 | 输入扩展名 | 输出 |
|------|-----------|------|
| 图片 | `.jpg` `.jpeg` `.png` `.bmp` `.webp` `.tiff` `.tif` | 同输入格式，并支持 `auto` / `png` / `jpeg` / `pdf` 格式转换 |
| PDF | `.pdf` | 固定输出 `.pdf`（逐页叠加水印） |

PDF 与图片可混合拖入同一批次，各自按对应引擎处理；选择 `PDF` 输出时两者都可产出 PDF，也可整批合并成一个多页 PDF。

---

## 快速开始

### 方式一：使用打包好的 exe（推荐普通用户）

1. 从 [Releases](https://github.com/MilanDeShallo/Batch-Image-Watermarking-Tool/releases/latest) 下载 `WatermarkTool-vX.Y.zip` 并解压。
2. 运行 `批量图片水印工具.exe`（或在项目根目录双击 `打开水印工具.bat`）。
3. 拖入图片或 PDF，点击「添加文件」即可开始。

无需安装 Python 或任何依赖。解压后请保持 `WatermarkTool` 文件夹完整 —— exe 与同目录的 `_internal` 是一体的，单独把 exe 拷出来无法运行。

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

**逐页不同参数**：传 `page_configs={页码: WatermarkConfig}` 只对指定页使用另一套参数（页码 0 起，未列出的页仍用 `cfg`）：

```python
base = WatermarkConfig(text="内部资料", opacity=40)
page_cfgs = {1: WatermarkConfig(text="样片", color_rgba=(255, 0, 0, 255), opacity=100)}

apply_pdf_watermark("input.pdf", base, "output.pdf", page_configs=page_cfgs)
```

**图片 → PDF 与多合一**：

```python
from src.pdf_engine import build_merged_pdf, image_file_to_pdf_bytes

data = image_file_to_pdf_bytes("photo.jpg", cfg, jpeg_quality=90)   # 单张图片 → 单页 PDF 字节

# 多输入合并为一个多页 PDF（图片一图一页，PDF 逐页并入，顺序即页序）
pages = build_merged_pdf(
    [
        {"path": "photo.jpg", "config": cfg},
        {"path": "report.pdf", "config": cfg, "page_configs": {0: cfg}},
    ],
    "merged.pdf",
    jpeg_quality=90,
)
```

`build_merged_pdf()` 支持 `progress(index, total, filename)` 与 `should_cancel()` 回调；
取消时抛 `MergeCancelled`，且**不会写出任何文件**（避免留下半成品）。

### 参数作用域 API

`src/overrides.py` 提供三层参数存储（无 GUI 依赖，GUI 与脚本共用同一套语义）：

```python
from src.overrides import ScopeStore

store = ScopeStore()
store.update_base({"text": "内部资料", "opacity": 50})   # 全局默认
store.update_file("B.jpg", {"text": "样片"})             # B 的专属参数
store.update_page("C.pdf", 2, {"opacity": 100})          # C 第 3 页的专属参数

store.config_for("C.pdf", 2).text      # '内部资料' —— 页层没设 text，逐层继承
store.config_for("C.pdf", 2).opacity   # 100        —— 被页层覆盖
store.marker_text("C.pdf")             # '专属 第3页'
```

面板提交的是**完整参数**而不是小补丁，这种场景用 `set_file` / `set_page`：
它们会自动与父层做差，只把真正不同的字段记下来。

```python
store.set_base({"text": "内部资料", "opacity": 50, "margin": 20})
# 面板提交了一整份参数，其中只有 text 与全局不同
store.set_file("B.jpg", {"text": "样片", "opacity": 50, "margin": 20})

store.to_dict()["files"]["B.jpg"]   # {'text': '样片'} —— 只存了差异

store.update_base({"opacity": 30})  # 之后再修改全局默认
store.config_for("B.jpg").opacity   # 30 —— 没动过的字段跟着变，没被旧快照冻住
```

每层只保存**改动过的字段**，未改动的字段继续向上继承；输出类字段（输出目录、格式、质量、文件名模板）不进入作用域。

`update_file` / `update_page` 保留为「按字段合并」语义，适合脚本里手写小补丁的场景。

### 批处理 API

`src/batch_planner.py` 把「文件列表 + 作用域参数 + 输出设置」翻译成可执行的任务清单，
不含任何 GUI 代码，因此导出方案可以被测试直接覆盖：

```python
from src.batch_planner import plan_batch, execute_task

plan = plan_batch(files, store, out_format="PDF", merge_pdf=True)
plan.out_format        # 'pdf'
plan.merge             # True
plan.tasks[1].config   # 该文件真正生效的 WatermarkConfig
plan.tasks[2].page_configs   # {页码: WatermarkConfig}，只含被单独设置过的页

for task, out_path in zip(plan.tasks, output_paths):
    execute_task(task, out_path, jpeg_quality=90)

from src.pdf_engine import build_merged_pdf
build_merged_pdf(plan.merge_items(), "merged.pdf", jpeg_quality=90)
```

`plan_batch` 的两条内置规则：PDF 源文件无论输出格式选什么都强制输出 PDF；`merge` 只在输出格式为 PDF 且显式勾选时才为真。

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
9. **界面不会被自己的弹窗卡死**：完成提示框不再调用 `grab_set()`。

   根因值得记一笔：CustomTkinter 的 `CTkToplevel` 为了刷新标题栏颜色，构造时会先 `withdraw()` 自己、再 `after(5)` 调 `deiconify()`。在这个窗口上再叠加模态抓取，抓取就落在了一个「当时不可见」的窗口上。若主窗口此刻正好被最小化，子对话框会被连带置为 `withdrawn`，而输入抓取仍然攥在它手里 —— 表现就是**导出完把软件最小化后，再也点不醒**（实测 `grab_current` 始终指向那个 withdrawn 的对话框）。

   修法有两层：
   - 完成对话框改为「单例 + `lift()` + `bind("<Destroy>")` 清理」，不动系统的输入抓取
   - 兜底安全网 `_release_stale_grab()`：绑定 `<Map>` / `<FocusIn>`，一旦发现 `grab_current()` 存在但该窗口不可见，就主动释放

   第一层治本，第二层保证即使将来有人重新引入模态，界面也不会被锁死。

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

页级参数已支持，见上文「参数作用范围」一节：`apply_pdf_watermark(..., page_configs={页码: WatermarkConfig})` 只对列出的页换参数，未列出的页仍用基准配置，并且只影响水印、不动原文。

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
├── tests/                     # 测试
│   ├── test_core.py           # 配置 / 路径 / 预设 / 引擎
│   ├── test_pdf.py            # PDF 水印引擎
│   ├── test_pdf_output.py     # 逐页参数 / 图片转 PDF / 多合一
│   ├── test_overrides.py      # 三层参数作用域
│   ├── test_batch_pipeline.py # 作用域 → 规划 → 实际写出 的端到端
│   ├── test_render_geometry.py # 合成正确性 / 平铺步长 / 旋转页几何
│   ├── test_preview_consistency.py # 预览与导出的几何一致性（含 PDF 参考尺寸）
│   ├── test_validators.py     # 输入校验 / 句柄释放 / 平铺兜底
│   ├── smoke_gui.py           # 界面冒烟（独立脚本，不经 pytest 运行）
│   └── smoke_preview.py       # 预览管线冒烟（真线程 + 真 mainloop）
└── src/
    ├── config.py              # WatermarkConfig 数据类 + JSON 序列化
    ├── watermark_engine.py    # 图片水印引擎（无 GUI 依赖）
    ├── pdf_engine.py          # PDF 引擎（覆盖层合并 / 逐页参数 / 图片转 PDF / 多合一）
    ├── overrides.py           # 三层参数作用域（无 GUI 依赖）
    ├── batch_planner.py       # 批处理规划：任务清单 / 输出格式 / 是否合并（无 GUI 依赖）
    ├── validators.py          # 输入值校验与归一化（无 GUI 依赖）
    ├── file_manager.py        # 文件收集 / 去重 / 输出路径生成
    ├── preset_manager.py      # 预设管理器
    ├── logger.py              # 日志系统（轮转）
    └── ui/
        ├── app_window.py          # 主窗口
        ├── drop_zone.py           # 拖拽区
        ├── file_list.py           # 文件列表（含专属参数标记）
        ├── scope_selector.py      # 作用范围选择器（全部/本文件/本页）
        ├── text_watermark_panel.py# 文字水印面板
        ├── image_watermark_panel.py# 图片水印面板
        └── preview_canvas.py      # 预览画布（含 PDF 逐页检查）
```

### 运行测试

```bash
python -m pytest tests/ -q          # 单元与集成测试（160 项）
python tests/smoke_gui.py           # 界面冒烟（74 项检查，退出码 0 表示全过）
python tests/smoke_preview.py       # 预览管线冒烟（23 项检查）
```

测试覆盖配置序列化与版本迁移、输出路径与重名保护、预设增删改查、图片与 PDF 水印引擎、逐页参数与多合一输出、三层参数作用域、输入值校验、文件句柄释放、合成与平铺几何，以及**预览与导出的几何一致性**。

界面冒烟会真的把主窗口装配起来跑一遍完整流程（加文件 → 设三档作用范围 → 专属标记 → 翻页取值 → 导出 PDF / 合并），
专门验证只存在于 `AppWindow` 里的那层胶水 —— 单元测试各自都过，不代表串起来是对的：

- 窗口创建后立刻隐藏、所有弹窗被替换成记录器，不打扰正在用电脑的人
- `APPDATA` 指向系统临时目录，不读写用户真实的会话与窗口位置文件
- 中间产物全部落在系统临时目录，不在仓库里留垃圾
- 额外断言：完成对话框不持有输入抓取、僵尸抓取安全网有效、两个 Tab 的参数能合并成完整配置

预览管线冒烟用**真线程 + 真 mainloop** 跑，覆盖后台渲染完成、单飞标记复位、线程不堆积、工作分辨率的上下限、参数微调触发重渲染、连续请求被合并、PDF 翻页、清空后丢弃底图缓存，并在真实链路上量出「预览 vs 导出」的边距偏差（当前 0.0019，换算约 12px；不换算 `scale` 的旧实现在同一场景偏差 0.0208，约 125px）。

### 怎么保证改预览没改坏导出

预览与导出走同一条代码路径，唯一的区别是 `scale` 参数。因此改预览时用**导出基线指纹**兜底：把若干种配置（平铺 / 单次定位 / 自定义坐标 / Logo / 两者叠加 / 触发字号下限）在固定底图上的输出取 SHA-256，改动前后必须逐字节一致。

`scale=1.0` 是导出唯一走的分支，这条约束保证「把预览改准」永远不会顺手把导出改歪。

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

**Q：预览里水印看着比导出的稀、离边更远？**
A：这是旧版本的缺陷，已修复。间距、边距这类**绝对像素量**在缩小后的预览底图上不换算，图片越大偏得越多（6000px 偏约 1.5 倍、8000px 偏约 2 倍）。现在引擎按 `scale` 同步换算全部绝对像素量，字号/Logo 的下限也一起缩放。若你仍看到明显差异，请确认用的是最新版本。

**Q：导出完成后把软件最小化，就再也点不醒了？**
A：这是旧版本完成提示框的模态抓取落在不可见窗口上导致的，已修复（详见上文「健壮性」第 9 条）。现在的完成对话框不接管系统输入抓取，另有一层安全网会主动释放任何「属于不可见窗口」的抓取。

**Q：为什么在文字 Tab 调参数，之前选好的 Logo 不见了？**
A：旧版本会在切换 Tab 时把另一种水印的类型覆盖掉，已修复。现在生效类型由内容推导，两个面板的参数会合并成一份完整配置，切 Tab 只影响你正在编辑哪一组。

---

## 许可证

本项目采用 [MIT License](LICENSE)。

第三方依赖许可证：Pillow（MIT-CMU）、customtkinter（MIT）、tkinterdnd2（MIT）、reportlab（BSD）、pypdf（BSD）、pypdfium2（Apache-2.0 / BSD），均与本项目的 MIT 协议兼容。

如需更换许可协议，请修改本声明并添加对应 LICENSE 文件。
