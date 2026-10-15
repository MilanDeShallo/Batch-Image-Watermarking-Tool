"""
渲染几何与不透明度回归测试

对应四个已修复问题：
1. 用 RGBA 精灵当遮罩 paste → 半透明水印被平方削弱（改用 alpha_composite）
2. 文字精灵的透明内边距被计入平铺步长 → 间距调到 1 仍然很稀（crop_to_ink）
3. PDF /Rotate 页面水印被拉伸约 2 倍（覆盖层改按可视方向渲染并反向旋转）
4. Logo「交错排列」参数从未传给引擎（logo_stagger）

运行: python -m pytest tests/test_render_geometry.py -v
"""

import os
import sys
import tempfile


from PIL import Image, ImageChops
from reportlab.pdfgen import canvas as rl_canvas

from src.config import WatermarkConfig
from src.pdf_engine import (
    _overlay_placement,
    apply_pdf_watermark,
    render_pdf_page,
    visual_size,
)
from src.watermark_engine import (
    _render_text_sprite,
    apply_watermark,
    crop_to_ink,
    load_font_with_fallback,
)

GRAY = (128, 128, 128)
WHITE_TEXT = (255, 255, 255, 255)


# ==================== 工具函数 ====================

def _max_diff(img_a: Image.Image, img_b: Image.Image) -> int:
    """两图的单像素最大差异（用于衡量水印浓度）"""
    diff = ImageChops.difference(
        img_a.convert("RGB"), img_b.convert("RGB")
    ).convert("L")
    return int(diff.getextrema()[1])


def _ink_ratio(img: Image.Image, threshold: int = 100) -> float:
    """亮于阈值的像素占比（黑底上的水印覆盖率）"""
    mask = img.convert("L").point(lambda v: 255 if v > threshold else 0)
    return mask.histogram()[255] / (img.width * img.height)


def _ink_bbox(img: Image.Image, tol: int = 60):
    """与纯白背景差异超过 tol 的像素外框（白底上找水印墨迹）"""
    white = Image.new("RGB", img.size, (255, 255, 255))
    diff = ImageChops.difference(img.convert("RGB"), white).convert("L")
    return diff.point(lambda v: 255 if v > tol else 0).getbbox()


def _blank_pdf(path: str, page_size=(595, 842), rotate: int = 0) -> None:
    """创建空白单页 PDF（rotate 非 0 时写入 /Rotate）"""
    base_path = path + ".base.pdf"
    c = rl_canvas.Canvas(base_path, pagesize=page_size)
    c.showPage()
    c.save()

    if not rotate:
        os.replace(base_path, path)
        return

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject

    writer = PdfWriter()
    writer.append(PdfReader(base_path))
    writer.pages[0][NameObject("/Rotate")] = NumberObject(rotate)
    with open(path, "wb") as f:
        writer.write(f)
    os.remove(base_path)


# ==================== 1. 不透明度合成 ====================

class TestOpacityCompositing:
    """滑块值（alpha）必须线性对应实际浓度"""

    @staticmethod
    def _cfg(alpha: int, **kwargs) -> WatermarkConfig:
        return WatermarkConfig(
            text="内部资料",
            font_ratio=0.06,
            color_rgba=(255, 255, 255, alpha),
            **kwargs,
        )

    def test_density_is_proportional_to_alpha(self):
        base = Image.new("RGB", (800, 600), GRAY)
        full = _max_diff(base, apply_watermark(base, self._cfg(255)))
        assert full >= 120, f"不透明度 100% 时浓度只有 {full}，白字在灰底上应接近最亮"

        for pct in (25, 50, 75):
            alpha = int(pct * 255 / 100)
            measured = _max_diff(base, apply_watermark(base, self._cfg(alpha)))
            expected = full * alpha / 255
            assert measured >= expected * 0.8, (
                f"不透明度 {pct}% 实测浓度 {measured}，期望约 {expected:.0f}"
                f"（旧实现用 paste 遮罩，浓度被平方削弱）"
            )

    def test_zero_alpha_is_invisible(self):
        base = Image.new("RGB", (400, 300), GRAY)
        assert _max_diff(base, apply_watermark(base, self._cfg(0))) == 0

    def test_single_position_uses_same_compositing(self):
        """九宫格定位（非平铺）路径同样不能被遮罩削弱"""
        base = Image.new("RGB", (400, 300), GRAY)
        alpha = 127
        cfg = self._cfg(alpha, position="center", margin=10)
        measured = _max_diff(base, apply_watermark(base, cfg))
        # 白字(255) 与灰底(128) 按 alpha 线性插值：255-128=127 是 100% 时的极值
        expected = 127 * alpha / 255
        assert measured >= expected * 0.8, (
            f"实测浓度 {measured}，期望约 {expected:.0f}（旧实现约 {expected / 4:.0f}）"
        )


