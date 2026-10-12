"""
输入值校验与文件句柄释放测试（Task #21 收尾）

运行: python -m pytest tests/test_validators.py -v

覆盖范围：
1. validators 的解析/夹取/批量归一化
2. 平铺间距为 0 或负数时不再静默失败（行为 + 性能双约束）
3. safe_open_image / Logo 加载不再占用源文件句柄（Windows 可删除性）
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image

from src.validators import (
    IMAGE_FIELD_SPECS,
    TEXT_FIELD_SPECS,
    clamp_number,
    normalize_fields,
    parse_number,
    safe_step,
)
from src.config import WatermarkConfig
from src.watermark_engine import apply_watermark, safe_open_image, tile_grid


# ==================== 1. 数值解析 ====================

class TestParseNumber:
    def test_accepts_int_and_float(self):
        assert parse_number(42) == 42
        assert parse_number(3.5) == 3.5

    def test_accepts_numeric_string(self):
        assert parse_number("42") == 42
        assert parse_number("12.5") == 12.5
        assert parse_number("  30  ") == 30

    def test_accepts_fullwidth_digits(self):
        """中文输入法常把数字打成全角"""
        assert parse_number("１２") == 12
        assert parse_number("３０") == 30

    def test_integer_mode_truncates(self):
        assert parse_number("12.9", integer=True) == 12
        assert parse_number(7.99, integer=True) == 7

    @pytest.mark.parametrize("bad", [None, "", "   ", "abc", "12abc", "nan", "inf", "-", True, False])
    def test_rejects_invalid(self, bad):
        assert parse_number(bad) is None


# ==================== 2. 夹取 ====================

class TestClampNumber:
    def test_in_range_untouched(self):
        spec = (0, 100, 50, True)
        assert clamp_number(30, spec) == (30, False)

    def test_below_min(self):
        spec = (0, 100, 50, True)
        assert clamp_number(-10, spec) == (0, True)

    def test_above_max(self):
        spec = (0, 100, 50, True)
        assert clamp_number(999, spec) == (100, True)

    def test_invalid_falls_back_to_default(self):
        spec = (0, 100, 50, True)
        assert clamp_number("abc", spec) == (50, True)

    def test_float_spec_keeps_float(self):
        spec = (0.005, 0.5, 0.04, False)
        value, corrected = clamp_number(0.9, spec)
        assert value == 0.5 and corrected is True

    def test_spacing_never_zero_or_negative(self):
        """间距必须 > 0：0 或负数会让平铺静默失效"""
        for raw in (0, -1, -999, "0", "-5"):
            value, corrected = clamp_number(raw, TEXT_FIELD_SPECS["spacing_x"])
            assert value >= 1, f"间距 {raw} 未被兜底"
            assert corrected is True


# ==================== 3. 批量归一化 ====================

class TestNormalizeFields:
    def test_returns_notice_on_correction(self):
        data, notices = normalize_fields({"custom_x": 999}, TEXT_FIELD_SPECS)
        assert data["custom_x"] == 100
        assert len(notices) == 1
        assert "位置 X" in notices[0]
        assert "100" in notices[0]

    def test_no_notice_when_valid(self):
        data, notices = normalize_fields({"custom_x": 50, "spacing_x": 100}, TEXT_FIELD_SPECS)
        assert notices == []
        assert data["custom_x"] == 50

    def test_absent_key_ignored(self):
        """缺失字段不视为错误（老预设补默认值由 WatermarkConfig 负责）"""
        data, notices = normalize_fields({"custom_x": 50}, TEXT_FIELD_SPECS)
        assert "spacing_x" not in data
        assert notices == []

    def test_undeclared_key_preserved(self):
        data, notices = normalize_fields(
            {"custom_x": 50, "text": "内部资料", "tile": True}, TEXT_FIELD_SPECS
        )
        assert data["text"] == "内部资料"
        assert data["tile"] is True
        assert notices == []

    def test_empty_string_spacing_falls_back(self):
        """清空间距输入框 → 回退默认值并提示，而不是抛 TclError"""
        data, notices = normalize_fields({"spacing_x": ""}, TEXT_FIELD_SPECS)
        assert data["spacing_x"] == 100
        assert notices and "空值" in notices[0]

    def test_image_specs_cover_logo_fields(self):
        data, notices = normalize_fields(
            {"logo_custom_x": 200, "logo_spacing_y": -3, "scale_ratio": 5.0},
            IMAGE_FIELD_SPECS,
        )
        assert data["logo_custom_x"] == 100
        assert data["logo_spacing_y"] == 1
        assert data["scale_ratio"] == 1.0
        assert len(notices) == 3

    def test_original_dict_not_mutated(self):
        original = {"custom_x": 999}
        normalize_fields(original, TEXT_FIELD_SPECS)
        assert original["custom_x"] == 999


# ==================== 4. 平铺步长兜底 ====================

class TestSafeStep:
    def test_normal(self):
        assert safe_step(40, 100) == 140

    def test_zero_spacing_gives_sprite_size(self):
        assert safe_step(40, 0) == 40

    def test_negative_spacing_falls_back_to_sprite_size(self):
        """步长 <= 0 时兜底为精灵尺寸，不能是 1（否则逐像素粘贴会卡死）"""
        assert safe_step(40, -10000) == 40
        assert safe_step(40, -40) == 40

    def test_small_negative_spacing_kept(self):
        """轻微负间距 = 有意重叠平铺，步长仍为正时保持原值"""
        assert safe_step(40, -5) == 35
        assert safe_step(40, -39) == 1


class TestTileGridRobustness:
    """间距异常时既不能抛异常，也不能静默画出空结果"""

    @pytest.mark.parametrize("spacing", [100, 0, -5, -10000])
    def test_always_draws_something(self, spacing):
        overlay = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        sprite = Image.new("RGBA", (40, 40), (255, 0, 0, 255))
        tile_grid(overlay, sprite, spacing, spacing, False)
        alphas = overlay.getchannel("A").getextrema()
        assert alphas[1] > 0, f"间距 {spacing} 时水印静默消失"

    def test_extreme_negative_spacing_is_fast(self):
        """兜底后不能退化为逐像素粘贴"""
        overlay = Image.new("RGBA", (2000, 2000), (0, 0, 0, 0))
        sprite = Image.new("RGBA", (40, 40), (255, 0, 0, 255))
        start = time.perf_counter()
        tile_grid(overlay, sprite, -10000, -10000, False)
        assert time.perf_counter() - start < 1.0


# ==================== 5. 文件句柄释放 ====================

class TestFileHandleRelease:
    def test_corrupt_file_does_not_lock_handle(self):
        """损坏文件抛异常后，源文件必须仍可删除"""
        with tempfile.TemporaryDirectory() as tmpdir:
            good = os.path.join(tmpdir, "good.png")
            Image.new("RGB", (40, 40), (9, 9, 9)).save(good)
            bad = os.path.join(tmpdir, "bad.jpg")
            with open(good, "rb") as f:
                payload = f.read()
            with open(bad, "wb") as f:
                f.write(payload[: len(payload) // 3])

            with pytest.raises(Exception):
                safe_open_image(bad)

            os.remove(bad)  # 句柄未释放时这里会抛 PermissionError

    @pytest.mark.parametrize("ext", [".jpg", ".png", ".bmp", ".tiff", ".webp"])
    def test_safe_open_image_releases_handle(self, ext):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, f"pic{ext}")
            Image.new("RGB", (60, 40), (10, 120, 200)).save(path)

            img = safe_open_image(path)
            assert img.size == (60, 40)

            os.remove(path)

    def test_returned_image_detached_from_file(self):
        """返回对象不应再持有文件句柄（可安全关闭文件后继续使用）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "p.png")
            Image.new("RGB", (50, 50), (1, 2, 3)).save(path)
            img = safe_open_image(path)
            os.remove(path)
            assert img.getpixel((0, 0)) == (1, 2, 3)

    def test_logo_file_releasable_after_watermark(self):
        """加完 Logo 水印后，Logo 源文件必须可删除"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logo = os.path.join(tmpdir, "logo.png")
            Image.new("RGBA", (80, 80), (255, 0, 0, 200)).save(logo)

            base = Image.new("RGB", (400, 300), (240, 240, 240))
            cfg = WatermarkConfig(
                watermark_type="image",
                logo_path=logo,
                scale_ratio=0.15,
                logo_opacity=60,
            )
            result = apply_watermark(base, cfg)
            assert result.size == (400, 300)

            os.remove(logo)
