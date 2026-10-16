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

import pytest


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
    """
    预设管理测试。

    预设目录现在指向 %APPDATA%，测试必须把它改到临时目录 ——
    否则会在用户真实的数据目录里增删文件。
    """

    @pytest.fixture(autouse=True)
    def _isolated_presets(self, tmp_path, monkeypatch):
        monkeypatch.setattr(PresetManager, "PRESETS_DIR", str(tmp_path / "presets"))
        monkeypatch.setattr(PresetManager, "LEGACY_PRESETS_DIR", str(tmp_path / "legacy"))
        monkeypatch.setattr(PresetManager, "_ready", False)
        yield

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

    def test_saving_creates_directory(self):
        """首次保存要能自己建目录（全新安装的用户目录里没有 presets/）"""
        assert not os.path.isdir(PresetManager.PRESETS_DIR)

        PresetManager.save_preset("first", WatermarkConfig(text="x"))

        assert os.path.isdir(PresetManager.PRESETS_DIR)

    def test_legacy_presets_are_migrated(self):
        """旧版存在项目 presets/ 的预设要复制到用户目录"""
        legacy = PresetManager.LEGACY_PRESETS_DIR
        os.makedirs(legacy, exist_ok=True)
        WatermarkConfig(text="旧预设", opacity=70).save_to_json(
            os.path.join(legacy, "legacy.json")
        )

        names = PresetManager.list_presets()

        assert "legacy" in names
        assert os.path.isfile(os.path.join(PresetManager.PRESETS_DIR, "legacy.json"))
        # 是复制不是移动：旧目录保持原样，用户随时能退回旧版本
        assert os.path.isfile(os.path.join(legacy, "legacy.json"))

        cfg, _ = PresetManager.load_preset("legacy")
        assert cfg.text == "旧预设"

    def test_migration_ignores_non_json(self):
        legacy = PresetManager.LEGACY_PRESETS_DIR
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, "notes.txt"), "w") as f:
            f.write("not a preset")

        PresetManager.list_presets()

        assert not os.path.exists(
            os.path.join(PresetManager.PRESETS_DIR, "notes.txt")
        )

    def test_migration_does_not_overwrite_existing(self):
        """同名时以用户目录里的那份为准（它可能更新）"""
        PresetManager.save_preset("dup", WatermarkConfig(text="新版"))

        legacy = PresetManager.LEGACY_PRESETS_DIR
        os.makedirs(legacy, exist_ok=True)
        WatermarkConfig(text="旧版").save_to_json(os.path.join(legacy, "dup.json"))

        PresetManager._ready = False        # 允许再迁一次
        PresetManager.list_presets()

        cfg, _ = PresetManager.load_preset("dup")
        assert cfg.text == "新版"


class TestPresetStorageLocation:
    """
    预设的默认存放位置（不受上面 fixture 影响，拿到的是真实类属性）。

    这是本项修复的核心：装到 Program Files 时安装目录只读，
    预设若写在 _internal/ 下，用户一保存就失败。
    """

    def test_default_dir_is_under_user_appdata(self):
        import src.preset_manager as pm

        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        assert pm.PresetManager.PRESETS_DIR == os.path.join(
            appdata, "WatermarkTool", "presets"
        )

    def test_legacy_dir_is_project_local_and_distinct(self):
        import src.preset_manager as pm

        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(pm.__file__))
        )
        legacy = pm.PresetManager.LEGACY_PRESETS_DIR

        assert legacy.startswith(project_root)
        assert legacy != pm.PresetManager.PRESETS_DIR


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


