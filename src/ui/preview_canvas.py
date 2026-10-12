"""
实时预览画布 — 后台渲染 + 缩放 + 拖动查看

特性：
1. 后台线程渲染水印，主线程仅通过 after() 更新 UI
2. 生成计数器丢弃过期结果
3. 缩放 25%-200%，Ctrl+滚轮，鼠标拖动查看
4. 缩放只缩放缓存图，不重新渲染水印
5. 窗口大小变化时自动重新适应
6. 拖动范围限制（不能拖出视野）
7. 缓存 1800px 分辨率，减少 200% 放大模糊
"""

import customtkinter as ctk
from PIL import Image, ImageTk
import threading
import os


class PreviewCanvas(ctk.CTkFrame):

    # 缓存图最大尺寸（提高到 1800 减少 200% 放大模糊）
    RENDER_MAX_SIZE = (1800, 1800)
    WORK_MAX_DIMENSION = 4000
    DEBOUNCE_MS = 150
    MAX_PIXELS_FOR_PREVIEW = 50_000_000

    def __init__(self, parent, on_config_changed=None, on_page_changed=None, **kwargs):
        super().__init__(parent, **kwargs)

        self.on_config_changed = on_config_changed
        # 翻页回调：主窗口据此把面板切换到该页的专属参数
        self.on_page_changed = on_page_changed
        self.current_image_path = None
        self._display_image_tk = None
        self._debounce_timer = None
        self._generation = 0

        # 缓存：已渲染的水印图（高分辨率）
        self._cached_render: Image.Image | None = None
        self._cached_orig_size = (0, 0)

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
        canvas_frame = ctk.CTkFrame(self, fg_color=("#f0f0f0", "#1e1e1e"), corner_radius=8)
        canvas_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 2))
        canvas_frame.grid_columnconfigure(0, weight=1)
        canvas_frame.grid_rowconfigure(0, weight=1)

        import tkinter as tk
        self._canvas = tk.Canvas(
            canvas_frame,
            bg=self._get_canvas_bg(),
            highlightthickness=0,
            cursor="hand2",
        )
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        self._placeholder_id = self._canvas.create_text(
            0, 0, text="拖入图片后在此预览水印效果",
            fill="gray", font=("Microsoft YaHei", 13)
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

        self._zoom_out_btn = ctk.CTkButton(
            zoom_bar, text="−", width=28, height=28,
            command=lambda: self._adjust_zoom(-0.1)
        )
        self._zoom_out_btn.pack(side="left", padx=2)

        self._zoom_slider = ctk.CTkSlider(
            zoom_bar, from_=0.25, to=2.0,
            variable=self._zoom_var, width=150,
            command=self._on_zoom_slider
        )
        self._zoom_slider.pack(side="left", padx=4)
        self._zoom_slider.set(1.0)

        self._zoom_in_btn = ctk.CTkButton(
            zoom_bar, text="+", width=28, height=28,
            command=lambda: self._adjust_zoom(0.1)
        )
        self._zoom_in_btn.pack(side="left", padx=2)

        self._fit_btn = ctk.CTkButton(
            zoom_bar, text="适应窗口", width=70, height=28,
            command=self._fit_to_window
        )
        self._fit_btn.pack(side="left", padx=(8, 2))

        self._zoom_display = ctk.CTkLabel(
            zoom_bar, textvariable=self._zoom_label_var,
            width=50, font=("Microsoft YaHei", 11)
        )
        self._zoom_display.pack(side="left", padx=4)

        self.info_label = ctk.CTkLabel(
            zoom_bar, text="", text_color="gray", font=("Microsoft YaHei", 10)
        )
        self.info_label.pack(side="right", padx=4)

        self.size_label = ctk.CTkLabel(
            zoom_bar, text="", text_color="gray", font=("Microsoft YaHei", 10)
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
            self._pdf_bar, text="跳到第", text_color="gray",
            font=("Microsoft YaHei", 10),
        ).pack(side="left", padx=(14, 2))

        self._pdf_jump_entry = ctk.CTkEntry(self._pdf_bar, width=54, height=26)
        self._pdf_jump_entry.pack(side="left")
        self._pdf_jump_entry.bind("<Return>", lambda e: self._jump_pdf_page())

        ctk.CTkLabel(
            self._pdf_bar, text="页（回车）", text_color="gray",
            font=("Microsoft YaHei", 10),
        ).pack(side="left", padx=(2, 4))

        # 哪些页被单独设过参数（由主窗口回填，翻页时一眼可见）
        self._pdf_marked_label = ctk.CTkLabel(
            self._pdf_bar, text="", text_color="#f59e0b",
            font=("Microsoft YaHei", 10),
        )
        self._pdf_marked_label.pack(side="left", padx=(12, 2))

    def _get_canvas_bg(self):
        """根据主题获取画布背景色"""
        return "#1e1e1e" if ctk.get_appearance_mode() == "Dark" else "#f0f0f0"

    def update_theme(self):
        """主题切换时更新画布颜色"""
        self._canvas.configure(bg=self._get_canvas_bg())
        self._apply_zoom()

    # ==================== 缩放 ====================

    def _on_zoom_slider(self, value):
        self._zoom_label_var.set(f"{int(value * 100)}%")
        self._apply_zoom()

    def _adjust_zoom(self, delta):
        new_val = max(0.25, min(2.0, self._zoom_var.get() + delta))
        self._zoom_var.set(new_val)
        self._zoom_label_var.set(f"{int(new_val * 100)}%")
        self._apply_zoom()

    def _fit_to_window(self):
        """适应窗口：重置缩放和偏移"""
        self._zoom_var.set(1.0)
        self._zoom_label_var.set("100%")
        self._pan_offset = (0, 0)
        self._apply_zoom()

    def _on_ctrl_scroll(self, event):
        if event.delta > 0:
            self._adjust_zoom(0.1)
        else:
            self._adjust_zoom(-0.1)

    def _on_canvas_resize(self, event=None):
        """Canvas 尺寸变化时重新适应（仅缩放显示，不重新渲染）"""
        if self._cached_render is not None:
            self._apply_zoom()
        else:
            self._center_placeholder()

    def _apply_zoom(self):
        """从缓存图按缩放比例显示"""
        if self._cached_render is None:
            return

        zoom = self._zoom_var.get()
        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)

        # 先 fit 到画布尺寸
        base = self._cached_render.copy()
        base.thumbnail((cw, ch), Image.LANCZOS)

        if zoom == 1.0:
            display = base
        else:
            new_w = max(1, int(base.width * zoom))
            new_h = max(1, int(base.height * zoom))
            display = base.resize((new_w, new_h), Image.LANCZOS)

        if display.mode != "RGB":
            display = display.convert("RGB")

        self._display_image_tk = ImageTk.PhotoImage(display)
        self._draw_canvas_image(display.width, display.height, cw, ch)

    def _draw_canvas_image(self, w, h, cw, ch):
        """在 Canvas 上绘制图片，限制拖动范围"""
        self._canvas.itemconfig(self._placeholder_id, text="")

        if self._image_item is not None:
            self._canvas.delete(self._image_item)

        # 限制偏移范围（不能把图片拖出视野）
        if w <= cw:
            # 图片比画布小：居中，不允许水平偏移
            max_dx = 0
        else:
            max_dx = (w - cw) // 2

        if h <= ch:
            max_dy = 0
        else:
            max_dy = (h - ch) // 2

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

        if self._cached_orig_size[0] > 0:
            self.size_label.configure(
                text=f"预览: {w}x{h} ({int(self._zoom_var.get() * 100)}%)"
            )

    # ==================== 拖动 ====================

    def _on_pan_start(self, event):
        self._pan_start = (event.x, event.y)

    def _on_pan_motion(self, event):
        if self._pan_start is None or self._cached_render is None:
            return
        dx = event.x - self._pan_start[0]
        dy = event.y - self._pan_start[1]
        self._pan_offset = (self._pan_offset[0] + dx, self._pan_offset[1] + dy)
        self._pan_start = (event.x, event.y)
        self._apply_zoom()

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
            # 换文件时回到第 1 页；总页数等首次渲染完成后回填
            self._pdf_page = 0
            self._pdf_page_count = None
        self.current_image_path = image_path
        self._update_pdf_bar()
        self._schedule_refresh()

    def request_refresh(self):
        if not self.current_image_path:
            return
        self._schedule_refresh()

    def clear_preview(self):
        self.current_image_path = None
        self._cached_render = None
        self._cached_orig_size = (0, 0)
        self._pdf_page = 0
        self._pdf_page_count = None
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

    def _schedule_refresh(self):
        self._cancel_debounce()
        self._debounce_timer = self.after(self.DEBOUNCE_MS, self._start_background_refresh)

    def _cancel_debounce(self):
        if self._debounce_timer:
            try:
                self.after_cancel(self._debounce_timer)
            except Exception:
                pass
            self._debounce_timer = None

    def _start_background_refresh(self):
        """主线程：收集参数，启动后台线程"""
        self._generation += 1
        gen = self._generation

        path = self.current_image_path
        if not path or not os.path.exists(path):
            return

        config = None
        if self.on_config_changed:
            config = self.on_config_changed()

        t = threading.Thread(
            target=self._render_in_background,
            args=(gen, path, config, self._pdf_page),
            daemon=True
        )
        t.start()

    def _render_in_background(self, gen: int, path: str, config, pdf_page: int = 0):
        """后台线程：渲染水印，完成后通过 after 回主线程"""
        if gen != self._generation:
            return

        try:
            from src.watermark_engine import safe_open_image, apply_watermark

            # PDF：渲染指定页（预览与导出使用相同相对几何，所见即所得）
            page_count = None
            if path.lower().endswith(".pdf"):
                from src.pdf_engine import render_pdf_page, get_pdf_page_count
                try:
                    page_count = get_pdf_page_count(path)
                except Exception:
                    page_count = None
                if page_count:
                    pdf_page = max(0, min(page_count - 1, pdf_page))
                else:
                    pdf_page = max(0, pdf_page)
                orig = render_pdf_page(path, page_num=pdf_page, scale=2.0)
            else:
                orig = safe_open_image(path)
            orig_size = orig.size

            pixels = orig.width * orig.height
            if pixels > self.MAX_PIXELS_FOR_PREVIEW:
                ratio = (self.MAX_PIXELS_FOR_PREVIEW / pixels) ** 0.5
                orig = orig.resize(
                    (int(orig.width * ratio), int(orig.height * ratio)),
                    Image.LANCZOS
                )

            max_dim = max(orig.size)
            if max_dim > self.WORK_MAX_DIMENSION:
                ratio = self.WORK_MAX_DIMENSION / max_dim
                work = orig.resize(
                    (int(orig.width * ratio), int(orig.height * ratio)),
                    Image.LANCZOS
                )
            else:
                work = orig.copy()

            if config is not None:
                result = apply_watermark(work, config)
            else:
                result = work

            # 缓存到高分辨率（1800px）
            display = result.copy()
            display.thumbnail(self.RENDER_MAX_SIZE, Image.LANCZOS)

            if display.mode != "RGB":
                display = display.convert("RGB")

            del orig, work, result

            if gen != self._generation:
                return

            # 回主线程显示
            self.after(
                0,
                lambda: self._cache_and_display(display, orig_size, gen, page_count, pdf_page),
            )

        except Exception as e:
            if gen == self._generation:
                self.after(0, lambda: self._show_placeholder(f"预览出错: {e}"))

    def _cache_and_display(self, display_img: Image.Image, orig_size, gen: int,
                           page_count=None, pdf_page: int = 0):
        """主线程：缓存结果 + 应用缩放"""
        if gen != self._generation:
            return

        self._cached_render = display_img
        self._cached_orig_size = orig_size
        if page_count is not None:
            # 回填页数并同步翻页栏（首帧渲染完成后按钮才有正确的可用状态）
            self._pdf_page_count = page_count
            self._pdf_page = pdf_page
            self.info_label.configure(
                text=f"PDF 共 {page_count} 页 | 第 {pdf_page + 1} 页: "
                     f"{orig_size[0]}x{orig_size[1]}"
            )
            self._update_pdf_bar()
        else:
            self.info_label.configure(text=f"原图: {orig_size[0]}x{orig_size[1]}")
        self._pan_offset = (0, 0)
        self._apply_zoom()

    def _show_placeholder(self, message: str):
        self._cached_render = None
        self._cached_orig_size = (0, 0)
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
        super().destroy()
