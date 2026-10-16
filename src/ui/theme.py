"""
界面主题 — 全项目颜色与圆角的唯一来源。

为什么要有这个文件
------------------
皮肤是一整套值，不是散落在各控件里的十六进制字符串。颜色写死在调用点，
就会变成"改一次主色要翻六个文件"。这里把三层收拢：

1. 调色板常量：控件代码直接引用；
2. build_ctk_theme() / install_theme()：生成 CustomTkinter 主题字典并交给
   ctk.set_default_color_theme()，决定所有"没写显式颜色"的控件长什么样
   （按钮、输入框、滑块、标签页、下拉菜单……）；
3. 自绘部件（PanedWindow 分隔线、tk.Canvas 底色）不在 CustomTkinter 的
   覆盖范围内，由调用方从本模块直接取值。

配色取向
--------
暖奶白底色 + 鼠尾草绿（sage）主色，整体低饱和：绿只用在"主操作、选中、
焦点"上，其余控件走暖中性色，保证界面安静、水印预览是主角。

危险 / 提醒走**低饱和 + 浅底深字**，不用正红实心块：正红在这套暖色界面里
过于扎眼、会跟主色抢注意力。做法是每个语义色给一对：
`DANGER`（文字/图标色）与 `DANGER_SOFT`（按钮底色）。
对比度（卡片底 #FFFCF6 上）：正文 #3C382F ≈ 11.6:1；次要文字 #7C7566 ≈ 4.5:1；
提醒 #8F5B1B ≈ 5.2:1；危险 #9A4741 ≈ 5.1:1；危险按钮文字在其浅底上 ≈ 4.9:1。

⚠️ 只有一套浅色皮肤（2026-10-15 起）
------------------------------------
原设计是 (浅色, 深色) 二元组 + 顶栏主题切换按钮。按用户要求**已去掉深色版**：
- 全部色值都是单一具体值，不再是二元组；
- AppWindow 固定 `set_appearance_mode("Light")`，界面上没有主题切换入口。

要恢复深色版：把下面的常量改成 `(浅色, 深色)` 二元组即可 ——
CustomTkinter 原生支持二元组，其余控件代码不用动；自绘部件
（tk.Canvas / tk.PanedWindow）届时需要一个按当前模式取具体值的小函数。
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Dict

# ============================================================
# 调色板（单一浅色值）
# ============================================================

# ---- 底层表面 ----
BG: str = "#F8F4EB"            # 窗口底色（暖奶白）
SURFACE: str = "#FFFCF6"       # 卡片表面
SURFACE_ALT: str = "#EFE8DA"   # 内嵌区域（画布、文件列表、标签页条）
BORDER: str = "#E3DAC7"        # 分隔线 / 描边
INPUT_BG: str = "#FFFEFA"      # 输入框底色

# ---- 文字 ----
TEXT: str = "#3C382F"          # 主文字
TEXT_MUTED: str = "#7C7566"    # 提示、注释（卡片底上约 4.5:1）
TEXT_DISABLED: str = "#B3AB99" # 禁用态

# ---- 主色（鼠尾草绿）----
ACCENT: str = "#4F8A66"        # 按钮 / 选中 / 焦点
ACCENT_HOVER: str = "#437A5A"
ACCENT_SOFT: str = "#D9EADF"   # 选中行的柔和底色
ACCENT_TRACK: str = "#B7D2C1"  # 滑块"已选段"（比选中底色深一档）
ON_ACCENT: str = "#FFFFFF"     # 主色上的文字

# ---- 中性控件（次要按钮等）----
NEUTRAL_BTN: str = "#E9E1D0"
NEUTRAL_BTN_HOVER: str = "#DCD3BE"
TRACK: str = "#E1D8C4"         # 滑块轨道 / 进度条槽
CHECK_BORDER: str = "#C6BCA6"  # 勾选框未选中描边
SCROLLBAR: str = "#D8CFBB"
SCROLLBAR_HOVER: str = "#C4BAA2"

# ---- 语义色 ----
# 提醒（"已修改"、专属标记、专属页提示）
WARN: str = "#8F5B1B"          # 文字色
WARN_SOFT: str = "#F7E8CE"     # 浅底（拖拽目标行高亮 / 提醒按钮底色）
WARN_SOFT_HOVER: str = "#EFDCBB"

# 危险（删除 / 覆盖原图 / 取消）：浅底深字，不用正红实心
DANGER: str = "#9A4741"        # 文字色 + 勾选框实心
DANGER_HOVER: str = "#8B3F39"
DANGER_SOFT: str = "#F5E5E2"   # 危险按钮底色
DANGER_SOFT_HOVER: str = "#EBD3CF"

# ---- 圆角 ----
RADIUS_CARD = 12     # 卡片 / 容器 / 标签页
RADIUS_BUTTON = 8
RADIUS_INPUT = 8

# 供测试与遍历使用的登记表（新增色值记得同步登记）
ALL_COLORS: Dict[str, str] = {
    "BG": BG, "SURFACE": SURFACE, "SURFACE_ALT": SURFACE_ALT,
    "BORDER": BORDER, "INPUT_BG": INPUT_BG,
    "TEXT": TEXT, "TEXT_MUTED": TEXT_MUTED, "TEXT_DISABLED": TEXT_DISABLED,
    "ACCENT": ACCENT, "ACCENT_HOVER": ACCENT_HOVER, "ACCENT_SOFT": ACCENT_SOFT,
    "ACCENT_TRACK": ACCENT_TRACK, "ON_ACCENT": ON_ACCENT,
    "NEUTRAL_BTN": NEUTRAL_BTN, "NEUTRAL_BTN_HOVER": NEUTRAL_BTN_HOVER,
    "TRACK": TRACK, "CHECK_BORDER": CHECK_BORDER,
    "SCROLLBAR": SCROLLBAR, "SCROLLBAR_HOVER": SCROLLBAR_HOVER,
    "WARN": WARN, "WARN_SOFT": WARN_SOFT, "WARN_SOFT_HOVER": WARN_SOFT_HOVER,
    "DANGER": DANGER, "DANGER_HOVER": DANGER_HOVER,
    "DANGER_SOFT": DANGER_SOFT, "DANGER_SOFT_HOVER": DANGER_SOFT_HOVER,
}


def build_ctk_theme() -> dict:
    """
    把调色板翻译成 CustomTkinter 主题字典。

    键与结构对齐 CustomTkinter 6.0 内置主题（assets/themes/*.json），
    多一个少一个键都会让控件在创建时取不到默认值。
    色值一律写成字符串：CustomTkinter 只在收到 (浅, 深) 二元组时才做模式
    切换，字符串在两种模式下都用它 —— 本工具只有一套浅色皮肤。
    """
    return {
        "CTk": {
            "fg_color": BG,
        },
        "CTkToplevel": {
            "fg_color": BG,
        },
        "CTkFrame": {
            "corner_radius": RADIUS_CARD,
            "border_width": 0,
            "fg_color": SURFACE,
            "top_fg_color": SURFACE_ALT,
            "border_color": BORDER,
        },
        # 默认按钮 = 次要按钮（暖中性）。
        # 主操作用 ACCENT、危险操作用 DANGER_SOFT 在调用点显式指定 ——
        # 全局默认若用主色，满屏按钮都会是绿的，主次不分。
        "CTkButton": {
            "corner_radius": RADIUS_BUTTON,
            "border_width": 0,
            "fg_color": NEUTRAL_BTN,
            "hover_color": NEUTRAL_BTN_HOVER,
            "border_color": BORDER,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkLabel": {
            "corner_radius": 0,
            "border_width": 0,
            "fg_color": "transparent",
            "border_color": BORDER,
            "text_color": TEXT,
        },
        "CTkEntry": {
            "corner_radius": RADIUS_INPUT,
            "border_width": 1,
            "fg_color": INPUT_BG,
            "border_color": BORDER,
            "text_color": TEXT,
            "placeholder_text_color": TEXT_MUTED,
        },
        "CTkCheckBox": {
            "corner_radius": 6,
            "border_width": 2,
            "fg_color": ACCENT,
            "border_color": CHECK_BORDER,
            "hover_color": ACCENT_HOVER,
            "checkmark_color": ON_ACCENT,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkSwitch": {
            "corner_radius": 1000,
            "border_width": 3,
            "button_length": 0,
            "fg_color": TRACK,
            "progress_color": ACCENT,
            "button_color": NEUTRAL_BTN,
            "button_hover_color": NEUTRAL_BTN_HOVER,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkRadioButton": {
            "corner_radius": 1000,
            "border_width_checked": 6,
            "border_width_unchecked": 2,
            "fg_color": ACCENT,
            "border_color": CHECK_BORDER,
            "hover_color": ACCENT_HOVER,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkProgressBar": {
            "corner_radius": 1000,
            "border_width": 0,
            "fg_color": TRACK,
            "progress_color": ACCENT,
            "border_color": "gray",
        },
        # 滑块：轨道用中性色，"已选段"用柔和主色，滑块钮用主色 ——
        # 三者拉开层次，看一眼就知道当前值在哪
        "CTkSlider": {
            "corner_radius": 1000,
            "button_corner_radius": 1000,
            "border_width": 6,
            "button_length": 0,
            "fg_color": TRACK,
            "progress_color": ACCENT_TRACK,
            "button_color": ACCENT,
            "button_hover_color": ACCENT_HOVER,
        },
        "CTkOptionMenu": {
            "corner_radius": RADIUS_INPUT,
            "fg_color": NEUTRAL_BTN,
            "button_color": NEUTRAL_BTN_HOVER,
            "button_hover_color": ACCENT_SOFT,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkComboBox": {
            "corner_radius": RADIUS_INPUT,
            "border_width": 1,
            "fg_color": INPUT_BG,
            "border_color": BORDER,
            "button_color": NEUTRAL_BTN,
            "button_hover_color": NEUTRAL_BTN_HOVER,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkScrollbar": {
            "corner_radius": 1000,
            "border_spacing": 4,
            "fg_color": "transparent",
            "button_color": SCROLLBAR,
            "button_hover_color": SCROLLBAR_HOVER,
        },
        # 标签页条（CTkTabview 内部就是一个分段按钮）：
        # 未选中 = 内嵌底色，选中 = 卡片表面色，与下方内容区连成一体
        "CTkSegmentedButton": {
            "corner_radius": RADIUS_CARD,
            "border_width": 2,
            "fg_color": SURFACE_ALT,
            "selected_color": SURFACE,
            "selected_hover_color": SURFACE,
            "unselected_color": SURFACE_ALT,
            "unselected_hover_color": NEUTRAL_BTN,
            "text_color": TEXT,
            "text_color_disabled": TEXT_DISABLED,
        },
        "CTkTextbox": {
            "corner_radius": RADIUS_INPUT,
            "border_width": 1,
            "fg_color": INPUT_BG,
            "border_color": BORDER,
            "text_color": TEXT,
            "scrollbar_button_color": SCROLLBAR,
            "scrollbar_button_hover_color": SCROLLBAR_HOVER,
        },
        "CTkScrollableFrame": {
            "label_fg_color": SURFACE_ALT,
        },
        "DropdownMenu": {
            "fg_color": SURFACE,
            "hover_color": ACCENT_SOFT,
            "text_color": TEXT,
        },
        "CTkFont": {
            "macOS": {
                "family": "SF Display",
                "size": 13,
                "weight": "normal",
            },
            "Windows": {
                "family": "Microsoft YaHei",
                "size": 13,
                "weight": "normal",
            },
            "Linux": {
                "family": "Roboto",
                "size": 13,
                "weight": "normal",
            },
        },
    }


# ============================================================
# 交互态
# ============================================================

def bind_focus_ring(entry) -> None:
    """
    让输入框在获得焦点时把描边换成主色。

    整套界面里输入框此前一直是米色描边，点进去看不出焦点落在哪个框 ——
    这是可用性问题，不是装饰。只改 border_color、不动 border_width，
    聚焦时控件尺寸不会跳动（跳一下会把整行挤歪）。

    CustomTkinter 的 CTkEntry 会把 <FocusIn>/<FocusOut> 转发给它内部的
    tkinter.Entry，所以直接 bind 即可，不用去碰私有属性。
    """
    entry.bind("<FocusIn>", lambda _e: entry.configure(border_color=ACCENT))
    entry.bind("<FocusOut>", lambda _e: entry.configure(border_color=BORDER))


# ============================================================
# 装载
# ============================================================

THEME_FILENAME = "ctk_theme.json"


def theme_file_path() -> pathlib.Path:
    """
    主题文件的落盘位置。

    与预设、布局文件同放 %APPDATA%/WatermarkTool/：安装目录是只读的
    （Program Files 下没有写权限），运行时生成的文件只能写用户目录。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return pathlib.Path(base) / "WatermarkTool" / THEME_FILENAME


def install_theme() -> str:
    """
    生成并装载主题，返回实际生效的主题标识。

    必须在创建**任何**控件之前调用：CustomTkinter 在控件构造时就把主题值
    读进去了，之后再 set_default_color_theme 只影响之后新建的控件。

    主题是外观，不是功能：任何一步失败都只能降级、不能拦截启动 ——
    所以兜底回内置主题，并把失败留给调用方记日志。
    """
    import customtkinter as ctk

    try:
        path = theme_file_path()
        payload = json.dumps(build_ctk_theme(), ensure_ascii=False, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 内容一致就不重写：启动路径上少一次磁盘写入，
        # 也避免把文件的修改时间无谓地刷新
        if not path.exists() or path.read_text(encoding="utf-8") != payload:
            path.write_text(payload, encoding="utf-8")
        ctk.set_default_color_theme(str(path))
        return str(path)
    except Exception:
        ctk.set_default_color_theme("blue")
        return "blue"