class TestOpenImageModes:
    """
    读取阶段的静默损失，以及失败时必须能定位到文件。

    1. LA / 带 transparency 的 P：模式名里不带 A，但确实有透明通道。
       以前一律 convert("RGB")，透明区域变成实心 —— 半透明 Logo 导出来
       是一块黑底方块，而且没有任何提示。
    2. I;16 等高/浮点模式：合成管线是 8 位/通道，位深必然压缩，
       至少要留下一条日志。
    3. 损坏 / 截断 / 不存在的文件：Pillow 的原始报错不带文件名，
       失败清单里只剩一个陌生的异常名，无从定位。
    """

    def test_la_keeps_alpha(self, tmp_path):
        p = os.path.join(str(tmp_path), "la.png")
        Image.new("LA", (4, 4), (200, 0)).save(p)

        out = safe_open_image(p)

        assert out.mode == "RGBA"
        assert out.getpixel((0, 0))[3] == 0

    def test_palette_with_transparency_becomes_rgba(self, tmp_path):
        img = Image.new("P", (4, 4), 0)
        img.putpalette([0, 0, 0, 255, 255, 255] + [0, 0, 0] * 254)
        img.info["transparency"] = 0
        p = os.path.join(str(tmp_path), "pal.png")
        img.save(p)

        out = safe_open_image(p)

        assert out.mode == "RGBA"
        assert out.getpixel((0, 0))[3] == 0, "透明调色板索引必须保留为 alpha=0"

    def test_palette_without_transparency_stays_rgb(self, tmp_path):
        img = Image.new("P", (4, 4), 1)
        img.putpalette([0, 0, 0, 255, 255, 255] + [0, 0, 0] * 254)
        p = os.path.join(str(tmp_path), "pal2.png")
        img.save(p)

        assert safe_open_image(p).mode == "RGB"

    def test_gray_stays_gray(self, tmp_path):
        """L 已能进管线，不该被无谓地转成三通道"""
        p = os.path.join(str(tmp_path), "l.png")
        Image.new("L", (4, 4), 128).save(p)

        assert safe_open_image(p).mode == "L"

    def test_16bit_gray_is_converted(self, tmp_path):
        p = os.path.join(str(tmp_path), "g16.tiff")
        Image.new("I;16", (4, 4), 60000).save(p)

        # 管线是 8 位/通道，位深无法保留；这里钉住"转换确实发生了"，
        # 用户可见的告警由 watermark_engine 写进日志
        assert safe_open_image(p).mode == "RGB"

    def test_truncated_file_error_contains_name(self, tmp_path):
        full = os.path.join(str(tmp_path), "full.png")
        Image.new("RGB", (200, 200), (255, 0, 0)).save(full)
        data = open(full, "rb").read()

        p = os.path.join(str(tmp_path), "half.png")
        with open(p, "wb") as f:
            f.write(data[: len(data) // 2])

        with pytest.raises(ValueError) as exc:
            safe_open_image(p)

        assert "half.png" in str(exc.value)

    def test_unreadable_header_error_contains_name(self, tmp_path):
        p = os.path.join(str(tmp_path), "broken.jpg")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + os.urandom(6000))

        with pytest.raises(ValueError) as exc:
            safe_open_image(p)

        assert "broken.jpg" in str(exc.value)

    def test_missing_file_error_contains_name(self, tmp_path):
        with pytest.raises(ValueError) as exc:
            safe_open_image(os.path.join(str(tmp_path), "nope.png"))

        assert "nope.png" in str(exc.value)


# ==================== 皮肤（只有一套浅色） ====================


class TestThemePalette:
    """
    皮肤只有一套浅色值 —— 把一条**设计约束**钉住。

    用户明确要求去掉深色版，而 CustomTkinter 只在收到 (浅, 深) 二元组时
    才做外观模式切换：只要有人往调色板里塞回二元组，界面就会出现
    "部分控件随系统深色变化、部分不变"的半深半浅状态，且不报任何错。
    所以这里直接对调色板与主题字典做结构断言，而不是等肉眼看出来。
    """

    def test_all_colors_are_single_values(self):
        """调色板里不允许出现二元组（深色版残留）"""
        from src.ui import theme

        assert theme.ALL_COLORS, "调色板是空的"
        for name, value in theme.ALL_COLORS.items():
            assert isinstance(value, str), f"{name} 不是单一色值：{value!r}"
            assert value.startswith("#") and len(value) == 7, f"{name}={value!r}"

    def test_ctk_theme_has_no_dual_mode_entries(self):
        """主题字典里同样不允许出现列表/元组值"""
        from src.ui import theme

        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}" if path else str(key))
            else:
                assert not isinstance(node, (list, tuple)), \
                    f"主题项 {path} 是二元组：{node!r}"

        walk(theme.build_ctk_theme())

    def test_theme_dict_covers_all_widget_keys(self):
        """键集合不能少 —— 少一个键，对应控件创建时就取不到默认色"""
        from src.ui import theme

        expected = {
            "CTk", "CTkToplevel", "CTkFrame", "CTkButton", "CTkLabel",
            "CTkEntry", "CTkCheckBox", "CTkSwitch", "CTkRadioButton",
            "CTkProgressBar", "CTkSlider", "CTkOptionMenu", "CTkComboBox",
            "CTkScrollbar", "CTkSegmentedButton", "CTkTextbox",
            "CTkScrollableFrame", "DropdownMenu", "CTkFont",
        }
        assert set(theme.build_ctk_theme()) == expected

    def test_install_theme_writes_file(self, tmp_path, monkeypatch):
        """install_theme 落盘并返回路径（AppWindow 启动时靠它装载皮肤）"""
        import json as _json

        from src.ui import theme

        target = tmp_path / "sub" / "ctk_theme.json"
        monkeypatch.setattr(theme, "theme_file_path", lambda: target)

        result = theme.install_theme()
        assert result == str(target)
        data = _json.loads(target.read_text(encoding="utf-8"))
        # 抽查一项：默认按钮底色必须来自调色板，而不是内置主题
        assert data["CTkButton"]["fg_color"] == theme.NEUTRAL_BTN

    def test_install_theme_falls_back_on_failure(self, monkeypatch):
        """皮肤坏不能拦启动：任何异常都回落到内置主题"""
        from src.ui import theme

        def _boom():
            raise OSError("用户目录不可写")

        monkeypatch.setattr(theme, "theme_file_path", _boom)
        assert theme.install_theme() == "blue"

    def test_titlebar_colorref_byte_order(self):
        """
        Windows COLORREF 是 0x00BBGGRR，与网页的 #RRGGBB 字节序相反。

        写反了不会报错，只会让标题栏颜色偏蓝 —— 属于"看起来只是不好看"、
        极难在代码里发现的那类错误，所以钉一个非对称色值做判据。
        """
        from src.ui.app_window import _colorref

        assert _colorref("#4F8A66") == 0x668A4F
        assert _colorref("#000000") == 0x000000
        assert _colorref("#FFFFFF") == 0xFFFFFF


