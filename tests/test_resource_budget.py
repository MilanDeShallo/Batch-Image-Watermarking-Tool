"""
资源预算单元测试 —— 并行度按像素内存算，而不是按压缩文件大小

为什么需要：
    旧实现按平均文件大小分档开线程，在高分辨率场景下会开太多 ——
    几张大图并发足以把内存吃光，而用户看到的现象只是"程序闪退"。

运行: python -m pytest tests/test_resource_budget.py -v
"""

import os
import sys


from PIL import Image

from src import resource_budget as rb
from src.watermark_engine import MAX_IMAGE_PIXELS_LIMIT, safe_open_image


GB = 1024 ** 3


# ==================== 内存估算 ====================

class TestEstimate:
    def test_scales_with_pixels_not_file_size(self, tmp_path):
        """
        关键性质：内存估算只跟像素数有关，与文件大小无关。

        同一个 2000×1500 的图，存成高质量 JPEG 和低质量 JPEG 的文件体积
        能差十几倍，但解码后占的内存是一样的。
        """
        big_file = tmp_path / "heavy.jpg"
        light_file = tmp_path / "light.jpg"
        Image.new("RGB", (2000, 1500), (10, 20, 30)).save(str(big_file), quality=98)
        Image.new("RGB", (2000, 1500), (10, 20, 30)).save(str(light_file), quality=20)

        assert rb.file_memory_estimate(str(big_file)) == rb.file_memory_estimate(str(light_file))

    def test_estimate_includes_slack(self):
        pixels = 1000 * 1000
        assert rb.estimate_decoded_bytes(1000, 1000) == int(
            pixels * rb.BYTES_PER_PIXEL * rb.TASK_SLACK
        )

    def test_bad_dimensions_fall_back(self):
        assert rb.estimate_decoded_bytes(0, 0) == rb.UNKNOWN_FILE_BYTES
        assert rb.estimate_decoded_bytes("x", None) == rb.UNKNOWN_FILE_BYTES

    def test_probe_reads_header_without_decoding(self, tmp_path):
        p = tmp_path / "a.png"
        Image.new("RGB", (321, 123), (0, 0, 0)).save(str(p))
        assert rb.probe_image_size(str(p)) == (321, 123)

    def test_probe_returns_none_for_pdf(self):
        assert rb.probe_image_size("report.pdf") is None

    def test_probe_returns_none_for_garbage(self, tmp_path):
        p = tmp_path / "broken.jpg"
        p.write_bytes(b"definitely not an image")
        assert rb.probe_image_size(str(p)) is None


# ==================== PDF 页面预算 ====================

def _write_pdf(path, width, height, pages=1):
    """
    写一个指定页面尺寸的空白 PDF。

    只用 pypdf（不引 reportlab）：pypdf 本来就是探测 PDF 页尺寸的依赖，
    这样测试不会因为"少装一个可选依赖"而失败。
    单位是 PDF 点（1/72 英寸）。
    """
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    with open(str(path), "wb") as f:
        writer.write(f)


