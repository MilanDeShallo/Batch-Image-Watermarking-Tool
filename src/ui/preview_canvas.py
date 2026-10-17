"""
实时预览画布 — 后台渲染 + 缩放 + 拖动查看

特性：
1. 后台线程渲染水印，主线程仅通过 after() 更新 UI
2. 渲染「单飞」：同一时刻只有一个渲染线程，期间的请求合并成一次
3. 缩放 25%-200%，Ctrl+滚轮，鼠标拖动查看
4. 缩放只缩放缓存图，不重新渲染水印
5. 窗口大小变化时自动重新适应
6. 拖动范围限制（不能拖出视野）
7. 预览分辨率随画布自适应，并把缩放比例交给引擎，
   使预览的几何与导出结果一致（见 watermark_engine.scaled_px）

性能约定（改这里之前先看这段）
------------------------------
预览要"丝滑"，必须避免三类重复劳动，本文件对每一类都做了缓存：
1. 解码：按 (路径, 页) 缓存解码+缩放后的底图，参数微调时不再重新解码
   （一张 6000x4000 JPEG 解码 61ms + LANCZOS 缩到 4000px 252ms）
2. 渲染：参数变化才重渲染；缩放/拖动只重采样已渲染好的缓存图
3. 显示：显示图按 (画布尺寸, 缩放, 渲染版本) 缓存，拖动只移动画布项
   （每次拖动事件重采样 1800px 图要 25ms，而事件频率约 60-100 次/秒）
历史实现三者皆无，实测拖动一帧 35ms、参数微调一次刷新 500ms 以上，
表现就是预览卡顿且拖不动。
"""

import customtkinter as ctk
from PIL import Image, ImageTk
import threading
import time
import os

from src.ui import theme
from src.video_engine import format_time, is_video_file