# ==================== 水印几何（预览拖动与导出共用）====================


class TestWatermarkGeometry:
    """
    预览图上直接拖水印，靠的是引擎里这一套换算 —— 与真正渲染用的是同一组函数，
    所以"拖到哪就导出到哪"是结构性保证，而不是靠两处代码各自保持同步。
    """

    def test_clamp_keeps_sprite_inside(self):
        from src.watermark_engine import clamp_anchor

        assert clamp_anchor((100, 100), (30, 20), 90, 90) == (70, 80)
        assert clamp_anchor((100, 100), (30, 20), -5, -5) == (0, 0)

    def test_percent_round_trip(self):
        """
        百分比 → 像素 → 百分比必须回到原值。

        拖动就是把鼠标位移换算成百分比写进配置，再让引擎按百分比定位；
        这个来回若对不上，就会出现"松手后水印跳一下"。
        （边界处不适用：pct 代表中心点，中心贴边时精灵会被夹回来，见下一个用例。）
        """
        from src.watermark_engine import (
            custom_anchor_position, custom_percent_for_anchor,
        )

        base, sprite = (1600, 1000), (200, 80)
        for pct in ((20.0, 20.0), (50.0, 50.0), (80.0, 80.0), (33.47, 66.13)):
            x, y = custom_anchor_position(base, sprite, *pct)
            got = custom_percent_for_anchor(base, sprite, x, y)
            assert got == pytest.approx(pct, abs=0.05), f"{pct} → {got}"

    def test_percent_converges_at_edges(self):
        """往图外拖：数值收敛到"精灵贴边"，与导出时的夹取同一套规则"""
        from src.watermark_engine import custom_percent_for_anchor

        assert custom_percent_for_anchor((1000, 800), (200, 100), -999, -999) == (
            10.0, 6.25,
        )

    def test_boxes_empty_when_tiled(self):
        """平铺没有位置概念（从左上角按间距铺开），不该出现在可拖列表里"""
        from src.config import WatermarkConfig
        from src.watermark_engine import watermark_boxes

        assert watermark_boxes(WatermarkConfig(text="W", tile=True), (400, 300)) == []

    def test_boxes_empty_without_content(self):
        from src.config import WatermarkConfig
        from src.watermark_engine import watermark_boxes

        assert watermark_boxes(WatermarkConfig(text="   "), (400, 300)) == []

    def test_boxes_for_single_text(self):
        from src.config import WatermarkConfig
        from src.watermark_engine import watermark_boxes

        cfg = WatermarkConfig(text="W", tile=False, position="bottom-right", margin=20)
        boxes = watermark_boxes(cfg, (400, 300))
        assert len(boxes) == 1 and boxes[0]["kind"] == "text"
        x, y, w, h = boxes[0]["box"]
        assert x >= 0 and y >= 0 and x + w <= 400 and y + h <= 300

    def test_boxes_use_custom_percent(self):
        """自定义定位的水印，命中框中心应落在给定百分比上"""
        from src.config import WatermarkConfig
        from src.watermark_engine import watermark_boxes

        cfg = WatermarkConfig(
            text="W", tile=False, position_mode="custom",
            custom_x=25.5, custom_y=70.25,
        )
        x, y, w, h = watermark_boxes(cfg, (1000, 800))[0]["box"]
        assert (x + w / 2) / 1000 * 100 == pytest.approx(25.5, abs=0.2)
        assert (y + h / 2) / 800 * 100 == pytest.approx(70.25, abs=0.2)


