"""
文件列表组件 — 懒加载显示（仅文件名，选中时动态加载缩略图）

设计原则：
- 文件列表仅展示文件名（CTkLabel），不预生成缩略图 Widget
- 用户选中某行时，才触发该文件的缩略图加载和实时预览
- 支持单个删除、清空、上下移动顺序
- 适合数百上千个文件的大批量场景
"""

from __future__ import annotations

import os
import threading
import customtkinter as ctk
from PIL import Image, ImageTk
from typing import Optional, Callable, List

# 缩略图缓存上限（防止大量文件时内存溢出）
MAX_THUMB_CACHE = 200


class FileList(ctk.CTkScrollableFrame):
    """
    懒加载文件列表组件。

    内部维护一个有序的文件路径列表，每行仅显示：
    - 序号
    - 文件名（不含完整路径）
    - 文件大小（可选）
    """

    def __init__(
        self,
        master,
        on_selection_changed: Callable[[Optional[str]], None] = None,
        **kwargs
    ):
        super().__init__(master, **kwargs)

        self.on_selection_changed = on_selection_changed or (lambda p: None)

        # 内部数据
        self._files: List[str] = []
        self._selected_index: int = -1
        self._row_widgets: dict[int, ctk.CTkFrame] = {}

        # 缩略图缓存
        self._thumb_cache: dict[str, ImageTk.PhotoImage] = {}
        self._thumb_loading: set[str] = set()

        # 拖拽排序状态
        self._drag_start_y: int | None = None
        self._drag_source_idx: int | None = None
        self._is_dragging: bool = False

        self._height_per_row = 40  # 行高（容纳缩略图）

    @property
    def files(self) -> List[str]:
        """获取当前文件列表（只读）"""
        return list(self._files)

    @property
    def count(self) -> int:
        """当前文件数量"""
        return len(self._files)

    @property
    def selected_file(self) -> Optional[str]:
        """当前选中的文件路径，无选中返回 None"""
        if 0 <= self._selected_index < len(self._files):
            return self._files[self._selected_index]
        return None

    @property
    def selected_index(self) -> int:
        """当前选中索引"""
        return self._selected_index

    def add_files(self, new_files: List[str]) -> int:
        """
        添加新文件到列表末尾。

        返回实际新增的数量（去重后）。
        """
        added_count = 0
        start_index = len(self._files)

        for path in new_files:
            # 去重
            if path in self._files:
                continue
            self._files.append(path)
            self._create_row_widget(len(self._files) - 1)
            added_count += 1

        # 更新滚动区域
        self._update_scroll_size()

        return added_count

    def remove_selected(self) -> bool:
        """移除当前选中的文件。返回是否成功。"""
        if not (0 <= self._selected_index < len(self._files)):
            return False

        idx = self._selected_index
        del self._files[idx]

        # 销毁对应行 Widget
        if idx in self._row_widgets:
            self._row_widgets[idx].destroy()
            del self._row_widgets[idx]

        # 重建后续行的索引映射（简单策略：全部销毁后重建）
        self._rebuild_all_rows()

        # 清除选中
        self._selected_index = -1
        self.on_selection_changed(None)

        self._update_scroll_size()
        return True

    def clear_all(self):
        """清空所有文件"""
        self._files.clear()
        self._selected_index = -1

        # 销毁所有行 Widget
        for widget in self._row_widgets.values():
            widget.destroy()
        self._row_widgets.clear()

        self._update_scroll_size()
        self.on_selection_changed(None)

    def move_up(self) -> bool:
        """将选中项上移一位。返回是否成功。"""
        if self._selected_index <= 0:
            return False

        idx = self._selected_index
        self._files[idx], self._files[idx - 1] = self._files[idx - 1], self._files[idx]
        self._selected_index = idx - 1
        self._rebuild_all_rows()
        self._highlight_row(self._selected_index)
        return True

    def move_down(self) -> bool:
        """将选中项下移一位。返回是否成功。"""
        if self._selected_index < 0 or self._selected_index >= len(self._files) - 1:
            return False

        idx = self._selected_index
        self._files[idx], self._files[idx + 1] = self._files[idx + 1], self._files[idx]
        self._selected_index = idx + 1
        self._rebuild_all_rows()
        self._highlight_row(self._selected_index)
        return True

    def select_by_index(self, index: int):
        """程序化选中指定索引的行"""
        if 0 <= index < len(self._files):
            self._selected_index = index
            self._highlight_row(index)
            self.on_selection_changed(self._files[index])
            # 滚动到可见区域
            if index in self._row_widgets:
                self._row_widgets[index].see()

    def _create_row_widget(self, index: int):
        """为指定索引创建一行 UI 组件"""
        path = self._files[index]
        filename = os.path.basename(path)

        try:
            size_kb = os.path.getsize(path) // 1024
            size_str = f"{size_kb} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
        except OSError:
            size_str = "-"

        row = ctk.CTkFrame(
            self,
            height=self._height_per_row,
            corner_radius=0,
            fg_color="transparent",
        )
        row.pack(fill="x", padx=2, pady=1)
        row.grid_propagate(False)

        # 布局：序号 | 缩略图 | 文件名 | 大小
        num_label = ctk.CTkLabel(row, text=f"{index + 1:>3}", width=30, anchor="e")
        num_label.pack(side="left", padx=(4, 4))

        # 缩略图占位
        thumb_label = ctk.CTkLabel(row, text="", width=36, height=36)
        thumb_label.pack(side="left", padx=(0, 4))

        name_label = ctk.CTkLabel(row, text=filename, anchor="w")
        name_label.pack(side="left", fill="x", expand=True, padx=4)

        size_label = ctk.CTkLabel(row, text=size_str, width=70, anchor="e")
        size_label.pack(side="right", padx=8)

        # 异步加载缩略图
        self._load_thumbnail_async(path, thumb_label)

        # 绑定点击 + 拖拽排序事件
        def on_press(event, idx=index):
            self.select_by_index(idx)
            self._drag_start_y = event.y_root
            self._drag_source_idx = idx
            self._is_dragging = False

        def on_motion(event):
            if self._drag_start_y is None or self._drag_source_idx is None:
                return
            dy = event.y_root - self._drag_start_y
            if not self._is_dragging and abs(dy) > 10:
                self._is_dragging = True
            if self._is_dragging:
                self._update_drag_feedback(event.y_root)

        def on_release(event):
            if self._is_dragging and self._drag_source_idx is not None:
                target_idx = self._get_row_index_at_y(event.y_root)
                if target_idx is not None and target_idx != self._drag_source_idx:
                    self._reorder(self._drag_source_idx, target_idx)
            self._drag_start_y = None
            self._drag_source_idx = None
            self._is_dragging = False
            self._clear_drag_feedback()

        for child in [row, num_label, thumb_label, name_label, size_label]:
            child.bind("<Button-1>", on_press)
            child.bind("<B1-Motion>", on_motion)
            child.bind("<ButtonRelease-1>", on_release)

        self._row_widgets[index] = row

    def _rebuild_all_rows(self):
        """销毁所有行并按当前 _files 列表重建"""
        for widget in self._row_widgets.values():
            widget.destroy()
        self._row_widgets.clear()

        for i in range(len(self._files)):
            self._create_row_widget(i)

        # 恢复高亮
        if 0 <= self._selected_index < len(self._files):
            self._highlight_row(self._selected_index)

    def _highlight_row(self, index: int):
        """高亮指定行，取消其他行的高亮"""
        for idx, widget in self._row_widgets.items():
            if idx == index:
                widget.configure(fg_color=("#3b82f6", "#4a90d9"))
            else:
                widget.configure(fg_color="transparent")

    # ==================== 缩略图懒加载 ====================

    def _load_thumbnail_async(self, path: str, label: ctk.CTkLabel):
        """异步加载缩略图（后台线程 → after 回主线程显示）"""

        # 已在缓存中：直接显示
        if path in self._thumb_cache:
            label.configure(image=self._thumb_cache[path], text="")
            return

        # 正在加载中：跳过
        if path in self._thumb_loading:
            return

        self._thumb_loading.add(path)

        def _load():
            try:
                if path.lower().endswith(".pdf"):
                    # PDF：渲染首页作为缩略图
                    from src.pdf_engine import render_pdf_page
                    img = render_pdf_page(path, page_num=0, scale=0.3)
                else:
                    img = Image.open(path)
                img.thumbnail((32, 32), Image.LANCZOS)
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                tk_img = ImageTk.PhotoImage(img)
                # 回主线程更新
                self.after(0, lambda: self._on_thumb_loaded(path, label, tk_img))
            except Exception:
                self._thumb_loading.discard(path)

        t = threading.Thread(target=_load, daemon=True)
        t.start()

    def _on_thumb_loaded(self, path: str, label: ctk.CTkLabel, tk_img: ImageTk.PhotoImage):
        """缩略图加载完成（主线程）"""
        self._thumb_loading.discard(path)

        # LRU：超过上限时清掉一半旧缓存
        if len(self._thumb_cache) >= MAX_THUMB_CACHE:
            keys = list(self._thumb_cache.keys())
            for k in keys[:len(keys) // 2]:
                del self._thumb_cache[k]

        self._thumb_cache[path] = tk_img

        try:
            label.configure(image=tk_img, text="")
        except Exception:
            pass

    # ==================== 拖拽排序 ====================

    def _get_row_index_at_y(self, y_root: int) -> Optional[int]:
        """根据屏幕 Y 坐标找到对应的行索引"""
        for idx, widget in self._row_widgets.items():
            wy = widget.winfo_rooty()
            if wy <= y_root <= wy + self._height_per_row:
                return idx
        # 超出范围：返回最接近的边界
        if self._row_widgets:
            max_idx = max(self._row_widgets.keys())
            last_widget = self._row_widgets[max_idx]
            wy = last_widget.winfo_rooty()
            if y_root > wy + self._height_per_row:
                return max_idx
            first_widget = self._row_widgets[0]
            wy = first_widget.winfo_rooty()
            if y_root < wy:
                return 0
        return None

    def _reorder(self, from_idx: int, to_idx: int):
        """移动文件从 from_idx 到 to_idx"""
        if from_idx == to_idx:
            return
        # 移动数据
        item = self._files.pop(from_idx)
        self._files.insert(to_idx, item)
        # 调整选中索引
        if self._selected_index == from_idx:
            self._selected_index = to_idx
        elif from_idx < self._selected_index <= to_idx:
            self._selected_index -= 1
        elif to_idx <= self._selected_index < from_idx:
            self._selected_index += 1
        # 重建
        self._rebuild_all_rows()

    def _update_drag_feedback(self, y_root: int):
        """拖拽中：高亮目标行"""
        target = self._get_row_index_at_y(y_root)
        for idx, widget in self._row_widgets.items():
            if idx == target and idx != self._drag_source_idx:
                widget.configure(fg_color=("#fbbf24", "#f59e0b"))  # 黄色高亮
            elif idx == self._selected_index:
                widget.configure(fg_color=("#3b82f6", "#4a90d9"))  # 保持选中蓝色
            else:
                widget.configure(fg_color="transparent")

    def _clear_drag_feedback(self):
        """清除拖拽视觉反馈"""
        self._highlight_row(self._selected_index)

    def _update_scroll_size(self):
        """更新滚动区域的总高度"""
        total_height = len(self._files) * self._height_per_row
        # 通过配置最小高度来影响 ScrollableFrame 的滚动范围
        self.configure(height=max(total_height + 10, 100))
