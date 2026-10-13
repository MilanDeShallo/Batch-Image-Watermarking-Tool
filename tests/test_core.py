"""
单元测试 — 核心函数覆盖

运行: python -m pytest tests/ -v
或:   python tests/test_core.py
"""

import os
import sys
import json
import tempfile
import shutil

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import WatermarkConfig, opacity_to_alpha
from src.file_manager import generate_output_path, get_default_output_dir, collect_files
from src.preset_manager import PresetManager
from src.watermark_engine import safe_open_image, apply_watermark, custom_anchor_position

from PIL import Image


# ==================== Config 测试 ====================

class TestConfig:
    """WatermarkConfig 序列化/反序列化测试"""

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict → from_dict 往返测试"""
        cfg = WatermarkConfig(
            text="测试水印",
            font_ratio=0.05,
            color_rgba=(255, 0, 0, 200),
            opacity=80,
            position="top-left",
            position_mode="custom",
            custom_x=30,
            custom_y=70,
            angle=45.0,
            tile=True,
        )
        d = cfg.to_dict()
        cfg2 = WatermarkConfig.from_dict(d)

        assert cfg2.text == "测试水印"
        assert cfg2.font_ratio == 0.05
        assert cfg2.opacity == 80
        # alpha 是派生值：唯一权威源是 opacity，同一次构造里两者必须自洽。
        # 旧实现把 alpha 当输入、opacity 当摆设，会出现「滑块 80%、渲染按 128」
        assert cfg2.color_rgba == (255, 0, 0, opacity_to_alpha(80))
        assert cfg2.position == "top-left"
        assert cfg2.position_mode == "custom"
        assert cfg2.custom_x == 30
        assert cfg2.custom_y == 70
        assert cfg2.angle == 45.0
        assert cfg2.tile is True

    def test_from_dict_ignores_unknown_fields(self):
        """from_dict 忽略未知字段"""
        data = {
            "text": "test",
            "unknown_field": 123,
            "another_unknown": "abc",
        }
        cfg = WatermarkConfig.from_dict(data)
        assert cfg.text == "test"
        # 不应崩溃

    def test_from_dict_color_rgba_list_to_tuple(self):
        """from_dict 将 list 转为 tuple（JSON 不支持 tuple）"""
        data = {
            "color_rgba": [100, 200, 50, 128],
        }
        cfg = WatermarkConfig.from_dict(data)
        assert cfg.color_rgba == (100, 200, 50, 128)
        assert isinstance(cfg.color_rgba, tuple)

    def test_version_migration_v0_to_v1(self):
        """旧配置（无 version 字段）自动迁移"""
        data = {
            "text": "old config",
            # 无 version 字段
        }
        cfg = WatermarkConfig.from_dict(data)
        assert cfg.version == 1
        assert cfg.text == "old config"

    def test_json_save_load(self):
        """JSON 文件保存/加载"""
        cfg = WatermarkConfig(text="JSON测试", opacity=75)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode='w') as f:
            tmp_path = f.name
        try:
            cfg.save_to_json(tmp_path)
            cfg2 = WatermarkConfig.load_from_json(tmp_path)
            assert cfg2.text == "JSON测试"
            assert cfg2.opacity == 75
        finally:
            os.unlink(tmp_path)


# ==================== file_manager 测试 ====================

class TestFileManager:
    """文件路径生成测试"""

    def test_generate_output_path_basic(self):
        """基本输出路径生成"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "photo.jpg")
            with open(src, 'wb') as f:
                f.write(b'fake')

            out = generate_output_path(src, tmpdir, output_format="jpeg")
            assert out.endswith("_watermarked.jpg")
            assert tmpdir in out

    def test_generate_output_path_duplicate(self):
        """重名文件自动编号"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "photo.jpg")
            with open(src, 'wb') as f:
                f.write(b'fake')

            existing = set()
            p1 = generate_output_path(src, tmpdir, output_format="jpeg", existing_names=existing)
            p2 = generate_output_path(src, tmpdir, output_format="jpeg", existing_names=existing)

            assert p1 != p2
            assert "_1" in p2

    def test_generate_output_path_disk_exists(self):
        """输出文件已存在于磁盘时自动编号"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "photo.jpg")
            with open(src, 'wb') as f:
                f.write(b'fake')

            # 先生成路径
            p1 = generate_output_path(src, tmpdir, output_format="jpeg")

            # 在磁盘上创建该文件
            with open(p1, 'wb') as f:
                f.write(b'exists')

            # 再生成应自动编号
            p2 = generate_output_path(src, tmpdir, output_format="jpeg")
            assert p1 != p2
            assert "_1" in p2

    def test_get_default_output_dir(self):
        """默认输出目录"""
        files = ["/path/a.jpg", "/path/b.jpg"]
        result = get_default_output_dir(files)
        assert "watermarked_output" in result

    def test_collect_files_dedup(self):
        """文件收集去重"""
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "a.jpg")
            f2 = os.path.join(tmpdir, "b.jpg")
            for f in [f1, f2]:
                with open(f, 'wb') as fh:
                    fh.write(b'fake')

            added, skipped = collect_files([f1, f2, f1])  # f1 重复
            assert f1 in added
            assert f2 in added
            assert len(added) == 2


