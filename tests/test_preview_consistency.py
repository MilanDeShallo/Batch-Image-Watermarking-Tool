"""
预览与导出几何一致性测试

为什么要专门测这个
------------------
预览为了流畅是在「缩小后的图」上渲染水印的（画布只有几百像素宽，
在原图分辨率上渲染既慢又看不出差别）。但间距、边距这类参数是按像素配的：
不换算就会出现「预览里的水印比导出更稀、离边更远」——
实测 6000px 图偏 1.5 倍、8000px 偏 2 倍，用户等于在瞎调。

引擎用 scale 参数解决：scale = 渲染分辨率 / 导出分辨率，
绝对像素类参数（间距、边距、字号下限、logo 宽度下限）按它等比换算。

本文件断言的就是「不同分辨率下渲染出来的水印，几何必须一致」。
"""

import os
import tempfile

import pytest
from PIL import Image, ImageDraw

from src.config import WatermarkConfig
from src.watermark_engine import apply_watermark, safe_open_image

# 几何一致性的容差（按图边长归一化）。
# 实测残差约 0.0007（= 1 个工作像素 / 1500px），而修复前的偏差是 0.011~0.012，
# 所以这个阈值既容得下取整，又拦得住"忘记换算 scale"的回归。
GEO_TOL = 0.003


# ============================================================
# 工具
# ============================================================

def make_image(path: str, size: tuple[int, int]) -> str:
    """生成一张纯色底图（纯色便于按亮度阈值取墨迹）"""
    Image.new("RGB", size, (30, 60, 90)).save(path, quality=95)
    return path


def make_square_logo(path: str, color=(255, 255, 255, 255)) -> str:
    """生成方形 logo（实心方块，便于数平铺个数）"""
    Image.new("RGBA", (100, 100), color).save(path)
    return path


def ink_mask(img: Image.Image, thresh: int = 200) -> Image.Image:
    return img.convert("L").point(lambda v: 255 if v >= thresh else 0)


def ink_bbox_norm(img: Image.Image, thresh: int = 200):
    """墨迹外框，按宽高归一化到 0-1（与分辨率无关）"""
    box = ink_mask(img, thresh).getbbox()
    if not box:
        return None
    w, h = img.size
    return (box[0] / w, box[1] / h, box[2] / w, box[3] / h)


def max_runs_per_row(img: Image.Image, thresh: int = 200) -> int:
    """
    墨迹最多的一行里有几个独立的块。

    对"实心方块平铺"来说，这一行的块数就等于该行的水印个数，
    于是可以在不关心分辨率的前提下比较平铺密度。
    """
    mask = ink_mask(img, thresh)
    data = mask.tobytes()
    w, h = mask.size
    best = 0
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        runs, prev_ink = 0, False
        for v in row:
            on = v > 0
            if on and not prev_ink:
                runs += 1
            prev_ink = on
        if runs > best:
            best = runs
    return best


def render_preview_style(src_path: str, cfg, target_dim: int):
    """
    按预览的方式渲染：先缩到工作尺寸，再用 scale=工作尺寸/导出尺寸 渲染。

    返回 (结果图, scale)
    """
    orig = safe_open_image(src_path)
    ref = orig.size
    long_side = max(ref)
    if long_side > target_dim:
        ratio = target_dim / long_side
        work = orig.resize(
            (max(1, int(ref[0] * ratio)), max(1, int(ref[1] * ratio))),
            Image.LANCZOS,
        )
    else:
        work = orig
    scale = work.width / max(ref[0], 1)
    return apply_watermark(work, cfg, scale), scale


# ============================================================
# 单次定位：位置/边距必须与导出一致
# ============================================================

