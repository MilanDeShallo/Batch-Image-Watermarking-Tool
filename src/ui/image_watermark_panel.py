"""
图片/Logo 水印设置面板
- Logo 上传与预览
- 缩放比例、透明度控制
- 9宫格位置 + 平铺模式（联动禁用）
- 旋转角度
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
import os
from PIL import Image, ImageTk

from src.config import WatermarkConfig, format_percent, normalize_position
from src.ui import theme


class ImageWatermarkPanel(ctk.CTkFrame):
    """图片水印设置面板"""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)

        self.logo_path = None  # 当前选中的 Logo 路径
        self.logo_preview_tk = None
        self.on_config_changed = lambda: None  # 变更通知回调
        self._loading = False  # 预设加载时暂停通知

        self._build_ui()

    # ==================== UI 构建 ====================

    def _build_ui(self):
        grid_opts = {"sticky": "ew", "padx": 8, "pady": (6, 2)}

        # --- Logo 选择区 ---
        logo_frame = ctk.CTkFrame(self, fg_color="transparent")
        logo_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 6))
        logo_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(logo_frame, text="Logo 图片:", width=70).grid(
            row=0, column=0, sticky="w"
        )

        self.logo_path_label = ctk.CTkLabel(
            logo_frame, text="未选择", text_color=theme.TEXT_MUTED,
            anchor="w"
        )
        self.logo_path_label.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        btn_frame = ctk.CTkFrame(logo_frame, fg_color="transparent")
        btn_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        ctk.CTkButton(
            btn_frame, text="选择 Logo", width=100,
            command=self._browse_logo
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            btn_frame, text="清除", width=60,
            command=self._clear_logo
        ).pack(side="left")

        # Logo 预览
        self.logo_preview_label = ctk.CTkLabel(
            self, text="", width=120, height=80,
            fg_color=theme.SURFACE_ALT, corner_radius=theme.RADIUS_INPUT
        )
        self.logo_preview_label.grid(row=1, column=0, **grid_opts)

        # --- 分隔线 ---
        # width 与 height 都要给：CTkFrame 默认 200×200（逻辑像素），
        # 只写 height=1 时请求宽度仍是 200 逻辑像素，会把面板的请求宽度撑起来
        sep = ctk.CTkFrame(
            self, width=1, height=1, corner_radius=0, fg_color=theme.BORDER
        )
        sep.grid(row=2, column=0, sticky="ew", padx=12, pady=8)

        # --- 缩放比例 ---
        row = 3
        ctk.CTkLabel(self, text="缩放比例:").grid(row=row, column=0, sticky="w", padx=12)
        row += 1

        self.scale_slider = ctk.CTkSlider(
            self, from_=5, to=50, number_of_steps=45,
            command=lambda v: self._update_label(self.scale_value_label, f"{int(v)}%")
        )
        self.scale_slider.set(15)
        self.scale_slider.grid(row=row, column=0, **grid_opts)

        self.scale_value_label = ctk.CTkLabel(self, text="15%", width=40)
        self.scale_value_label.grid(row=row, column=0, sticky="e", padx=(0, 16))
        row += 1

        # --- 透明度 ---
        ctk.CTkLabel(self, text="不透明度:").grid(row=row, column=0, sticky="w", padx=12)
        row += 1

        self.opacity_slider = ctk.CTkSlider(
            self, from_=10, to=100, number_of_steps=90,
            command=lambda v: self._update_label(self.opacity_value_label, f"{int(v)}%")
        )
        self.opacity_slider.set(50)
        self.opacity_slider.grid(row=row, column=0, **grid_opts)

        self.opacity_value_label = ctk.CTkLabel(self, text="50%", width=40)
        self.opacity_value_label.grid(row=row, column=0, sticky="e", padx=(0, 16))
        row += 1

        # --- 平铺模式 ---
        self.tile_var = ctk.BooleanVar(value=False)
        tile_check = ctk.CTkCheckBox(
            self, text="平铺模式", variable=self.tile_var,
            command=self._on_tile_toggle
        )
        tile_check.grid(row=row, column=0, sticky="w", padx=12, pady=(8, 2))
        row += 1

        # --- 位置选择（受平铺控制） ---
        pos_frame = ctk.CTkFrame(self, fg_color="transparent")
        pos_frame.grid(row=row, column=0, **grid_opts)

        # 模式切换：九宫格 / 自定义坐标
        mode_toggle = ctk.CTkFrame(pos_frame, fg_color="transparent")
        mode_toggle.pack(anchor="w")

        self.pos_mode_var = ctk.StringVar(value="grid")
        grid_radio = ctk.CTkRadioButton(
            mode_toggle, text="九宫格", variable=self.pos_mode_var,
            value="grid", command=self._on_pos_mode_changed
        )
        grid_radio.pack(side="left", padx=(0, 10))

        custom_radio = ctk.CTkRadioButton(
            mode_toggle, text="自定义坐标", variable=self.pos_mode_var,
            value="custom", command=self._on_pos_mode_changed
        )
        custom_radio.pack(side="left")

        # --- 九宫格按钮组 ---
        self.position_frame = ctk.CTkFrame(pos_frame)
        self.position_frame.pack(pady=2)
        self.pos_buttons = []
        positions = [
            "左上", "上中", "右上",
            "左中", "居中", "右中",
            "左下", "下中", "右下",
        ]
        for i, label in enumerate(positions):
            r, c = divmod(i, 3)
            btn = ctk.CTkButton(
                self.position_frame, text=label, width=48, height=28,
                font=("Microsoft YaHei", 10),
                command=lambda idx=i: self._select_position(idx)
            )
            btn.grid(row=r, column=c, padx=1, pady=1)
            self.pos_buttons.append(btn)

        # 默认选中右下
        self.selected_position = 8
        self._restyle_position_buttons()

        # --- 自定义坐标（默认隐藏） ---
        self.custom_pos_frame = ctk.CTkFrame(pos_frame, fg_color="transparent")

        x_row = ctk.CTkFrame(self.custom_pos_frame, fg_color="transparent")
        x_row.pack(fill="x", pady=1)
        ctk.CTkLabel(x_row, text="X (%):").pack(side="left")
        self.custom_x_var = ctk.StringVar(value="50")
        self.custom_x_entry = ctk.CTkEntry(x_row, width=60, textvariable=self.custom_x_var)
        self.custom_x_entry.pack(side="left", padx=5)
        self.custom_x_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self.custom_x_entry)
        ctk.CTkLabel(x_row, text="(0=左 100=右)", text_color=theme.TEXT_MUTED, font=("Microsoft YaHei", 9)).pack(side="left")

        y_row = ctk.CTkFrame(self.custom_pos_frame, fg_color="transparent")
        y_row.pack(fill="x", pady=1)
        ctk.CTkLabel(y_row, text="Y (%):").pack(side="left")
        self.custom_y_var = ctk.StringVar(value="50")
        self.custom_y_entry = ctk.CTkEntry(y_row, width=60, textvariable=self.custom_y_var)
        self.custom_y_entry.pack(side="left", padx=5)
        self.custom_y_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self.custom_y_entry)
        ctk.CTkLabel(y_row, text="(0=上 100=下)", text_color=theme.TEXT_MUTED, font=("Microsoft YaHei", 9)).pack(side="left")
        row += 1

        # --- 边距 ---
        margin_frame = ctk.CTkFrame(self, fg_color="transparent")
        margin_frame.grid(row=row, column=0, **grid_opts)
        ctk.CTkLabel(margin_frame, text="边距 (px):").pack(side="left")
        self.margin_slider = ctk.CTkSlider(
            margin_frame, from_=0, to=100, width=140,
            command=lambda v: self._update_label(self.margin_value_label, f"{int(v)}")
        )
        self.margin_slider.pack(side="left", padx=(8, 4))
        self.margin_slider.set(5)
        self.margin_value_label = ctk.CTkLabel(margin_frame, text="5", width=30)
        self.margin_value_label.pack(side="left")
        self.margin_widget = margin_frame  # 保存引用用于联动禁用
        row += 1

        # --- 分隔线 ---
        sep2 = ctk.CTkFrame(
            self, width=1, height=1, corner_radius=0, fg_color=theme.BORDER
        )
        sep2.grid(row=row, column=0, sticky="ew", padx=12, pady=8)
        row += 1

        # --- 平铺参数（默认隐藏） ---
        self.tile_params_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.tile_params_frame.grid(row=row, column=0, **grid_opts)
        self.tile_params_frame.grid_remove()  # 初始隐藏

        ctk.CTkLabel(
            self.tile_params_frame, text="水平间距:"
        ).grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.spacing_x_entry = ctk.CTkEntry(
            self.tile_params_frame, width=60, placeholder_text="100"
        )
        self.spacing_x_entry.insert(0, "100")
        self.spacing_x_entry.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.spacing_x_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        theme.bind_focus_ring(self.spacing_x_entry)

        ctk.CTkLabel(
            self.tile_params_frame, text="垂直间距:"
        ).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.spacing_y_entry = ctk.CTkEntry(
            self.tile_params_frame, width=60, placeholder_text="100"
        )
        self.spacing_y_entry.insert(0, "100")
        self.spacing_y_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        self.spacing_y_entry.grid(row=0, column=3, sticky="w")
        theme.bind_focus_ring(self.spacing_y_entry)

        self._stagger_var = ctk.BooleanVar(value=False)
        stagger_check = ctk.CTkCheckBox(
            self.tile_params_frame, text="交错排列", width=90,
            variable=self._stagger_var,
            command=self._notify_change,  # 勾选后立即刷新预览
        )
        stagger_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.stagger_var = self._stagger_var
        row += 1

        # --- 旋转角度 ---
        angle_frame = ctk.CTkFrame(self, fg_color="transparent")
        angle_frame.grid(row=row, column=0, **grid_opts)
        ctk.CTkLabel(angle_frame, text="旋转角度:").pack(side="left")
        self.angle_slider = ctk.CTkSlider(
            angle_frame, from_=-180, to=180, width=140,
            command=lambda v: self._update_label(self.angle_value_label, f"{int(v)}°")
        )
        self.angle_slider.pack(side="left", padx=(8, 4))
        self.angle_slider.set(0)
        self.angle_value_label = ctk.CTkLabel(angle_frame, text="0°", width=40)
        self.angle_value_label.pack(side="left")

        # 用 trace_add 监听变量变化（覆盖粘贴/程序设值，比 KeyRelease 更可靠）
        _trace_cb = lambda *_: self._notify_change()
        for var in [self.custom_x_var, self.custom_y_var, self.pos_mode_var]:
            try:
                var.trace_add("write", _trace_cb)
            except Exception:
                pass

        # 所有控件初值统一取自 WatermarkConfig 的默认值。
        # 面板里原先硬编码"缩放 15% / 边距 5px"，而配置类写的是 10% / 20px ——
        # 两套默认值，用户看到的就是"和文档写的不一样"。
        self.set_from_config(WatermarkConfig())

    # ==================== 回调方法 ====================

    def _browse_logo(self):
        """打开文件对话框选择 Logo"""
        path = filedialog.askopenfilename(
            title="选择 Logo 图片",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.webp"),
                ("所有文件", "*.*"),
            ]
        )
        if path:
            self._load_logo(path)

    def _clear_logo(self, notify: bool = True):
        """
        清除已选 Logo。

        :param notify: 是否触发变更通知。加载预设 / 回填面板期间传 False，
                       由调用方统一刷新一次，避免重复渲染。
        """
        self.logo_path = None
        self.logo_path_label.configure(text="未选择", text_color=theme.TEXT_MUTED)
        # 无条件清掉预览图：曾经写成 `if self.logo_preview_tk:` 才清理，
        # 而该字段为 None 时就会跳过 —— 界面上残留上一张 Logo 的缩略图，
        # 用户以为已经清掉了
        self.logo_preview_label.configure(image="", text="")
        self.logo_preview_tk = None
        if notify:
            self._notify_change()

    def _load_logo(self, path, silent: bool = False) -> bool:
        """
        加载并预览 Logo。

        :param silent: True 时不弹错误框，只返回 False。
                       预设加载走静默路径 —— 用户只是想换个预设，
                       不该被一个模态错误框拦住
        :return: 是否加载成功
        """
        try:
            # with 确保源文件句柄及时释放（Windows 下否则 Logo 文件无法移动/删除）
            with Image.open(path) as src:
                src.load()
                img = src.convert("RGBA")
            # 缩放到预览尺寸
            preview = img.copy()
            preview.thumbnail((110, 70), Image.LANCZOS)
            self.logo_preview_tk = ImageTk.PhotoImage(preview)
            self.logo_preview_label.configure(image=self.logo_preview_tk, text="")
            self.logo_path = path
            # 显示文件名
            self.logo_path_label.configure(
                text=os.path.basename(path),
                text_color=theme.TEXT
            )
            self._notify_change()
            return True
        except Exception as e:
            if not silent:
                messagebox.showerror("Logo 加载失败", f"无法打开图片:\n{e}")
            # 加载失败不能留着旧 Logo：否则界面显示的是"上一次那张"，
            # 导出却按新的 logo_path 渲染 —— 两者对不上
            self._clear_logo(notify=False)
            return False

    def _on_tile_toggle(self):
        """平铺模式切换时的联动禁用"""
        is_tile = self.tile_var.get()
        state = "disabled" if is_tile else "normal"
        if is_tile:
            # 禁用位置和边距
            for btn in self.pos_buttons:
                btn.configure(state="disabled", fg_color=theme.TRACK)
            # 注意：要禁用真正可交互的滑块，而不是外层容器。
            # CTkFrame 不支持 state 参数，对它 configure(state=...) 会抛
            # ValueError；异常一旦抛出会中断整个 set_from_config，
            # 表现为切换作用范围时图片面板只回填了一半
            self.margin_slider.configure(state="disabled")
            # 也禁用自定义坐标
            self.custom_x_entry.configure(state="disabled")
            self.custom_y_entry.configure(state="disabled")
            # 显示平铺参数
            self.tile_params_frame.grid()
        else:
            # 启用位置和边距
            for btn in self.pos_buttons:
                btn.configure(state="normal")
            # 恢复当前选中的高亮
            self._restyle_position_buttons()
            self.margin_slider.configure(state="normal")
            # 启用自定义坐标（根据当前模式）
            if hasattr(self, 'custom_x_entry'):
                pos_mode = self.pos_mode_var.get() if hasattr(self, 'pos_mode_var') else "grid"
                if pos_mode == "custom":
                    self.custom_x_entry.configure(state="normal")
                    self.custom_y_entry.configure(state="normal")
            # 隐藏平铺参数
            self.tile_params_frame.grid_remove()
        self._notify_change()

    def set_custom_position(self, x_pct: float, y_pct: float):
        """
        由预览图上拖动 Logo 水印写入坐标（公开入口，主窗口调用）。

        与文字面板同名同义：顺带切到"自定义"定位，写入走面板自己的提交通道，
        所以作用范围（全部 / 本文件 / 本页）与手输坐标一致。
        """
        self.custom_x_var.set(format_percent(x_pct))
        self.custom_y_var.set(format_percent(y_pct))
        if self.pos_mode_var.get() != "custom":
            self.pos_mode_var.set("custom")
            self._on_pos_mode_changed()    # 内部会 notify，别重复触发
        else:
            self._notify_change()

    def _on_pos_mode_changed(self):
        """位置模式切换：九宫格 / 自定义坐标"""
        mode = self.pos_mode_var.get()
        if mode == "grid":
            self.position_frame.pack(pady=2)
            self.custom_pos_frame.pack_forget()
        else:
            self.position_frame.pack_forget()
            self.custom_pos_frame.pack(pady=2)
        self._notify_change()

    def _restyle_position_buttons(self):
        """
        按当前选中项刷新九宫格配色：选中 = 主色实底，未选中 = 暖中性底。

        单独收一个函数，是因为"选中态样式"原先在创建、点击、解除禁用、
        回填预设四处各写一遍，改主色时要改四个地方，漏一个就会出现
        "点过的按钮和回填后的按钮颜色不一样"。
        """
        for i, btn in enumerate(self.pos_buttons):
            if i == self.selected_position:
                btn.configure(
                    fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                    text_color=theme.ON_ACCENT,
                )
            else:
                btn.configure(
                    fg_color=theme.NEUTRAL_BTN, hover_color=theme.NEUTRAL_BTN_HOVER,
                    text_color=theme.TEXT,
                )

    def _select_position(self, index):
        """9 宫格按钮点击"""
        self.selected_position = index
        self._restyle_position_buttons()
        self._notify_change()

    def _update_label(self, label, text):
        """更新数值标签并触发预览刷新"""
        label.configure(text=text)
        if not self._loading:
            self.on_config_changed()

    def _notify_change(self):
        """通知配置变更（加载预设时暂停）"""
        if not self._loading:
            self.on_config_changed()

    # ==================== 公共接口 ====================

    # 位置索引 → 字符串映射
    # 必须与 text_watermark_panel._POSITIONS 用同一套写法，也要与引擎
    # anchor_position 对齐（垂直分量只有 top / middle / bottom）。
    # 这里曾写作 center-left / center-right，同一个「左中」经本面板出去
    # 会被引擎当成 v_pos="center" 而渲染在正中 —— 与文字面板的同一按钮
    # 表现不一致，且界面高亮正常、完全看不出问题。
    _POS_NAMES = [
        "top-left", "top-center", "top-right",
        "middle-left", "center", "middle-right",
        "bottom-left", "bottom-center", "bottom-right",
    ]

    def get_config_dict(self) -> dict:
        """
        返回当前面板配置字典
        字段名与 WatermarkConfig 完全对应

        所有数值字段统一经 validators 校验：
        越界值夹取到合法区间、非法值回退默认值，并把修正说明写入
        self.last_notices（由主窗口在状态栏提示用户）。
        """
        from src.validators import IMAGE_FIELD_SPECS, normalize_fields

        # 位置索引转字符串
        pos_idx = self.selected_position if isinstance(self.selected_position, int) else 8
        pos_str = self._POS_NAMES[pos_idx] if 0 <= pos_idx < 9 else "bottom-right"

        data = {
            "watermark_type": "image",
            "logo_path": self.logo_path or "",
            "scale_ratio": int(self.scale_slider.get()) / 100.0,
            "logo_opacity": int(self.opacity_slider.get()),
            "logo_position": pos_str,
            "logo_position_mode": self.pos_mode_var.get() if hasattr(self, 'pos_mode_var') else "grid",
            # 自定义坐标/间距传原始文本，交由 validators 解析并夹取
            "logo_custom_x": self.custom_x_var.get() if hasattr(self, 'custom_x_var') else 50,
            "logo_custom_y": self.custom_y_var.get() if hasattr(self, 'custom_y_var') else 50,
            "logo_margin": int(self.margin_slider.get()),
            "logo_tile": self.tile_var.get(),
            "logo_spacing_x": self.spacing_x_entry.get(),
            "logo_spacing_y": self.spacing_y_entry.get(),
            "logo_stagger": self._stagger_var.get(),
            "logo_angle": int(self.angle_slider.get()),
        }

        data, self.last_notices = normalize_fields(data, IMAGE_FIELD_SPECS)
        return data

    def set_from_config(self, cfg):
        """
        从 WatermarkConfig 恢复面板状态（暂停通知，恢复后只刷新一次）
        :param cfg: WatermarkConfig 实例或 dict
        """
        self._loading = True
        if isinstance(cfg, dict):
            data = cfg
        else:
            # 从 WatermarkConfig 对象提取所有字段
            data = {
                "logo_path": getattr(cfg, 'logo_path', ''),
                "scale_ratio": getattr(cfg, 'scale_ratio', 0.10),
                "logo_opacity": getattr(cfg, 'logo_opacity', 50),
                "logo_position": getattr(cfg, 'logo_position', 'bottom-right'),
                "logo_position_mode": getattr(cfg, 'logo_position_mode', 'grid'),
                "logo_custom_x": getattr(cfg, 'logo_custom_x', 50),
                "logo_custom_y": getattr(cfg, 'logo_custom_y', 50),
                "logo_margin": getattr(cfg, 'logo_margin', 5),
                "logo_tile": getattr(cfg, 'logo_tile', False),
                "logo_spacing_x": getattr(cfg, 'logo_spacing_x', 100),
                "logo_spacing_y": getattr(cfg, 'logo_spacing_y', 100),
                "logo_angle": getattr(cfg, 'logo_angle', 0),
            }

        # Logo 路径 —— 无论预设有没有带 Logo，都必须**显式落地一次**。
        #
        # 只在"路径有效"时加载、其余情况什么都不做，会把上一次选中的 Logo
        # 留在面板上：加载一个纯文字预设（或 Logo 已失效的预设）之后，
        # 类型会被推导成 both，导出依旧带着旧 Logo ——
        # 用户以为是纯文字，实际发出去的是带旧 Logo 的图。
        logo_path = data.get("logo_path", "") or ""
        if logo_path and os.path.exists(logo_path):
            self._load_logo(logo_path, silent=True)
        else:
            self._clear_logo(notify=False)

        # 缩放比例：WatermarkConfig 用小数 (0.10)，滑块用整数 (10)
        scale_val = data.get("scale_ratio", 0.10)
        if isinstance(scale_val, float) and scale_val <= 1.0:
            scale_val = int(scale_val * 100)
        self.scale_slider.set(int(scale_val))
        self._update_label(self.scale_value_label, f"{int(self.scale_slider.get())}%")

        # 透明度
        self.opacity_slider.set(data.get("logo_opacity", 50))
        self._update_label(self.opacity_value_label, f"{int(self.opacity_slider.get())}%")

        # 边距
        self.margin_slider.set(data.get("logo_margin", 5))
        self._update_label(self.margin_value_label, str(int(self.margin_slider.get())))

        # 旋转角度
        self.angle_slider.set(data.get("logo_angle", 0))
        self._update_label(self.angle_value_label, f"{int(self.angle_slider.get())}°")

        # 位置模式
        pos_mode = data.get("logo_position_mode", "grid")
        if hasattr(self, 'pos_mode_var'):
            self.pos_mode_var.set(pos_mode)
            self._on_pos_mode_changed()

        if pos_mode == "grid":
            # 字符串位置 → 索引
            # 先归一化：旧预设里可能存着 center-left / center-right，
            # 它们不在 _POS_NAMES 里，不翻译会静默回退到右下角
            pos_str = normalize_position(data.get("logo_position"))
            pos_map = {name: i for i, name in enumerate(self._POS_NAMES)}
            pos_idx = pos_map.get(pos_str, 8)
            self.selected_position = pos_idx
            self._restyle_position_buttons()
        else:
            if hasattr(self, 'custom_x_var'):
                # format_percent 而非 str()：拖出来的坐标是浮点，
                # str() 会出现 33.470000000000006 这种噪声值
                self.custom_x_var.set(
                    format_percent(data.get("logo_custom_x", 50)))
                self.custom_y_var.set(
                    format_percent(data.get("logo_custom_y", 50)))

        # 平铺模式
        tile = data.get("logo_tile", False)
        self.tile_var.set(tile)
        self._on_tile_toggle()

        # 平铺参数
        self.spacing_x_entry.delete(0, "end")
        self.spacing_x_entry.insert(0, str(data.get("logo_spacing_x", 100)))
        self.spacing_y_entry.delete(0, "end")
        self.spacing_y_entry.insert(0, str(data.get("logo_spacing_y", 100)))
        if self.stagger_var:
            self.stagger_var.set(data.get("logo_stagger", False))

        self._loading = False
        self._notify_change()