class PreviewCanvas(ctk.CTkFrame):

    # 工作分辨率 = 画布长边 × 该倍数（放大到 200% 仍有足够像素），再夹取到上下限。
    # 直接在原图分辨率上渲染水印既慢又看不出差别：画布只有几百像素宽，
    # 4000px 与 1600px 的渲染结果缩到画布上看不出区别，但耗时差 6 倍。
    WORK_ZOOM_FACTOR = 2.0
    WORK_MIN_DIM = 900
    WORK_MAX_DIM = 1800

    DEBOUNCE_MS = 150
    ZOOM_THROTTLE_MS = 50
    # 拖动视频时间轴的抽帧防抖。
    # 比普通参数防抖长一些：抽一帧要起一次 ffmpeg 进程（1080p 约 80~200ms），
    # 按 150ms 的节奏抽帧会排队，滑块反而跟手变卡。
    VIDEO_SEEK_MS = 260

    def __init__(self, parent, on_config_changed=None, on_page_changed=None,
                 on_watermark_moved=None, **kwargs):
        super().__init__(parent, **kwargs)

        self.on_config_changed = on_config_changed
        # 翻页回调：主窗口据此把面板切换到该页的专属参数
        self.on_page_changed = on_page_changed
        # 拖动水印结束时的回调：(kind, x_pct, y_pct) —— 由主窗口写回面板
        self.on_watermark_moved = on_watermark_moved
        self.current_image_path = None
        self._display_image_tk = None
        self._debounce_timer = None
        self._generation = 0

        # 缓存：已渲染的水印图
        self._cached_render: Image.Image | None = None
        self._cached_orig_size = (0, 0)

        # 渲染单飞状态（只允许主线程读写）
        self._render_busy = False
        self._render_pending = False

        # 底图缓存（解码+缩放后的渲染底图，供后台线程复用）
        # ref = 参考分辨率（导出时用的尺寸）；scale = 工作尺寸 / 参考尺寸
        self._base_lock = threading.Lock()
        self._base_key = None
        self._base_work: Image.Image | None = None
        self._base_ref = (0, 0)
        self._base_scale = 1.0
        self._base_page_count = None   # PDF 总页数（随之缓存，避免重复解析）
        self._base_page_index = 0      # 底图对应的页码（已夹到合法范围）

        # 视频预览状态。
        # _video_time 是"当前预览到第几秒"，进底图缓存键 —— 拖时间轴就是换这一帧。
        # _video_info 在选中文件时探测一次（主线程，约 100ms），
        # 之后底图准备、时间轴上限都复用它，不再重复 ffprobe。
        self._video_time = 0.0
        self._video_duration = 0.0
        self._video_info = None
        self._video_seek_timer = None

        # 显示缓存：显示图只在 (画布尺寸, 缩放, 渲染版本) 变化时重采样
        self._display_key = None
        self._display_size = (0, 0)
        self._render_token = 0
        self._zoom_timer = None
        self._last_zoom_apply = 0.0

        # PDF 翻页状态（仅 PDF 文件使用）
        self._pdf_page = 0            # 当前预览页码（0 起）
        self._pdf_page_count = None   # 总页数（首次渲染完成后回填）
        self._pdf_marked: list[int] = []  # 有专属参数的页码（0 起），由主窗口回填

        # 缩放
        self._zoom_var = ctk.DoubleVar(value=1.0)
        self._zoom_label_var = ctk.StringVar(value="100%")

        # 拖动偏移
        self._pan_offset = (0, 0)
        self._pan_start = None

        # ==== 拖动水印：预览图上直接定位 ====
        # 拖动期间只移动 canvas item（零重绘），所以是丝滑的；
        # 松手后才走正常渲染链路提交一次，导出与预览仍然同源
        self._wm_boxes = []            # 可拖动水印的矩形（渲染图坐标，上层在前）
        self._wm_base_render = None    # 不含水印的渲染底图（拖动时当背景，避免残影）
        self._wm_cfg = None            # 上次渲染用的配置（现算精灵/命中框要用）
        self._wm_scale = 1.0           # 上次渲染的 scale
        self._wm_drag = None           # 拖动状态；None = 未在拖
        self._wm_sprite_tk = None      # 拖动期间显示的精灵（Tk 图像）
        self._wm_sprite_item = None    # 精灵的 canvas item
        self._wm_base_tk = None        # 无水印底图的 Tk 图像（显示尺寸，按需生成）
        self._wm_base_key = None       # 底图 Tk 图像对应的（渲染版本, 显示尺寸）
        self._wm_guides = []           # 吸附对齐线
        self._wm_readout = None        # 拖动中的坐标读数 canvas item
        self._wm_cursor = ""           # 当前光标（悬停在水印上是手型）
        self._wm_live = None           # 拖动中的实时坐标 (x_pct, y_pct)

        self._build_ui()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 预览 Canvas
        canvas_frame = ctk.CTkFrame(
            self, fg_color=theme.SURFACE_ALT, corner_radius=theme.RADIUS_CARD
        )
        canvas_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 2))
        canvas_frame.grid_columnconfigure(0, weight=1)
        canvas_frame.grid_rowconfigure(0, weight=1)

        import tkinter as tk
        # 原生 tk.Canvas 不认 CustomTkinter 的主题值，底色直接从 theme 取；
        # 皮肤固定浅色，不存在"跟随切换"的场景（见 src/ui/theme.py）
        self._canvas = tk.Canvas(
            canvas_frame,
            bg=theme.SURFACE_ALT,
            highlightthickness=0,
            cursor="hand2",
        )
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        self._placeholder_id = self._canvas.create_text(
            0, 0, text="拖入图片后在此预览水印效果",
            fill=theme.TEXT_MUTED, font=("Microsoft YaHei", 13)
        )
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._image_item = None

        # 鼠标拖动 + Ctrl 滚轮
        self._canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self._canvas.bind("<B1-Motion>", self._on_pan_motion)
        self._canvas.bind("<ButtonRelease-1>", self._on_pan_end)
        self._canvas.bind("<Control-MouseWheel>", self._on_ctrl_scroll)
        # 悬停时光标变手型：不这样没人知道水印是可以拖的
        self._canvas.bind("<Motion>", self._on_canvas_motion)
        self._canvas.bind("<Leave>", self._on_canvas_leave)

        # 底部缩放控制栏
        zoom_bar = ctk.CTkFrame(self, fg_color="transparent")
        zoom_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))

        # 缩放按钮：默认字号下 "−" 只有一条细缝，几乎看不见；
        # 提到 15pt 加粗后与「适应窗口」按钮的视觉重量才匹配
        zoom_btn_font = ("Microsoft YaHei", 15, "bold")
        self._zoom_out_btn = ctk.CTkButton(
            zoom_bar, text="−", width=30, height=28, font=zoom_btn_font,
            command=lambda: self._adjust_zoom(-0.1)
        )
        self._zoom_out_btn.pack(side="left", padx=2)

        # 滑块宽度 110：这一条栏里还有"显示/原图分辨率"两个右侧标签，
        # 实测滑块 150 时整栏会超宽，两个标签被挤成 1px（信息直接消失）
        self._zoom_slider = ctk.CTkSlider(
            zoom_bar, from_=0.25, to=2.0,
            variable=self._zoom_var, width=110,
            command=self._on_zoom_slider
        )
        self._zoom_slider.pack(side="left", padx=4)
        self._zoom_slider.set(1.0)

        self._zoom_in_btn = ctk.CTkButton(
            zoom_bar, text="+", width=30, height=28, font=zoom_btn_font,
            command=lambda: self._adjust_zoom(0.1)
        )
        self._zoom_in_btn.pack(side="left", padx=2)

        self._fit_btn = ctk.CTkButton(
            zoom_bar, text="适应窗口", width=78, height=28,
            command=self._fit_to_window
        )
        self._fit_btn.pack(side="left", padx=(8, 2))

        self._zoom_display = ctk.CTkLabel(
            zoom_bar, textvariable=self._zoom_label_var,
            width=50, font=("Microsoft YaHei", 11)
        )
        self._zoom_display.pack(side="left", padx=4)

        self.info_label = ctk.CTkLabel(
            zoom_bar, text="", text_color=theme.TEXT_MUTED, font=("Microsoft YaHei", 10)
        )
        self.info_label.pack(side="right", padx=4)

        self.size_label = ctk.CTkLabel(
            zoom_bar, text="", text_color=theme.TEXT_MUTED, font=("Microsoft YaHei", 10)
        )
        self.size_label.pack(side="right", padx=4)

        # ---- PDF 翻页栏（仅 PDF 文件时显示） ----
        self._pdf_bar = ctk.CTkFrame(self, fg_color="transparent")
        self._pdf_bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        self._pdf_bar.grid_remove()  # 初始隐藏

        self._pdf_prev_btn = ctk.CTkButton(
            self._pdf_bar, text="◀ 上一页", width=78, height=26,
            font=("Microsoft YaHei", 11),
            command=lambda: self._go_pdf_page(-1),
        )
        self._pdf_prev_btn.pack(side="left", padx=(0, 4))

        self._pdf_page_var = ctk.StringVar(value="第 1 页 / 共 ? 页")
        self._pdf_page_label = ctk.CTkLabel(
            self._pdf_bar, textvariable=self._pdf_page_var,
            width=130, font=("Microsoft YaHei", 11),
        )
        self._pdf_page_label.pack(side="left", padx=4)

        self._pdf_next_btn = ctk.CTkButton(
            self._pdf_bar, text="下一页 ▶", width=78, height=26,
            font=("Microsoft YaHei", 11),
            command=lambda: self._go_pdf_page(1),
        )
        self._pdf_next_btn.pack(side="left", padx=(4, 0))

        ctk.CTkLabel(
            self._pdf_bar, text="跳到第", text_color=theme.TEXT_MUTED,
            font=("Microsoft YaHei", 10),
        ).pack(side="left", padx=(14, 2))

        self._pdf_jump_entry = ctk.CTkEntry(self._pdf_bar, width=54, height=26)
        self._pdf_jump_entry.pack(side="left")
        self._pdf_jump_entry.bind("<Return>", lambda e: self._jump_pdf_page())
        theme.bind_focus_ring(self._pdf_jump_entry)

        ctk.CTkLabel(
            self._pdf_bar, text="页（回车）", text_color=theme.TEXT_MUTED,
            font=("Microsoft YaHei", 10),
        ).pack(side="left", padx=(2, 4))

        # 哪些页被单独设过参数（由主窗口回填，翻页时一眼可见）
        self._pdf_marked_label = ctk.CTkLabel(
            self._pdf_bar, text="", text_color=theme.WARN,
            font=("Microsoft YaHei", 10),
        )
        self._pdf_marked_label.pack(side="left", padx=(12, 2))

        # ---- 视频时间轴（仅视频文件时显示） ----
        # 与 PDF 翻页栏共用 row=2：同一个文件不可能既是 PDF 又是视频，
        # 两者互斥显示，不额外占用预览区高度。
        # 拖动滑块 → 抽该秒的画面当底图 → 水印照常叠加，于是"后面那一帧长什么样"
        # 与首帧完全一样地可见、可拖。
        self._video_bar = ctk.CTkFrame(self, fg_color="transparent")
        self._video_bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        self._video_bar.grid_remove()  # 初始隐藏

        self._video_time_var = ctk.StringVar(value="00:00")
        ctk.CTkLabel(
            self._video_bar, textvariable=self._video_time_var, width=46,
            font=("Consolas", 11),
        ).pack(side="left")

        self._video_slider = ctk.CTkSlider(
            self._video_bar, from_=0, to=1, number_of_steps=1000,
            height=18, command=self._on_video_seek,
        )
        self._video_slider.pack(side="left", fill="x", expand=True, padx=10)
        self._video_slider.set(0)

        self._video_duration_label = ctk.CTkLabel(
            self._video_bar, text="00:00", width=46,
            text_color=theme.TEXT_MUTED, font=("Consolas", 11),
        )
        self._video_duration_label.pack(side="left")

    # ==================== 缩放 ====================

    def _on_zoom_slider(self, value):
        """滑块回调（拖动时高频触发）→ 节流后再重采样"""
        self._zoom_label_var.set(f"{int(value * 100)}%")
        self._request_zoom_apply()

    def _request_zoom_apply(self):
        """
        节流：第一次立即响应，之后的拖动合并到 ZOOM_THROTTLE_MS 后。

        拖滑块时每个像素都会触发 command，若每帧都重采样 1800px 缓存图，
        一次拖动就是几十次 20ms+ 的重采样，滑动条会明显跟手迟滞。
        """
        now = time.monotonic()
        if self._zoom_timer is None:
            if (now - self._last_zoom_apply) * 1000 >= self.ZOOM_THROTTLE_MS:
                self._last_zoom_apply = now
                self._apply_zoom()
            else:
                self._zoom_timer = self.after(
                    self.ZOOM_THROTTLE_MS, self._zoom_timer_fire
                )

    def _zoom_timer_fire(self):
        self._zoom_timer = None
        self._last_zoom_apply = time.monotonic()
        self._apply_zoom()

    def _adjust_zoom(self, delta):
        new_val = max(0.25, min(2.0, self._zoom_var.get() + delta))
        self._zoom_var.set(new_val)
        self._zoom_label_var.set(f"{int(new_val * 100)}%")
        self._last_zoom_apply = time.monotonic()
        self._apply_zoom()

    def _fit_to_window(self):
        """适应窗口：重置缩放和偏移"""
        self._zoom_var.set(1.0)
        self._zoom_label_var.set("100%")
        self._pan_offset = (0, 0)
        self._last_zoom_apply = time.monotonic()
        self._apply_zoom()

    def _on_ctrl_scroll(self, event):
        if event.delta > 0:
            self._adjust_zoom(0.1)
        else:
            self._adjust_zoom(-0.1)

    def _on_canvas_resize(self, event=None):
        """Canvas 尺寸变化时重新适应（仅缩放显示，不重新渲染水印）"""
        if self._cached_render is not None:
            self._apply_zoom()
        else:
            self._center_placeholder()

    def _apply_zoom(self, force: bool = False):
        """
        从缓存图按缩放比例显示。

        显示图按 (画布尺寸, 缩放, 渲染版本) 缓存：拖动时参数不变，
        命中缓存后只做「移动画布项」，不再做任何图像重采样。
        """
        if self._cached_render is None:
            return

        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)
        zoom = round(self._zoom_var.get(), 4)

        key = (cw, ch, zoom, self._render_token)
        if force or key != self._display_key:
            self._rebuild_display(cw, ch, zoom)
            self._display_key = key

        self._draw_canvas_image()

    def _rebuild_display(self, cw: int, ch: int, zoom: float):
        """按「适应窗口 × zoom」重采样缓存图，并生成 Tk 图像对象"""
        base = self._cached_render
        if not base or base.width <= 0:
            return

        # 先 fit 到画布，再乘缩放：zoom=1.0 即"适应窗口"
        fit = min(cw / base.width, ch / base.height)
        total = fit * zoom
        w = max(1, int(round(base.width * total)))
        h = max(1, int(round(base.height * total)))

        if (w, h) == base.size:
            display = base
        elif total < 1.0:
            display = base.resize((w, h), Image.LANCZOS)   # 缩小：LANCZOS 更锐利
        else:
            display = base.resize((w, h), Image.BICUBIC)   # 放大：BICUBIC 更快且够用

        if display.mode != "RGB":
            display = display.convert("RGB")

        self._display_image_tk = ImageTk.PhotoImage(display)
        self._display_size = (display.width, display.height)

        if self._cached_orig_size[0] > 0:
            # 显示的是"适应窗口后的像素数"，不是原图分辨率 —— 别让用户误判清晰度
            self.size_label.configure(
                text=f"显示: {display.width}x{display.height} ({int(zoom * 100)}%)"
            )

    def _draw_canvas_image(self):
        """把当前显示图放到 Canvas 上（只移动位置，不重采样）"""
        w, h = self._display_size
        if w <= 0 or h <= 0 or self._display_image_tk is None:
            return

        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)

        self._canvas.itemconfig(self._placeholder_id, text="")

        if self._image_item is not None:
            self._canvas.delete(self._image_item)

        # 限制偏移范围（不能把图片拖出视野）
        max_dx = (w - cw) // 2 if w > cw else 0
        max_dy = (h - ch) // 2 if h > ch else 0

        self._pan_offset = (
            max(-max_dx, min(max_dx, self._pan_offset[0])),
            max(-max_dy, min(max_dy, self._pan_offset[1])),
        )

        # 居中 + 偏移（与 _image_origin 同源：命中判定要按同一处坐标换算）
        x, y = self._image_origin()

        self._image_item = self._canvas.create_image(
            x, y, anchor="nw", image=self._display_image_tk
        )

    # ==================== 拖动 ====================

    def _on_pan_start(self, event):
        # 按在水印墨迹上 = 拖水印；按在别处 = 平移画面
        hit = self._wm_hit_test(event.x, event.y)
        if hit is not None and self._wm_drag_begin(hit, event):
            return
        self._pan_start = (event.x, event.y)

    def _on_pan_motion(self, event):
        """
        拖动查看：只移动画布项，完全不碰图像数据。

        拖动事件每秒 60-100 次，旧实现在这里调 _apply_zoom()，
        每次都要复制 1800px 缓存图 + LANCZOS 重采样（实测 25ms/帧），
        于是拖动既迟滞又掉帧。显示图已按缩放缓存，移动是零成本的。
        """
        if self._wm_drag is not None and self._wm_drag_motion(event):
            return
        if self._pan_start is None or self._cached_render is None:
            return
        dx = event.x - self._pan_start[0]
        dy = event.y - self._pan_start[1]
        self._pan_offset = (self._pan_offset[0] + dx, self._pan_offset[1] + dy)
        self._pan_start = (event.x, event.y)
        self._draw_canvas_image()

    def _on_pan_end(self, event):
        if self._wm_drag is not None:
            self._wm_drag_end(event)
            return
        self._pan_start = None

    # ==================== 拖动水印（预览图上直接定位）====================
    #
    # 交互：按在**水印墨迹上**拖动 = 移动水印（自动切到"自定义"定位）；
    #       按在其它地方拖动 = 平移画面（原有行为不变）。
    #
    # 为什么拖动期间不重新渲染：一次完整渲染要过后台线程 + 重采样 + 生成 Tk 图像，
    # 每帧几十毫秒，拖起来就是"黏"的。这里的做法是——按下时准备好两张显示图
    # （无水印底图 + 精灵），拖动期间只 canvas.coords() 移动精灵项；
    # 松手后再走正常渲染链路提交一次。手感丝滑，最终结果仍由引擎产出。

    _SNAP_PX = 8                     # 吸附阈值（显示像素）
    _SNAP_LINES = (0.0, 50.0, 100.0)  # 吸附线：左/上、居中、右/下
    _ALT_MASK = 0x0008 | 0x20000     # Windows 上 Alt 的 state 位（两个都认）

    def _refresh_watermark_boxes(self):
        """按当前配置算出可拖动水印的矩形（渲染图坐标，上层在前）"""
        self._wm_boxes = []
        try:
            from src.watermark_engine import watermark_boxes

            cfg = self._wm_cfg
            base = self._wm_base_render
            if cfg is None or base is None:
                return
            self._wm_boxes = watermark_boxes(cfg, base.size, self._wm_scale)
        except Exception:
            # 命中判定失败只意味着"这次拖不了水印"，平移画面照常
            self._wm_boxes = []

    def _image_origin(self) -> tuple:
        """当前显示图在画布上的左上角（居中 + 平移量），与 _draw_canvas_image 同源"""
        w, h = self._display_size
        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)
        return (cw - w) // 2 + self._pan_offset[0], (ch - h) // 2 + self._pan_offset[1]

    def _display_scale(self) -> float:
        """显示图 / 渲染图 的缩放比（渲染图坐标 × 它 = 显示图坐标）"""
        if self._cached_render is None or self._cached_render.width <= 0:
            return 0.0
        if self._display_size[0] <= 0:
            return 0.0
        return self._display_size[0] / self._cached_render.width

    def _wm_hit_test(self, cx: float, cy: float):
        """画布坐标是否落在某个可拖动水印上；是则返回那一条描述"""
        k = self._display_scale()
        if k <= 0 or not self._wm_boxes:
            return None
        ox, oy = self._image_origin()
        rx = (cx - ox) / k
        ry = (cy - oy) / k
        for item in self._wm_boxes:      # 已按"上层在前"排好序
            x, y, w, h = item["box"]
            if x <= rx <= x + w and y <= ry <= y + h:
                return item
        return None

    def _wm_tile_hint(self) -> bool:
        """
        有水印可画、却因为没有位置可拖（平铺）而不能拖 → 值得给一句提示。

        只看"存在哪些水印、它们是不是平铺"，不看内容细节；
        _wm_boxes 非空说明本来就有可拖的水印，那就没什么要解释的。
        """
        cfg = self._wm_cfg
        if cfg is None or self._wm_boxes:
            return False
        wtype = getattr(cfg, "watermark_type", "text")
        has_text = wtype in ("text", "both") and bool(getattr(cfg, "text", "").strip())
        has_logo = wtype in ("image", "both") and bool(getattr(cfg, "logo_path", None))
        if not (has_text or has_logo):
            return False
        return ((has_text and getattr(cfg, "tile", False))
                or (has_logo and getattr(cfg, "logo_tile", False)))

    def _on_canvas_motion(self, event):
        """悬停反馈：水印上给手型光标（"这里可以拖"的唯一提示）"""
        if self._wm_drag is not None:
            return
        want = "hand2" if self._wm_hit_test(event.x, event.y) else ""
        if want != self._wm_cursor:
            self._wm_cursor = want
            try:
                self._canvas.configure(cursor=want)
            except Exception:
                pass

    def _on_canvas_leave(self, _event=None):
        if self._wm_cursor and self._wm_drag is None:
            self._wm_cursor = ""
            try:
                self._canvas.configure(cursor="")
            except Exception:
                pass

    def _wm_drag_begin(self, item, event) -> bool:
        """进入拖动状态：备好"无水印底图 + 精灵"两张显示图"""
        from src.watermark_engine import build_watermark_sprite

        base = self._wm_base_render
        cfg = self._wm_cfg
        k = self._display_scale()
        if base is None or cfg is None or k <= 0:
            return False

        try:
            # kind 要显式传：两种水印可以同时存在（watermark_type="both"），
            # 拖的是哪个就得建哪个
            sprite_render = build_watermark_sprite(
                cfg, base.size, self._wm_scale, item["kind"]
            )
        except Exception:
            return False
        if sprite_render is None:
            return False

        dw, dh = self._display_size
        sw = max(1, int(round(sprite_render.width * k)))
        sh = max(1, int(round(sprite_render.height * k)))

        # 无水印底图：拖动期间把画面换成它，否则原位置会留下一份水印残影。
        # 按（渲染版本, 显示尺寸）缓存 —— 缩放变化会自然失效重算。
        key = (self._render_token, dw, dh)
        if self._wm_base_tk is None or self._wm_base_key != key:
            resample = Image.LANCZOS if k < 1 else Image.BICUBIC
            base_display = base.convert("RGB").resize((dw, dh), resample)
            self._wm_base_tk = ImageTk.PhotoImage(base_display)
            self._wm_base_key = key

        # 精灵直接做成 Tk 图像：Tk 8.6 的 photo 支持逐像素 alpha，
        # 半透明文字叠在底图上是对的（A/B 实测过，见 .workbuddy/tmp/ab_sprite.py）。
        # 这样拖动期间每帧只剩一次 canvas.coords()，零重绘 —— 手感最顺。
        self._wm_sprite_tk = ImageTk.PhotoImage(
            sprite_render.resize((sw, sh), Image.LANCZOS)
        )

        x, y, _w, _h = item["box"]
        ox, oy = self._image_origin()
        if self._image_item is not None:
            self._canvas.itemconfig(self._image_item, image=self._wm_base_tk)
        self._wm_sprite_item = self._canvas.create_image(
            ox + x * k, oy + y * k, anchor="nw", image=self._wm_sprite_tk
        )
        self._wm_drag = {
            "kind": item["kind"],
            "render_size": base.size,
            "sprite_size": sprite_render.size,
            "start": (event.x, event.y),
            "anchor": (float(x), float(y)),
            "origin": (ox, oy),
            "k": k,
            "live": None,
        }
        try:
            self._canvas.configure(cursor="fleur")
        except Exception:
            pass
        return True

    def _wm_drag_motion(self, event) -> bool:
        """拖动中：只移动精灵项 + 更新对齐线/读数，不触发任何渲染"""
        st = self._wm_drag
        if st is None:
            return False

        from src.watermark_engine import (
            custom_anchor_position, custom_percent_for_anchor,
        )

        k = st["k"]
        target_x = st["anchor"][0] + (event.x - st["start"][0]) / k
        target_y = st["anchor"][1] + (event.y - st["start"][1]) / k

        # 像素 → 百分比：夹取规则与导出共用同一个 clamp_anchor，
        # 所以拖出边界时数值会自己收敛，不会越拖越偏
        x_pct, y_pct = custom_percent_for_anchor(
            st["render_size"], st["sprite_size"], target_x, target_y
        )

        guides = None
        if not (event.state & self._ALT_MASK):
            x_pct, y_pct, guides = self._wm_snap(x_pct, y_pct)

        # 再由引擎的同一套换算回算像素位置：看到的 = 导出的
        px, py = custom_anchor_position(
            st["render_size"], st["sprite_size"], x_pct, y_pct
        )
        ox, oy = st["origin"]
        sx, sy = ox + px * k, oy + py * k
        self._canvas.coords(self._wm_sprite_item, sx, sy)

        self._wm_show_guides(guides, ox, oy, k, st)
        self._wm_show_readout(x_pct, y_pct, sx, sy, k, st)
        st["live"] = (x_pct, y_pct)
        return True

    def _wm_snap(self, x_pct, y_pct):
        """
        靠近 0 / 50 / 100% 时吸附到整值，返回 (x, y, 参考线)。

        阈值按**显示像素**折算成百分比：写死百分比的话，图越大越难吸住
        （同样 6px 在 4000px 宽的图上只有 0.15%）。按住 Alt 可临时关闭。
        """
        dw = max(1, self._display_size[0])
        dh = max(1, self._display_size[1])
        thr_x = self._SNAP_PX / dw * 100
        thr_y = self._SNAP_PX / dh * 100

        guides = {"x": None, "y": None}
        for line in self._SNAP_LINES:
            if abs(x_pct - line) <= thr_x:
                x_pct, guides["x"] = line, line
                break
        for line in self._SNAP_LINES:
            if abs(y_pct - line) <= thr_y:
                y_pct, guides["y"] = line, line
                break
        return x_pct, y_pct, (guides if (guides["x"] is not None or guides["y"] is not None) else None)

    def _wm_show_guides(self, guides, ox, oy, k, st):
        """画吸附参考线（虚线，主色）"""
        for item in self._wm_guides:
            self._canvas.delete(item)
        self._wm_guides = []
        if not guides:
            return
        rw, rh = st["render_size"]
        if guides["x"] is not None:
            gx = ox + rw * guides["x"] / 100 * k
            self._wm_guides.append(self._canvas.create_line(
                gx, oy, gx, oy + rh * k, fill=theme.ACCENT, dash=(4, 4)
            ))
        if guides["y"] is not None:
            gy = oy + rh * guides["y"] / 100 * k
            self._wm_guides.append(self._canvas.create_line(
                ox, gy, ox + rw * k, gy, fill=theme.ACCENT, dash=(4, 4)
            ))

    def _wm_show_readout(self, x_pct, y_pct, sx, sy, k, st):
        """水印旁边显示实时百分比（图片深浅不定，所以垫一块底色）"""
        from src.config import format_percent

        text = f"{format_percent(x_pct)}% , {format_percent(y_pct)}%"
        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)
        sw = st["sprite_size"][0] * k
        tx = min(max(sx + sw + 6, 4), max(4, cw - 96))
        ty = min(max(sy, 4), max(4, ch - 22))

        if self._wm_readout is None:
            self._wm_readout = self._canvas.create_text(
                tx, ty, anchor="nw", text=text, fill=theme.TEXT,
                font=("Microsoft YaHei", 10),
            )
        else:
            self._canvas.itemconfig(self._wm_readout, text=text)
            self._canvas.coords(self._wm_readout, tx, ty)

    def _wm_drag_end(self, _event=None) -> bool:
        """松手：收尾并把最终坐标交回主窗口（由它写进面板 → 正常渲染）"""
        st = self._wm_drag
        if st is None:
            return False
        live = st.get("live")
        kind = st["kind"]
        self._wm_drag_cancel()
        if live is not None and self.on_watermark_moved is not None:
            self.on_watermark_moved(kind, live[0], live[1])
        return True

    def _wm_drag_cancel(self):
        """清理拖动状态：删临时项、画面换回正常渲染图"""
        st = self._wm_drag
        self._wm_drag = None

        for item in self._wm_guides:
            self._canvas.delete(item)
        self._wm_guides = []
        if self._wm_readout is not None:
            self._canvas.delete(self._wm_readout)
            self._wm_readout = None
        if self._wm_sprite_item is not None:
            self._canvas.delete(self._wm_sprite_item)
            self._wm_sprite_item = None
        if self._image_item is not None and self._display_image_tk is not None:
            try:
                self._canvas.itemconfig(self._image_item, image=self._display_image_tk)
            except Exception:
                pass
        self._wm_sprite_tk = None

        if st is not None:
            self._wm_cursor = ""
            try:
                self._canvas.configure(cursor="")
            except Exception:
                pass

    # ==================== 占位文字 ====================

    def _center_placeholder(self, event=None):
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        self._canvas.coords(self._placeholder_id, cw // 2, ch // 2)

    # ==================== 公共接口 ====================

    def set_image(self, image_path: str):
        if not image_path or not os.path.exists(image_path):
            self._show_placeholder("图片不存在")
            return
        if image_path != self.current_image_path:
            # 换文件时回到第 1 页与居中视野；总页数等首次渲染完成后回填
            self._pdf_page = 0
            self._pdf_page_count = None
            self._pan_offset = (0, 0)
            # 视频也回到开头：沿用上一条片子停在的秒数，
            # 换文件后会莫名其妙地看到"第 12 秒"，而那条片子可能只有 5 秒
            self._video_time = 0.0
            self._video_duration = 0.0
            self._video_info = None
        self.current_image_path = image_path
        if self._is_video():
            self._load_video_info(image_path)
        self._update_pdf_bar()
        self._update_video_bar()
        self._schedule_refresh()

    def request_refresh(self):
        if not self.current_image_path:
            return
        self._schedule_refresh()

    def clear_preview(self):
        self.current_image_path = None
        self._invalidate_base()
        self._pdf_page = 0
        self._pdf_page_count = None
        self._pan_offset = (0, 0)
        self._video_time = 0.0
        self._video_duration = 0.0
        self._video_info = None
        self._cancel_debounce()
        self._cancel_video_seek()
        self._update_pdf_bar()
        self._update_video_bar()
        self._show_placeholder("拖入图片后在此预览水印效果")

    @property
    def current_pdf_page(self) -> int:
        """当前预览页（0 起；非 PDF 时恒为 0）"""
        return self._pdf_page

    def set_pdf_marked_pages(self, pages) -> None:
        """回填「哪些页被单独设过参数」，在翻页栏里显示"""
        try:
            self._pdf_marked = sorted({int(p) for p in (pages or [])})
        except (TypeError, ValueError):
            self._pdf_marked = []
        self._update_pdf_marked_label()

    def _update_pdf_marked_label(self):
        if not self._pdf_marked:
            self._pdf_marked_label.configure(text="")
            return
        shown = self._pdf_marked[:8]
        text = "专属页: " + ", ".join(str(p + 1) for p in shown)
        if len(self._pdf_marked) > len(shown):
            text += f" …共 {len(self._pdf_marked)} 页"
        self._pdf_marked_label.configure(text=text)

    # ==================== PDF 翻页 ====================

    def _is_pdf(self) -> bool:
        """当前预览的文件是否为 PDF"""
        return bool(self.current_image_path) and self.current_image_path.lower().endswith(".pdf")

    def _go_pdf_page(self, delta: int):
        """上一页 / 下一页"""
        if not self._is_pdf():
            return
        self._set_pdf_page(self._pdf_page + delta)

    def _jump_pdf_page(self):
        """跳到输入框指定的页（用户输入 1 起页码）"""
        from src.validators import parse_number

        value = parse_number(self._pdf_jump_entry.get(), integer=True)
        if value is None:
            self._update_pdf_bar()  # 输入非法：还原为当前页
            return
        self._set_pdf_page(int(value) - 1)

    def _set_pdf_page(self, page_index: int):
        """切换预览页（越界夹取；页码未变则不重复渲染）"""
        if not self._is_pdf():
            return

        count = self._pdf_page_count
        if count:
            page_index = max(0, min(count - 1, page_index))
        else:
            page_index = max(0, page_index)

        if page_index == self._pdf_page:
            self._update_pdf_bar()
            return

        self._pdf_page = page_index
        self._update_pdf_bar()
        # 通知主窗口：面板需要切换到这一页的专属参数
        if self.on_page_changed:
            self.on_page_changed(self._pdf_page)
        self._schedule_refresh()

    def _update_pdf_bar(self):
        """刷新翻页栏的显示与按钮可用状态"""
        if not self._is_pdf():
            self._pdf_bar.grid_remove()
            return

        self._pdf_bar.grid()
        count = self._pdf_page_count
        total = str(count) if count else "?"
        self._pdf_page_var.set(f"第 {self._pdf_page + 1} 页 / 共 {total} 页")
        self._pdf_prev_btn.configure(
            state="normal" if (not count or self._pdf_page > 0) else "disabled"
        )
        self._pdf_next_btn.configure(
            state="normal" if (not count or self._pdf_page < count - 1) else "disabled"
        )
        # 回填当前页；正在输入时不打断用户
        try:
            editing = self.focus_get() is self._pdf_jump_entry
        except Exception:
            editing = False
        if not editing:
            self._pdf_jump_entry.delete(0, "end")
            self._pdf_jump_entry.insert(0, str(self._pdf_page + 1))

    # ==================== 视频时间轴 ====================

    def _is_video(self) -> bool:
        """当前预览的文件是否为视频"""
        return bool(self.current_image_path) and is_video_file(self.current_image_path)

    @property
    def current_video_time(self) -> float:
        """当前预览的秒数（非视频恒为 0）"""
        return self._video_time if self._is_video() else 0.0

    def _load_video_info(self, path: str):
        """
        探测视频时长（一次性）。

        为什么在 set_image 里同步做：时间轴的右端得立刻显示总时长，
        否则用户看到的是一根"长度未知"的滑块。一次 ffprobe 约 100ms，
        落在刚点完文件的那一下，感知不到。
        """
        from src.video_engine import probe_video

        try:
            info = probe_video(path)
            self._video_info = info
            self._video_duration = max(0.1, float(info.duration or 0.0))
        except Exception:
            # 探测失败不该挡住界面：时间轴退化成"只有 0 秒"，
            # 首帧预览仍会走 _prepare_base 里的兜底路径
            self._video_info = None
            self._video_duration = 0.0

    def _update_video_bar(self):
        """刷新时间轴的显示、上限与两端时间文字"""
        if not self._is_video():
            self._video_bar.grid_remove()
            return

        self._video_bar.grid()
        duration = max(0.5, self._video_duration)
        self._video_slider.configure(to=duration)
        self._video_slider.set(min(self._video_time, duration))
        self._video_time_var.set(format_time(self._video_time))
        self._video_duration_label.configure(text=format_time(self._video_duration))

    def _on_video_seek(self, value):
        """
        拖动时间轴：先更新时间文字，防抖之后再抽帧。

        文字必须即时响应 —— 等抽帧完成才刷文字的话，拖动会明显滞后，
        而"拖到第几秒"这件事本身根本不需要等 ffmpeg。
        """
        try:
            self._video_time = max(0.0, float(value))
        except (TypeError, ValueError):
            return

        self._video_time_var.set(format_time(self._video_time))
        self._cancel_video_seek()
        self._video_seek_timer = self.after(self.VIDEO_SEEK_MS, self._apply_video_seek)

    def _apply_video_seek(self):
        """
        真正抽帧：丢掉旧底图并立即重渲染。

        必须 invalidate：缓存键里虽然带了时间点（不会取到错帧），
        但旧帧（1080p RGBA 约 8MB，4K 约 33MB）会一直挂在内存里，
        连续拖十几次白占几百 MB。
        """
        self._video_seek_timer = None
        if not self._is_video():
            return
        self._invalidate_base()
        self._schedule_refresh(immediate=True)

    def _cancel_video_seek(self):
        if self._video_seek_timer:
            try:
                self.after_cancel(self._video_seek_timer)
            except Exception:
                pass
            self._video_seek_timer = None

    # ==================== 内部 ====================

    def _schedule_refresh(self, immediate: bool = False):
        self._cancel_debounce()
        delay = 0 if immediate else self.DEBOUNCE_MS
        self._debounce_timer = self.after(delay, self._start_background_refresh)

    def _cancel_debounce(self):
        if self._debounce_timer:
            try:
                self.after_cancel(self._debounce_timer)
            except Exception:
                pass
            self._debounce_timer = None

    def _preview_work_dim(self) -> int:
        """
        预览工作分辨率（长边）。

        以画布长边 × WORK_ZOOM_FACTOR 为基准：放大到 200% 时仍有足够像素，
        再夹到 [WORK_MIN_DIM, WORK_MAX_DIM]。必须在主线程调用（要读控件尺寸）。
        """
        try:
            side = max(self._canvas.winfo_width(), self._canvas.winfo_height(), 400)
        except Exception:
            side = 800
        return int(max(self.WORK_MIN_DIM,
                       min(self.WORK_MAX_DIM, side * self.WORK_ZOOM_FACTOR)))

    def _start_background_refresh(self):
        """
        主线程：收集参数、算工作分辨率，启动后台渲染。

        渲染「单飞」：已有渲染在跑时只登记一个待办标记。
        旧实现每次参数变化都新起一个线程，拖一次滑块能堆十几个线程，
        每个都在解码+缩放+渲染同一张图，CPU 被占满、预览越拖越卡。
        """
        self._debounce_timer = None
        self._generation += 1

        if self._render_busy:
            self._render_pending = True
            return

        path = self.current_image_path
        if not path or not os.path.exists(path):
            return

        config = None
        if self.on_config_changed:
            config = self.on_config_changed()

        self._render_busy = True
        threading.Thread(
            target=self._render_in_background,
            args=(self._generation, path, config, self._pdf_page,
                  self._preview_work_dim()),
            daemon=True
        ).start()

    def _prepare_base(self, path: str, pdf_page: int, target_dim: int):
        """
        取（并缓存）渲染底图，返回 (工作图, 参考分辨率, scale)。

        参考分辨率 = 导出时使用的尺寸：
        - 图片 → 原图像素尺寸
        - PDF  → 页面可视尺寸 × OVERLAY_SCALE（覆盖层底图尺寸）
        - 视频 → 转正后的显示尺寸（与 ffmpeg 输出、覆盖层尺寸完全一致）；
                 取的是"当前时间点"那一帧，拖时间轴只换帧、不换参考尺寸
        scale = 工作图宽度 / 参考宽度，交给引擎换算绝对像素类参数，
        使预览与导出的水印几何一致。
        """
        is_pdf = path.lower().endswith(".pdf")
        is_video = is_video_file(path)
        # 覆盖层倍率也进缓存键：调过 set_overlay_scale 之后参考尺寸会变，
        # 不带上它就会命中一份"口径已经过期"的底图，
        # 表现为改了倍率但预览纹丝不动（导出却变了）
        overlay_scale = None
        if is_pdf:
            from src.pdf_engine import get_overlay_scale
            overlay_scale = get_overlay_scale()

        # 视频的时间点同样进缓存键：拖到第 3 秒就该拿到第 3 秒的帧，
        # 而不是复用第 0 秒的底图（那会让滑块看起来完全没生效）
        video_time = round(self._video_time, 2) if is_video else 0.0
        key = (path, int(pdf_page), int(target_dim), overlay_scale, video_time)
        with self._base_lock:
            if self._base_key == key and self._base_work is not None:
                return self._base_work, self._base_ref, self._base_scale

        from src.watermark_engine import safe_open_image

        page_count = None
        if is_pdf:
            from src.pdf_engine import (
                get_pdf_page_count, pdf_page_reference_size,
                render_pdf_page,
            )
            try:
                page_count = get_pdf_page_count(path)
            except Exception:
                page_count = None
            if page_count:
                pdf_page = max(0, min(page_count - 1, pdf_page))
            else:
                pdf_page = max(0, pdf_page)
            ref_size = pdf_page_reference_size(path, pdf_page)
            # 渲染倍率按参考尺寸折算：目标工作尺寸 ÷ 覆盖层底图尺寸 × 覆盖层倍率
            render_scale = overlay_scale * (target_dim / max(ref_size))
            img = render_pdf_page(path, page_num=pdf_page, scale=render_scale)
        elif is_video:
            # 视频用"当前时间点的那一帧"当静态底图：水印位置/平铺与时间无关，
            # 所以"在这一帧上摆好"等价于"整条片子都摆好"。
            # 抽帧由 ffmpeg 完成，输出的就是转正后的画面（与导出一致），
            # 参考尺寸取 display_size，覆盖层与导出的像素完全对齐。
            from src.video_engine import extract_frame, probe_video

            # _video_info 由 set_image 在主线程探测好；这里只读不写。
            # 探测失败时兜底再探一次，避免后台线程拿到 None 直接崩。
            info = self._video_info or probe_video(path)
            ref_size = info.display_size
            img = extract_frame(path, video_time)
        else:
            img = safe_open_image(path)
            ref_size = img.size

        # 缩放到工作尺寸：这是省时间的关键，也是对 scale 的唯一来源
        long_side = max(img.size)
        if long_side > target_dim:
            ratio = target_dim / long_side
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.LANCZOS
            )

        scale = img.width / max(ref_size[0], 1)

        with self._base_lock:
            self._base_key = key
            self._base_work = img
            self._base_ref = ref_size
            self._base_scale = scale
            # 页数一并缓存：PDF 解析不便宜，别在每次参数微调时再解析一遍
            self._base_page_count = page_count
            self._base_page_index = pdf_page

        return img, ref_size, scale

    def _invalidate_base(self):
        """丢弃底图缓存（换文件/清空时调用，避免大图常驻内存）"""
        with self._base_lock:
            self._base_key = None
            self._base_work = None
            self._base_ref = (0, 0)
            self._base_scale = 1.0
            self._base_page_count = None
            self._base_page_index = 0

    def _render_in_background(self, gen: int, path: str, config, pdf_page: int = 0,
                              target_dim: int = 1200):
        """后台线程：渲染水印，完成后通过 after 回主线程"""
        payload = None
        error = None
        try:
            from src.watermark_engine import apply_watermark

            work, ref_size, scale = self._prepare_base(path, pdf_page, target_dim)

            # 关键：把 scale 交给引擎，绝对像素类参数（间距/边距/字号下限）
            # 会等比换算，于是预览与导出的几何完全一致
            result = apply_watermark(work, config, scale) if config is not None else work

            display = result if result.mode == "RGB" else result.convert("RGB")
            # 页码/页数取底图缓存里记下的值（_prepare_base 已经把页码夹到合法范围）
            page_count = self._base_page_count
            pdf_page = self._base_page_index

            # 连同"不含水印的底图 + 配置 + scale"一起带回主线程：
            # 预览图上拖动水印时要用它们现算精灵与命中框，
            # 用的还是引擎那套函数，所以拖到哪就导出到哪
            payload = (display, ref_size, gen, page_count, pdf_page,
                       work, config, scale)
        except Exception as e:
            error = str(e)
        finally:
            # 无论成功、失败还是被新一轮请求取代，都必须回主线程收尾，
            # 否则「单飞」标记会永远卡住，预览再也不刷新
            self.after(0, lambda: self._on_render_done(gen, payload, error))

    def _on_render_done(self, gen: int, payload, error):
        """主线程：收尾 +（若期间又有请求）立刻补一次渲染"""
        self._render_busy = False

        if gen == self._generation:
            if payload is not None:
                (display, ref_size, _gen, page_count, pdf_page,
                 base_work, config, scale) = payload
                self._cache_and_display(display, ref_size, gen, page_count, pdf_page,
                                        base_work, config, scale)
            elif error is not None:
                self._show_placeholder(f"预览出错: {error}")

        if self._render_pending or gen != self._generation:
            self._render_pending = False
            self._schedule_refresh(immediate=True)

    def _cache_and_display(self, display_img: Image.Image, ref_size, gen: int,
                           page_count=None, pdf_page: int = 0,
                           base_work=None, config=None, scale: float = 1.0):
        """主线程：缓存结果 + 应用缩放"""
        if gen != self._generation:
            return

        self._cached_render = display_img
        self._cached_orig_size = ref_size
        self._render_token += 1      # 让显示图缓存失效
        self._display_key = None

        # 拖动水印要用的三件套：无水印底图、配置、scale
        self._wm_base_render = base_work
        self._wm_cfg = config
        self._wm_scale = scale
        self._wm_base_tk = None      # 渲染换了，底图显示缓存也就失效了
        self._wm_base_key = None
        self._refresh_watermark_boxes()
        if self._wm_drag is not None:
            # 拖动过程中被别的刷新打断（例如切了文件）：直接收尾，别留下悬空状态
            self._wm_drag_cancel()

        if page_count is not None:
            # 回填页数并同步翻页栏（首帧渲染完成后按钮才有正确的可用状态）
            self._pdf_page_count = page_count
            self._pdf_page = pdf_page
            self.info_label.configure(
                text=f"PDF 共 {page_count} 页 | 第 {pdf_page + 1} 页: "
                     f"{ref_size[0]}x{ref_size[1]}"
            )
            self._update_pdf_bar()
        else:
            text = f"原图: {ref_size[0]}x{ref_size[1]}"
            if self._wm_tile_hint():
                # 平铺的水印没有"位置"可拖（从左上角按间距铺开）。
                # 在用户会试着拖的地方说清楚，比让他试半天再放弃好。
                text += "　·　平铺模式：位置由间距决定，不支持拖动"
            self.info_label.configure(text=text)
        # 注意：这里不再重置 _pan_offset —— 微调参数时视图跳回中心很打断操作，
        # 换文件时由 set_image() 重置
        self._apply_zoom(force=True)

    def _show_placeholder(self, message: str):
        # 占位状态下没有图，也就没有可拖动的水印 —— 一并收掉拖动状态，
        # 否则残留的精灵/参考线会盖在占位文字上
        self._wm_drag_cancel()
        self._wm_boxes = []
        self._wm_base_render = None
        self._wm_base_tk = None
        self._wm_base_key = None
        self._cached_render = None
        self._cached_orig_size = (0, 0)
        self._display_key = None
        self._display_size = (0, 0)
        self._render_token += 1
        if self._image_item is not None:
            self._canvas.delete(self._image_item)
            self._image_item = None
        self._canvas.itemconfig(self._placeholder_id, text=message)
        self._center_placeholder()
        self.info_label.configure(text="")
        self.size_label.configure(text="")
        self._display_image_tk = None

    def destroy(self):
        self._cancel_debounce()
        self._generation += 1
        self._invalidate_base()
        super().destroy()
