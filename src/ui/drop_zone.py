"""
拖拽区域组件 — 支持 tkinterdnd2 拖放 + 路径清洗 + 视觉反馈

功能：
1. 注册 tkinterdnd2 拖放目标（CTkFrame + DnDWrapper 混入）
2. DragEnter/DragLeave 视觉反馈（高亮边框 + 变色提示文字）
3. Drop 事件处理（路径清洗 + 扩展名过滤）
4. 兜底：提供"浏览添加"按钮
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk
from tkinterdnd2 import DND_FILES
from tkinterdnd2.TkinterDnD import DnDWrapper

from src.file_manager import collect_files, is_media_file, sanitize_path
from src.ui import theme


def _draw_photo_icon(canvas: tk.Canvas):
    """
    在画布上画一个"图片"图标，返回 (描边项, 填充项) 两组 id。

    为什么要画而不是用 emoji/字体符号：Windows 上 emoji 由 Tk 当普通字符渲染，
    字号一变就错位甚至变方块；这里用 Canvas 画，颜色和尺寸都可控。
    """
    outline = [canvas.create_rectangle(
        3, 5, 45, 35, outline=theme.TEXT_DISABLED, width=2
    )]
    fill = [
        canvas.create_oval(11, 11, 18, 18, fill=theme.TEXT_DISABLED, outline=""),
        canvas.create_polygon(
            7, 33, 18, 19, 27, 33, fill=theme.TEXT_DISABLED, outline=""
        ),
        canvas.create_polygon(
            23, 33, 32, 22, 41, 33, fill=theme.TEXT_DISABLED, outline=""
        ),
    ]
    return outline, fill


class DropZone(ctk.CTkFrame, DnDWrapper):
    """
    可拖拽文件的区域组件。

    继承 DnDWrapper 获得 drop_target_register / dnd_bind 方法。
    父窗口 AppWindow 已通过 TkinterDnD._require() 加载 tkdnd Tcl 包。

    视觉状态：
    - 默认：灰色边框 + 灰色提示文字
    - DragEnter：蓝色高亮边框(2px) + 蓝色"松开以添加图片"
    - DragLeave / Drop 后：恢复默认样式
    """

    # 样式常量（取色统一来自 src/ui/theme.py）
    _DEFAULT_BORDER_COLOR = theme.BORDER
    _HOVER_BORDER_COLOR = theme.ACCENT
    _DEFAULT_BORDER_WIDTH = 1
    _HOVER_BORDER_WIDTH = 2
    _DEFAULT_TEXT_COLOR = theme.TEXT_MUTED
    _HOVER_TEXT_COLOR = theme.ACCENT
    _DEFAULT_HINT = "将图片、PDF 或视频拖拽到这里\n或点击下方按钮添加"
    _HOVER_HINT = "松开以添加文件"

    def __init__(
        self,
        master,
        on_files_added: callable = None,
        **kwargs
    ):
        super().__init__(master, **kwargs)

        self.on_files_added = on_files_added or (lambda files: None)

        # 默认样式配置
        self.configure(
            border_width=self._DEFAULT_BORDER_WIDTH,
            border_color=self._DEFAULT_BORDER_COLOR,
            corner_radius=theme.RADIUS_CARD,
            fg_color=theme.SURFACE_ALT,
        )

        # 图标 + 提示文字（整体在框内居中）
        # 空状态只有两行字的话，"这里能放什么"得读完才明白；给一个图片图标，
        # 一眼就知道是"放图的地方"。图标与文字同色，拖入时一起变主色。
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.place(relx=0.5, rely=0.5, anchor="center")

        self._icon_canvas = tk.Canvas(
            content, width=48, height=40,
            bg=theme.SURFACE_ALT, highlightthickness=0,
        )
        self._icon_canvas.pack(pady=(0, 8))
        self._icon_outline_ids, self._icon_fill_ids = _draw_photo_icon(self._icon_canvas)

        self._hint_label = ctk.CTkLabel(
            content,
            text=self._DEFAULT_HINT,
            text_color=self._DEFAULT_TEXT_COLOR,
            font=ctk.CTkFont(size=14),
        )
        self._hint_label.pack()

        # 注册拖放（直接在 CTkFrame 上注册，DnDWrapper 提供 drop_target_register 方法）
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<DragEnter>>", self._on_enter)
        self.dnd_bind("<<DragLeave>>", self._on_leave)
        self.dnd_bind("<<Drop>>", self._on_drop)

        # 内部状态
        self._is_hovering = False

    def _on_enter(self, event):
        """鼠标拖入 → 高亮视觉反馈"""
        self._is_hovering = True
        self.configure(
            border_color=self._HOVER_BORDER_COLOR,
            border_width=self._HOVER_BORDER_WIDTH,
        )
        self._hint_label.configure(
            text=self._HOVER_HINT,
            text_color=self._HOVER_TEXT_COLOR,
        )
        self._set_icon_color(self._HOVER_TEXT_COLOR)

    def _on_leave(self, event):
        """鼠标离开 → 恢复默认样式"""
        self._is_hovering = False
        self._reset_style()

    def _on_drop(self, event):
        """松开鼠标 → 处理文件"""
        # 先恢复样式
        self._reset_style()

        # 解析路径
        raw_paths = self.tk.splitlist(event.data)
        clean_paths = [sanitize_path(p) for p in raw_paths]

        # 过滤有效输入文件（图片 / PDF / 视频）
        media_files = [p for p in clean_paths if is_media_file(p)]

        if media_files:
            self.on_files_added(media_files)

    def _set_icon_color(self, color: str):
        """
        重画图标颜色。

        描边项只有 outline、填充项只有 fill —— 两者都试着设一遍，
        对不适用的那个 canvas 会抛 TclError，直接忽略（这是 tk 的既定行为）。
        """
        for item in self._icon_outline_ids:
            try:
                self._icon_canvas.itemconfig(item, outline=color)
            except Exception:
                pass
        for item in self._icon_fill_ids:
            try:
                self._icon_canvas.itemconfig(item, fill=color)
            except Exception:
                pass

    def _reset_style(self):
        """恢复到默认视觉样式"""
        self.configure(
            border_color=self._DEFAULT_BORDER_COLOR,
            border_width=self._DEFAULT_BORDER_WIDTH,
        )
        self._hint_label.configure(
            text=self._DEFAULT_HINT,
            text_color=self._DEFAULT_TEXT_COLOR,
        )
        self._set_icon_color(theme.TEXT_DISABLED)
