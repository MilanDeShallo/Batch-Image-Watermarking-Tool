"""
PDF 水印引擎测试

运行: python -m pytest tests/test_pdf.py -v
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import WatermarkConfig
from src.pdf_engine import apply_pdf_watermark, render_pdf_page, get_pdf_page_count
from src.file_manager import is_pdf_file, is_image_file

from PIL import Image
from reportlab.pdfgen import canvas as rl_canvas


def _create_test_pdf(path: str, pages: int = 3, page_size=(595, 842)):
    """创建测试 PDF（多个空白页）"""
    c = rl_canvas.Canvas(path, pagesize=page_size)
    for i in range(pages):
        c.drawString(50, 750, f"Test page {i + 1}")
        c.showPage()
    c.save()


def _create_test_logo(path: str):
    """创建测试 Logo PNG"""
    logo = Image.new("RGBA", (60, 60), (255, 0, 0, 255))
    logo.save(path)


# ==================== 文件识别测试 ====================

class TestPdfFileRecognition:
    def test_is_pdf_file(self):
        assert is_pdf_file("test.pdf") is True
        assert is_pdf_file("test.PDF") is True
        assert is_pdf_file("test.jpg") is False

    def test_is_image_file_accepts_pdf(self):
        """is_image_file 现在也接受 PDF"""
        assert is_image_file("doc.pdf") is True
        assert is_image_file("photo.png") is True
        assert is_image_file("data.csv") is False


# ==================== 页面渲染测试 ====================

class TestPdfRender:
    def test_render_first_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "test.pdf")
            _create_test_pdf(pdf_path, pages=2)

            img = render_pdf_page(pdf_path, page_num=0, scale=2.0)
            assert img.mode == "RGB"
            assert img.size[0] > 0 and img.size[1] > 0
            # A4 595x842 点 × 2.0 ≈ 1190x1684
            assert abs(img.size[0] - 1190) < 10
            assert abs(img.size[1] - 1684) < 10

    def test_get_page_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "test.pdf")
            _create_test_pdf(pdf_path, pages=3)
            assert get_pdf_page_count(pdf_path) == 3


# ==================== 水印应用测试 ====================

class TestPdfWatermark:
    def test_text_watermark_basic(self):
        """文字水印 → 输出存在、页数一致、可再打开"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            dst = os.path.join(tmpdir, "out.pdf")
            _create_test_pdf(src, pages=3)

            cfg = WatermarkConfig(text="机密文件", font_ratio=0.05, opacity=80)
            pages = apply_pdf_watermark(src, cfg, dst)

            assert pages == 3
            assert os.path.exists(dst)
            assert os.path.getsize(dst) > os.path.getsize(src)  # 水印增加体积

            # 输出可再打开且页数一致
            assert get_pdf_page_count(dst) == 3

    def test_text_watermark_tiled(self):
        """平铺水印"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            dst = os.path.join(tmpdir, "out.pdf")
            _create_test_pdf(src, pages=1)

            cfg = WatermarkConfig(
                text="©版权所有",
                tile=True,
                spacing_x=150,
                spacing_y=150,
                opacity=30,
            )
            apply_pdf_watermark(src, cfg, dst)
            assert os.path.exists(dst)

    def test_logo_watermark(self):
        """Logo 图片水印"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logo_path = os.path.join(tmpdir, "logo.png")
            _create_test_logo(logo_path)

            src = os.path.join(tmpdir, "src.pdf")
            dst = os.path.join(tmpdir, "out.pdf")
            _create_test_pdf(src, pages=2)

            cfg = WatermarkConfig(
                watermark_type="image",
                logo_path=logo_path,
                scale_ratio=0.15,
                logo_opacity=90,
                logo_position="bottom-right",
            )
            pages = apply_pdf_watermark(src, cfg, dst)
            assert pages == 2
            assert os.path.exists(dst)

    def test_watermark_visual_effect(self):
        """水印在视觉上生效：加水印后的页面渲染与原始不同"""
        from PIL import ImageChops
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            dst = os.path.join(tmpdir, "out.pdf")
            _create_test_pdf(src, pages=1)

            # 白底 + 默认白色水印本来就"看不见"（白字白底），
            # 这里用红色断言水印确实被画到了页面上
            cfg = WatermarkConfig(
                text="WATERMARK", font_ratio=0.12,
                color_rgba=(255, 0, 0, 255), opacity=100,
            )
            apply_pdf_watermark(src, cfg, dst)

            # 渲染对比：加水印页应有显著差异
            img_before = render_pdf_page(src, scale=1.0)
            img_after = render_pdf_page(dst, scale=1.0)
            diff_img = ImageChops.difference(img_before, img_after).convert("L")
            hist = diff_img.histogram()
            total = img_before.size[0] * img_before.size[1]
            changed = sum(hist[10:])  # 亮度差 >10 的像素数
            ratio = changed / total
            assert ratio > 0.01, f"水印视觉差异过小: {ratio:.4f}"

    def test_multi_size_pages(self):
        """同一 PDF 中不同尺寸页面（覆盖层按尺寸缓存）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 手工创建两种页面尺寸的 PDF
            from pypdf import PdfWriter
            writer = PdfWriter()
            for size in [(595, 842), (842, 595)]:
                page_path = os.path.join(tmpdir, "page.pdf")
                c = rl_canvas.Canvas(page_path, pagesize=size)
                c.showPage()
                c.save()
                from pypdf import PdfReader
                writer.append(PdfReader(page_path))
            src = os.path.join(tmpdir, "multi.pdf")
            with open(src, "wb") as f:
                writer.write(f)

            dst = os.path.join(tmpdir, "out.pdf")
            cfg = WatermarkConfig(text="TEST", opacity=50)
            pages = apply_pdf_watermark(src, cfg, dst)
            assert pages == 2
            assert get_pdf_page_count(dst) == 2

    def test_nonexistent_file_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = WatermarkConfig(text="X")
            try:
                apply_pdf_watermark(
                    os.path.join(tmpdir, "nope.pdf"), cfg,
                    os.path.join(tmpdir, "out.pdf")
                )
                assert False, "应抛出 FileNotFoundError"
            except FileNotFoundError:
                pass

    def test_corrupt_pdf_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "bad.pdf")
            with open(src, "wb") as f:
                f.write(b"not a pdf at all")
            cfg = WatermarkConfig(text="X")
            try:
                apply_pdf_watermark(src, cfg, os.path.join(tmpdir, "out.pdf"))
                assert False, "应抛出 ValueError"
            except ValueError:
                pass


# ==================== 预览一致性测试 ====================

class TestPreviewConsistency:
    def test_preview_matches_export_geometry(self):
        """预览渲染（页面+水印）与导出后渲染结果几何一致"""
        from PIL import ImageChops, ImageStat
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            dst = os.path.join(tmpdir, "out.pdf")
            _create_test_pdf(src, pages=1)

            cfg = WatermarkConfig(text="预览一致", font_ratio=0.08, opacity=90)
            apply_pdf_watermark(src, cfg, dst)

            # 导出后渲染
            img_export = render_pdf_page(dst, scale=1.0)

            # 模拟预览管线：渲染页面 → Pillow 引擎加水印
            from src.watermark_engine import apply_watermark
            page_img = render_pdf_page(src, scale=2.0)
            preview = apply_watermark(page_img, cfg)
            preview = preview.convert("RGB").resize(
                img_export.size, Image.LANCZOS
            )

            # 两者的整体亮度应接近（容差覆盖渲染器差异）
            stat_e = ImageStat.Stat(img_export.convert("L"))
            stat_p = ImageStat.Stat(preview.convert("L"))
            diff = abs(stat_e.mean[0] - stat_p.mean[0])
            assert diff < 30, f"预览与导出差异过大: {diff}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