# ==================== PresetManager 测试 ====================

class TestPresetManager:
    """预设管理测试"""

    def test_save_load_delete(self):
        """保存→加载→删除"""
        cfg = WatermarkConfig(text="预设测试", opacity=60)

        # 保存
        path = PresetManager.save_preset("__test_preset__", cfg)
        assert os.path.exists(path)

        try:
            # 加载
            cfg2, warnings = PresetManager.load_preset("__test_preset__")
            assert cfg2.text == "预设测试"
            assert cfg2.opacity == 60

            # 列表
            presets = PresetManager.list_presets()
            assert "__test_preset__" in presets

            # 存在检查
            assert PresetManager.preset_exists("__test_preset__")

        finally:
            # 删除
            result = PresetManager.delete_preset("__test_preset__")
            assert result is True
            assert not PresetManager.preset_exists("__test_preset__")

    def test_sanitize_name(self):
        """预设名安全化"""
        assert PresetManager._sanitize_name("test/name") == "test_name"
        assert PresetManager._sanitize_name("test:name") == "test_name"
        assert PresetManager._sanitize_name("") == "unnamed"
        assert PresetManager._sanitize_name("normal") == "normal"


# ==================== watermark_engine 测试 ====================

class TestWatermarkEngine:
    """水印引擎测试"""

    def _create_test_image(self, size=(200, 200), color=(100, 150, 200)):
        """创建测试图片"""
        return Image.new("RGB", size, color)

    def test_safe_open_image_valid(self):
        """safe_open_image 打开正常图片"""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        try:
            img = self._create_test_image()
            img.save(tmp_path)
            result = safe_open_image(tmp_path)
            assert result.size == (200, 200)
        finally:
            os.unlink(tmp_path)

    def test_safe_open_image_corrupt(self):
        """safe_open_image 处理损坏图片"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b'not an image')
            tmp_path = f.name
        try:
            try:
                safe_open_image(tmp_path)
                assert False, "应该抛出异常"
            except (ValueError, Exception):
                pass  # 预期行为
        finally:
            os.unlink(tmp_path)

    def test_text_watermark_basic(self):
        """文字水印基本渲染"""
        cfg = WatermarkConfig(text="TEST", font_ratio=0.05)
        img = self._create_test_image()
        result = apply_watermark(img, cfg)
        assert result.size == (200, 200)
        assert result.mode == "RGBA"

    def test_text_watermark_empty_text(self):
        """空文字水印返回原图"""
        cfg = WatermarkConfig(text="")
        img = self._create_test_image()
        result = apply_watermark(img, cfg)
        # 空文字应直接返回原图（可能转RGBA）
        assert result.size == (200, 200)

    def test_image_watermark_no_logo(self):
        """无 Logo 时返回原图"""
        cfg = WatermarkConfig(watermark_type="image", logo_path="")
        img = self._create_test_image()
        result = apply_watermark(img, cfg)
        assert result.size == (200, 200)

    def test_image_watermark_with_logo(self):
        """带 Logo 的图片水印"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logo_path = os.path.join(tmpdir, "logo.png")
            logo = Image.new("RGBA", (50, 50), (255, 0, 0, 255))
            logo.save(logo_path)

            cfg = WatermarkConfig(
                watermark_type="image",
                logo_path=logo_path,
                scale_ratio=0.2,
                logo_opacity=100,
                logo_angle=30,
            )
            img = self._create_test_image()
            result = apply_watermark(img, cfg)
            assert result.size == (200, 200)
            assert result.mode == "RGBA"

    def test_custom_anchor_position(self):
        """自定义坐标定位"""
        # 50% = 居中
        x, y = custom_anchor_position((200, 200), (50, 50), 50, 50)
        assert x == 75  # (200-50)/2
        assert y == 75

        # 0% = 左上角
        x, y = custom_anchor_position((200, 200), (50, 50), 0, 0)
        assert x == 0
        assert y == 0

        # 100% = 右下角
        x, y = custom_anchor_position((200, 200), (50, 50), 100, 100)
        assert x == 150  # 200-50
        assert y == 150


# ==================== 运行入口 ====================

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