class TestPdfPageBudget:
    """
    PDF 也要按「页面的显示像素」进预算。

    旧实现：`probe_image_size` 对 .pdf 直接返回 None → `file_memory_estimate`
    一律给 `UNKNOWN_FILE_BYTES=64MB`。实测单页覆盖层峰值：
    A4 40.1MB（反而高估 1.6×）、A3 83.0MB、A2 169.6MB、A1 345.8MB、A0 702.7MB
    —— A0 被低估 11 倍，8 本并发直接冲破预算。
    """

    def test_page_pixels_use_display_size(self, tmp_path):
        """
        像素基数 = 页面点数 × 覆盖层倍率。

        PDF 是矢量、没有"分辨率"，真正吃内存的是按显示尺寸渲染出来的
        那张覆盖层位图，所以这里必须带上 overlay_scale。
        """
        from src.pdf_engine import get_overlay_scale

        scale = get_overlay_scale()
        p = tmp_path / "a4.pdf"
        _write_pdf(p, 595, 842)

        assert rb.probe_pdf_page_pixels(str(p)) == (int(595 * scale), int(842 * scale))

    def test_max_page_wins_across_pages(self, tmp_path):
        """取的是前 N 页里最大的一页，不是第一页"""
        p = tmp_path / "mixed.pdf"
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_blank_page(width=1191, height=1684)      # A2
        with open(str(p), "wb") as f:
            writer.write(f)

        w, h = rb.probe_pdf_page_pixels(str(p))
        assert w > 595 and h > 842, (w, h)

    def test_large_page_estimate_far_exceeds_unknown(self, tmp_path):
        """A0 级页面的估算必须远高于旧的 64MB 兜底"""
        p = tmp_path / "a0.pdf"
        _write_pdf(p, 2384, 3370)

        assert rb.file_memory_estimate(str(p)) > rb.UNKNOWN_FILE_BYTES * 5

    def test_estimate_grows_with_page_area(self, tmp_path):
        small = tmp_path / "a4.pdf"
        big = tmp_path / "a1.pdf"
        _write_pdf(small, 595, 842)
        _write_pdf(big, 1684, 2384)

        assert rb.file_memory_estimate(str(big)) > rb.file_memory_estimate(str(small))

    def test_many_large_pdfs_reduce_workers(self, tmp_path):
        """
        这条是本次修复的实际收益：并行度真的被压下来了。

        旧实现：每本都按 64MB 估 → 预算 1.2GB 能开 18 线程 → CPU 封顶 8。
        现在：按 858MB/本估 → 只能开 1 线程。
        """
        p = tmp_path / "a0.pdf"
        _write_pdf(p, 2384, 3370)
        est = rb.file_memory_estimate(str(p))

        assert est > rb.UNKNOWN_FILE_BYTES * 5, "估算没接上 PDF 页尺寸"

        workers, reason = rb.plan_workers(
            [est] * 8, cpu_count=16, available=int(2.4 * GB)
        )
        assert workers == 1, reason

    def test_broken_pdf_falls_back_to_unknown(self, tmp_path):
        """损坏文件回退到 64MB，且不抛异常（预检绝不能阻断批处理）"""
        p = tmp_path / "broken.pdf"
        p.write_bytes(b"%PDF-1.4 truncated, no xref")

        assert rb.probe_pdf_page_pixels(str(p)) is None
        assert rb.file_memory_estimate(str(p)) == rb.UNKNOWN_FILE_BYTES

    def test_non_pdf_is_not_probed_as_pdf(self, tmp_path):
        p = tmp_path / "a.png"
        Image.new("RGB", (64, 64), (0, 0, 0)).save(str(p))

        assert rb.probe_pdf_page_pixels(str(p)) is None

    def test_page_pixels_keeps_image_branch(self, tmp_path):
        """图片仍走原图像素这条路，不能被 PDF 分支抢走"""
        p = tmp_path / "a.png"
        Image.new("RGB", (321, 123), (0, 0, 0)).save(str(p))

        assert rb.probe_page_pixels(str(p)) == (321, 123)


# ==================== 并行度决策 ====================

class TestPlanWorkers:
    def test_no_info_uses_cpu_limit(self):
        workers, reason = rb.plan_workers([], cpu_count=8, available=8 * GB)
        assert 1 <= workers <= rb.MAX_WORKERS
        assert "尺寸" in reason

    def test_large_images_reduce_workers_by_memory(self):
        """
        内存受限时必须真的降下来。

        100 百万像素的图单张峰值约
        100e6 × 4 × 2.5 = 1.0GB；只给 2.5GB 预算 → 最多 2 线程。
        """
        one_giant = rb.estimate_decoded_bytes(10000, 10000)   # 约 1GB
        workers, reason = rb.plan_workers(
            [one_giant] * 4, cpu_count=16, available=int(2.5 * GB)
        )
        assert workers <= 2, (workers, reason)
        assert workers >= 1

    def test_small_images_allow_full_speed(self):
        tiny = rb.estimate_decoded_bytes(800, 600)
        workers, _ = rb.plan_workers([tiny] * 10, cpu_count=8, available=8 * GB)
        assert workers == min(rb.MAX_WORKERS, 7)

    def test_never_below_one(self):
        """内存再紧也必须能跑（单线程），不能算出 0"""
        huge = rb.estimate_decoded_bytes(30000, 30000)
        workers, _ = rb.plan_workers([huge], cpu_count=8, available=1)
        assert workers == 1

    def test_respects_worker_cap(self):
        tiny = rb.estimate_decoded_bytes(100, 100)
        workers, _ = rb.plan_workers([tiny], cpu_count=64, available=512 * GB)
        assert workers == rb.MAX_WORKERS

    def test_uses_worst_case_not_average(self):
        """
        必须按最坏的那张来定并行度。

        用平均值会低估：9 张小图 + 1 张巨图，平均下来很"轻"，
        但真正的峰值由那张巨图决定。
        """
        small = rb.estimate_decoded_bytes(800, 600)
        giant = rb.estimate_decoded_bytes(12000, 12000)
        workers, _ = rb.plan_workers(
            [small] * 9 + [giant], cpu_count=16, available=int(2.0 * GB)
        )
        assert workers == 1, workers


# ==================== 超限与预警 ====================

