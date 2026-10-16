"""
文字水印设置面板

功能：
- 文字内容（多行输入 CTkTextbox）
- 字体选择（下拉 + 浏览按钮）
- 字号（相对短边百分比滑块 1%-20%）
- 颜色 + 透明度（颜色选择器 + 滑块）
- 位置九宫格选择器（3x3 按钮组）
- 边距滑块
- 旋转角度滑块
- 平铺模式 Checkbox（勾选时禁用九宫格+边距）
- 平铺参数（间距、错位）

交互联动：
- 勾选平铺 → 九宫格按钮组和边距滑块 disabled
- 取消平铺 → 九宫格和边距恢复可用
- 所有控件变更 → 触发 on_config_changed 回调
"""

from __future__ import annotations

import customtkinter as ctk
from typing import Callable, Optional

from src.config import (
    WatermarkConfig, format_percent, normalize_position, opacity_to_alpha,
)
from src.ui import theme

# 九宫格位置定义
_POSITIONS = [
    ("top-left", "左上"), ("top-center", "上中"), ("top-right", "右上"),
    ("middle-left", "左中"), ("center", "正中"), ("middle-right", "右中"),
    ("bottom-left", "左下"), ("bottom-center", "下中"), ("bottom-right", "右下"),
]


class TextWatermarkPanel(ctk.CTkFrame):
    """文字水印设置面板"""

    def __init__(
        self,
        master,
        on_config_changed: Optional[Callable] = None,
        **kwargs
    ):
        super().__init__(master, **kwargs)

        self.on_config_changed = on_config_changed or (lambda: None)
        self._loading = False  # 预设加载时暂停通知

        # 内部状态
        self._selected_position: str = "bottom-right"
        self._position_buttons: dict[str, ctk.CTkButton] = {}

        self._build_ui()

    def _build_ui(self):
        """构建面板 UI"""
        # 标题
        title = ctk.CTkLabel(self, text="文字水印设置", font=ctk.CTkFont(size=14, weight="bold"))
        title.pack(anchor="w", padx=10, pady=(10, 5))

        # ---- 文字内容 ----
        ctk.CTkLabel(self, text="文字内容").pack(anchor="w", padx=10)
        self._text_box = ctk.CTkTextbox(
            self,
            height=60,
            font=ctk.CTkFont(family="Microsoft YaHei", size=12),
        )
        self._text_box.pack(fill="x", padx=10, pady=(0, 8))
        self._text_box.bind("<KeyRelease>", lambda e: self._notify_change())

        # ---- 字体选择 ----
        font_frame = ctk.CTkFrame(self, fg_color="transparent")
        font_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(font_frame, text="字体:").pack(side="left")

        # 预设字体列表（自动探测系统中文字体）
        self._font_var = ctk.StringVar(value="微软雅黑 (默认)")
        self._font_menu = ctk.CTkOptionMenu(
            font_frame,
            variable=self._font_var,
            values=["微软雅黑 (默认)", "黑体 (SimHei)", "自定义..."],
            width=140,
            command=self._on_font_selected,
        )
        self._font_menu.pack(side="left", padx=5)

        self._font_path: str = ""  # 自定义字体路径

        # ---- 字号（相对短边 %） ----
        size_frame = ctk.CTkFrame(self, fg_color="transparent")
        size_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(size_frame, text="字号:").pack(side="left")
        self._font_ratio_label = ctk.CTkLabel(size_frame, text="4%", width=30)
        self._font_ratio_label.pack(side="right", padx=5)

        self._font_ratio_var = ctk.DoubleVar(value=4.0)
        self._font_ratio_slider = ctk.CTkSlider(
            size_frame,
            from_=1, to=20,
            variable=self._font_ratio_var,
            command=lambda v: self._on_font_ratio_changed(v),
        )
        self._font_ratio_slider.pack(side="left", fill="x", expand=True, padx=5)

        # ---- 颜色 + 透明度 ----
        color_frame = ctk.CTkFrame(self, fg_color="transparent")
        color_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(color_frame, text="颜色:").pack(side="left")

        # 色块按钮显示的是"水印颜色"本身（数据），不取主题色 ——
        # 只有 hover 用一个中性灰，避免与任何主题色撞车
        self._color_btn = ctk.CTkButton(
            color_frame,
            text="      ",
            width=40,
            fg_color="#ffffff",
            hover_color="#e0e0e0",
            command=self._pick_color,
        )
        self._color_btn.pack(side="left", padx=5)
        self._color_rgba: tuple = (255, 255, 255, 128)  # 默认白色半透明

        # 语义澄清：滑块值就是 alpha（不透明度），不是"透明度"
        # 0 = 完全看不见，100 = 完全不透明
        ctk.CTkLabel(color_frame, text="不透明度:").pack(side="left", padx=(15, 0))
        self._opacity_label = ctk.CTkLabel(color_frame, text="50%", width=35)
        self._opacity_label.pack(side="right", padx=5)

        self._opacity_var = ctk.IntVar(value=50)
        self._opacity_slider = ctk.CTkSlider(
            color_frame,
            from_=0, to=100,
            variable=self._opacity_var,
            command=lambda v: self._on_opacity_changed(v),
        )
        self._opacity_slider.pack(side="left", fill="x", expand=True, padx=5)

        # 透明度是最容易误解的控件：滑块值即 alpha（不透明度），
        # 0 = 完全看不见。浅色底图配白色水印在 100% 时同样会"看不见"，
        # 因此这里直接给出可见提示，避免反复试滑块
        ctk.CTkLabel(
            self,
            text="0 = 完全透明（看不见），100 = 完全不透明；浅色底图请选深色水印",
            text_color=theme.TEXT_MUTED,
            font=("Microsoft YaHei", 9),
            justify="left",
            wraplength=260,
        ).pack(anchor="w", padx=12, pady=(0, 2))

        # ---- 平铺模式 ----
        self._tile_var = ctk.BooleanVar(value=False)
        self._tile_checkbox = ctk.CTkCheckBox(
            self,
            text="平铺模式",
            variable=self._tile_var,
            command=self._on_tile_toggled,
        )
        self._tile_checkbox.pack(anchor="w", padx=10, pady=5)

        # ---- 位置选择（受平铺控制） ----
        pos_frame = ctk.CTkFrame(self, fg_color="transparent")
        pos_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(pos_frame, text="位置:").pack(anchor="w")

        # 模式切换：九宫格 / 自定义坐标
        mode_toggle = ctk.CTkFrame(pos_frame, fg_color="transparent")
        mode_toggle.pack(pady=2)

        self._pos_mode_var = ctk.StringVar(value="grid")
        grid_radio = ctk.CTkRadioButton(
            mode_toggle, text="九宫格", variable=self._pos_mode_var,
            value="grid", command=self._on_pos_mode_changed
        )
        grid_radio.pack(side="left", padx=(0, 10))

        custom_radio = ctk.CTkRadioButton(
            mode_toggle, text="自定义坐标", variable=self._pos_mode_var,
            value="custom", command=self._on_pos_mode_changed
        )
        custom_radio.pack(side="left")

        # --- 九宫格按钮组 ---
        self._grid_frame = ctk.CTkFrame(pos_frame)
        self._grid_frame.pack(pady=2)

        for i, (pos_key, pos_label) in enumerate(_POSITIONS):
            row, col = divmod(i, 3)
            btn = ctk.CTkButton(
                self._grid_frame,
                # 带文字标签而不是空方块：空方块要用户自己数格子猜方位，
                # 与图片面板的九宫格（一直有文字）也对不上
                text=pos_label,
                width=48, height=28,
                font=("Microsoft YaHei", 10),
                command=lambda p=pos_key: self._select_position(p),
            )
            btn.grid(row=row, column=col, padx=2, pady=2)
            self._position_buttons[pos_key] = btn

        # 默认选中右下角
        self._select_position("bottom-right")

        # --- 自定义坐标（默认隐藏） ---
        self._custom_pos_frame = ctk.CTkFrame(pos_frame, fg_color="transparent")

        x_row = ctk.CTkFrame(self._custom_pos_frame, fg_color="transparent")
        x_row.pack(fill="x", pady=1)
        ctk.CTkLabel(x_row, text="X (%):").pack(side="left")
        self._custom_x_var = ctk.StringVar(value="50")
        self._custom_x_entry = ctk.CTkEntry(x_row, width=60, textvariable=self._custom_x_var)
        self._custom_x_entry.pack(side="left", padx=5)
        self._custom_x_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self._custom_x_entry)
        ctk.CTkLabel(x_row, text="(0=左 100=右)", text_color=theme.TEXT_MUTED, font=("Microsoft YaHei", 9)).pack(side="left")

        y_row = ctk.CTkFrame(self._custom_pos_frame, fg_color="transparent")
        y_row.pack(fill="x", pady=1)
        ctk.CTkLabel(y_row, text="Y (%):").pack(side="left")
        self._custom_y_var = ctk.StringVar(value="50")
        self._custom_y_entry = ctk.CTkEntry(y_row, width=60, textvariable=self._custom_y_var)
        self._custom_y_entry.pack(side="left", padx=5)
        self._custom_y_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self._custom_y_entry)
        ctk.CTkLabel(y_row, text="(0=上 100=下)", text_color=theme.TEXT_MUTED, font=("Microsoft YaHei", 9)).pack(side="left")

        # ---- 边距 ----
        margin_frame = ctk.CTkFrame(self, fg_color="transparent")
        margin_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(margin_frame, text="边距:").pack(side="left")
        self._margin_label = ctk.CTkLabel(margin_frame, text="20 px", width=45)
        self._margin_label.pack(side="right", padx=5)

        self._margin_var = ctk.IntVar(value=20)
        self._margin_slider = ctk.CTkSlider(
            margin_frame,
            from_=0, to=100,
            variable=self._margin_var,
            command=lambda v: self._on_margin_changed(v),
        )
        self._margin_slider.pack(side="left", fill="x", expand=True, padx=5)

        # ---- 旋转角度 ----
        angle_frame = ctk.CTkFrame(self, fg_color="transparent")
        angle_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(angle_frame, text="旋转:").pack(side="left")
        self._angle_label = ctk.CTkLabel(angle_frame, text="0°", width=35)
        self._angle_label.pack(side="right", padx=5)

        self._angle_var = ctk.DoubleVar(value=0.0)
        self._angle_slider = ctk.CTkSlider(
            angle_frame,
            from_=-180, to=180,
            variable=self._angle_var,
            command=lambda v: self._on_angle_changed(v),
        )
        self._angle_slider.pack(side="left", fill="x", expand=True, padx=5)

        # ---- 平铺参数（仅平铺模式下有意义） ----
        tile_params_frame = ctk.CTkFrame(self, fg_color="transparent")
        tile_params_frame.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(tile_params_frame, text="水平间距:").pack(side="left")
        self._spacing_x_var = ctk.IntVar(value=100)
        self._spacing_x_entry = ctk.CTkEntry(tile_params_frame, width=50, textvariable=self._spacing_x_var)
        self._spacing_x_entry.pack(side="left", padx=5)
        self._spacing_x_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self._spacing_x_entry)

        ctk.CTkLabel(tile_params_frame, text="垂直间距:").pack(side="left", padx=(15, 0))
        self._spacing_y_var = ctk.IntVar(value=100)
        self._spacing_y_entry = ctk.CTkEntry(tile_params_frame, width=50, textvariable=self._spacing_y_var)
        self._spacing_y_entry.pack(side="left", padx=5)
        self._spacing_y_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self._spacing_y_entry)

        self._stagger_var = ctk.BooleanVar(value=True)
        self._stagger_cb = ctk.CTkCheckBox(
            tile_params_frame,
            text="错位",
            variable=self._stagger_var,
            command=self._notify_change,
        )
        self._stagger_cb.pack(side="left", padx=10)

        # 用 trace_add 监听变量变化（覆盖粘贴/程序设值，比 KeyRelease 更可靠）
        _trace_cb = lambda *_: self._notify_change()
        for var in [self._custom_x_var, self._custom_y_var,
                     self._spacing_x_var, self._spacing_y_var,
                     self._pos_mode_var]:
            try:
                var.trace_add("write", _trace_cb)
            except Exception:
                pass

        # 所有控件初值统一取自 WatermarkConfig 的默认值，面板里不再各写一份硬编码。
        # 这正是"配置写 Logo 缩放 10%、界面初值 15%"这类不一致的来源。
        # 默认文字为空：不会一打开就已经配好水印却没人注意到
        # （这是"意外批量水印"最主要的成因）。
        self.set_from_config(WatermarkConfig())

    # ============================================================
    # 控件回调
    # ============================================================

    def _notify_change(self):
        """通知配置变更（加载预设时暂停）"""
        if not self._loading:
            self.on_config_changed()

    def _on_font_selected(self, choice: str):
        """字体下拉框选择"""
        if choice == "自定义...":
            from tkinter.filedialog import askopenfilename
            path = askopenfilename(
                title="选择字体文件",
                filetypes=[("字体文件", "*.ttf *.ttc *.otf"), ("所有文件", "*.*")],
            )
            if path:
                self._font_path = path
                self._font_var.set(f"自定义: {path.split('/')[-1]}")
        elif choice == "微软雅黑 (默认)":
            self._font_path = ""
        elif choice == "黑体 (SimHei)":
            self._font_path = r"C:\Windows\Fonts\simhei.ttf"
        self._notify_change()

    def _on_font_ratio_changed(self, value):
        """字号滑块变更"""
        self._font_ratio_label.configure(text=f"{value:.1f}%")
        self._notify_change()

    def _pick_color(self):
        """打开颜色选择器"""
        from tkinter import colorchooser
        color = colorchooser.askcolor(title="选择水印颜色")[0]
        if color:
            # color 是 (R, G, B) 元组，范围 0-1 或 0-255
            r, g, b = [int(c) for c in color]
            a = opacity_to_alpha(self._opacity_var.get())  # 唯一换算口径
            self._color_rgba = (r, g, b, a)
            # 更新按钮背景色显示（近似）
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            self._color_btn.configure(fg_color=hex_color)
            self._notify_change()

    def _on_opacity_changed(self, value):
        """不透明度滑块变更 — 百分比(0-100)映射到 alpha(0-255)"""
        self._opacity_label.configure(text=f"{int(value)}%")
        r, g, b = self._color_rgba[:3]
        # 用 config.opacity_to_alpha 而不是就地 int(v*255/100)：
        # 就地截断会与配置类的四舍五入差 1/255，
        # 造成"面板算出的 alpha"和"配置里的 alpha"对不上
        self._color_rgba = (r, g, b, opacity_to_alpha(value))
        self._notify_change()

    def _on_tile_toggled(self):
        """平铺模式切换 —— 联动禁用/启用位置和边距"""
        is_tile = self._tile_var.get()
        state = "disabled" if is_tile else "normal"

        # 九宫格按钮组
        for btn in self._position_buttons.values():
            btn.configure(state=state)

        # 自定义坐标输入框
        self._custom_x_entry.configure(state=state)
        self._custom_y_entry.configure(state=state)

        # 边距滑块
        self._margin_slider.configure(state=state)

        self._notify_change()

    def _select_position(self, position: str):
        """选中九宫格位置"""
        self._selected_position = position
        # 更新按钮视觉反馈：选中 = 主色实底，未选中 = 暖中性底
        for pos, btn in self._position_buttons.items():
            if pos == position:
                btn.configure(
                    fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                    text_color=theme.ON_ACCENT,
                )
            else:
                btn.configure(
                    fg_color=theme.NEUTRAL_BTN, hover_color=theme.NEUTRAL_BTN_HOVER,
                    text_color=theme.TEXT,
                )
        self._notify_change()

    def set_custom_position(self, x_pct: float, y_pct: float):
        """
        由预览图上拖动水印写入坐标（公开入口，主窗口调用）。

        顺带把定位切到"自定义"：拖动本身就表示用户要的是一个任意位置，
        留着九宫格会让面板参数与实际画面不符。
        写入走的是面板自己的提交通道，所以作用范围（全部/本文件/本页）
        与手输坐标完全一致，不新增语义。
        """
        self._custom_x_var.set(format_percent(x_pct))
        self._custom_y_var.set(format_percent(y_pct))
        if self._pos_mode_var.get() != "custom":
            self._pos_mode_var.set("custom")
            self._on_pos_mode_changed()    # 内部会 notify，别重复触发
        else:
            self._notify_change()

    def _on_pos_mode_changed(self):
        """位置模式切换：九宫格 / 自定义坐标"""
        mode = self._pos_mode_var.get()
        if mode == "grid":
            # 显示九宫格，隐藏自定义坐标
            self._grid_frame.pack(pady=2)
            self._custom_pos_frame.pack_forget()
        else:
            # 隐藏九宫格，显示自定义坐标
            self._grid_frame.pack_forget()
            self._custom_pos_frame.pack(pady=2)
        self._notify_change()

    def _on_margin_changed(self, value):
        """边距滑块变更"""
        self._margin_label.configure(text=f"{int(value)} px")
        self._notify_change()

    def _on_angle_changed(self, value):
        """旋转角度滑块变更"""
        self._angle_label.configure(text=f"{int(value)}°")
        self._notify_change()

    # ============================================================
    # 公共接口 — 读取当前配置
    # ============================================================

    def get_config_dict(self) -> dict:
        """
        返回当前面板的所有参数字典。

        返回的键与 WatermarkConfig 的字段对应，
        由调用方组装成完整的 WatermarkConfig。

        所有数值字段统一经 validators 校验：
        越界值夹取到合法区间、非法值回退默认值，并把修正说明写入
        self.last_notices（由主窗口在状态栏提示用户）。
        """
        from src.validators import TEXT_FIELD_SPECS, normalize_fields

        data = {
            "watermark_type": "text",
            "text": self._text_box.get("1.0", "end").strip(),
            "font_path": self._font_path,
            "font_ratio": self._font_ratio_var.get() / 100.0,  # 百分比 → 小数
            "color_rgba": self._color_rgba,
            "opacity": self._opacity_var.get(),
            "position": self._selected_position,
            "position_mode": self._pos_mode_var.get(),  # "grid" 或 "custom"
            "custom_x": self._custom_x_var.get(),
            "custom_y": self._custom_y_var.get(),
            "margin": self._margin_var.get(),
            "angle": self._angle_var.get(),
            "tile": self._tile_var.get(),
            # 间距直接读 Entry 文本而非 IntVar：
            # IntVar 绑定 textvariable 后，输入非数字/清空时 .get() 会抛 TclError，
            # 异常被主窗口吞掉后整个配置变成 None，表现为预览停滞、导出无反应
            "spacing_x": self._spacing_x_entry.get() if hasattr(self, '_spacing_x_entry') else 100,
            "spacing_y": self._spacing_y_entry.get() if hasattr(self, '_spacing_y_entry') else 100,
            "stagger": self._stagger_var.get() if hasattr(self, '_stagger_var') else True,
        }

        data, self.last_notices = normalize_fields(data, TEXT_FIELD_SPECS)
        return data

    def set_from_config(self, cfg) -> None:
        """从 WatermarkConfig 恢复面板状态（暂停通知，恢复后只刷新一次）"""
        self._loading = True  # 暂停通知
        # 文字内容
        self._text_box.delete("1.0", "end")
        self._text_box.insert("1.0", getattr(cfg, 'text', ''))

        # 字体路径
        self._font_path = getattr(cfg, 'font_path', '')
        if not self._font_path:
            self._font_var.set("微软雅黑 (默认)")
        else:
            self._font_var.set(f"自定义: {self._font_path.split('/')[-1]}")

        # 字号
        ratio = getattr(cfg, 'font_ratio', 0.04) * 100
        self._font_ratio_var.set(ratio)
        self._font_ratio_label.configure(text=f"{ratio:.1f}%")

        # 颜色 + 不透明度
        # 不透明度以 cfg.opacity 为唯一权威来源，color_rgba 只提供 RGB。
        # 历史预设里这两者可能各写一个值，若直接用 color_rgba 的 alpha，
        # 就会出现「滑块显示 80% 但实际按 128 渲染」的错位。
        rgba = getattr(cfg, 'color_rgba', (255, 255, 255, 128))
        try:
            r, g, b = [int(v) for v in list(rgba)[:3]]
        except (TypeError, ValueError):
            r, g, b = 255, 255, 255
        self._color_btn.configure(fg_color=f"#{r:02x}{g:02x}{b:02x}")

        opacity = int(getattr(cfg, 'opacity', 50))
        self._color_rgba = (r, g, b, opacity_to_alpha(opacity))
        self._opacity_var.set(opacity)
        self._opacity_label.configure(text=f"{opacity}%")

        # 位置（含模式切换）
        pos_mode = getattr(cfg, 'position_mode', 'grid')
        self._pos_mode_var.set(pos_mode)
        self._on_pos_mode_changed()  # 触发 UI 切换

        if pos_mode == "grid":
            # 归一化后再回填：旧预设里可能存着 center-left / center-right，
            # 它们不在 _POSITIONS 的键里，不翻译会让九个按钮一个都不高亮
            pos = normalize_position(getattr(cfg, 'position', None))
            self._select_position(pos)
        else:
            cx = getattr(cfg, 'custom_x', 50)
            cy = getattr(cfg, 'custom_y', 50)
            # 用 format_percent 而不是 str()：拖出来的坐标是浮点，
            # str() 会出现 33.470000000000006 这种噪声值
            self._custom_x_var.set(format_percent(cx))
            self._custom_y_var.set(format_percent(cy))

        # 边距
        margin = getattr(cfg, 'margin', 20)
        self._margin_var.set(margin)
        self._margin_label.configure(text=f"{margin} px")

        # 角度
        angle = getattr(cfg, 'angle', 0.0)
        self._angle_var.set(angle)
        self._angle_label.configure(text=f"{int(angle)}°")

        # 平铺
        tile = getattr(cfg, 'tile', False)
        self._tile_var.set(tile)
        self._on_tile_toggled()  # 触发联动

        # 平铺参数
        sx = getattr(cfg, 'spacing_x', 100)
        sy = getattr(cfg, 'spacing_y', 100)
        self._spacing_x_var.set(sx)
        self._spacing_y_var.set(sy)

        stagger = getattr(cfg, 'stagger', True)
        self._stagger_var.set(stagger)

        self._loading = False  # 恢复通知
        self._notify_change()  # 只刷新一次