class TestSinglePositionConsistency:

    CFG = dict(
        watermark_type="text", text="WATERMARK", color_rgba=(255, 255, 255, 255),
        font_ratio=0.06, angle=0, tile=False,
        position="bottom-right", margin=20,
    )

    @pytest.mark.parametrize("size", [(6000, 4000), (8000, 6000), (3000, 2000)])
    def test_corner_distance_matches_export(self, size):
        """右下角水印到边角的距离，按比例必须与导出一致"""
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(os.path.join(tmp, "a.jpg"), size)
            cfg = WatermarkConfig(**self.CFG)

            export = apply_watermark(safe_open_image(src), cfg)      # scale=1.0
            preview, scale = render_preview_style(src, cfg, 1500)

            e = ink_bbox_norm(export)
            p = ink_bbox_norm(preview)
            assert e and p, "渲染结果里没有找到墨迹"

            for idx, name in ((2, "右边距"), (3, "下边距")):
                assert abs(e[idx] - p[idx]) <= GEO_TOL, (
                    f"{size} {name}：导出 {e[idx]:.4f} vs 预览 {p[idx]:.4f}"
                    f"（scale={scale:.3f}），偏差超过 {GEO_TOL}"
                )

    def test_scale_one_equals_export(self):
        """scale 默认为 1.0 时必须与不传参完全一致（导出路径不受影响）"""
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(os.path.join(tmp, "a.jpg"), (1200, 900))
            cfg = WatermarkConfig(**self.CFG)
            img = safe_open_image(src)

            default = apply_watermark(img.copy(), cfg)
            explicit = apply_watermark(img.copy(), cfg, 1.0)
            assert default.tobytes() == explicit.tobytes()

    def test_without_scale_the_offset_is_visible(self):
        """
        反向验证：不传 scale 时偏差确实明显（防止有人"顺手去掉"这个参数）

        这不是在测旧代码，而是在钉住 scale 的作用 —— 若 scaled_px 失效，
        本用例会先失败。
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(os.path.join(tmp, "a.jpg"), (6000, 4000))
            cfg = WatermarkConfig(**self.CFG)

            export = apply_watermark(safe_open_image(src), cfg)
            preview, _scale = render_preview_style(src, cfg, 1500)

            work = safe_open_image(src)
            work = work.resize((1500, 1000), Image.LANCZOS)
            naive = apply_watermark(work, cfg)          # 忘了换算 scale

            e = ink_bbox_norm(export)
            p = ink_bbox_norm(preview)
            n = ink_bbox_norm(naive)

            assert abs(e[2] - n[2]) > GEO_TOL * 2, (
                "未换算 scale 的结果竟然也一致，说明 scale 参数没有生效"
            )
            assert abs(e[2] - p[2]) < abs(e[2] - n[2]) / 3


# ============================================================
# 平铺：密度必须与导出一致
# ============================================================

class TestTileConsistency:

    def _cfg(self, logo_path):
        return WatermarkConfig(
            watermark_type="image", logo_path=logo_path, scale_ratio=0.06,
            logo_opacity=100, logo_tile=True,
            logo_spacing_x=200, logo_spacing_y=200,
        )

    @pytest.mark.parametrize("size", [(6000, 4000), (8000, 6000)])
    def test_tile_count_per_row_matches_export(self, size):
        """
        同一行里的水印个数，预览与导出必须一致。

        用实心方块 logo 而不是文字：文字有笔画结构，一行里会出现多个墨块，
        数出来的个数没有意义；方块则一块就是一个水印。
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(os.path.join(tmp, "a.jpg"), size)
            logo = make_square_logo(os.path.join(tmp, "logo.png"))
            cfg = self._cfg(logo)

            export = apply_watermark(safe_open_image(src), cfg)
            preview, scale = render_preview_style(src, cfg, 1500)

            n_export = max_runs_per_row(export)
            n_preview = max_runs_per_row(preview)

            assert n_export >= 3, f"导出结果里只数到 {n_export} 个水印，测试图不合适"
            assert abs(n_export - n_preview) <= 1, (
                f"{size} 每行水印数：导出 {n_export} vs 预览 {n_preview}"
                f"（scale={scale:.3f}），平铺密度不一致"
            )

    def test_logo_tile_without_scale_is_denser(self):
        """反向验证：不换算 scale 时预览的平铺会明显更稀（间距按原图像素算）"""
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(os.path.join(tmp, "a.jpg"), (6000, 4000))
            logo = make_square_logo(os.path.join(tmp, "logo.png"))
            cfg = self._cfg(logo)

            export = apply_watermark(safe_open_image(src), cfg)
            work = safe_open_image(src).resize((1500, 1000), Image.LANCZOS)
            naive = apply_watermark(work, cfg)

            assert max_runs_per_row(export) > max_runs_per_row(naive) + 1, (
                "未换算 scale 的预览竟然与导出密度一致，说明 scale 没生效"
            )


