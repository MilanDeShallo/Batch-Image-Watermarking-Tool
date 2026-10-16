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


class PreviewCanvas(ctk.CTkFrame):

    # 工作分辨率 = 画布长边 × 该倍数（放大到 200% 仍有足够像素），再夹取到上下限。
    # 直接在原图分辨率上渲染水印既慢又看不出差别：画布只有几百像素宽，
    # 4000px 与 1600px 的渲染结果缩到画布上看不出区别，但耗时差 6 倍。
    WORK_ZOOM_FACTOR = 2.0
    WORK_MIN_DIM = 900
    WORK_MAX_DIM = 1800

    DEBOUNCE_MS = 150
    ZOOM_THROTTLE_MS = 50

    def __init__(self, parent, on_config_changed=None, on_page_changed=None, **kwargs):
        super().__init__(parent, **kwargs)

        self.on_config_changed = on_config_changed
        # 翻页回调：主窗口据此把面板切换到该页的专属参数
        self.on_page_changed = on_page_changed
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

        # 居中 + 偏移
        x = (cw - w) // 2 + self._pan_offset[0]
        y = (ch - h) // 2 + self._pan_offset[1]

        self._image_item = self._canvas.create_image(
            x, y, anchor="nw", image=self._display_image_tk
        )

    # ==================== 拖动 ====================

    def _on_pan_start(self, event):
        self._pan_start = (event.x, event.y)

    def _on_pan_motion(self, event):
        """
        拖动查看：只移动画布项，完全不碰图像数据。

        拖动事件每秒 60-100 次，旧实现在这里调 _apply_zoom()，
        每次都要复制 1800px 缓存图 + LANCZOS 重采样（实测 25ms/帧），
        于是拖动既迟滞又掉帧。显示图已按缩放缓存，移动是零成本的。
        """
        if self._pan_start is None or self._cached_render is None:
            return
        dx = event.x - self._pan_start[0]
        dy = event.y - self._pan_start[1]
        self._pan_offset = (self._pan_offset[0] + dx, self._pan_offset[1] + dy)
        self._pan_start = (event.x, event.y)
        self._draw_canvas_image()

    def _on_pan_end(self, event):
        self._pan_start = None

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
        self.current_image_path = image_path
        self._update_pdf_bar()
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
        self._cancel_debounce()
        self._update_pdf_bar()
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
        scale = 工作图宽度 / 参考宽度，交给引擎换算绝对像素类参数，
        使预览与导出的水印几何一致。
        """
        is_pdf = path.lower().endswith(".pdf")
        # 覆盖层倍率也进缓存键：调过 set_overlay_scale 之后参考尺寸会变，
        # 不带上它就会命中一份"口径已经过期"的底图，
        # 表现为改了倍率但预览纹丝不动（导出却变了）
        overlay_scale = None
        if is_pdf:
            from src.pdf_engine import get_overlay_scale
            overlay_scale = get_overlay_scale()

        key = (path, int(pdf_page), int(target_dim), overlay_scale)
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

            payload = (display, ref_size, gen, page_count, pdf_page)
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
                display, ref_size, _gen, page_count, pdf_page = payload
                self._cache_and_display(display, ref_size, gen, page_count, pdf_page)
            elif error is not None:
                self._show_placeholder(f"预览出错: {error}")

        if self._render_pending or gen != self._generation:
            self._render_pending = False
            self._schedule_refresh(immediate=True)

    def _cache_and_display(self, display_img: Image.Image, ref_size, gen: int,
                           page_count=None, pdf_page: int = 0):
        """主线程：缓存结果 + 应用缩放"""
        if gen != self._generation:
            return

        self._cached_render = display_img
        self._cached_orig_size = ref_size
        self._render_token += 1      # 让显示图缓存失效
        self._display_key = None

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
            self.info_label.configure(text=f"原图: {ref_size[0]}x{ref_size[1]}")
        # 注意：这里不再重置 _pan_offset —— 微调参数时视图跳回中心很打断操作，
        # 换文件时由 set_image() 重置
        self._apply_zoom(force=True)

    def _show_placeholder(self, message: str):
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
