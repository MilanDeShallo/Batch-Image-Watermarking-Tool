"""
作用范围选择器 — 决定当前这套水印参数记到哪一层

三档：
- 全部文件：作为全局默认，所有文件共用（没单独设置过的文件/页都用它）
- 仅本文件：只记给当前选中的这个文件
- 仅本页　：只记给当前选中 PDF 的当前预览页

配套控件：
- 「清除专属」：把当前层的专属参数删掉，让该文件/页回落到上一层
- 提示行：直接写明"当前这套参数作用在谁身上"，避免误改
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from src.overrides import SCOPE_ALL, SCOPE_FILE, SCOPE_LABELS, SCOPE_PAGE
from src.ui import theme


class ScopeSelector(ctk.CTkFrame):
    """三档作用范围选择器"""

    _SHORT_LABELS = {
        SCOPE_ALL: "全部文件",
        SCOPE_FILE: "仅本文件",
        SCOPE_PAGE: "仅本页",
    }

    def __init__(
        self,
        master,
        on_scope_changed: Optional[Callable[[str], None]] = None,
        on_clear: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(master, fg_color="transparent", **kwargs)

        self._on_scope_changed = on_scope_changed or (lambda scope: None)
        self._on_clear = on_clear or (lambda: None)

        self._scope = SCOPE_ALL
        self._syncing = False  # 程序化切换时不回调
        self._radios: dict[str, ctk.CTkRadioButton] = {}

        self._build_ui()

    # ==================== UI ====================

    def _build_ui(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x")

        ctk.CTkLabel(
            head, text="参数作用范围",
            font=("Microsoft YaHei", 11, "bold"), text_color=theme.TEXT,
        ).pack(side="left")

        self._clear_btn = ctk.CTkButton(
            head, text="清除专属", width=66, height=22,
            font=("Microsoft YaHei", 10),
            fg_color=theme.NEUTRAL_BTN, hover_color=theme.NEUTRAL_BTN_HOVER,
            command=self._handle_clear,
        )
        self._clear_btn.pack(side="right")
        self._clear_btn.configure(state="disabled")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))

        self._var = ctk.StringVar(value=SCOPE_ALL)
        for scope in (SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE):
            rb = ctk.CTkRadioButton(
                row, text=self._SHORT_LABELS[scope],
                variable=self._var, value=scope, width=70,
                font=("Microsoft YaHei", 11),
                command=self._handle_radio,
            )
            rb.pack(side="left", padx=(0, 6))
            self._radios[scope] = rb

        self._hint = ctk.CTkLabel(
            self, text="", text_color=theme.TEXT_MUTED, anchor="w", justify="left",
            font=("Microsoft YaHei", 10), wraplength=290,
        )
        self._hint.pack(fill="x", pady=(2, 0))

    # ==================== 回调 ====================

    def _handle_radio(self):
        if self._syncing:
            return
        self._scope = self._var.get()
        self._on_scope_changed(self._scope)

    def _handle_clear(self):
        if self._syncing:
            return
        self._on_clear()

    # ==================== 公共接口 ====================

    def get_scope(self) -> str:
        return self._scope

    def set_scope(self, scope: str, notify: bool = False):
        """程序化切档（默认不触发回调，用于自动定位到某文件/某页的专属层）"""
        if scope not in (SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE):
            scope = SCOPE_ALL

        self._syncing = True
        self._var.set(scope)
        self._scope = scope
        self._syncing = False

        if notify:
            self._on_scope_changed(scope)

    def set_page_enabled(self, enabled: bool):
        """非 PDF 时禁用「仅本页」"""
        self._radios[SCOPE_PAGE].configure(state="normal" if enabled else "disabled")

    def set_clear_enabled(self, enabled: bool):
        self._clear_btn.configure(state="normal" if enabled else "disabled")

    def set_hint(self, text: str, warn: bool = False):
        """
        设置提示文案。

        warn=True 用于「当前改的层不会影响正在预览的文件」这类需要留意的状态。
        """
        self._hint.configure(
            text=text,
            text_color=theme.WARN if warn else theme.TEXT_MUTED,
        )

    def describe(self, scope: str) -> str:
        """档位的中文全称（提示行与状态栏共用）"""
        return SCOPE_LABELS.get(scope, SCOPE_LABELS[SCOPE_ALL])