# ============================================================
# 字号下限：小图/小比例下也要等比
# ============================================================

class TestFontFloorScaling:

    CFG = dict(
        watermark_type="text", text="A", color_rgba=(255, 255, 255, 255),
        font_ratio=0.01, angle=0, position="middle-center",
    )

    def test_floor_scales_with_preview_resolution(self):
        """
        font_ratio 很小时会撞上 12px 字号下限。

        预览若仍按 12px 渲染，字号相对图尺寸就比导出大一倍（下限没跟着缩），
        所以下限必须按 scale 一起换算。

        阈值说明：缩到 6px 的白字经抗锯齿后峰值亮度低于 200，
        因此这里用 100（底色亮度约 54，仍能干净地排除背景）。
        """
        THRESH = 100
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(os.path.join(tmp, "a.jpg"), (800, 600))
            cfg = WatermarkConfig(**self.CFG)

            export = apply_watermark(safe_open_image(src), cfg)
            preview, scale = render_preview_style(src, cfg, 400)

            e = ink_bbox_norm(export, THRESH)
            p = ink_bbox_norm(preview, THRESH)
            assert e and p, f"没找到墨迹（导出={e}, 预览={p}）"

            h_export = e[3] - e[1]
            h_preview = p[3] - p[1]
            assert abs(h_export - h_preview) <= 0.006, (
                f"字号占比：导出 {h_export:.4f} vs 预览 {h_preview:.4f}"
                f"（scale={scale:.3f}）"
            )

            # 不换算时下限顶在 12px，占比会明显更大
            work = safe_open_image(src).resize((400, 300), Image.LANCZOS)
            naive = apply_watermark(work, cfg)
            nb = ink_bbox_norm(naive, THRESH)
            h_naive = nb[3] - nb[1]
            assert h_naive > h_export * 1.5, (
                f"未换算 scale 时字号占比 {h_naive:.4f} 未明显偏大，测试失去意义"
            )


# ============================================================
# PDF：参考尺寸口径必须与覆盖层一致
# ============================================================

class TestPdfReferenceSize:

    def _make_pdf(self, tmp: str, rotate_second: bool = False) -> str:
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.pagesizes import A4, landscape
        from pypdf import PdfReader, PdfWriter

        raw = os.path.join(tmp, "raw.pdf")
        c = rl_canvas.Canvas(raw, pagesize=A4)
        c.drawString(72, 700, "page 1")
        c.showPage()
        c.setPageSize(landscape(A4))
        c.drawString(72, 400, "page 2")
        c.showPage()
        c.save()

        if not rotate_second:
            return raw

        reader = PdfReader(raw)
        writer = PdfWriter()
        writer.append(reader)
        writer.pages[1].rotate(90)
        out = os.path.join(tmp, "rotated.pdf")
        with open(out, "wb") as f:
            writer.write(f)
        return out

    def test_reference_size_matches_overlay_geometry(self):
        """pdf_page_reference_size 用的就是覆盖层的几何口径（含旋转页）"""
        from src.pdf_engine import (
            OVERLAY_SCALE, _page_geometry, overlay_reference_size,
            pdf_page_reference_size, render_pdf_page,
        )
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_pdf(tmp, rotate_second=True)
            reader = PdfReader(path)

            for index in range(len(reader.pages)):
                media_w, media_h, rotate, _ox, _oy = _page_geometry(reader.pages[index])
                ref = pdf_page_reference_size(path, index)
                assert ref == overlay_reference_size(media_w, media_h, rotate)

                # 与 pypdfium2 的实际渲染尺寸一致（允许 1px 的取整差）
                rendered = render_pdf_page(path, page_num=index, scale=OVERLAY_SCALE).size
                assert abs(rendered[0] - ref[0]) <= 1 and abs(rendered[1] - ref[1]) <= 1, (
                    f"第 {index + 1} 页：渲染 {rendered} vs 参考 {ref}"
                )

    def test_page_index_out_of_range_falls_back_to_first(self):
        from src.pdf_engine import pdf_page_reference_size

        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_pdf(tmp)
            assert pdf_page_reference_size(path, 99) == pdf_page_reference_size(path, 0)
