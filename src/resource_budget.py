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

# 读不到尺寸（PDF 打不开、损坏文件）时的单任务内存估算
UNKNOWN_FILE_BYTES = 64 * 1024 * 1024

# PDF 单页的峰值系数（等效 28 B/px）。
#
# 为什么不能沿用图片的 TASK_SLACK：实测 PDF 页的 B/px 比图片高。
# 覆盖层渲染的 RSS 峰值增量（overlay_scale=2.0，psutil 采样，缓存未命中，
# text 与 both 两种水印类型结果一致 —— 说明峰值由"页面位图同时存活的份数"
# 决定，与合成内容无关，与图片侧同理）：
#     A4  2.00 Mpx   40.1 MB   21.0 B/px
#     A3  4.01 Mpx   83.0 MB   21.7 B/px
#     A2  8.02 Mpx  169.6 MB   22.2 B/px
#     A1 16.06 Mpx  345.8 MB   22.6 B/px
# 取 7.0（=28 B/px）比实测上限高约 25%，与 TASK_SLACK 的余量口径一致。
# **不要**为了"复用图片系数"把它降到 5.0：实测 22.6 > 20，那样是负余量，
# 大页 PDF 并发时会真的 OOM。
PDF_PAGE_SLACK = 7.0

# 预检时最多读多少页的 MediaBox。
#
# 成本几乎全在"打开 PDF"（解析 xref），与页数关系很小 —— 实测 5 页 1.5ms、
# 60 页 5.8ms、400 页 33.3ms，扫 24 页与只读页树根耗时相同。
# 但仍要设上限：整本遍历会在上千个文件时累积。代价是"页面尺寸不一致、
# 且巨页排在第 24 页之后"的文档会被低估，这是刻意取舍
# （绝大多数文档全篇同尺寸，且巨页靠后本身极罕见）。
# 另注：不能只读页树根的 /MediaBox —— 它是可继承的，但很多生成器
# （实测 reportlab 就是）并不写这一项，那样会直接探不到尺寸。
PDF_SIZE_PROBE_PAGES = 24

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


def estimate_pdf_page_bytes(width: int, height: int) -> int:
    """PDF 单页的峰值内存估算（显示像素 × PDF_PAGE_SLACK）"""
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        return UNKNOWN_FILE_BYTES
    if w <= 0 or h <= 0:
        return UNKNOWN_FILE_BYTES
    return int(w * h * BYTES_PER_PIXEL * PDF_PAGE_SLACK)


def probe_pdf_page_pixels(path: str) -> tuple[int, int] | None:
    """
    最大一页的**显示像素**尺寸（media box × 覆盖层倍率）。

    为什么是"显示像素"而不是 PDF 自身的分辨率：PDF 是矢量，没有分辨率；
    真正吃内存的是按页面显示尺寸渲染出来的那张覆盖层位图，
    所以像素基数 = 页面点数 × 覆盖层倍率（与 pdf_engine.overlay_reference_size 同源）。

    只扫前 PDF_SIZE_PROBE_PAGES 页（取舍见常量处的说明）。
    读不到（不是 PDF / 没装 pypdf / 文件损坏）返回 None，
    调用方按"未知尺寸"处理，**绝不阻断**预检。
    """
    if not str(path).lower().endswith(".pdf"):
        return None
    try:
        from pypdf import PdfReader
        with open(path, "rb") as f:
            reader = PdfReader(f)
            best = None
            for index in range(min(len(reader.pages), PDF_SIZE_PROBE_PAGES)):
                box = reader.pages[index].mediabox
                w = float(box.width)
                h = float(box.height)
                if w <= 0 or h <= 0:
                    continue
                if best is None or w * h > best[0] * best[1]:
                    best = (w, h)
    except Exception:
        return None
    if best is None:
        return None
    # 倍率取当前全局值：预检就发生在批处理开始之前
    try:
        from src.pdf_engine import get_overlay_scale
        scale = float(get_overlay_scale())
    except Exception:
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    return int(best[0] * scale), int(best[1] * scale)


def probe_page_pixels(path: str) -> tuple[int, int] | None:
    """
    预检统一入口：图片取原图像素，PDF 取最大页的显示像素。

    这里只解决"尺寸"；换算成字节时两者 Slack 不同，
    由 file_memory_estimate 按来源分别调用。
    """
    size = probe_image_size(path)
    if size is not None:
        return size
    return probe_pdf_page_pixels(path)


def file_memory_estimate(path: str) -> int:
    """
    单个文件的峰值内存估算。

    图片按解码后的原图像素算；PDF 按**最大页的显示像素**算。
    旧实现把 PDF 一律当 UNKNOWN_FILE_BYTES=64MB：A4 页反而高估，
    但 A3 起开始低估，A0 级取页面低估约 11 倍（实测 702.7MB vs 64MB）。
    """
    size = probe_image_size(path)
    if size is not None:
        return estimate_decoded_bytes(*size)
    pdf_size = probe_pdf_page_pixels(path)
    if pdf_size is not None:
        return estimate_pdf_page_bytes(*pdf_size)
    return UNKNOWN_FILE_BYTES


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
    而不是交给 Pillow / 覆盖层去分配不可能成功的内存。

    走 probe_page_pixels：**PDF 也要进来**。旧实现走 probe_image_size，
    而它对 .pdf 直接返回 None，于是超大页的 PDF 既不进预算也不进预警，
    连一句提示都没有。
    """
    out = []
    for path in files or []:
        size = probe_page_pixels(path)
        if size is None:
            continue
        pixels = size[0] * size[1]
        if pixels > limit:
            out.append((path, pixels))
    return out


def large_images(files, threshold: int = LARGE_IMAGE_PIXELS) -> list[tuple[str, int, int, int]]:
    """
    像素数超过预警阈值的文件（含 PDF —— 见 oversized_files 的说明）。

    :return: [(路径, 宽, 高, 像素数)]，按像素数从大到小
    """
    out = []
    for path in files or []:
        size = probe_page_pixels(path)
        if size is None:
            continue
        w, h = size
        pixels = w * h
        if pixels > threshold:
            out.append((path, w, h, pixels))
    out.sort(key=lambda item: item[3], reverse=True)
    return out
