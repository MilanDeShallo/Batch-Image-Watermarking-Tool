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
