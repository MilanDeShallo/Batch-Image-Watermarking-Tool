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
from src.file_manager import (
    collect_files,
    is_image_file,
    is_pdf_file,
    resolve_output_target,
)
from src.config import (
    CONFLICT_LABELS,
    CONFLICT_RENAME,
    DEFAULT_FILENAME_TEMPLATE,
    DEFAULT_JPEG_QUALITY,
    WatermarkConfig,
)
from src.logger import log_error, log_exception, log_info, log_path
from src.overrides import SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE, ScopeStore

# 布局配置文件：存到用户 AppData 目录，与水印预设完全分离
LAYOUT_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WatermarkTool", "ui_layout.json")

# 主题与布局同存一份：都是"界面偏好"，没必要再开一个文件
APPEARANCE_KEY = "appearance"


def read_saved_appearance(default: str = "Dark") -> str:
    """
    读取上次保存的主题（"Dark" / "Light"）。

    必须在构建 UI **之前**调用，所以对"文件不存在 / 内容损坏 / 值不认识"
    一律返回默认值 —— 主题若等到 UI 建好之后再设置，用户会先看到一帧深色
    再跳变；而且两道 PanedWindow 分隔线是我们自己画的，不随
    set_appearance_mode 变化，浅色模式下会留下一道深色条纹。
    """
    try:
        with open(LAYOUT_FILE, 'r', encoding='utf-8') as f:
            value = json.load(f).get(APPEARANCE_KEY)
    except Exception:
        return default
    return value if value in ("Dark", "Light") else default

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
        ctk.set_default_color_theme("blue")
        # 主题沿用上次的选择（与布局同存一份），构建 UI 之前就要定下来
        ctk.set_appearance_mode(read_saved_appearance())

        # 恢复窗口大小和位置（优先于居中）
        if not self._restore_window_geometry():
            self._center_window()  # 无保存数据时居中

        # 内部状态
        self._cancel_requested = False
        self._processing = False         # 是否有批处理在跑（关窗确认用）
        self._completion_dialog = None   # 完成对话框单例
        self._last_failure_report = ""   # 最近一次批处理的失败清单路径
        self._last_output_dir = ""       # 最近一次批处理的实际输出位置

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

        # ---- 底部操作栏（两行：输出设置 / 执行控制）----
        # 输出设置项变多之后单行放不下：文件名模板、同名冲突策略、覆盖原图
        # 都是"导出结果长什么样"的一部分，藏进二级对话框反而更难发现
        bottom_bar = ctk.CTkFrame(self._vpaned, height=88)
        self._vpaned.add(bottom_bar, minsize=88, height=88)

        # ===== 第一行：输出位置 / 文件名 / 格式 =====
        self._row1 = ctk.CTkFrame(bottom_bar, fg_color="transparent")
        self._row1.pack(fill="x", padx=6, pady=(5, 0))

        ctk.CTkLabel(self._row1, text="输出目录:").pack(side="left", padx=(2, 2))
        self._output_dir_var = ctk.StringVar(value="")
        self._output_dir_entry = ctk.CTkEntry(
            self._row1, textvariable=self._output_dir_var, width=170
        )
        self._output_dir_entry.pack(side="left", padx=2)
        self._output_dir_btn = ctk.CTkButton(
            self._row1, text="浏览...", width=58, command=self._browse_output_dir
        )
        self._output_dir_btn.pack(side="left", padx=2)

        self._output_hint_label = ctk.CTkLabel(
            self._row1, text="留空 → 源目录\\watermarked_output",
            text_color="gray", font=("Microsoft YaHei", 9)
        )
        self._output_hint_label.pack(side="left", padx=(4, 8))

        ctk.CTkLabel(self._row1, text="文件名模板:").pack(side="left", padx=(0, 2))
        self._template_var = ctk.StringVar(value=DEFAULT_FILENAME_TEMPLATE)
        self._template_entry = ctk.CTkEntry(
            self._row1, textvariable=self._template_var, width=168
        )
        self._template_entry.pack(side="left", padx=2)
        # 失焦时归一化：空模板会生成没有主名的文件，必须回落到默认
        self._template_entry.bind("<FocusOut>", lambda e: self._on_template_changed())

        ctk.CTkFrame(self._row1, width=1, fg_color=("gray70", "gray30")).pack(
            side="left", fill="y", padx=8
        )

        ctk.CTkLabel(self._row1, text="格式:").pack(side="left", padx=(0, 2))
        self._format_var = ctk.StringVar(value="自动")
        self._format_menu = ctk.CTkOptionMenu(
            self._row1, variable=self._format_var,
            values=["自动", "PNG", "JPEG", "PDF"], width=78,
            command=self._on_format_changed,
        )
        self._format_menu.pack(side="left", padx=2)

        # 仅格式=PDF 时出现：把整批合成一个多页 PDF
        self._merge_pdf_var = ctk.BooleanVar(value=False)
        self._merge_pdf_cb = ctk.CTkCheckBox(
            self._row1, text="合并为单个 PDF", variable=self._merge_pdf_var,
            width=112, font=("Microsoft YaHei", 11),
        )
        self._merge_pdf_cb.pack(side="left", padx=(6, 2))
        self._merge_pdf_cb.pack_forget()  # 非 PDF 格式时隐藏

        # ===== 第二行：同名策略 / 覆盖原图 / 质量 / 进度 / 执行 =====
        row2 = ctk.CTkFrame(bottom_bar, fg_color="transparent")
        row2.pack(fill="x", padx=6, pady=(3, 5))

        ctk.CTkLabel(row2, text="同名文件:").pack(side="left", padx=(2, 2))
        self._conflict_var = ctk.StringVar(value=CONFLICT_LABELS[CONFLICT_RENAME])
        self._conflict_menu = ctk.CTkOptionMenu(
            row2, variable=self._conflict_var,
            values=list(CONFLICT_LABELS.values()), width=112,
        )
        self._conflict_menu.pack(side="left", padx=2)

        # 覆盖原图：危险操作，勾选后开始处理时还会再确认一次
        self._overwrite_original_var = ctk.BooleanVar(value=False)
        self._overwrite_original_cb = ctk.CTkCheckBox(
            row2, text="覆盖原图", variable=self._overwrite_original_var,
            width=92, font=("Microsoft YaHei", 11),
            fg_color="#ef4444", hover_color="#dc2626",
        )
        self._overwrite_original_cb.pack(side="left", padx=(8, 2))

        self._template_hint_label = ctk.CTkLabel(
            row2, text="模板变量: {original} {date} {time} {seq}",
            text_color="gray", font=("Microsoft YaHei", 9)
        )
        self._template_hint_label.pack(side="left", padx=(8, 2))

        ctk.CTkLabel(row2, text="质量:").pack(side="left", padx=(8, 2))
        self._quality_var = ctk.IntVar(value=DEFAULT_JPEG_QUALITY)
        self._quality_slider = ctk.CTkSlider(
            row2, from_=60, to=100, variable=self._quality_var, width=70,
            command=lambda v: self._quality_value_label.configure(text=str(int(v)))
        )
        self._quality_slider.pack(side="left", padx=2)
        self._quality_value_label = ctk.CTkLabel(
            row2, text=str(DEFAULT_JPEG_QUALITY), width=24,
            font=("Microsoft YaHei", 11)
        )
        self._quality_value_label.pack(side="left", padx=2)

        self._progress_bar = ctk.CTkProgressBar(row2, width=170)
        self._progress_bar.pack(side="left", padx=5)
        self._progress_bar.set(0)
        self._progress_bar.pack_forget()

        self._status_label = ctk.CTkLabel(row2, text="", anchor="w")
        self._status_label.pack(side="left", padx=5)

        self._start_btn = ctk.CTkButton(
            row2, text="开始处理", width=100,
            command=self._on_start_processing,
            fg_color="#22c55e", hover_color="#16a34a",
        )
        self._start_btn.pack(side="right", padx=5)

        self._cancel_btn = ctk.CTkButton(
            row2, text="取消", width=70,
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

        # 安全网：窗口重新出现或获得焦点时，清掉挂在不可见窗口上的僵尸抓取
        self.bind("<Map>", self._release_stale_grab)
        self.bind("<FocusIn>", self._release_stale_grab)

    def _schedule_layout_save(self, event=None):
        """防抖保存布局（500ms 后执行）"""
        if self._layout_save_timer:
            self.after_cancel(self._layout_save_timer)
        self._layout_save_timer = self.after(500, self._save_layout)

    def _on_window_close(self):
        """窗口关闭时保存布局 + 清理会话。

        处理中必须先确认：调度器跑在 daemon 线程上，直接 destroy 会把线程
        连同未完成的任务一起截断 —— 用户以为"关掉就停了"，实际上刚写了一半的
        文件可能还在，剩下的静默消失，而且不会有任何提示。
        """
        if getattr(self, "_processing", False):
            if not messagebox.askyesno(
                "正在处理",
                "批处理还没有结束。\n\n"
                "现在关闭会中断剩余任务：已经写出的文件会保留，"
                "正在处理的那个不会留下半成品。\n\n"
                "确定要退出吗？",
                parent=self,
            ):
                return
            # 先请求取消再退出，让运行中的任务在可中断点尽早收手
            self._cancel_requested = True

        self._save_layout()
        self._clear_session()
        self.destroy()

    def _release_stale_grab(self, event=None):
        """
        若模态抓取落在了一个不可见的窗口上，主动释放它。

        为什么需要：抓取一旦挂到「已隐藏/已最小化」的窗口上，
        Tk 会把应用的全部输入事件都送给那个看不见的窗口，
        于是主窗口看着正常却完全点不动、从任务栏也唤不醒。
        这里在窗口重新映射或获得焦点时兜底清理，避免任何一处
        （包括第三方控件内部）留下这种状态。
        """
        try:
            grabber = self.grab_current()
        except Exception:
            return
        if grabber is None:
            return
        try:
            alive = bool(grabber.winfo_exists())
            visible = bool(grabber.winfo_viewable()) if alive else False
        except Exception:
            alive, visible = False, False
        if alive and visible:
            return
        try:
            grabber.grab_release()
        except Exception:
            pass

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
                # 同理：IntVar.set 不会触发 CTkSlider 的 command，
                # 不手动同步的话滑块已经动了、旁边的数字还停在 90
                self._quality_value_label.configure(text=str(int(quality)))

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
                        log_exception("恢复参数作用域失败", e)
                        self._init_scope_base()
                else:
                    self._init_scope_base()

                self._refresh_scope_marks()
                self._refresh_scope_ui()
                self._status_label.configure(text="已恢复上次会话，可重新开始处理")

            # 无论是否恢复，都清除会话文件
            self._clear_session()

        except Exception as exc:
            # 以前这里是静默吞掉的：恢复失败与"没有可恢复的会话"对用户来说
            # 看着一模一样，但日志里必须留下痕迹 —— 否则用户反馈
            # "提示说恢复了、结果什么都没有"时完全无从查证
            log_exception("恢复上次会话失败", exc)
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
        """保存当前布局比例与主题到 ui_layout.json"""
        try:
            total_w = self._hpaned.winfo_width()
            total_h = self._vpaned.winfo_height()

            layout = {}

            # 窗口尺寸还没确定时（隐藏、最小化、首次布局之前，winfo 返回 1）
            # 跳过几何部分 —— 但主题必须照常写入：它和窗口大小无关，
            # 不该因为"面板还没量出宽度"就一起丢掉。
            # 旧实现是直接 return，于是"只切主题、不动面板就关窗"时选择留不下来。
            if total_w > 1 and total_h > 1:
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

            # 主题与布局同存一份，都是"下次启动该恢复的界面偏好"
            layout[APPEARANCE_KEY] = ctk.get_appearance_mode()

            # 读-改-写：几何被跳过时不能把上一次存下来的窗口大小/位置抹掉
            merged = {}
            try:
                with open(LAYOUT_FILE, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged = loaded
            except Exception:
                merged = {}
            merged.update(layout)

            os.makedirs(os.path.dirname(LAYOUT_FILE), exist_ok=True)
            with open(LAYOUT_FILE, 'w') as f:
                json.dump(merged, f)
        except Exception:
            pass

    # ============================================================
    # 配置联动 / 三层参数作用域
    # ============================================================

    def _full_panel_dict(self) -> dict | None:
        """
        读取「两个面板合起来」的完整参数字典。

        为什么必须合并，而不是只取当前 Tab：
        1. 作用域存的是一份完整参数 —— ScopeStore.set_file/set_page 需要完整参数
           才能算出"与上一层不同的字段"。只提交当前 Tab，会把另一个 Tab 刚设的
           专属改动从该层挤掉；
        2. watermark_type 必须由两个面板的实际内容推导，不能采纳"当前 Tab 的类型"。
           两个面板各自固定返回 "text" / "image"，若直接采纳，用户在图片页配好
           Logo 后切到文字页看一眼，类型就会被写成 text，Logo 被静默关掉
           （预览和导出都会没有 Logo）。

        :return: 完整参数字典；两个面板都读不到时返回 None
        """
        merged: dict = {}
        found = False
        for panel in (self._text_panel, self._image_panel):
            try:
                data = panel.get_config_dict()
            except Exception as e:
                log_exception("读取面板参数失败", e)
                continue
            if not data:
                continue
            found = True
            merged.update(data)
            # 输入值被修正时在状态栏提示（越界夹取 / 非法回退）
            notices = getattr(panel, 'last_notices', None)
            if notices and hasattr(self, '_status_label'):
                self._status_label.configure(text="；".join(notices))

        if not found:
            return None

        merged["watermark_type"] = self._derived_watermark_type(merged)
        return merged

    @staticmethod
    def _derived_watermark_type(full: dict) -> str:
        """
        由面板内容推导水印类型（而不是"当前 Tab 是哪个"）。

        - 有文字 + 有 Logo → both
        - 只有 Logo        → image（文字为空时不该把配好的 Logo 一起关掉）
        - 其余             → text
        """
        text = str(full.get("text", "") or "").strip()
        logo = str(full.get("logo_path", "") or "").strip()
        if text and logo:
            return "both"
        if logo and not text:
            return "image"
        return "text"

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
        """把面板当前参数（两个 Tab 合并）合并进「当前作用层」"""
        if self._syncing:
            return  # 回填面板期间不算用户编辑，否则会把继承值写死在该层
        data = self._full_panel_dict()
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
            log_exception("提交参数到作用域失败", e)
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
        获取当前生效的完整 WatermarkConfig（两个面板合并 + 底部栏输出参数）。

        由 PreviewCanvas.on_config_changed 回调调用。这里必须与导出用的那份
        参数同源（同样是"两个面板合并 + 三层作用域"），否则预览与导出会不一致。
        """
        try:
            data = self._full_panel_dict()
            if data is None:
                return None

            # 补充底部栏的通用参数
            from src.batch_planner import normalize_format
            data["output_format"] = normalize_format(self._format_var.get())
            data["jpeg_quality"] = self._quality_var.get()
            output_dir = self._output_dir_var.get().strip()

            if output_dir and os.path.isdir(output_dir):
                data["output_dir"] = output_dir

            return WatermarkConfig.from_dict(data)
        except Exception as e:
            log_exception("读取水印配置失败", e)
            return None

    def _init_scope_base(self):
        """
        把两个面板的初始状态登记为全局默认。

        用合并后的完整参数整体替换，而不是分别合并两个面板：
        分别合并时 watermark_type 会取"最后写入的那个面板"的类型，
        与用户实际配置无关。
        """
        try:
            data = self._full_panel_dict()
            if data:
                self._scopes.set_base(data)
        except Exception as e:
            log_exception("初始化参数作用域失败", e)
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
            log_exception("同步参数到面板失败", e)
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

    def _apply_theme(self, mode: str, save: bool = True):
        """
        应用主题并同步所有依赖颜色的自绘部件。

        customtkinter 只负责自己的控件：PanedWindow 的分隔线和预览画布底色
        都是我们自己的，必须手动跟随 —— 否则浅色模式下会留下一道深色分隔线。
        """
        mode = "Light" if str(mode).lower().startswith("light") else "Dark"
        ctk.set_appearance_mode(mode)

        # 同步 PanedWindow 分隔线颜色
        paned_bg = "#f0f0f0" if mode == "Light" else "#2b2b2b"
        try:
            self._vpaned.configure(bg=paned_bg)
            self._hpaned.configure(bg=paned_bg)
        except Exception:
            pass

        # 同步预览画布颜色
        if hasattr(self, '_preview_canvas'):
            self._preview_canvas.update_theme()

        if save:
            # 立即落盘：否则"切了主题但没正常关窗"（崩溃 / 强杀）就白切了
            self._save_layout()

    def _toggle_theme(self):
        """切换深色/浅色主题 + 同步原生控件颜色"""
        target = "Light" if ctk.get_appearance_mode() == "Dark" else "Dark"
        self._apply_theme(target)

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
        # 档位也要回落：没有文件就没有"本文件/本页"，否则面板会停在
        # 一个不存在的目标上，后续编辑会写入一个空路径的层
        self._scope = SCOPE_ALL
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
            self._refresh_output_hint()

    # ---------- 输出设置（文件名模板 / 同名策略 / 覆盖原图） ----------

    def _on_template_changed(self):
        """
        归一化文件名模板。

        模板留空会生成"只有扩展名的文件"（例如 .jpg），用户在资源管理器里
        根本看不出这是哪张图。这里直接回落到默认模板并提示。
        """
        raw = self._template_var.get().strip()
        if not raw:
            self._template_var.set(DEFAULT_FILENAME_TEMPLATE)
            self._status_label.configure(
                text=f"文件名模板不能为空，已回落到 {DEFAULT_FILENAME_TEMPLATE}"
            )
        elif raw != self._template_var.get():
            self._template_var.set(raw)

    def _conflict_mode(self) -> str:
        """当前同名冲突策略（内部标识）"""
        label = self._conflict_var.get()
        for key, text in CONFLICT_LABELS.items():
            if text == label:
                return key
        return CONFLICT_RENAME

    def _is_overwrite_original(self) -> bool:
        """是否勾选了"覆盖原图" """
        try:
            return bool(self._overwrite_original_var.get())
        except Exception:
            return False

    def _refresh_output_hint(self):
        """
        刷新输出目录提示，使提示文案与实际行为一致。

        以前提示固定写「留空→原图目录\\watermarked_output」，但多目录输入时
        实际会被统一丢到桌面「水印输出」—— 用户按提示理解就会找错地方。
        现在直接问 resolve_output_target 要结论，提示与执行同源。
        """
        try:
            files = self._file_list.files
            chosen = self._output_dir_var.get().strip()
            merge = bool(self._merge_pdf_var.get())
            _, message = resolve_output_target(files, chosen, merge)
            if not message:
                message = "输出到指定目录"
                if self._is_overwrite_original():
                    message = "⚠ 覆盖原图：输出即源文件（会先确认）"
                elif merge:
                    message = "输出到指定目录（合并为单个 PDF）"
            self._output_hint_label.configure(text=message)
        except Exception:
            pass

    def _confirm_overwrite_original(self, files) -> bool:
        """
        「覆盖原图」的二次确认。

        为什么要两道确认：
        这个选项是**不可逆**的 —— 水印一旦写回源文件，原图就没有了。
        批量场景下一次误勾可能毁掉整批素材，所以既要列出数量与目录，
        还要用户确认所选的存储位置确实是自己的素材（而不是产出的成品）。
        """
        from tkinter import messagebox

        count = len(files)
        groups = {}
        for f in files:
            groups[os.path.dirname(f)] = groups.get(os.path.dirname(f), 0) + 1
        dirs = "\n".join(f"  {d}（{n} 张）" for d, n in list(groups.items())[:8])
        if len(groups) > 8:
            dirs += f"\n  ... 另外 {len(groups) - 8} 个目录"

        first = messagebox.askyesno(
            "确认覆盖原图",
            f"你勾选了「覆盖原图」。\n\n"
            f"即将处理的 {count} 个文件的**原图会被直接改写**，"
            f"处理完成后无法还原：\n\n{dirs}\n\n"
            f"确认要继续吗？",
            parent=self,
        )
        if not first:
            return False

        second = messagebox.askyesno(
            "最后确认",
            "请再确认一次：\n\n原图将被覆盖，没有备份。\n\n"
            "如果这批文件本身就是需要长期保存的素材，建议先取消，"
            "改成输出到单独目录。\n\n确定继续覆盖吗？",
            parent=self,
        )
        return bool(second)

    def _update_count(self):
        """更新文件计数显示"""
        count = self._file_list.count
        self._count_label.configure(text=f"共 {count} 个文件")
        self._refresh_output_hint()

    def _write_failure_report(self, output_dir: str, failures: list) -> str:
        """
        把失败清单写到输出目录，返回文件路径（无失败返回空字符串）。

        为什么单独落一个文件：
        exe 没有控制台，状态栏一行字几秒后就被下一次操作覆盖，
        用户想回头查"到底是哪几张失败了、为什么"时无从查起。
        """
        if not failures:
            return ""
        try:
            from datetime import datetime

            target_dir = output_dir or os.path.expanduser("~")
            os.makedirs(target_dir, exist_ok=True)
            name = f"失败清单_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            path = os.path.join(target_dir, name)

            lines = [
                "批量图片水印工具 — 失败清单",
                f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"失败数量: {len(failures)}",
                f"日志文件: {log_path()}",
                "-" * 60,
            ]
            for index, (filename, reason) in enumerate(failures, start=1):
                lines.append(f"{index}. {filename}")
                lines.append(f"   原因: {reason}")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            return path
        except Exception as exc:
            log_exception("写失败清单失败", exc)
            return ""

    def _on_start_processing(self):
        """
        开始处理 —— 先在后台把慢 IO 预检跑完，再进入批处理。

        为什么分两步：预检要逐个读文件头（判断像素规模），还要检查每个 PDF
        的数字签名，上千个文件时是几秒的纯 IO。放在主线程会直接冻结界面，
        用户只会看到"点了没反应"。
        """
        import threading

        from src import resource_budget

        files = list(self._file_list.files)
        if not files:
            self._show_message("请先添加图片文件")
            return

        # 预检期间先禁掉开始按钮，避免连点导致并发预检；
        # 同时打开取消按钮 —— 检查上千个文件也要几秒，中途应该能反悔
        self._cancel_requested = False
        self._processing = True          # 预检也算"在处理"，关窗时要拦一下
        self._start_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._status_label.configure(text=f"正在检查 {len(files)} 个文件…")
        self.update_idletasks()

        chosen_dir = self._output_dir_var.get().strip()
        merge_pdf = (
            bool(self._merge_pdf_var.get()) and self._format_var.get() == "PDF"
        )
        scan_dir, _note = resolve_output_target(files, chosen_dir, merge_pdf)

        def _scan_worker():
            import shutil

            report = {
                "oversized": resource_budget.oversized_files(files),
                "large": resource_budget.large_images(files),
                "signed": [],
                "total_input_size": 0,
                "free_space": None,
            }
            try:
                from src.pdf_engine import signed_pdf_files
                report["signed"] = signed_pdf_files(files)
            except Exception as exc:
                log_exception("PDF 签名预检失败", exc)
            try:
                space_root = scan_dir or os.path.expanduser("~")
                report["total_input_size"] = sum(
                    os.path.getsize(f) for f in files if os.path.exists(f)
                )
                report["free_space"] = shutil.disk_usage(space_root).free
            except Exception:
                pass        # 预检失败不阻止处理，后面的分支会各自兜底

            self.after(0, lambda r=report: self._run_batch(files, r))

        threading.Thread(target=_scan_worker, daemon=True).start()

    def _run_batch(self, files, preflight):
        """批处理主流程（此时预检结果已就绪）"""
        import gc
        import threading
        import time
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
        from concurrent.futures import wait as futures_wait
        from tkinter import messagebox

        from src import resource_budget
        from src.batch_planner import (
            TaskCancelled,
            execute_task,
            plan_output_paths,
        )

        # 预检期间临时禁用了开始按钮，这里先恢复 ——
        # 下面的 early return 分支依赖"按钮处于可用状态"这个前提，
        # 不恢复的话任何一次提前返回都会让按钮永久卡在 disabled
        self._start_btn.configure(state="normal")
        # 预检阶段到此结束：真正的"处理中"由第 7 步重新标记，
        # 这样下面任何一次 early return 都不会把状态卡在"处理中"
        self._processing = False

        # 预检期间用户可能已经点了取消，那就别再往下走
        if self._cancel_requested:
            self._cancel_requested = False
            self._cancel_btn.configure(state="disabled")
            self._status_label.configure(text="已取消（尚未开始处理）")
            return

        # ---------- 1. 前置检查 ----------

        # 输出目录的行为统一由 resolve_output_target 决定：
        # 返回空字符串 = 每个文件写到各自源目录下的 watermarked_output/
        # （旧实现会把多目录输入统一丢到桌面「水印输出」，与界面提示不符）
        chosen_dir = self._output_dir_var.get().strip()
        if chosen_dir and not os.path.isdir(chosen_dir):
            chosen_dir = ""
        merge_pdf = bool(self._merge_pdf_var.get()) and self._format_var.get() == "PDF"
        output_dir, output_note = resolve_output_target(files, chosen_dir, merge_pdf)

        if output_dir:
            try:
                os.makedirs(output_dir, exist_ok=True)
            except Exception as exc:
                self._show_message(f"无法创建输出目录：\n{output_dir}\n\n{exc}")
                return
            self._output_dir_var.set(output_dir)

        if output_note:
            self._status_label.configure(text=output_note)
        self._last_output_dir = output_dir or "（各源文件目录）"

        config = self._get_current_config()
        if config is None:
            self._show_message("配置错误，无法获取水印设置")
            return

        template = (self._template_var.get() or DEFAULT_FILENAME_TEMPLATE).strip()
        conflict = self._conflict_mode()
        overwrite_original = self._is_overwrite_original()

        # ---------- 2. 覆盖原图：两次确认（不可逆操作） ----------
        if overwrite_original and not self._confirm_overwrite_original(files):
            self._status_label.configure(text="已取消：未覆盖原图")
            return

        # ---------- 3. 像素上限：超限直接剔除，不交给 Pillow 去撞内存 ----------
        #    扫描已在后台预检里完成（见 _on_start_processing），这里只处理结果
        oversized = preflight.get("oversized") or []
        if oversized:
            detail = "\n".join(
                f"  {os.path.basename(p)} — {px / 1e6:.0f} 百万像素"
                for p, px in oversized[:10]
            )
            if len(oversized) > 10:
                detail += f"\n  ... 另外 {len(oversized) - 10} 个"
            self._show_message(
                "以下文件超过安全像素上限"
                f"（{resource_budget.MAX_IMAGE_PIXELS / 1e6:.0f} 百万像素），已跳过：\n\n"
                f"{detail}\n\n"
                "这类文件解码后需要数 GB 内存，处理它极可能让程序直接崩溃。"
            )
            log_info(f"超限跳过 {len(oversized)} 个文件")
            blocked = {p for p, _ in oversized}
            files = [f for f in files if f not in blocked]
            if not files:
                self._status_label.configure(text="全部文件都超过像素上限，未处理")
                return

        # ---------- 3.1 大图预警：给量级，让用户自己决定 ----------
        large = preflight.get("large") or []
        if large:
            peak_gb = sum(item[3] for item in large) * (
                resource_budget.BYTES_PER_PIXEL * resource_budget.TASK_SLACK
            ) / (1024 ** 3)
            detail = "\n".join(
                f"  {os.path.basename(p)} — {w}×{h}（{px / 1e6:.0f} 百万像素）"
                for p, w, h, px in large[:8]
            )
            if len(large) > 8:
                detail += f"\n  ... 另外 {len(large) - 8} 个"
            if not messagebox.askyesno(
                "大图内存预警",
                f"这批文件里有 {len(large)} 张高分辨率图片：\n\n{detail}\n\n"
                f"处理这些图预计峰值内存约 {peak_gb:.1f} GB。\n"
                "程序会按可用内存自动降低并行度，但仍可能需要较长时间。\n\n"
                "是否继续？",
                parent=self,
            ):
                self._status_label.configure(text="已取消（大图内存预警）")
                return

        # ---------- 3.2 磁盘空间预检 ----------
        #    文件总大小与可用空间都在后台预检里量好了，这里只做判断
        try:
            space_root = output_dir or os.path.expanduser("~")
            total_input_size = preflight.get("total_input_size") or 0
            free_space = preflight.get("free_space")
            needed_size = int(total_input_size * 1.2)  # 预留 20% 余量
            if free_space is not None and free_space < needed_size:
                self._show_message(
                    f"磁盘空间不足！\n\n"
                    f"预计需要: {needed_size / (1024 * 1024):.0f} MB\n"
                    f"可用空间: {free_space / (1024 * 1024):.0f} MB\n"
                    f"输出位置: {space_root}"
                )
                return
        except Exception:
            pass  # 检查失败不阻止处理

        # ---------- 3.3 PDF 数字签名预检 ----------
        # 加水印是改写文件，签名必然失效。必须先说清楚，不能悄悄弄坏
        # （扫描在后台预检里做：检查签名要解析 /Root/Perms 与 AcroForm）
        signed = preflight.get("signed") or []
        if signed:
            detail = "\n".join(f"  {os.path.basename(p)}" for p in signed[:8])
            if len(signed) > 8:
                detail += f"\n  ... 另外 {len(signed) - 8} 个"
            if not messagebox.askyesno(
                "PDF 数字签名将失效",
                f"以下 {len(signed)} 个 PDF 带数字签名：\n\n{detail}\n\n"
                "处理后的文件签名会失效，阅读器会提示「文档已被修改」。\n"
                "这是无法避免的（重新签名需要你的私钥）。\n\n"
                "是否继续处理？",
                parent=self,
            ):
                self._status_label.configure(text="已取消（保留 PDF 签名）")
                return

        # ---------- 4. 生成任务计划与输出路径 ----------
        # 决策逻辑全部在 src/batch_planner.py（无 GUI 依赖，可被测试直接覆盖）
        plan = self._build_batch_plan(files)
        specs = plan.tasks
        out_format = plan.out_format
        merge_pdf = plan.merge
        quality = self._quality_var.get()

        output_paths, merged_path = plan_output_paths(
            plan, output_dir,
            filename_template=template,
            on_conflict=conflict,
            overwrite_original=overwrite_original,
        )

        if merge_pdf and not merged_path:
            self._show_message("无法确定合并输出的目标路径")
            return

        # 冲突策略 = 跳过时，部分任务会被标成 None（不处理）
        skipped = sum(1 for p in output_paths if not p)
        active = [
            (index, task)
            for index, (task, path) in enumerate(zip(specs, output_paths))
            if path
        ]
        if not merge_pdf and not active:
            self._show_message("所有输出文件都已存在，且当前策略是「跳过」。")
            return

        log_info(
            "批次开始：文件 %d 个，输出 %s，格式 %s，模板 %s，同名策略 %s%s"
            % (
                len(files), self._last_output_dir, out_format, template, conflict,
                "，覆盖原图" if overwrite_original else "",
            )
        )

        # ---------- 5. 并行度：按解码后的像素内存预算决定 ----------
        # 旧实现按"压缩文件大小"分档（>15MB 限 2 线程）。文件大小与内存占用
        # 并没有稳定关系：200KB 的 10000×7000 JPEG 解出来就是 280MB，
        # 而 5MB 的 1080p PNG 只有 8MB —— 按大小分档会在高分辨率场景下
        # 开出过多线程，几张大图并发就能把内存吃光。
        estimates = [
            resource_budget.file_memory_estimate(task.path) for _, task in active
        ]
        num_workers, worker_reason = resource_budget.plan_workers(estimates)
        log_info(f"并行度决策：{worker_reason}")

        # ---------- 6. 更新 UI 状态 ----------
        self._start_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress_bar.pack()
        self._progress_bar.set(0)
        hint = "合并中..." if merge_pdf else "处理中..."
        if skipped:
            hint += f"（{skipped} 个同名文件将跳过）"
        self._status_label.configure(text=hint)

        # ---------- 7. 会话与进度状态 ----------
        self._cancel_requested = False
        self._processing = True          # 关窗时要据此确认，别把工作线程无声截断
        self._last_failure_report = ""
        self._save_session()
        self._batch_start_time = time.time()

        # 覆盖层倍率快照：整批固定用开始这一刻的值。
        # 任务是在线程池里跑的，若让 pdf_engine 各自去读全局倍率，
        # 用户在处理途中拖动倍率滑块就会让同一份 PDF 的不同页
        # 按不同参考尺寸渲染 —— 同一个文件里出现两种水印大小。
        from src.pdf_engine import get_overlay_scale
        overlay_scale_snapshot = get_overlay_scale()

        # 任务结果三态：取消不算失败，否则用户分不清"我取消了"和"程序出错了"
        STATUS_OK = "ok"
        STATUS_CANCELLED = "cancelled"
        STATUS_FAILED = "failed"

        # ---------- 8. 单个文件处理函数（在工作线程中执行） ----------
        #    具体的分支决策在 src/batch_planner.execute_task 里，这里只管调度与异常
        def _process_one(index, out_path):
            task = specs[index]
            name = os.path.basename(task.path)
            try:
                execute_task(
                    task, out_path, jpeg_quality=quality,
                    should_cancel=lambda: self._cancel_requested,
                    overlay_scale=overlay_scale_snapshot,
                )
                return (index, STATUS_OK, None, name)
            except TaskCancelled:
                # 用户在可中断点退出：原子写出已保证不留半成品，这里不计入失败
                return (index, STATUS_CANCELLED, None, name)
            except Exception as exc:
                log_exception(f"处理失败 {name}", exc)
                return (index, STATUS_FAILED, str(exc), name)

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
                    overlay_scale=overlay_scale_snapshot,
                )
            except MergeCancelled:
                # 用户取消：原子写出保证不留半成品
                _elapsed = time.time() - start_time
                self.after(0, lambda e=_elapsed: _on_done(
                    0, elapsed=e, was_cancelled=True, merged_pages=0,
                    output_dir=output_dir,
                ))
                return
            except Exception as exc:
                log_exception("合并 PDF 失败", exc)
                _elapsed = time.time() - start_time
                self.after(0, lambda e=_elapsed, m=str(exc): _on_done(
                    0, elapsed=e, merged_pages=0, fatal=m, output_dir=output_dir,
                ))
                return

            _elapsed = time.time() - start_time
            self.after(0, lambda e=_elapsed, p=pages: _on_done(
                total, elapsed=e, merged_pages=p, output_dir=output_dir,
            ))

        # ---------- 9. 调度器线程 — 有界滑动窗口 + 结果收集 + 进度更新 ----------
        def _dispatcher():
            """
            调度线程入口 —— 保证无论成功、失败还是异常，都一定走到 _on_done。

            为什么外面还要再包一层：_on_done 是唯一把「开始」按钮恢复可用、
            清掉取消标志的地方。工作线程里任何一个没被内层捕获的异常
            （文件系统报错、内存不足、第三方库意外抛出）都会让它不执行 ——
            界面从此停在「处理中」，用户只能重启程序，而且不知道发生了什么。
            真正的调度逻辑在 _dispatch_impl，这里只负责兜底。
            """
            try:
                _dispatch_impl()
            except Exception as exc:
                log_exception("批处理调度异常", exc)
                self.after(0, lambda m=str(exc): _on_done(
                    0, elapsed=0.0, fatal=f"处理中断：{m}",
                    output_dir=self._last_output_dir or "",
                ))

        def _dispatch_impl():
            if merge_pdf:
                _merge_all()
                return

            total_active = len(active)
            success_count = 0
            cancelled_count = 0
            failures = []            # [(文件名, 原因)]
            start_time = time.time()

            # 失败清单落盘位置：优先用户指定目录；多目录模式落到第一个源文件所在目录
            report_dir = output_dir or (
                os.path.dirname(active[0][1].path) if active else ""
            )

            # 进度节流：双阈值（200ms OR 10 张）
            last_progress_time = time.time()
            last_progress_done = 0
            PROGRESS_TIME_MS = 200
            PROGRESS_COUNT = 10

            def _report_progress(done, filename):
                nonlocal last_progress_time, last_progress_done
                now = time.time()
                if (now - last_progress_time) * 1000 >= PROGRESS_TIME_MS \
                        or done - last_progress_done >= PROGRESS_COUNT \
                        or done >= total_active:
                    progress = done / total_active if total_active else 1.0
                    self.after(0, lambda p=progress, d=done, fn=filename:
                               self._update_progress(p, d, total_active, fn))
                    last_progress_time = now
                    last_progress_done = done

            # 有界滑动窗口：在飞任务数始终不超过并行度。
            # 旧实现一次性 submit 全部任务，队列里排队的任务在取消后仍会被逐个启动，
            # 于是"取消"要等整批跑完才生效 —— 大批量高分辨率图下会白烧几十分钟。
            inflight = max(1, num_workers)
            pending = set()
            cursor = 0

            def _fill_window(executor):
                nonlocal cursor
                while cursor < total_active and len(pending) < inflight:
                    if self._cancel_requested:
                        return          # 取消后不再提交任何新任务
                    index, _task = active[cursor]
                    cursor += 1
                    pending.add(executor.submit(_process_one, index, output_paths[index]))

            with ThreadPoolExecutor(max_workers=inflight) as executor:
                _fill_window(executor)
                while pending:
                    done_set, pending = futures_wait(
                        pending, return_when=FIRST_COMPLETED
                    )
                    for future in done_set:
                        try:
                            _index, status, error, name = future.result()
                        except Exception as exc:      # 兜底：_process_one 自身崩了
                            log_exception("任务执行异常", exc)
                            status, error, name = STATUS_FAILED, str(exc), "?"
                        if status == STATUS_OK:
                            success_count += 1
                        elif status == STATUS_CANCELLED:
                            cancelled_count += 1
                        else:
                            failures.append((name, error))
                        processed = success_count + len(failures) + cancelled_count
                        _report_progress(processed, name)
                        # GC 控制：每 10 张手动回收
                        if processed % 10 == 0:
                            gc.collect()
                    _fill_window(executor)

            gc.collect()

            elapsed = time.time() - start_time
            self.after(0, lambda: _on_done(
                success_count,
                failures=failures,
                elapsed=elapsed,
                was_cancelled=self._cancel_requested,
                skipped=skipped,
                cancelled=cancelled_count,
                output_dir=report_dir,
                total=len(files),
            ))

        def _on_done(success, failures=(), elapsed=0.0, was_cancelled=False,
                     merged_pages=None, skipped=0, cancelled=0, fatal=None,
                     output_dir="", total=0):
            """处理完成回调（主线程）

            merged_pages 为 None 表示逐文件模式；为整数表示合并模式收尾，
            此时 success 记的是参与合并的输入数。
            failures: [(文件名, 原因)]；cancelled: 因取消而中断的数量；
            skipped: 因同名冲突策略而跳过的数量。
            """
            self._start_btn.configure(state="normal")
            self._cancel_btn.configure(state="disabled")
            self._cancel_requested = False
            self._processing = False
            self._clear_session()  # 正常完成，清除会话

            failures = list(failures or [])
            fail = len(failures)
            first_error = f"{failures[0][0]}: {failures[0][1]}" if failures else fatal

            # 失败清单落盘：exe 没有控制台，状态栏一行字转头就被覆盖，
            # 用户回头想查"哪几张失败了、为什么"只能靠这个文件
            self._last_failure_report = self._write_failure_report(output_dir, failures)
            if failures:
                log_info(f"批次结束：失败 {fail} 个，清单 {self._last_failure_report}")

            # ---------------- 合并模式收尾 ----------------
            if merged_pages is not None:
                if was_cancelled:
                    self._status_label.configure(text=f"已取消合并 ({elapsed:.1f}s)")
                    return
                if fatal:
                    self._status_label.configure(text=f"合并失败: {fatal}")
                    self._show_message(f"合并失败：\n\n{fatal}")
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
            bits = [f"成功 {success} 张"]
            if fail:
                bits.append(f"失败 {fail} 张")
            if cancelled:
                bits.append(f"已取消 {cancelled} 张")
            if skipped:
                bits.append(f"跳过 {skipped} 张")
            summary = ", ".join(bits)

            if was_cancelled:
                self._status_label.configure(text=f"已取消! {summary} ({elapsed:.1f}s)")
                if success > 0:
                    self._show_completion_dialog(
                        output_dir, success, fail,
                        note=f"{summary}（已取消）",
                        report=self._last_failure_report,
                    )
                return

            if success > 0:
                self._status_label.configure(text=f"完成! {summary} ({elapsed:.1f}s)")
                self._show_completion_dialog(
                    output_dir, success, fail,
                    note=summary, report=self._last_failure_report,
                )
                self._progress_bar.set(1)
            else:
                if fatal and not failures:
                    # 调度层崩溃 —— 与「每张都失败」是两回事，文案必须区分开，
                    # 否则用户会去逐个检查文件，而问题其实出在程序自己身上
                    self._status_label.configure(text=f"处理中断: {fatal}")
                    self._show_message(
                        f"处理中断：\n\n{fatal}\n\n"
                        "已经写出的输出文件仍然保留，程序日志里有完整堆栈。"
                    )
                elif first_error:
                    self._status_label.configure(text=f"全部失败: {first_error}")
                    extra = (f"\n\n完整失败清单：\n{self._last_failure_report}"
                             if self._last_failure_report else "")
                    self._show_message(f"没有成功处理的文件。\n\n首个错误：\n{first_error}{extra}")
                else:
                    self._status_label.configure(text="处理失败")
                    self._show_message("没有成功处理的文件")

        # 启动调度器线程（取消标志与会话已在第 7 步初始化）
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
                                note: str = None, report: str = None):
        """显示完成对话框，包含"打开文件夹"按钮

        :param note: 可选补充说明，会替换默认的「成功 N 张 | 失败 M 张」那行
                     （合并模式用它来显示输出页数）
        :param report: 可选失败清单路径；有失败时给用户一个明确的入口，
                       否则状态栏那行字被覆盖后就没法回溯了

        为什么不用模态抓取（grab_set）：
        - CTkToplevel 在构造时为了刷新标题栏颜色，会先 withdraw() 自己、
          再延迟 deiconify()。抓取如果落在"当前不可见"的窗口上，就会一直挂着；
        - 主窗口最小化时子对话框被系统置为 withdrawn，而抓取仍在它身上，
          此时应用的全部输入事件都会被吞掉 —— 表现就是窗口看着还在、
          点任务栏也唤不醒（实测 grab_current 始终指向那个 withdrawn 的对话框）；
        - 完成提示不需要阻塞操作，非模态反而更顺手（可以立刻改参数再跑一次）。
        """
        import subprocess
        import os

        # 单例：连续导出时不要叠出一堆对话框
        old = getattr(self, '_completion_dialog', None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.destroy()
            except Exception:
                pass
            self._completion_dialog = None

        dialog = ctk.CTkToplevel(self)
        self._completion_dialog = dialog
        dialog.title("处理完成")
        dialog.geometry("400x230")
        dialog.resizable(False, False)
        dialog.transient(self)

        # 居中显示
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 200
        y = self.winfo_y() + (self.winfo_height() // 2) - 115
        dialog.geometry(f"+{x}+{y}")

        # 没有抓取，就靠 lift 保证它不被主窗口挡在后面
        dialog.lift()
        dialog.after(80, dialog.lift)

        # 标题栏关闭按钮与「关闭」按钮走同一条收尾路径
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._close_completion_dialog(dialog))
        dialog.bind("<Destroy>", lambda e: self._forget_completion_dialog(e, dialog))

        # 内容
        ctk.CTkLabel(
            dialog,
            text=f"批量处理完成！",
            font=ctk.CTkFont(size=18, weight="bold")
        ).pack(pady=(25, 10))

        detail = note if note else f"成功: {success} 张  |  失败: {fail} 张"
        lines = [detail, f"输出目录: {output_dir}"]
        if report:
            lines.append(f"失败清单: {os.path.basename(report)}")
        ctk.CTkLabel(
            dialog,
            text="\n".join(lines),
            font=ctk.CTkFont(size=12),
            text_color=("gray20", "gray80"),
            justify="left",
            wraplength=360,
        ).pack(pady=5)

        # 按钮区
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=20)

        # 输出目录可能是空字符串（多目录输入 + 留空），此时不提供"打开文件夹"
        open_dir = output_dir if output_dir and os.path.isdir(output_dir) else ""
        if open_dir:
            ctk.CTkButton(
                btn_frame, text="打开输出文件夹", width=140,
                command=lambda d=open_dir: (
                    os.startfile(os.path.abspath(d)) if os.name == 'nt'
                    else subprocess.run(["xdg-open", d])
                )
            ).pack(side="left", padx=10)

        if report and os.path.exists(report):
            ctk.CTkButton(
                btn_frame, text="查看失败清单", width=120,
                fg_color="#8B3A3A", hover_color="#A04545",
                command=lambda p=report: (
                    os.startfile(os.path.abspath(p)) if os.name == 'nt'
                    else subprocess.run(["xdg-open", p])
                )
            ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame, text="关闭", width=80,
            command=lambda: self._close_completion_dialog(dialog)
        ).pack(side="left", padx=10)

    def _close_completion_dialog(self, dialog):
        """关闭完成对话框，并把焦点交回主窗口"""
        # 主动释放可能存在的抓取：万一将来有人给它加了模态，
        # 也不会留下"抓取挂在已销毁窗口上"的僵尸状态
        try:
            if self.grab_current() is dialog:
                dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass
        if getattr(self, '_completion_dialog', None) is dialog:
            self._completion_dialog = None
        try:
            self.lift()
            self.focus_set()
        except Exception:
            pass

    def _forget_completion_dialog(self, event, dialog):
        """对话框被销毁（含用户点标题栏 X）时清掉单例引用"""
        if getattr(event, 'widget', None) is not dialog:
            return
        if getattr(self, '_completion_dialog', None) is dialog:
            self._completion_dialog = None


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    app = AppWindow()
    app.mainloop()