class TestPercentFormatting:
    def test_format_percent(self):
        from src.config import format_percent

        assert format_percent(50) == "50"
        assert format_percent(50.0) == "50"
        assert format_percent(33.470000000000006) == "33.47"
        assert format_percent(0.5) == "0.5"
        assert format_percent("12.345") == "12.35"

    def test_custom_coords_accept_decimals(self):
        """
        拖动写回的是小数百分比，校验器不能把它取整。

        曾经规格表里 custom_x 标的是"整数"，3000px 宽的图上一步就是 30px，
        与"完全自定义位置"的目标不符。
        """
        from src.validators import TEXT_FIELD_SPECS, clamp_number

        value, corrected = clamp_number("33.47", TEXT_FIELD_SPECS["custom_x"])
        assert value == pytest.approx(33.47)
        assert corrected is False

        # 越界仍然要夹住
        assert clamp_number("150", TEXT_FIELD_SPECS["custom_x"])[0] == 100.0


class TestWindowGeometryClamp:
    """
    fit_window_geometry 的边界。

    窗口尺寸与位置是原样存盘、原样恢复的 —— 用户只要在最大化状态下关一次
    窗口，存下来的就是"屏幕那么大"的尺寸；换显示器或改缩放同理。
    实测：存成 2564×1570 而工作区只有 2560×1528，下次打开右边缘与底部
    跑到屏幕外，标题栏按钮被裁掉一半。
    """

    def test_size_larger_than_work_area_is_clamped(self):
        from src.ui.app_window import fit_window_geometry

        assert fit_window_geometry(2564, 1570, 38, 38, 2560, 1528) == (
            2560, 1528, 0, 0
        )

    def test_normal_geometry_untouched(self):
        from src.ui.app_window import fit_window_geometry

        assert fit_window_geometry(1340, 880, 40, 30, 2560, 1528) == (
            1340, 880, 40, 30
        )

    def test_position_pulled_back_so_window_fits(self):
        """x 本身"不离谱"，但配上屏幕宽的 w 仍会把右边缘顶出去"""
        from src.ui.app_window import fit_window_geometry

        assert fit_window_geometry(2000, 900, 1500, 700, 2560, 1528)[2:] == (
            560, 628,
        )

    def test_min_size_floor(self):
        from src.ui.app_window import fit_window_geometry

        assert fit_window_geometry(10, 10, None, None, 2560, 1528)[:2] == (
            950, 620,
        )

    def test_none_position_stays_none(self):
        """没存过位置就不要凭空造坐标（留给系统居中）"""
        from src.ui.app_window import fit_window_geometry

        assert fit_window_geometry(1200, 800, None, None, 2560, 1528) == (
            1200, 800, None, None,
        )

    def test_zero_work_area_skips_size_clamp(self):
        """取不到屏幕尺寸（0）时不能把窗口夹成 0"""
        from src.ui.app_window import fit_window_geometry

        assert fit_window_geometry(1200, 800, 10, 10, 0, 0)[:2] == (1200, 800)

    def test_physical_to_tk_size(self):
        """
       物理像素尺寸 ÷ 缩放 = Tk 单位（geometry() 收的那种）。

        Tk 在 Windows 上两组 winfo 单位不同：winfo_width/height 是物理像素，
        winfo_x/y 与 geometry() 的偏移同为 Tk 单位。少了这一步，
        150% 缩放下窗口会大 1.5 倍，然后被 Tk 夹到整屏大小
        （用户看到的就是"窗口自己变成了全屏"，且再也回不到正常尺寸）。
        """
        from src.ui.app_window import physical_to_tk_size

        assert physical_to_tk_size(1725, 1170, 1.5) == (1150, 780)
        assert physical_to_tk_size(1425, 930, 1.5) == (950, 620)

    def test_physical_to_tk_size_handles_bad_scale(self):
        """缩放系数拿不到（0/None）时按 1:1 处理，不能除零"""
        from src.ui.app_window import physical_to_tk_size

        assert physical_to_tk_size(1200, 800, 0) == (1200, 800)
        assert physical_to_tk_size(1200, 800, None) == (1200, 800)


# ==================== 运行入口 ====================

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
