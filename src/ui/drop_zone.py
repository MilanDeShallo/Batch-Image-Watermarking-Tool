"""
拖拽区域组件 — 支持 tkinterdnd2 拖放 + 路径清洗 + 视觉反馈

功能：
1. 注册 tkinterdnd2 拖放目标（CTkFrame + DnDWrapper 混入）
2. DragEnter/DragLeave 视觉反馈（高亮边框 + 变色提示文字）
3. Drop 事件处理（路径清洗 + 扩展名过滤）
4. 兜底：提供"浏览添加"按钮
"""

from __future__ import annotations

import customtkinter as ctk
from tkinterdnd2 import DND_FILES
from tkinterdnd2.TkinterDnD import DnDWrapper

from src.file_manager import collect_files, is_image_file, sanitize_path


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

    # 样式常量
    _DEFAULT_BORDER_COLOR = ("gray40", "gray60")
    _HOVER_BORDER_COLOR = ("#3b82f6", "#60a5fa")
    _DEFAULT_BORDER_WIDTH = 1
    _HOVER_BORDER_WIDTH = 2
    _DEFAULT_TEXT_COLOR = ("gray50", "gray50")
    _HOVER_TEXT_COLOR = ("#3b82f6", "#60a5fa")
    _DEFAULT_HINT = "将图片拖拽到这里\n或点击下方按钮添加"
    _HOVER_HINT = "松开以添加图片"

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
            corner_radius=8,
            fg_color=("gray95", "gray15"),
        )

        # 提示文字标签
        self._hint_label = ctk.CTkLabel(
            self,
            text=self._DEFAULT_HINT,
            text_color=self._DEFAULT_TEXT_COLOR,
            font=ctk.CTkFont(size=14),
        )
        self._hint_label.place(relx=0.5, rely=0.5, anchor="center")

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

        # 过滤有效图片文件
        image_files = [p for p in clean_paths if is_image_file(p)]

        if image_files:
            self.on_files_added(image_files)

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