class TestThresholds:
    def test_oversized_detects_beyond_hard_limit(self, tmp_path, monkeypatch):
        p = tmp_path / "a.png"
        Image.new("RGB", (10, 10), (0, 0, 0)).save(str(p))
        # 假装它是超限图（真造一张 3 亿像素的图会直接吃掉内存）
        monkeypatch.setattr(rb, "probe_image_size", lambda _p: (40000, 40000))
        found = rb.oversized_files([str(p)])
        assert len(found) == 1
        assert found[0][1] == 40000 * 40000

    def test_large_but_allowed_is_not_oversized(self, tmp_path, monkeypatch):
        p = tmp_path / "a.png"
        Image.new("RGB", (10, 10), (0, 0, 0)).save(str(p))
        monkeypatch.setattr(rb, "probe_image_size", lambda _p: (10000, 8000))
        assert rb.oversized_files([str(p)]) == []

    def test_large_images_sorted_desc(self, tmp_path, monkeypatch):
        paths = []
        for i in range(3):
            p = tmp_path / f"{i}.png"
            Image.new("RGB", (10, 10), (0, 0, 0)).save(str(p))
            paths.append(str(p))
        sizes = {"0": (8000, 8000), "1": (12000, 12000), "2": (9000, 9000)}
        monkeypatch.setattr(
            rb, "probe_image_size",
            lambda p: sizes[os.path.basename(p).split(".")[0]],
        )
        found = rb.large_images(paths)
        assert [item[3] for item in found] == sorted(
            [item[3] for item in found], reverse=True
        )
        assert len(found) == 3

    def test_oversized_includes_pdf(self, tmp_path):
        """
        超大页 PDF 也要进硬上限清单。

        旧实现走 probe_image_size，对 .pdf 返回 None 就 continue ——
        超大页 PDF 既不计入预算、也不进预警，连一句提示都没有。
        """
        p = tmp_path / "huge.pdf"
        # 10000×14000 点 → 显示像素 20000×28000 = 5.6 亿 px，超过 3 亿硬上限
        _write_pdf(p, 10000, 14000)

        found = rb.oversized_files([str(p)])

        assert [os.path.basename(item[0]) for item in found] == ["huge.pdf"]
        assert found[0][1] > rb.MAX_IMAGE_PIXELS

    def test_large_images_includes_pdf(self, tmp_path):
        """预警清单同样不能漏掉 PDF"""
        p = tmp_path / "a0.pdf"
        _write_pdf(p, 2384, 3370)          # 显示像素约 4768×6740 ≈ 3214 万 px

        found = rb.large_images([str(p)], threshold=10_000_000)

        assert [os.path.basename(item[0]) for item in found] == ["a0.pdf"]
        assert found[0][3] > 30_000_000

    def test_format_bytes(self):
        assert rb.format_bytes(0) == "0 B"
        assert "MB" in rb.format_bytes(5 * 1024 * 1024)
        assert "GB" in rb.format_bytes(3 * GB)


# ==================== 与引擎的硬上限一致 ====================

class TestEngineLimit:
    def test_engine_limit_is_finite(self):
        """曾经是 None（完全关掉保护），现在必须是个有限值"""
        assert Image.MAX_IMAGE_PIXELS is not None
        assert Image.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS_LIMIT

    def test_bomb_error_becomes_clear_value_error(self, tmp_path, monkeypatch):
        """
        解压炸弹要被转成"带文件名的明确错误"，
        而不是让整批只看到一个陌生的异常类型。
        """
        p = tmp_path / "bomb.png"
        Image.new("RGB", (10, 10), (0, 0, 0)).save(str(p))

        def _boom(_path):
            raise Image.DecompressionBombError("too many pixels")

        monkeypatch.setattr(Image, "open", _boom)
        try:
            safe_open_image(str(p))
            raised = None
        except Exception as exc:   # noqa: BLE001 - 就是要拿到它
            raised = exc
        assert isinstance(raised, ValueError)
        assert "bomb.png" in str(raised)


class TestPeakFactorCalibration:
    """
    峰值系数与实测值的对照。

    守护的是"别把系数调回平均值"：那种改动在本机不一定立刻出问题，
    但会在别人的高分辨率图上 OOM，而且很难复现。
    """

    # 实测（Windows 工作集采样，both 类型走导出路径；
    # 6000×4000 与 8000×6000 两次一致，说明与尺寸无关）
    MEASURED_BOTH_BYTES_PER_PIXEL = 16.2

    def test_factor_covers_measured_peak(self):
        import src.resource_budget as rb

        effective = rb.BYTES_PER_PIXEL * rb.TASK_SLACK
        assert effective >= self.MEASURED_BOTH_BYTES_PER_PIXEL, (
            f"峰值系数 {effective:.1f} B/px 低于实测 "
            f"{self.MEASURED_BOTH_BYTES_PER_PIXEL} B/px，并行度会被算多"
        )

    def test_estimate_actually_uses_the_factor(self):
        """防止"改了常量但估算函数没接线"这种假修复"""
        import src.resource_budget as rb

        expected = int(1000 * 1000 * rb.BYTES_PER_PIXEL * rb.TASK_SLACK)
        assert rb.estimate_decoded_bytes(1000, 1000) == expected
