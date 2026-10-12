"""
主窗口 — 三栏布局 + 事件编排（完整版）

布局：
+------------------------------------------------------------------+
| 标题栏 [深色/浅色切换] [预设下拉]                              |
+--------+------------------------+-------------------------------+
| 文件区 | 预览区                  | 水印设置区                   |
| 拖拽区  | 实时预览画布            | Tab: 文字 / 图片             |
| 文件列表|                        | 九宫格/平铺/参数              |
+--------+------------------------+-------------------------------+
| 输出目录| 格式/质量 | 进度条     | [开始] [取消]               |
+------------------------------------------------------------------+

Phase 4a 完成：整合文字水印面板、图片水印面板、逆向合成预览画布
"""

from __future__ import annotations

import os
import json
import customtkinter as ctk
import tkinter as tk
from tkinterdnd2 import TkinterDnD
from tkinterdnd2.TkinterDnD import DnDWrapper

from src.ui.drop_zone import DropZone
from src.ui.file_list import FileList
from src.ui.text_watermark_panel import TextWatermarkPanel
from src.ui.image_watermark_panel import ImageWatermarkPanel
from src.ui.preview_canvas import PreviewCanvas
from src.ui.scope_selector import ScopeSelector
from src.file_manager import collect_files, is_image_file, is_pdf_file
from src.config import WatermarkConfig
from src.overrides import SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE, ScopeStore

# 布局配置文件：存到用户 AppData 目录，与水印预设完全分离
LAYOUT_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WatermarkTool", "ui_layout.json")

# 崩溃恢复会话文件
SESSION_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WatermarkTool", "session.json")

# 各区域最小尺寸
MIN_LEFT = 180
MIN_CENTER = 300
MIN_RIGHT = 260
MIN_BOTTOM = 40