# ==================== 2. 平铺间距 ====================

class TestTileSpacing:
    """间距必须按"看得见的边缘"计算"""

    def test_text_sprite_cropped_to_ink(self):
        """精灵四周不应再有成片透明内边距（旧实现水平浪费 39%、垂直 64%+）"""
        font, _ = load_font_with_fallback(None, 48)
        for angle in (0, 30, 45):
            sprite = _render_text_sprite(
                "内部资料\nConfidential", font, WHITE_TEXT, angle
            )
            ink = sprite.getchannel("A").point(
                lambda v: 255 if v >= 8 else 0
            ).getbbox()
            assert ink is not None, f"角度 {angle} 未渲染出任何墨迹"
            waste_x = (ink[0] + sprite.width - ink[2]) / sprite.width
            waste_y = (ink[1] + sprite.height - ink[3]) / sprite.height
            assert waste_x <= 0.05, f"角度 {angle} 水平透明边距 {waste_x:.1%} 过大"
            assert waste_y <= 0.05, f"角度 {angle} 垂直透明边距 {waste_y:.1%} 过大"

    def test_spacing_one_gives_tight_tiling(self):
        """
        间距=1 时应接近紧密平铺。
        实测参考：本实现覆盖率约 29%，旧实现（精灵 74x72，含 20px 透明内边距）
        只有约 9% —— 差值全部来自被算进步长的透明边距。
        """
        base = Image.new("RGB", (600, 400), (0, 0, 0))
        cfg = WatermarkConfig(
            text="密", font_ratio=0.08, color_rgba=WHITE_TEXT,
            tile=True, spacing_x=1, spacing_y=1,
        )
        ratio = _ink_ratio(apply_watermark(base, cfg))
        assert ratio > 0.20, f"间距=1 时覆盖率仅 {ratio:.1%}（旧实现约 9%），墨迹裁剪可能失效"

    def test_tile_step_equals_sprite_size_plus_spacing(self):
        """步长必须精确等于「精灵尺寸 + 间距」，间距 1 就是 1px 缝隙"""
        from src.watermark_engine import tile_grid

        sprite = Image.new("RGBA", (40, 40), (255, 255, 255, 255))
        for spacing in (1, 10, 37):
            overlay = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
            tile_grid(overlay, sprite, spacing, spacing, False)
            mask = overlay.getchannel("A").point(lambda v: 255 if v > 0 else 0)

            # 找一行确实落在精灵带内的行
            for y in range(overlay.height):
                row = [mask.getpixel((x, y)) for x in range(overlay.width)]
                if any(row):
                    break
            assert any(row), f"间距 {spacing}：整幅画布没有任何墨迹"

            runs = []
            for value in row:
                if runs and runs[-1][0] == value:
                    runs[-1][1] += 1
                else:
                    runs.append([value, 1])

            gap_runs = [length for value, length in runs if value == 0]
            ink_runs = [length for value, length in runs if value > 0]
            # 首段与末段会被画布边缘截断，只校验中间部分
            assert all(g == spacing for g in gap_runs[1:]), (
                f"间距 {spacing} 实际缝隙 {gap_runs}"
            )
            assert all(i == sprite.width for i in ink_runs[1:-1]), (
                f"间距 {spacing} 实际墨迹宽度 {ink_runs}"
            )

    def test_spacing_monotonically_reduces_coverage(self):
        base = Image.new("RGB", (600, 400), (0, 0, 0))

        def coverage(spacing: int) -> float:
            cfg = WatermarkConfig(
                text="密", font_ratio=0.08, color_rgba=WHITE_TEXT,
                tile=True, spacing_x=spacing, spacing_y=spacing,
            )
            return _ink_ratio(apply_watermark(base, cfg))

        assert coverage(1) > coverage(50) > coverage(150)

    def test_nonpositive_spacing_still_renders(self):
        """兜底：间距为 0 / 负数时水印既不能消失也不能退化成逐像素粘贴"""
        base = Image.new("RGB", (400, 300), (0, 0, 0))
        for spacing in (0, -5, -10000):
            cfg = WatermarkConfig(
                text="密", font_ratio=0.08, color_rgba=WHITE_TEXT,
                tile=True, spacing_x=spacing, spacing_y=spacing,
            )
            assert _ink_ratio(apply_watermark(base, cfg)) > 0.05, f"间距 {spacing} 水印消失"

    def test_crop_to_ink_keeps_fully_transparent_sprite(self):
        blank = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
        assert crop_to_ink(blank).size == (50, 50)

    def test_logo_stagger_survives_config_roundtrip(self):
        cfg = WatermarkConfig(logo_stagger=True)
        assert WatermarkConfig.from_dict(cfg.to_dict()).logo_stagger is True

    def test_logo_stagger_changes_tiling(self):
        """勾选交错排列后平铺结果应变化（此前该参数从未传到引擎）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logo = os.path.join(tmpdir, "logo.png")
            Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(logo)

            base = Image.new("RGB", (500, 400), (255, 255, 255))
            common = dict(
                watermark_type="image", logo_path=logo, scale_ratio=0.1,
                logo_opacity=100, logo_tile=True,
                logo_spacing_x=80, logo_spacing_y=80,
            )
            plain = apply_watermark(base, WatermarkConfig(logo_stagger=False, **common))
            stagger = apply_watermark(base, WatermarkConfig(logo_stagger=True, **common))
            assert _max_diff(plain, stagger) > 0


# ==================== 3. PDF 旋转页 ====================

class TestRotatedPdfPages:
    """带 /Rotate 的页面（扫描件常见）不能被拉伸"""

    def test_geometry_helpers(self):
        assert visual_size(595, 842, 0) == (595, 842)
        assert visual_size(595, 842, 90) == (842, 595)
        assert visual_size(595, 842, 180) == (595, 842)
        assert visual_size(595, 842, 270) == (842, 595)

        assert _overlay_placement(595, 842, 0) == (0, 0, 0)
        assert _overlay_placement(595, 842, 90) == (595, 0, 90)
        assert _overlay_placement(595, 842, 180) == (595, 842, 180)
        assert _overlay_placement(595, 842, 270) == (0, 842, -90)

    def test_all_rotations_keep_size_and_draw_watermark(self):
        for rotate in (0, 90, 180, 270):
            with tempfile.TemporaryDirectory() as tmpdir:
                src = os.path.join(tmpdir, "src.pdf")
                dst = os.path.join(tmpdir, "out.pdf")
                _blank_pdf(src, rotate=rotate)

                cfg = WatermarkConfig(
                    text="机密", font_ratio=0.08, color_rgba=(255, 0, 0, 255)
                )
                assert apply_pdf_watermark(src, cfg, dst) == 1

                before = render_pdf_page(src, scale=1.0)
                after = render_pdf_page(dst, scale=1.0)
                assert before.size == after.size, f"/Rotate {rotate} 输出页面尺寸被改变"
                assert _max_diff(before, after) > 60, f"/Rotate {rotate} 未见水印"

    def test_rotated_page_watermark_not_distorted(self):
        """水印宽高比应与普通页一致（旧实现在 90° 下拉伸约 2 倍）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            plain = os.path.join(tmpdir, "plain.pdf")
            rotated = os.path.join(tmpdir, "rot90.pdf")
            plain_out = os.path.join(tmpdir, "plain_out.pdf")
            rotated_out = os.path.join(tmpdir, "rot90_out.pdf")
            _blank_pdf(plain)
            _blank_pdf(rotated, rotate=90)

            def make_cfg() -> WatermarkConfig:
                return WatermarkConfig(
                    text="WATERMARK", font_ratio=0.06,
                    color_rgba=(255, 0, 0, 255),
                    position="top-left", margin=20,
                )

            apply_pdf_watermark(plain, make_cfg(), plain_out)
            apply_pdf_watermark(rotated, make_cfg(), rotated_out)

            box_plain = _ink_bbox(render_pdf_page(plain_out, scale=1.0))
            box_rot = _ink_bbox(render_pdf_page(rotated_out, scale=1.0))
            assert box_plain and box_rot, "未检测到水印墨迹"

            ratio_plain = (box_plain[2] - box_plain[0]) / (box_plain[3] - box_plain[1])
            ratio_rot = (box_rot[2] - box_rot[0]) / (box_rot[3] - box_rot[1])
            assert abs(ratio_rot / ratio_plain - 1) < 0.15, (
                f"水印被拉伸：普通页宽高比 {ratio_plain:.2f}，"
                f"/Rotate 90 页 {ratio_rot:.2f}"
            )

    def test_rotated_page_position_follows_display_orientation(self):
        """水印应落在"看得见"的左上角（与预览的显示方向一致）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            dst = os.path.join(tmpdir, "out.pdf")
            _blank_pdf(src, rotate=90)

            cfg = WatermarkConfig(
                text="WATERMARK", font_ratio=0.06, color_rgba=(255, 0, 0, 255),
                position="top-left", margin=20,
            )
            apply_pdf_watermark(src, cfg, dst)

            img = render_pdf_page(dst, scale=1.0)
            box = _ink_bbox(img)
            assert box, "未检测到水印"
            assert box[0] < img.width * 0.25, f"水印未贴近显示区左侧: {box}"
            assert box[1] < img.height * 0.25, f"水印未贴近显示区上侧: {box}"


class TestPositionNaming:
    """
    位置命名的单一口径。

    历史问题：图片（Logo）面板用 center-left / center-right，文字面板用
    middle-left / middle-right，而引擎只认 top / middle / bottom 作为垂直分量。
    于是同一个「左中」，从图片面板出去会被引擎当成 v_pos="center"，
    落进 y 字典的 .get() 默认值 → 水印渲染在正中。界面按钮高亮正常、不报错，
    只有看图才发现不对 —— 所以这条必须有测试钉住。

    面板在类内局部导入：pytest 收集时不必为几何测试加载 GUI 依赖。
    """

    BASE = (1000, 800)
    SPRITE = (100, 50)
    MARGIN = 20

    def _anchor(self, pos):
        from src.watermark_engine import anchor_position
        return anchor_position(self.BASE, self.SPRITE, pos, self.MARGIN)

    def test_both_panels_share_one_position_vocabulary(self):
        """两个面板的九宫格键必须一字不差，否则同一个按钮两种表现"""
        from src.ui.image_watermark_panel import ImageWatermarkPanel
        from src.ui.text_watermark_panel import _POSITIONS

        assert [k for k, _ in _POSITIONS] == list(ImageWatermarkPanel._POS_NAMES)

    def test_legacy_center_left_renders_like_middle_left(self):
        """旧写法 center-left / center-right 必须落到与 middle-* 相同的坐标"""
        assert self._anchor("center-left") == self._anchor("middle-left")
        assert self._anchor("center-right") == self._anchor("middle-right")

    def test_left_is_not_center(self):
        """回归核心：左中要真的靠左，不能滑到正中（垂直居中是对的）"""
        x_left, y_left = self._anchor("middle-left")
        x_mid, y_mid = self._anchor("center")

        assert x_left < x_mid, "「左中」的水平位置跑到了正中"
        assert y_left == y_mid, "「左中」的垂直位置应为居中"

    def test_nine_positions_land_where_named(self):
        """九个位置各自归位：横竖两轴都单调"""
        from src.ui.text_watermark_panel import _POSITIONS

        seen = {key: self._anchor(key) for key, _ in _POSITIONS}

        assert seen["middle-left"][0] < seen["center"][0] < seen["middle-right"][0]
        assert seen["top-left"][1] < seen["center"][1] < seen["bottom-left"][1]
        assert len({seen["top-left"], seen["center"], seen["bottom-right"]}) == 3

    def test_legacy_preset_is_migrated_on_load(self):
        """旧预设文件里存的 center-* 反序列化后应已被翻译"""
        cfg = WatermarkConfig.from_dict({
            "text": "x",
            "position": "center-left",
            "logo_position": "center-right",
        })

        assert cfg.position == "middle-left"
        assert cfg.logo_position == "middle-right"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
