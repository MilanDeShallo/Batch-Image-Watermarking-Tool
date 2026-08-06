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
            logo_frame, text="未选择", text_color="gray",
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
            fg_color="#2a2d32", corner_radius=6
        )
        self.logo_preview_label.grid(row=1, column=0, **grid_opts)

        # --- 分隔线 ---
        sep = ctk.CTkFrame(self, height=1, fg_color=("gray70", "gray30"))
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
        ctk.CTkLabel(self, text="透明度:").grid(row=row, column=0, sticky="w", padx=12)
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
        self.pos_buttons[8].configure(fg_color="#3b82f6")

        # --- 自定义坐标（默认隐藏） ---
        self.custom_pos_frame = ctk.CTkFrame(pos_frame, fg_color="transparent")

        x_row = ctk.CTkFrame(self.custom_pos_frame, fg_color="transparent")
        x_row.pack(fill="x", pady=1)
        ctk.CTkLabel(x_row, text="X (%):").pack(side="left")
        self.custom_x_var = ctk.StringVar(value="50")
        self.custom_x_entry = ctk.CTkEntry(x_row, width=60, textvariable=self.custom_x_var)
        self.custom_x_entry.pack(side="left", padx=5)
        self.custom_x_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        ctk.CTkLabel(x_row, text="(0=左 100=右)", text_color="gray", font=("Microsoft YaHei", 9)).pack(side="left")

        y_row = ctk.CTkFrame(self.custom_pos_frame, fg_color="transparent")
        y_row.pack(fill="x", pady=1)
        ctk.CTkLabel(y_row, text="Y (%):").pack(side="left")
        self.custom_y_var = ctk.StringVar(value="50")
        self.custom_y_entry = ctk.CTkEntry(y_row, width=60, textvariable=self.custom_y_var)
        self.custom_y_entry.pack(side="left", padx=5)
        self.custom_y_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        ctk.CTkLabel(y_row, text="(0=上 100=下)", text_color="gray", font=("Microsoft YaHei", 9)).pack(side="left")
        row += 1

        # --- 边距 ---
        margin_frame = ctk.CTkFrame(self, fg_color="transparent")
        margin_frame.grid(row=row, column=0, **grid_opts)
        ctk.CTkLabel(margin_frame, text="边距 (%):").pack(side="left")
        self.margin_slider = ctk.CTkSlider(
            margin_frame, from_=0, to=15, width=140,
            command=lambda v: self._update_label(self.margin_value_label, f"{int(v)}")
        )
        self.margin_slider.pack(side="left", padx=(8, 4))
        self.margin_slider.set(5)
        self.margin_value_label = ctk.CTkLabel(margin_frame, text="5", width=30)
        self.margin_value_label.pack(side="left")
        self.margin_widget = margin_frame  # 保存引用用于联动禁用
        row += 1

        # --- 分隔线 ---
        sep2 = ctk.CTkFrame(self, height=1, fg_color=("gray70", "gray30"))
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

        ctk.CTkLabel(
            self.tile_params_frame, text="垂直间距:"
        ).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.spacing_y_entry = ctk.CTkEntry(
            self.tile_params_frame, width=60, placeholder_text="100"
        )
        self.spacing_y_entry.insert(0, "100")
        self.spacing_y_entry.bind("<KeyRelease>", lambda e: self._notify_change())
        self.spacing_y_entry.grid(row=0, column=3, sticky="w")

        self._stagger_var = ctk.BooleanVar(value=False)
        stagger_check = ctk.CTkCheckBox(
            self.tile_params_frame, text="交错排列", width=90,
            variable=self._stagger_var
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

    def _clear_logo(self):
        """清除已选 Logo"""
        self.logo_path = None
        self.logo_path_label.configure(text="未选择", text_color="gray")
        if self.logo_preview_tk:
            self.logo_preview_label.configure(image="", text="")
            self.logo_preview_tk = None
        self._notify_change()  # 清除后必须刷新预览

    def _load_logo(self, path):
        """加载并预览 Logo"""
        try:
            img = Image.open(path).convert("RGBA")
            # 缩放到预览尺寸
            preview = img.copy()
            preview.thumbnail((110, 70), Image.LANCZOS)
            self.logo_preview_tk = ImageTk.PhotoImage(preview)
            self.logo_preview_label.configure(image=self.logo_preview_tk, text="")
            self.logo_path = path
            # 显示文件名
            self.logo_path_label.configure(
                text=os.path.basename(path),
                text_color=("gray10", "#e0e0e0")
            )
            self._notify_change()
        except Exception as e:
            messagebox.showerror("Logo 加载失败", f"无法打开图片:\n{e}")

    def _on_tile_toggle(self):
        """平铺模式切换时的联动禁用"""
        is_tile = self.tile_var.get()
        state = "disabled" if is_tile else "normal"
        if is_tile:
            # 禁用位置和边距
            for btn in self.pos_buttons:
                btn.configure(state="disabled", fg_color="#44474e")
            self.margin_widget.configure(state="disabled")
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
            self.pos_buttons[self.selected_position].configure(fg_color="#3b82f6")
            self.margin_widget.configure(state="normal")
            # 启用自定义坐标（根据当前模式）
            if hasattr(self, 'custom_x_entry'):
                pos_mode = self.pos_mode_var.get() if hasattr(self, 'pos_mode_var') else "grid"
                if pos_mode == "custom":
                    self.custom_x_entry.configure(state="normal")
                    self.custom_y_entry.configure(state="normal")
            # 隐藏平铺参数
            self.tile_params_frame.grid_remove()
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

    def _select_position(self, index):
        """9 宫格按钮点击"""
        self.selected_position = index
        for i, btn in enumerate(self.pos_buttons):
            if i == index:
                btn.configure(fg_color="#3b82f6")
            else:
                btn.configure(fg_color=("#c0c0c0", "#3b3b3b"))
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
    _POS_NAMES = [
        "top-left", "top-center", "top-right",
        "center-left", "center", "center-right",
        "bottom-left", "bottom-center", "bottom-right",
    ]

    def get_config_dict(self) -> dict:
        """
        返回当前面板配置字典
        字段名与 WatermarkConfig 完全对应
        """
        # 位置索引转字符串
        pos_idx = self.selected_position if isinstance(self.selected_position, int) else 8
        pos_str = self._POS_NAMES[pos_idx] if 0 <= pos_idx < 9 else "bottom-right"

        return {
            "watermark_type": "image",
            "logo_path": self.logo_path or "",
            "scale_ratio": int(self.scale_slider.get()) / 100.0,
            "logo_opacity": int(self.opacity_slider.get()),
            "logo_position": pos_str,
            "logo_position_mode": self.pos_mode_var.get() if hasattr(self, 'pos_mode_var') else "grid",
            "logo_custom_x": self._safe_int(self.custom_x_var.get(), 50) if hasattr(self, 'custom_x_var') else 50,
            "logo_custom_y": self._safe_int(self.custom_y_var.get(), 50) if hasattr(self, 'custom_y_var') else 50,
            "logo_margin": int(self.margin_slider.get()),
            "logo_tile": self.tile_var.get(),
            "logo_spacing_x": self._safe_int(self.spacing_x_entry.get(), 100),
            "logo_spacing_y": self._safe_int(self.spacing_y_entry.get(), 100),
            "logo_angle": int(self.angle_slider.get()),
        }

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

        # Logo 路径
        logo_path = data.get("logo_path", "")
        if logo_path and os.path.exists(logo_path):
            self._load_logo(logo_path)

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
        self._update_label(self.angle_value_label, f"{int(self.angle_slider.get())}")

        # 位置模式
        pos_mode = data.get("logo_position_mode", "grid")
        if hasattr(self, 'pos_mode_var'):
            self.pos_mode_var.set(pos_mode)
            self._on_pos_mode_changed()

        if pos_mode == "grid":
            # 字符串位置 → 索引
            pos_str = data.get("logo_position", "bottom-right")
            pos_map = {name: i for i, name in enumerate(self._POS_NAMES)}
            pos_idx = pos_map.get(pos_str, 8)
            self.selected_position = pos_idx
            for i, btn in enumerate(self.pos_buttons):
                if i == pos_idx:
                    btn.configure(fg_color="#3b82f6")
                else:
                    btn.configure(fg_color=("#c0c0c0", "#3b3b3b"))
        else:
            if hasattr(self, 'custom_x_var'):
                self.custom_x_var.set(str(data.get("logo_custom_x", 50)))
                self.custom_y_var.set(str(data.get("logo_custom_y", 50)))

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
            self.stagger_var.set(data.get("stagger", False))

        self._loading = False
        self._notify_change()

    @staticmethod
    def _safe_int(value, default):
        """安全转换为整数"""
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
