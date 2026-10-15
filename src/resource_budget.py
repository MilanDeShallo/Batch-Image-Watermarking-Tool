"""
并发与内存预算 —— 按「解码后的像素」而不是「压缩后的文件大小」决定并行度。

为什么需要
----------
文件大小与内存占用没有稳定关系：
    一张 200KB 的 10000×7000 JPEG 解出来是 280MB RGBA（压缩率高），
    而一张 5MB 的 1080p PNG 只有 8MB。
旧实现按平均文件大小分档（>15MB 限 2 线程、>5MB 限 3 线程），
在高分辨率场景下会开出过多线程：几张大图并发就能把内存吃光。

这里改为先读图片**文件头**拿到像素尺寸（不解码像素，代价极小），
据此估算单任务峰值内存，再反推本轮最多能并行几个任务。

本模块不 import 任何 GUI 库，可独立单元测试。
"""

from __future__ import annotations

import os

from src.config import MAX_IMAGE_PIXELS      # 像素上限的单一来源（见下）

# 解码后每像素字节数（RGBA）
BYTES_PER_PIXEL = 4

# 单任务峰值系数（相对"解码后的单份位图"）。
#
# 实测（Windows 工作集采样；both 类型 = 文字 + Logo，走导出路径）：
#   6000×4000（24M px）→ 16.2 B/px
#   8000×6000（48M px）→ 16.1 B/px
# 两个量级结果一致，说明峰值取决于"同时存活的整图份数"而不是图本身大小：
#   输入图 1 份 + 文字合成的 base/overlay/结果 3 份
#   + Logo 再次合成的 base/overlay/结果 3 份 + 存 JPEG 时的白底 1 份
#
# 旧值 2.5（=10 B/px）是按"平均占用"估的，低于实测峰值 ——
# 4 张 4000×3000 的图按旧值算能开 8 线程，实际峰值会冲出预算。
# 取 5.0（=20 B/px），比实测值高约 25%：宁可少开线程，也不要 OOM。
TASK_SLACK = 5.0

# 超过此像素数给出显式预警（仍允许处理）
LARGE_IMAGE_PIXELS = 50_000_000

# 读不到尺寸（PDF、损坏文件）时的单任务内存估算
UNKNOWN_FILE_BYTES = 64 * 1024 * 1024

# 进程可用内存中允许用于图像缓冲的比例
MEMORY_BUDGET_RATIO = 0.5

# 并行度上限：内存再充裕也不开更多，避免磁盘与 GIL 争抢反而变慢
MAX_WORKERS = 8

# 拿不到系统可用内存时的兜底预算（2GB）
FALLBACK_AVAILABLE_BYTES = 2 * 1024 ** 3


def format_bytes(count: int) -> str:
    """把字节数格式化成便于阅读的字符串"""
    try:
        n = float(count)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def estimate_decoded_bytes(width: int, height: int) -> int:
    """一张 w×h 的图在本工具里大致要占多少内存"""
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        return UNKNOWN_FILE_BYTES
    if w <= 0 or h <= 0:
        return UNKNOWN_FILE_BYTES
    return int(w * h * BYTES_PER_PIXEL * TASK_SLACK)


def probe_image_size(path: str) -> tuple[int, int] | None:
    """
    只读文件头拿到图片尺寸（不解码像素）。

    Image.open 是惰性的：构造出对象只解析文件头，不会加载像素数据，
    因此对上千个文件做预检的代价可以忽略。
    读不到时返回 None（调用方按"未知尺寸"处理，不阻断流程）。
    """
    if str(path).lower().endswith(".pdf"):
        return None
    try:
        from PIL import Image
        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return None


def file_memory_estimate(path: str) -> int:
    """单个文件的峰值内存估算（图片按像素，其余按固定权重）"""
    size = probe_image_size(path)
    if size is None:
        return UNKNOWN_FILE_BYTES
    return estimate_decoded_bytes(*size)


def available_memory_bytes() -> int:
    """
    系统当前可用内存。

    优先用 psutil（准确），拿不到就退回固定预算 ——
    预检失败绝不能阻断批处理。
    """
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return FALLBACK_AVAILABLE_BYTES


def plan_workers(
    estimates,
    cpu_count: int | None = None,
    available: int | None = None,
) -> tuple[int, str]:
    """
    按内存预算与 CPU 上限算出本轮并行度。

    :param estimates: 每个待处理文件的峰值内存估算（字节）
    :param cpu_count: CPU 核数，默认取 os.cpu_count()
    :param available: 可用内存，默认自动探测
    :return: (并行度, 决策说明) —— 说明会写进日志，便于事后复盘
    """
    cpu = int(cpu_count or os.cpu_count() or 4)
    cpu_workers = max(1, min(MAX_WORKERS, cpu - 1 if cpu > 1 else 1))

    values = [int(v or 0) for v in (estimates or [])]
    peak = max(values) if values else 0
    if peak <= 0:
        return cpu_workers, f"无可用的尺寸信息，按 CPU 上限开 {cpu_workers} 线程"

    avail = int(available if available is not None else available_memory_bytes())
    budget = max(1, int(avail * MEMORY_BUDGET_RATIO))
    by_memory = max(1, budget // peak)
    workers = max(1, min(cpu_workers, by_memory))

    reason = (
        f"单张峰值约 {format_bytes(peak)}，内存预算 {format_bytes(budget)}"
        f"（可用 {format_bytes(avail)} × {MEMORY_BUDGET_RATIO:.0%}），"
        f"按内存可开 {by_memory} 线程 / CPU 上限 {cpu_workers} 线程 → 实际 {workers}"
    )
    return workers, reason


def oversized_files(files, limit: int = MAX_IMAGE_PIXELS) -> list[tuple[str, int]]:
    """
    像素数超过硬上限的文件 —— 调用方应直接跳过并报错，
    而不是交给 Pillow 去分配不可能成功的内存。
    """
    out = []
    for path in files or []:
        size = probe_image_size(path)
        if size is None:
            continue
        pixels = size[0] * size[1]
        if pixels > limit:
            out.append((path, pixels))
    return out


def large_images(files, threshold: int = LARGE_IMAGE_PIXELS) -> list[tuple[str, int, int, int]]:
    """
    像素数超过预警阈值的文件。

    :return: [(路径, 宽, 高, 像素数)]，按像素数从大到小
    """
    out = []
    for path in files or []:
        size = probe_image_size(path)
        if size is None:
            continue
        w, h = size
        pixels = w * h
        if pixels > threshold:
            out.append((path, w, h, pixels))
    out.sort(key=lambda item: item[3], reverse=True)
    return out