class AppWindow(ctk.CTk, DnDWrapper):
    """主应用窗口 — 继承 CTk + DnDWrapper 获得 tkinterdnd2 支持"""

    WINDOW_WIDTH = 1150
    WINDOW_HEIGHT = 780

    def __init__(self):
        super().__init__()

        # 加载 tkdnd Tcl 包（传入 self 而非 self.tk）
        self.TkdndVersion = TkinterDnD._require(self)

        # 窗口基本配置
        self.title("批量图片水印工具")
        self.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}")
        self.minsize(950, 620)

        # CustomTkinter 内置 DPI 机制
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        # 恢复窗口大小和位置（优先于居中）
        if not self._restore_window_geometry():
            self._center_window()  # 无保存数据时居中

        # 内部状态
        self._cancel_requested = False

        # 三层参数作用域：全局默认 / 本文件专属 / 本页专属
        self._scopes = ScopeStore()
        self._scope = SCOPE_ALL   # 当前正在编辑哪一层
        self._syncing = False     # 面板回填期间暂停"提交到当前层"

        # 构建 UI
        self._build_ui()

        # 把两个面板的初始状态登记为全局默认。
        # 不做这一步的话，首次按作用域回填面板会用 WatermarkConfig 的默认值
        # （text 为空）覆盖面板里的初始文字，预览会突然"没有水印"
        self._init_scope_base()

        # 检测上次是否意外崩溃
        self.after(500, self._check_crash_recovery)

    def _center_window(self):
        """将窗口居中显示在屏幕上"""
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        """构建可拖动分隔线布局"""

        # ====== 顶部栏：标题 + 主题切换 + 预设 ======
        self._top_bar = ctk.CTkFrame(self, height=40)
        self._top_bar.pack(fill="x", side="top", padx=10, pady=(10, 5))
        self._top_bar.pack_propagate(False)

        title_label = ctk.CTkLabel(
            self._top_bar,
            text="批量图片水印工具",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        title_label.pack(side="left", padx=10)

        self._theme_btn = ctk.CTkButton(
            self._top_bar, text="深色/浅色", width=100,
            command=self._toggle_theme,
        )
        self._theme_btn.pack(side="right", padx=5)

        preset_frame = ctk.CTkFrame(self._top_bar, fg_color="transparent")
        preset_frame.pack(side="right", padx=5)

        self._preset_var = ctk.StringVar(value="-- 选择预设 --")
        self._preset_menu = ctk.CTkOptionMenu(
            preset_frame, variable=self._preset_var,
            values=self._get_preset_list(), width=140,
            command=self._on_preset_selected,
        )
        self._preset_menu.pack(side="left", padx=(0, 3))

        self._save_preset_btn = ctk.CTkButton(
            preset_frame, text="保存预设", width=70,
            command=self._on_save_preset,
        )
        self._save_preset_btn.pack(side="left", padx=2)

        self._delete_preset_btn = ctk.CTkButton(
            preset_frame, text="删除", width=50,
            fg_color="#ef4444", hover_color="#dc2626",
            command=self._on_delete_preset,
        )
        self._delete_preset_btn.pack(side="left", padx=2)

        # 预设已修改提示
        self._preset_loaded_name: str | None = None
        self._preset_just_loaded = False
        self._preset_modified_label = ctk.CTkLabel(
            preset_frame, text="●已修改", text_color="#f59e0b",
            font=("Microsoft YaHei", 10)
        )
        # 初始隐藏（pack 后再 pack_forget）

        # ====== 垂直 PanedWindow：主内容区 | 底部栏 ======
        self._vpaned = tk.PanedWindow(
            self, orient="vertical",
            sashwidth=4, sashrelief="flat",
            bg="#2b2b2b", bd=0,
        )
        self._vpaned.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        # ====== 水平 PanedWindow：左 | 中 | 右 ======
        self._hpaned = tk.PanedWindow(
            self._vpaned, orient="horizontal",
            sashwidth=4, sashrelief="flat",
            bg="#2b2b2b", bd=0,
        )
        self._vpaned.add(self._hpaned, minsize=200, stretch="always")

        # ---- 左栏：文件区 ----
        left_frame = ctk.CTkFrame(self._hpaned)
        self._hpaned.add(left_frame, minsize=MIN_LEFT, width=220)

        self._drop_zone = DropZone(
            left_frame, on_files_added=self._on_files_dropped, height=120,
        )
        self._drop_zone.pack(fill="x", padx=5, pady=5)

        self._browse_btn = ctk.CTkButton(
            left_frame, text="+ 浏览添加图片", command=self._browse_files,
        )
        self._browse_btn.pack(fill="x", padx=5, pady=2)

        btn_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=5, pady=2)

        self._clear_btn = ctk.CTkButton(btn_frame, text="清空", width=60, command=self._on_clear_files)
        self._clear_btn.pack(side="left", padx=2)
        self._remove_btn = ctk.CTkButton(btn_frame, text="删除选中", width=70, command=self._on_remove_selected)
        self._remove_btn.pack(side="left", padx=2)
        self._up_btn = ctk.CTkButton(btn_frame, text="上移", width=50, command=self._on_move_up)
        self._up_btn.pack(side="left", padx=2)
        self._down_btn = ctk.CTkButton(btn_frame, text="下移", width=50, command=self._on_move_down)
        self._down_btn.pack(side="left", padx=2)

        self._file_list = FileList(
            left_frame, on_selection_changed=self._on_file_selected,
        )
        self._file_list.pack(fill="both", expand=True, padx=5, pady=5)

        self._count_label = ctk.CTkLabel(left_frame, text="共 0 个文件")
        self._count_label.pack(pady=2)

        # ---- 中栏：预览区 ----
        center_frame = ctk.CTkFrame(self._hpaned)
        self._hpaned.add(center_frame, minsize=MIN_CENTER, width=500)

        self._preview_canvas = PreviewCanvas(
            center_frame, on_config_changed=self._get_current_config,
            on_page_changed=self._on_preview_page_changed,
        )
        self._preview_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        # ---- 右栏：水印设置区 ----
        right_frame = ctk.CTkFrame(self._hpaned)
        self._hpaned.add(right_frame, minsize=MIN_RIGHT, width=320)

        # 作用范围：决定当前这套参数记到哪一层（全部文件 / 仅本文件 / 仅本页）
        self._scope_selector = ScopeSelector(
            right_frame,
            on_scope_changed=self._on_scope_changed,
            on_clear=self._on_clear_scope_override,
        )
        self._scope_selector.pack(fill="x", padx=8, pady=(8, 0))

        ctk.CTkFrame(right_frame, height=1, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=10, pady=(8, 2)
        )

        self._settings_tabview = ctk.CTkTabview(right_frame)
        self._settings_tabview.pack(fill="both", expand=True, padx=4, pady=4)

        tab_text = self._settings_tabview.add("文字水印")
        self._text_panel = TextWatermarkPanel(tab_text)
        self._text_panel.on_config_changed = self._request_preview_refresh
        self._text_panel.pack(fill="both", expand=True)

        tab_image = self._settings_tabview.add("图片水印")
        self._image_panel = ImageWatermarkPanel(tab_image)
        self._image_panel.on_config_changed = self._request_preview_refresh
        self._image_panel.pack(fill="both", expand=True)

        self._settings_tabview.configure(command=lambda x: self._request_preview_refresh())

        # ---- 底部操作栏 ----
        bottom_bar = ctk.CTkFrame(self._vpaned, height=50)
        self._vpaned.add(bottom_bar, minsize=MIN_BOTTOM, height=50)

        ctk.CTkLabel(bottom_bar, text="输出目录:").pack(side="left", padx=5)
        self._output_dir_var = ctk.StringVar(value="")
        self._output_dir_entry = ctk.CTkEntry(bottom_bar, textvariable=self._output_dir_var, width=220)
        self._output_dir_entry.pack(side="left", padx=2)
        self._output_dir_btn = ctk.CTkButton(bottom_bar, text="浏览...", width=60, command=self._browse_output_dir)
        self._output_dir_btn.pack(side="left", padx=2)
        self._output_hint_label = ctk.CTkLabel(
            bottom_bar, text="留空→原图目录\\watermarked_output",
            text_color="gray", font=("Microsoft YaHei", 9)
        )
        self._output_hint_label.pack(side="left", padx=2)

        ctk.CTkFrame(bottom_bar, width=1, fg_color=("gray70", "gray30")).pack(side="left", fill="y", padx=8)

        ctk.CTkLabel(bottom_bar, text="格式:").pack(side="left", padx=5)
        self._format_var = ctk.StringVar(value="自动")
        self._format_menu = ctk.CTkOptionMenu(
            bottom_bar, variable=self._format_var,
            values=["自动", "PNG", "JPEG", "PDF"], width=80,
            command=self._on_format_changed,
        )
        self._format_menu.pack(side="left", padx=2)

        # 仅格式=PDF 时出现：把整批合成一个多页 PDF
        self._merge_pdf_var = ctk.BooleanVar(value=False)
        self._merge_pdf_cb = ctk.CTkCheckBox(
            bottom_bar, text="合并为单个 PDF", variable=self._merge_pdf_var,
            width=118, font=("Microsoft YaHei", 11),
        )
        self._merge_pdf_cb.pack(side="left", padx=(6, 2))
        self._merge_pdf_cb.pack_forget()  # 非 PDF 格式时隐藏

        ctk.CTkLabel(bottom_bar, text="质量:").pack(side="left", padx=5)
        self._quality_var = ctk.IntVar(value=90)
        self._quality_slider = ctk.CTkSlider(
            bottom_bar, from_=60, to=100, variable=self._quality_var, width=80,
            command=lambda v: self._quality_value_label.configure(text=str(int(v)))
        )
        self._quality_slider.pack(side="left", padx=2)
        self._quality_value_label = ctk.CTkLabel(bottom_bar, text="90", width=25, font=("Microsoft YaHei", 11))
        self._quality_value_label.pack(side="left", padx=2)
        ctk.CTkLabel(
            bottom_bar, text="(JPEG/PDF)", text_color="gray", font=("Microsoft YaHei", 9)
        ).pack(side="left", padx=2)

        ctk.CTkFrame(bottom_bar, width=1, fg_color=("gray70", "gray30")).pack(side="left", fill="y", padx=8)

        self._progress_bar = ctk.CTkProgressBar(bottom_bar, width=200)
        self._progress_bar.pack(side="left", padx=5)
        self._progress_bar.set(0)
        self._progress_bar.pack_forget()

        self._status_label = ctk.CTkLabel(bottom_bar, text="")
        self._status_label.pack(side="left", padx=5)

        self._start_btn = ctk.CTkButton(
            bottom_bar, text="开始处理", width=100,
            command=self._on_start_processing,
            fg_color="#22c55e", hover_color="#16a34a",
        )
        self._start_btn.pack(side="right", padx=5)

        self._cancel_btn = ctk.CTkButton(
            bottom_bar, text="取消", width=70,
            command=self._on_cancel_processing,
            fg_color="#ef4444", hover_color="#dc2626",
        )
        self._cancel_btn.pack(side="right", padx=2)
        self._cancel_btn.configure(state="disabled")

        # 恢复布局
        self._restore_layout()

        # 拖动分隔线后自动保存（防抖 500ms）
        self._layout_save_timer = None
        self._hpaned.bind("<ButtonRelease-1>", self._schedule_layout_save)
        self._vpaned.bind("<ButtonRelease-1>", self._schedule_layout_save)

        # 窗口关闭时保存布局
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _schedule_layout_save(self, event=None):
        """防抖保存布局（500ms 后执行）"""
        if self._layout_save_timer:
            self.after_cancel(self._layout_save_timer)
        self._layout_save_timer = self.after(500, self._save_layout)

    def _on_window_close(self):
        """窗口关闭时保存布局 + 清理会话"""
        self._save_layout()
        self._clear_session()
        self.destroy()

    # ============================================================
    # 崩溃恢复
    # ============================================================

    def _check_crash_recovery(self):
        """启动时检测上次是否意外崩溃（session.json 存在=未正常关闭）"""
        try:
            if not os.path.exists(SESSION_FILE):
                return

            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                session = json.load(f)

            from tkinter import messagebox
            result = messagebox.askyesno(
                "恢复上次会话",
                f"检测到上次处理时未正常退出（可能崩溃了）。\n\n"
                f"时间: {session.get('timestamp', '未知')}\n"
                f"文件数: {len(session.get('files', []))}\n\n"
                f"是否恢复上次会话？",
                parent=self,
            )

            if result:
                # 恢复文件列表
                files = session.get("files", [])
                if files:
                    existing = [f for f in files if os.path.exists(f)]
                    missing = len(files) - len(existing)
                    if existing:
                        self._file_list.add_files(existing)
                        self._update_count()
                    # 显示恢复结果
                    if missing > 0:
                        self._status_label.configure(
                            text=f"已恢复 {len(existing)} 张，{missing} 张文件已找不到"
                        )
                    else:
                        self._status_label.configure(
                            text=f"已恢复全部 {len(existing)} 张文件"
                        )

                # 恢复输出目录
                output_dir = session.get("output_dir", "")
                if output_dir:
                    self._output_dir_var.set(output_dir)

                # 恢复格式和质量
                fmt = session.get("format", "自动")
                self._format_var.set(fmt)
                self._on_format_changed(fmt)  # set() 不触发下拉回调，需手动同步
                self._merge_pdf_var.set(bool(session.get("merge_pdf", False)))
                quality = session.get("quality", 90)
                self._quality_var.set(quality)

                # 恢复配置到面板
                config_dict = session.get("config", {})
                if config_dict:
                    try:
                        cfg = WatermarkConfig.from_dict(config_dict)
                        self._text_panel.set_from_config(cfg)
                        self._image_panel.set_from_config(cfg)
                    except Exception:
                        pass

                # 恢复作用域数据（文件层 / 页层专属参数）
                # 顺序必须在面板回填之后：没有存过作用域数据时，
                # 需要把恢复后的面板状态重新登记为全局默认。
                scopes_dict = session.get("scopes", {})
                if scopes_dict:
                    try:
                        self._scopes = ScopeStore.from_dict(scopes_dict)
                        self._scopes.prune(self._file_list.files)
                    except Exception as e:
                        print(f"[Scope Restore Error] {e}")
                        self._init_scope_base()
                else:
                    self._init_scope_base()

                self._refresh_scope_marks()
                self._refresh_scope_ui()
                self._status_label.configure(text="已恢复上次会话，可重新开始处理")

            # 无论是否恢复，都清除会话文件
            self._clear_session()

        except Exception:
            self._clear_session()

    def _save_session(self):
        """保存当前处理会话（处理开始时调用）"""
        try:
            from datetime import datetime
            config = self._get_current_config()
            session = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "files": self._file_list.files,
                "config": config.to_dict() if config else {},
                "output_dir": self._output_dir_var.get(),
                "format": self._format_var.get(),
                "merge_pdf": bool(self._merge_pdf_var.get()),
                "quality": self._quality_var.get(),
                "scopes": self._scopes.to_dict(),
            }
            os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
            with open(SESSION_FILE, 'w', encoding='utf-8') as f:
                json.dump(session, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _clear_session(self):
        """清除会话文件（正常完成或关闭时调用）"""
        try:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        except Exception:
            pass

    # ============================================================
    # 布局持久化（存比例，不存像素）
    # ============================================================

    def _restore_window_geometry(self) -> bool:
        """从布局文件恢复窗口大小和位置，返回是否成功"""
        try:
            if not os.path.exists(LAYOUT_FILE):
                return False
            with open(LAYOUT_FILE, 'r') as f:
                layout = json.load(f)

            w = max(950, layout.get("win_width", self.WINDOW_WIDTH))
            h = max(620, layout.get("win_height", self.WINDOW_HEIGHT))
            x = layout.get("win_x", None)
            y = layout.get("win_y", None)

            # 校验坐标在屏幕内（多显示器场景）
            if x is not None and y is not None:
                # 确保窗口至少有一部分在屏幕内
                screen_w = self.winfo_screenwidth()
                screen_h = self.winfo_screenheight()
                if x < -w + 100 or x > screen_w - 100:
                    x = None
                if y < 0 or y > screen_h - 100:
                    y = None

            if x is not None and y is not None:
                self.geometry(f"{w}x{h}+{x}+{y}")
            else:
                self.geometry(f"{w}x{h}")
            self.update_idletasks()
            return True
        except Exception:
            return False

    def _restore_layout(self):
        """从 ui_layout.json 恢复分隔线比例"""
        try:
            if os.path.exists(LAYOUT_FILE):
                with open(LAYOUT_FILE, 'r') as f:
                    layout = json.load(f)
                self.after(300, lambda: self._apply_layout(layout))
        except Exception:
            pass

    def _apply_layout(self, layout: dict):
        """按比例设置分隔线位置"""
        try:
            total_w = self._hpaned.winfo_width()
            total_h = self._vpaned.winfo_height()
            if total_w <= 1:
                total_w = self.WINDOW_WIDTH
            if total_h <= 1:
                total_h = self.WINDOW_HEIGHT

            # 水平：左栏比例 + 中栏比例（右栏自动占余）
            left_ratio = layout.get("left_ratio", 0.20)
            center_ratio = layout.get("center_ratio", 0.45)

            left_w = max(MIN_LEFT, int(total_w * left_ratio))
            center_w = max(MIN_CENTER, int(total_w * center_ratio))

            if len(self._hpaned.panes()) >= 2:
                self._hpaned.sash_place(0, left_w, 0)
            if len(self._hpaned.panes()) >= 3:
                self._hpaned.sash_place(1, left_w + center_w, 0)

            # 垂直：底部栏比例
            bottom_ratio = layout.get("bottom_ratio", 0.08)
            bottom_h = max(MIN_BOTTOM, int(total_h * bottom_ratio))
            content_h = max(100, total_h - bottom_h)

            if len(self._vpaned.panes()) >= 2:
                self._vpaned.sash_place(0, 0, content_h)
        except Exception:
            pass

    def _save_layout(self):
        """保存当前布局比例到 ui_layout.json"""
        try:
            total_w = self._hpaned.winfo_width()
            total_h = self._vpaned.winfo_height()
            if total_w <= 1 or total_h <= 1:
                return

            layout = {}

            # 窗口大小和位置
            layout["win_width"] = self.winfo_width()
            layout["win_height"] = self.winfo_height()
            layout["win_x"] = self.winfo_x()
            layout["win_y"] = self.winfo_y()

            if len(self._hpaned.panes()) >= 2:
                sash1 = self._hpaned.sash_coord(0)[0]
                layout["left_ratio"] = round(max(MIN_LEFT, sash1) / total_w, 4)
            if len(self._hpaned.panes()) >= 3:
                sash2 = self._hpaned.sash_coord(1)[0]
                layout["center_ratio"] = round(max(MIN_CENTER, sash2 - sash1) / total_w, 4)
            if len(self._vpaned.panes()) >= 2:
                v_sash = self._vpaned.sash_coord(0)[1]
                layout["bottom_ratio"] = round(max(MIN_BOTTOM, total_h - v_sash) / total_h, 4)

            os.makedirs(os.path.dirname(LAYOUT_FILE), exist_ok=True)
            with open(LAYOUT_FILE, 'w') as f:
                json.dump(layout, f)
        except Exception:
            pass

    # ============================================================
    # 配置联动 / 三层参数作用域
    # ============================================================

    def _active_panel(self):
        """当前激活 Tab 对应的设置面板"""
        current_tab = self._settings_tabview.get()
        if current_tab == "文字水印":
            return self._text_panel
        if current_tab == "图片水印":
            return self._image_panel
        return None

    def _panel_dict(self) -> dict | None:
        """读取当前面板参数字典（含输入值校验与状态栏提示）"""
        panel = self._active_panel()
        if panel is None:
            return None

        data = panel.get_config_dict()

        # 输入值被修正时在状态栏提示（越界夹取 / 非法回退）
        notices = getattr(panel, 'last_notices', None)
        if notices and hasattr(self, '_status_label'):
            self._status_label.configure(text="；".join(notices))

        return data

    def _request_preview_refresh(self):
        """请求预览刷新（统一入口）+ 预设已修改检测 + 提交到当前作用层"""
        # 预设已修改检测
        if self._preset_just_loaded:
            # 预设加载后的首次刷新，不算"已修改"
            self._preset_just_loaded = False
        elif self._preset_loaded_name is not None:
            # 已加载预设后用户又改了参数 → 显示"已修改"
            self._preset_modified_label.pack(side="left", padx=2)

        # 面板改动写入当前作用层（全部文件 / 本文件 / 本页）
        self._commit_current_layer()

        self._preview_canvas.request_refresh()

    def _commit_current_layer(self):
        """把面板当前参数合并进「当前作用层」"""
        if self._syncing:
            return  # 回填面板期间不算用户编辑，否则会把继承值写死在该层
        data = self._panel_dict()
        if not data:
            return

        path = self._file_list.selected_file
        try:
            if self._scope == SCOPE_FILE and path:
                # 用 set_file 而不是 update_file：面板提交的是完整参数，
                # set_file 只记下与全局默认的差异，没动过的字段才能继续继承。
                # 否则这一层会变成一份"当时的全局默认"旧快照，
                # 用户之后修改全局默认时这个文件不跟着变
                self._scopes.set_file(path, data)
            elif self._scope == SCOPE_PAGE and path:
                self._scopes.set_page(
                    path, self._preview_canvas.current_pdf_page, data
                )
            else:
                self._scopes.update_base(data)
        except Exception as e:
            print(f"[Scope Commit Error] {e}")
            return

        # 只刷新受影响的那一行：滑块拖动时每帧全量刷新会在上千文件时卡住
        if path and self._scope in (SCOPE_FILE, SCOPE_PAGE):
            try:
                self._file_list.update_marker(path, self._scopes.marker_text(path))
                if is_pdf_file(path):
                    self._preview_canvas.set_pdf_marked_pages(
                        self._scopes.page_numbers(path)
                    )
            except Exception:
                pass
        self._refresh_scope_ui()

    def _get_current_config(self) -> WatermarkConfig | None:
        """
        获取当前激活 Tab 对应的 WatermarkConfig
        由 PreviewCanvas.on_config_changed 回调调用
        """
        try:
            data = self._panel_dict()
            if data is None:
                return None

            # 补充底部栏的通用参数
            data["output_format"] = self._format_var.get()
            data["jpeg_quality"] = self._quality_var.get()
            output_dir = self._output_dir_var.get().strip()
            if output_dir and os.path.isdir(output_dir):
                data["output_dir"] = output_dir

            return WatermarkConfig.from_dict(data)
        except Exception as e:
            print(f"[Config Error] {e}")
            return None

    def _init_scope_base(self):
        """
        把两个面板的初始状态登记为全局默认。

        注意顺序：先图片面板后文字面板，让 watermark_type 落在默认 Tab（文字水印）上。
        """
        for panel in (self._image_panel, self._text_panel):
            try:
                self._scopes.update_base(panel.get_config_dict())
            except Exception as e:
                print(f"[Scope Init Error] {e}")
        self._refresh_scope_ui()

    # ---------- 作用范围：档位切换、面板回填与标记 ----------

    def _on_format_changed(self, choice: str):
        """输出格式切换：仅 PDF 显示「合并为单个 PDF」开关"""
        if choice == "PDF":
            self._merge_pdf_cb.pack(side="left", padx=(6, 2))
        else:
            self._merge_pdf_cb.pack_forget()
            self._merge_pdf_var.set(False)

    def _on_scope_changed(self, scope: str):
        """用户切换作用范围 → 面板回填该层的参数"""
        self._scope = scope
        self._sync_panels_to_target()

    def _on_clear_scope_override(self):
        """清除当前层的专属参数"""
        path = self._file_list.selected_file
        page = self._preview_canvas.current_pdf_page

        cleared = False
        if self._scope == SCOPE_PAGE and path:
            cleared = self._scopes.clear_page(path, page)
        elif self._scope == SCOPE_FILE and path:
            cleared = self._scopes.clear_file(path)

        if cleared:
            self._status_label.configure(
                text=f"已清除「{self._scope_selector.describe(self._scope)}」的专属参数"
            )
        else:
            self._status_label.configure(text="当前层没有专属参数")

        self._sync_panels_to_target()

    def _on_preview_page_changed(self, page_index: int):
        """预览翻页 → 面板切换到该页的专属参数"""
        self._sync_panels_to_target()

    def _is_pdf_selected(self) -> bool:
        """当前选中的是否 PDF"""
        path = self._file_list.selected_file
        return bool(path) and is_pdf_file(path)

    def _sync_panels_to_target(self):
        """
        把面板回填成「当前目标」生效的参数。

        目标由作用范围决定：
        - 全部文件 → 全局默认
        - 仅本文件 → 当前选中文件
        - 仅本页   → 当前选中 PDF 的当前页

        回填期间用 _syncing 挡住「提交到作用层」，
        否则 set_from_config 触发的回调会把继承来的整份参数写死在当前层。
        """
        path = self._file_list.selected_file
        page = self._preview_canvas.current_pdf_page

        # 目标不存在时自动降档，避免"面板显示全局、导出用专属"的错觉
        if self._scope != SCOPE_ALL and not path:
            self._scope = SCOPE_ALL
        elif self._scope == SCOPE_PAGE and not self._is_pdf_selected():
            self._scope = SCOPE_FILE if path else SCOPE_ALL
        self._scope_selector.set_scope(self._scope)

        if self._scope == SCOPE_ALL:
            cfg = self._scopes.config_for(None)
        else:
            cfg = self._scopes.config_for(path, page)

        self._syncing = True
        try:
            self._text_panel.set_from_config(cfg)
            self._image_panel.set_from_config(cfg)
        except Exception as e:
            print(f"[Scope Sync Error] {e}")
        finally:
            self._syncing = False

        self._refresh_scope_marks(path)
        self._refresh_scope_ui()
        self._preview_canvas.request_refresh()

    def _refresh_scope_marks(self, path: str = None):
        """
        刷新专属标记。

        - 传 path：只刷这一行（编辑时高频调用）
        - 不传：整表刷新（增删文件、清空专属后调用）
        """
        try:
            if path:
                self._file_list.update_marker(path, self._scopes.marker_text(path))
            else:
                self._file_list.refresh_markers(self._scopes.markers())
        except Exception:
            pass

        # 预览翻页栏的「专属页」提示
        try:
            current = path or self._file_list.selected_file
            if current and is_pdf_file(current):
                self._preview_canvas.set_pdf_marked_pages(
                    self._scopes.page_numbers(current)
                )
            else:
                self._preview_canvas.set_pdf_marked_pages([])
        except Exception:
            pass

    def _refresh_scope_ui(self):
        """刷新作用范围控件：可用状态、清除按钮、提示文案"""
        path = self._file_list.selected_file
        page = self._preview_canvas.current_pdf_page
        is_pdf = self._is_pdf_selected()

        self._scope_selector.set_page_enabled(is_pdf)
        self._scope_selector.set_scope(self._scope)

        has_override = False
        if self._scope == SCOPE_PAGE and path:
            has_override = self._scopes.has_page(path, page)
        elif self._scope == SCOPE_FILE and path:
            has_override = self._scopes.has_file(path)
        self._scope_selector.set_clear_enabled(has_override)

        def short(name, limit=16):
            return name if len(name) <= limit else name[:limit - 1] + "…"

        warn = False
        if self._scope == SCOPE_PAGE and path:
            text = f"当前：只作用于 {short(os.path.basename(path))} 第 {page + 1} 页"
        elif self._scope == SCOPE_FILE and path:
            text = f"当前：只作用于 {short(os.path.basename(path))}"
        elif path and self._scopes.has_override(path):
            text = (f"当前：全局默认。{short(os.path.basename(path))} 另有专属参数，"
                    f"此处修改不影响它")
            warn = True
        else:
            text = "当前：全局默认，所有文件共用"

        if self._scope != SCOPE_ALL and not path:
            text = "未选中文件，已回落到全局默认"
            warn = True

        self._scope_selector.set_hint(text, warn=warn)

    # ---------- 导出用的参数解析 ----------

    def _build_batch_plan(self, files: list):
        """
        把「当前界面设置 + 三层作用域」翻译成批处理计划。

        真正的决策逻辑在 src/batch_planner.py（无 GUI 依赖，可被测试直接覆盖），
        这里只负责把界面上的两个控件值喂进去。
        """
        from src.batch_planner import plan_batch

        return plan_batch(
            files,
            self._scopes,
            out_format=self._format_var.get(),
            merge_pdf=bool(self._merge_pdf_var.get()),
        )

    # ============================================================
    # 事件处理
    # ============================================================

    def _toggle_theme(self):
        """切换深色/浅色主题 + 同步原生控件颜色"""
        current = ctk.get_appearance_mode()
        if current == "Dark":
            ctk.set_appearance_mode("Light")
        else:
            ctk.set_appearance_mode("Dark")

        # 同步 PanedWindow 分隔线颜色
        paned_bg = "#f0f0f0" if ctk.get_appearance_mode() == "Light" else "#2b2b2b"
        try:
            self._vpaned.configure(bg=paned_bg)
            self._hpaned.configure(bg=paned_bg)
        except Exception:
            pass

        # 同步预览画布颜色
        if hasattr(self, '_preview_canvas'):
            self._preview_canvas.update_theme()

    # ============================================================
    # 预设管理
    # ============================================================

    def _get_preset_list(self) -> list[str]:
        """获取预设列表用于下拉菜单"""
        try:
            from src.preset_manager import PresetManager
            presets = PresetManager.list_presets()
            return presets if presets else ["-- 无预设 --"]
        except Exception:
            return ["-- 无预设 --"]

    def _refresh_preset_menu(self):
        """刷新预设下拉菜单"""
        presets = self._get_preset_list()
        self._preset_menu.configure(values=presets)
        self._preset_var.set("-- 选择预设 --")

    def _on_preset_selected(self, choice: str):
        """选择预设 → 加载到面板"""
        if choice in ("-- 选择预设 --", "-- 无预设 --", ""):
            return

        try:
            from src.preset_manager import PresetManager

            config, warnings = PresetManager.load_preset(choice)

            # 标记预设已加载（首次刷新不算"已修改"）
            self._preset_loaded_name = choice
            self._preset_just_loaded = True
            self._preset_modified_label.pack_forget()  # 清除"已修改"标记

            # 应用到文字面板
            self._text_panel.set_from_config(config)
            # 应用到图片面板
            self._image_panel.set_from_config(config)

            # 切换到对应 Tab
            if config.watermark_type == "image":
                self._settings_tabview.set("图片水印")
            else:
                self._settings_tabview.set("文字水印")

            # 刷新预览
            self._request_preview_refresh()

            # 显示警告（如有）
            if warnings:
                self._status_label.configure(text="; ".join(warnings))
            else:
                self._status_label.configure(text=f"已加载预设: {choice}")

        except FileNotFoundError:
            self._show_message(f"预设不存在: {choice}")
            self._refresh_preset_menu()
        except Exception as e:
            self._show_message(f"加载预设失败: {e}")

    def _on_save_preset(self):
        """保存当前配置为预设"""
        from tkinter import simpledialog, messagebox
        from src.preset_manager import PresetManager

        # 获取当前配置
        config = self._get_current_config()
        if config is None:
            self._show_message("当前配置无效，无法保存")
            return

        # 输入预设名
        name = simpledialog.askstring(
            "保存预设",
            "请输入预设名称:",
            parent=self,
        )
        if not name or not name.strip():
            return

        name = name.strip()

        # 检查是否覆盖
        if PresetManager.preset_exists(name):
            if not messagebox.askyesno(
                "确认覆盖",
                f"预设 '{name}' 已存在，是否覆盖？",
                parent=self,
            ):
                return

        try:
            PresetManager.save_preset(name, config)
            self._refresh_preset_menu()
            # 选中新保存的预设
            self._preset_var.set(name)
            self._preset_loaded_name = name
            self._preset_just_loaded = True
            self._preset_modified_label.pack_forget()  # 保存后清除"已修改"
            self._status_label.configure(text=f"预设已保存: {name}")
        except Exception as e:
            self._show_message(f"保存预设失败: {e}")

    def _on_delete_preset(self):
        """删除选中的预设"""
        from tkinter import messagebox
        from src.preset_manager import PresetManager

        name = self._preset_var.get()
        if name in ("-- 选择预设 --", "-- 无预设 --", ""):
            self._show_message("请先选择要删除的预设")
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确定要删除预设 '{name}' 吗？\n此操作不可撤销。",
            parent=self,
        ):
            return

        if PresetManager.delete_preset(name):
            self._refresh_preset_menu()
            self._preset_loaded_name = None
            self._preset_modified_label.pack_forget()
            self._status_label.configure(text=f"预设已删除: {name}")
        else:
            self._show_message(f"预设不存在: {name}")
            self._refresh_preset_menu()

    def _on_files_dropped(self, files: list[str]):
        """拖拽添加文件回调 — 显示添加/跳过反馈"""
        added, skipped = collect_files(files, existing_files=set(self._file_list.files))
        if added:
            count = self._file_list.add_files(added)
            self._update_count()
            current_count = self._file_list.count
            self._file_list.select_by_index(current_count - count)

        # 显示添加结果反馈
        from collections import Counter
        if skipped:
            reasons = Counter(reason for _, reason in skipped)
            reason_str = "、".join(f"{c}张{r}" for r, c in reasons.most_common())
            if added:
                self._status_label.configure(
                    text=f"已添加 {len(added)} 张；跳过 {len(skipped)} 张（{reason_str}）"
                )
            else:
                self._status_label.configure(text=f"跳过 {len(skipped)} 张（{reason_str}）")
        elif added:
            self._status_label.configure(text=f"已添加 {len(added)} 张")

    def _browse_files(self):
        """浏览添加文件（拖拽的兜底入口）"""
        from tkinter.filedialog import askopenfilenames

        files = askopenfilenames(
            title="选择图片/PDF文件",
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff *.tif"),
                ("PDF 文件", "*.pdf"),
                ("所有支持的格式", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff *.tif *.pdf"),
                ("所有文件", "*.*"),
            ],
        )
        if files:
            self._on_files_dropped(files)

    def _on_clear_files(self):
        """清空文件列表（列表不为空时二次确认）"""
        if self._file_list.count == 0:
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
            "确认清空",
            f"确定要清空全部 {self._file_list.count} 个文件吗？"
            + self._overrides_clear_hint(),
            parent=self,
        ):
            return
        self._file_list.clear_all()
        self._update_count()
        self._preview_canvas.clear_preview()
        # 文件都不在了，挂在它们身上的专属参数一并清掉
        self._scopes.clear_overrides()
        self._preview_canvas.set_pdf_marked_pages([])
        self._refresh_scope_marks()
        self._refresh_scope_ui()

    def _on_remove_selected(self):
        """删除选中项"""
        self._file_list.remove_selected()
        self._update_count()
        self._prune_scopes()
        self._sync_panels_to_target()

    def _prune_scopes(self) -> int:
        """丢弃已不在列表里的文件所带的专属参数"""
        removed = self._scopes.prune(self._file_list.files)
        self._refresh_scope_marks()
        return removed

    def _overrides_clear_hint(self) -> str:
        """清空确认框里提示会连带清掉多少专属参数"""
        count = len(self._scopes.override_paths())
        if count:
            return f"\n\n注意：其中 {count} 个文件带有专属参数，将一并清除"
        return ""

    def _on_move_up(self):
        """上移选中项"""
        self._file_list.move_up()

    def _on_move_down(self):
        """下移选中项"""
        self._file_list.move_down()

    def _on_file_selected(self, path: str | None):
        """
        文件选中变更回调 → 自动定位作用层、回填参数、刷新预览
        :param path: 选中的文件路径，None 表示无选中
        """
        if path and os.path.exists(path):
            self._preview_canvas.set_image(path)

            # 该文件（或它的某一页）已有专属参数时，自动把作用范围切到那一层。
            # 面板显示的必须是「这个文件真正会用的参数」，否则预览会与导出不一致
            deepest = self._scopes.deepest_scope_for(
                path, self._preview_canvas.current_pdf_page
            )
            if deepest and self._scope == SCOPE_ALL:
                self._scope = deepest

            self._sync_panels_to_target()
        else:
            self._preview_canvas.clear_preview()
            self._refresh_scope_ui()

    def _browse_output_dir(self):
        """浏览选择输出目录"""
        from tkinter.filedialog import askdirectory

        d = askdirectory(title="选择输出目录")
        if d:
            self._output_dir_var.set(d)

    def _update_count(self):
        """更新文件计数显示"""
        count = self._file_list.count
        self._count_label.configure(text=f"共 {count} 个文件")

    def _on_start_processing(self):
        """开始处理按钮回调 — ThreadPoolExecutor 多核并行版本"""
        import os
        import time
        import gc
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from src.file_manager import generate_output_path, get_default_output_dir

        # 1. 前置检查
        files = self._file_list.files
        if not files:
            self._show_message("请先添加图片文件")
            return

        output_dir = self._output_dir_var.get().strip()
        if not output_dir or not os.path.isdir(output_dir):
            output_dir = get_default_output_dir(files)
            if not output_dir:
                output_dir = os.path.join(os.path.expanduser("~"), "Desktop", "水印输出")
            os.makedirs(output_dir, exist_ok=True)
            self._output_dir_var.set(output_dir)

        config = self._get_current_config()
        if config is None:
            self._show_message("配置错误，无法获取水印设置")
            return

        # 磁盘空间预检
        import shutil
        try:
            total_input_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
            needed_size = int(total_input_size * 1.2)  # 预留 20% 余量
            free_space = shutil.disk_usage(output_dir).free
            if free_space < needed_size:
                free_mb = free_space / (1024 * 1024)
                needed_mb = needed_size / (1024 * 1024)
                self._show_message(
                    f"磁盘空间不足！\n\n"
                    f"预计需要: {needed_mb:.0f} MB\n"
                    f"可用空间: {free_mb:.0f} MB\n"
                    f"输出目录: {output_dir}"
                )
                return
        except Exception:
            pass  # 检查失败不阻止处理

        # 整批处理计划：每个文件的生效参数 + 输出格式 + 是否合并
        # （决策逻辑见 src/batch_planner.py，可被测试直接覆盖）
        plan = self._build_batch_plan(files)
        specs = plan.tasks
        out_format = plan.out_format
        merge_pdf = plan.merge
        quality = self._quality_var.get()

        # 2. 更新 UI 状态
        self._start_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress_bar.pack()
        self._progress_bar.set(0)
        self._status_label.configure(text="合并中..." if merge_pdf else "处理中...")

        # 3. 动态线程池大小：基于文件大小调整，避免大图内存占满
        avg_size_mb = sum(os.path.getsize(f) for f in files if os.path.exists(f)) / max(len(files), 1) / (1024 * 1024)
        cpu_workers = max(2, (os.cpu_count() or 4) - 1)
        if avg_size_mb > 15:
            num_workers = min(2, cpu_workers)  # 大图限制 2 线程
        elif avg_size_mb > 5:
            num_workers = min(3, cpu_workers)  # 中等图限制 3 线程
        else:
            num_workers = cpu_workers  # 小图全速

        # 3.5 预生成输出路径（避免多线程竞争 + 重名检测）
        # - 合并模式只有一个输出文件，用时间戳命名避免与逐文件结果撞名
        existing_names: set[str] = set()
        output_paths: list[str] = []
        merged_path = ""
        if merge_pdf:
            from src.file_manager import generate_merged_output_path
            merged_path = generate_merged_output_path(output_dir, existing_names)
        else:
            for i, task in enumerate(specs):
                out_path = generate_output_path(
                    task.path, output_dir,
                    output_format=task.output_format,
                    seq_counter=i + 1,
                    existing_names=existing_names
                )
                output_paths.append(out_path)

        # 4. 单个文件处理函数（在工作线程中执行）
        #    具体的分支决策在 src/batch_planner.execute_task 里，这里只管调度与异常
        def _process_one(task):
            idx, src_path = task
            try:
                from src.batch_planner import execute_task
                execute_task(specs[idx], output_paths[idx], quality)
                return (idx, True, None, os.path.basename(src_path))
            except Exception as e:
                return (idx, False, str(e), os.path.basename(src_path))

        # 4.5 合并模式：把所有输入按列表顺序拼成一个多页 PDF
        #     图片一图一页，PDF 逐页并入；每个输入用各自的文件层/页层参数。
        #     单线程顺序执行（合并本身是纯顺序 IO），进度与取消走回调。
        def _merge_all():
            from src.pdf_engine import MergeCancelled, build_merged_pdf

            total = plan.count
            start_time = time.time()

            items = plan.merge_items()

            # 进度节流：200ms
            state = {"last": time.time()}

            def _on_item(index, count, filename):
                # build_merged_pdf 收尾时会再发一次 index == count 的通知，
                # 此时不算进度（进度 100% 由 _on_done 统一设置）
                if index >= count:
                    return
                now = time.time()
                if (now - state["last"]) * 1000 >= 200 or index + 1 >= count:
                    state["last"] = now
                    progress = (index + 1) / max(count, 1)
                    self.after(0, lambda p=progress, d=index + 1, fn=filename:
                               self._update_progress(p, d, count, fn))

            try:
                pages = build_merged_pdf(
                    items, merged_path,
                    jpeg_quality=quality,
                    progress=_on_item,
                    should_cancel=lambda: self._cancel_requested,
                )
            except MergeCancelled:
                # 用户取消：不写出任何文件
                elapsed = time.time() - start_time
                self.after(0, lambda: _on_done(0, 0, elapsed, None, True, 0))
                return
            except Exception as e:
                elapsed = time.time() - start_time
                self.after(0, lambda msg=str(e): _on_done(0, total, elapsed, msg, False, 0))
                return

            elapsed = time.time() - start_time
            self.after(0, lambda: _on_done(total, 0, elapsed, None, False, pages))

        # 5. 调度器线程 — 提交任务 + 收集结果 + 更新 UI
        def _dispatcher():
            if merge_pdf:
                _merge_all()
                return

            total = len(files)
            success_count = 0
            fail_count = 0
            first_error = None
            start_time = time.time()

            # 进度节流：双阈值（200ms OR 10 张）
            last_progress_time = time.time()
            last_progress_idx = 0
            PROGRESS_TIME_MS = 200
            PROGRESS_COUNT = 10

            tasks = list(enumerate(files))

            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                # 提交所有任务
                future_to_task = {
                    executor.submit(_process_one, task): task
                    for task in tasks
                }
                cancelled_pending = False

                for future in as_completed(future_to_task):
                    # 取消：只取消尚未开始的任务，等待已运行的完成
                    if self._cancel_requested and not cancelled_pending:
                        for f in future_to_task:
                            f.cancel()
                        cancelled_pending = True
                        self.after(0, lambda: self._status_label.configure(text="正在等待已处理的图片完成..."))

                    # 跳过被取消的任务
                    if future.cancelled():
                        continue

                    idx, success, error, filename = future.result()

                    if success:
                        success_count += 1
                    else:
                        fail_count += 1
                        if first_error is None:
                            first_error = f"{filename}: {error}"
                        print(f"[Process Error] {filename}: {error}")

                    done = success_count + fail_count

                    # 进度节流：超过 200ms 或超过 10 张才更新 UI
                    now = time.time()
                    if (now - last_progress_time) * 1000 >= PROGRESS_TIME_MS \
                            or done - last_progress_idx >= PROGRESS_COUNT \
                            or done == total:
                        progress = done / total
                        self.after(0, lambda p=progress, d=done, fn=filename: self._update_progress(p, d, total, fn))
                        last_progress_time = now
                        last_progress_idx = done

                    # GC 控制：每 10 张手动回收
                    if done % 10 == 0:
                        gc.collect()

            # 最终 GC
            gc.collect()

            elapsed = time.time() - start_time
            was_cancelled = self._cancel_requested
            self.after(0, lambda: _on_done(success_count, fail_count, elapsed, first_error, was_cancelled))

        def _on_done(success, fail, elapsed, first_error, was_cancelled=False, merged_pages=None):
            """处理完成回调（主线程）

            merged_pages 为 None 表示逐文件模式；
            为整数（可为 0）表示合并模式的收尾，此时 success 记的是参与合并的输入数。
            """
            self._start_btn.configure(state="normal")
            self._cancel_btn.configure(state="disabled")
            self._cancel_requested = False
            self._clear_session()  # 正常完成，清除会话

            # ---------------- 合并模式收尾 ----------------
            if merged_pages is not None:
                if was_cancelled:
                    self._status_label.configure(text=f"已取消合并 ({elapsed:.1f}s)")
                    return
                if first_error:
                    self._status_label.configure(text=f"合并失败: {first_error}")
                    self._show_message(f"合并失败：\n\n{first_error}")
                    return
                self._progress_bar.set(1)
                self._status_label.configure(
                    text=f"完成! 已合并 {success} 个输入共 {merged_pages} 页 ({elapsed:.1f}s)"
                )
                self._show_completion_dialog(
                    output_dir, success, 0,
                    note=f"输出 1 个 PDF，共 {merged_pages} 页",
                )
                return

            # ---------------- 逐文件模式收尾 ----------------
            if was_cancelled:
                self._status_label.configure(
                    text=f"已取消! 成功 {success} 张, 失败 {fail} 张 ({elapsed:.1f}s)"
                )
                if success > 0:
                    self._show_completion_dialog(output_dir, success, fail)
                return

            if success > 0:
                self._status_label.configure(
                    text=f"完成! 成功 {success} 张, 失败 {fail} 张 ({elapsed:.1f}s)"
                )
                self._show_completion_dialog(output_dir, success, fail)
            else:
                if first_error:
                    self._status_label.configure(text=f"全部失败: {first_error}")
                else:
                    self._status_label.configure(text="处理失败")
                    self._show_message("没有成功处理的文件")

        # 初始化取消标志并启动调度器线程
        self._cancel_requested = False
        self._save_session()  # 保存会话（崩溃恢复用）
        import time as _time
        self._batch_start_time = _time.time()
        t = threading.Thread(target=_dispatcher, daemon=True)
        t.start()

    def _update_progress(self, progress, current, total, filename=""):
        """更新进度条 + 当前文件名 + 已用时间 + 内存（主线程）"""
        self._progress_bar.set(progress)
        mem_str = ""
        try:
            import psutil
            mem = psutil.Process().memory_info().rss / (1024 * 1024)
            mem_str = f" | {mem:.0f}MB"
        except Exception:
            pass
        elapsed = ""
        try:
            import time
            if hasattr(self, '_batch_start_time'):
                e = time.time() - self._batch_start_time
                elapsed = f" | {e:.0f}s"
        except Exception:
            pass
        fn_str = f" | {filename}" if filename else ""
        self._status_label.configure(text=f"处理中... {current}/{total}{fn_str}{elapsed}{mem_str}")

    def _on_cancel_processing(self):
        """取消处理按钮回调"""
        self._cancel_requested = True
        self._status_label.configure(text="正在取消...")
        self._cancel_btn.configure(state="disabled")

    def _show_message(self, message: str):
        """显示提示消息对话框"""
        from tkinter import messagebox
        messagebox.showinfo("提示", message)

    def _show_completion_dialog(self, output_dir: str, success: int, fail: int,
                                note: str = None):
        """显示完成对话框，包含"打开文件夹"按钮

        :param note: 可选补充说明，会替换默认的「成功 N 张 | 失败 M 张」那行
                     （合并模式用它来显示输出页数）
        """
        import subprocess
        import os

        dialog = ctk.CTkToplevel(self)
        dialog.title("处理完成")
        dialog.geometry("400x200")
        dialog.resizable(False, False)
        # 模态对话框
        dialog.transient(self)
        dialog.grab_set()

        # 居中显示
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 200
        y = self.winfo_y() + (self.winfo_height() // 2) - 100
        dialog.geometry(f"+{x}+{y}")

        # 内容
        ctk.CTkLabel(
            dialog,
            text=f"批量处理完成！",
            font=ctk.CTkFont(size=18, weight="bold")
        ).pack(pady=(25, 10))

        detail = note if note else f"成功: {success} 张  |  失败: {fail} 张"
        ctk.CTkLabel(
            dialog,
            text=f"{detail}\n输出目录: {output_dir}",
            font=ctk.CTkFont(size=12),
            text_color=("gray20", "gray80")
        ).pack(pady=5)

        # 按钮区
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=20)

        ctk.CTkButton(
            btn_frame, text="打开输出文件夹", width=140,
            command=lambda: (
                os.startfile(os.path.abspath(output_dir)) if os.name == 'nt'
                else subprocess.run(["xdg-open", output_dir])
            )
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame, text="关闭", width=80,
            command=dialog.destroy
        ).pack(side="left", padx=10)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    app = AppWindow()
    app.mainloop()
