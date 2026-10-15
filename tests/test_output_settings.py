"""
输出设置单元测试 —— 文件名模板 / 同名冲突策略 / 覆盖原图 / 多目录输出 / 原子写出

为什么单独一个文件：
    这些以前是"文档写了、代码没实现"的死配置：
      - filename_template 有参数但没人传，README 却宣称支持变量
      - overwrite_original 只有字段定义，没有任何执行逻辑
      - 多目录 + 输出目录留空时统一落到桌面，与界面提示不符
    现在它们都有了真实行为，就必须有测试把它们钉住 ——
    否则下一次重构又会悄悄退回"文档写着一套、程序是另一套"。

运行: python -m pytest tests/test_output_settings.py -v
"""

import os
import sys

import pytest


from src.batch_planner import (
    BatchPlan,
    FileTask,
    TaskCancelled,
    execute_task,
    plan_output_paths,
)
from src.config import (
    CONFLICT_OVERWRITE,
    CONFLICT_RENAME,
    CONFLICT_SKIP,
    DEFAULT_FILENAME_TEMPLATE,
    DEFAULT_OPACITY,
    WatermarkConfig,
    opacity_to_alpha,
)
from src.file_manager import (
    atomic_output,
    generate_output_path,
    make_temp_output_path,
    resolve_output_target,
)

from PIL import Image


# ==================== 文件名模板 ====================

class TestFilenameTemplate:
    def test_default_template_appends_suffix(self, tmp_path):
        src = str(tmp_path / "photo.jpg")
        out = generate_output_path(src, str(tmp_path), output_format="jpeg")
        assert os.path.basename(out) == "photo_watermarked.jpg"

    def test_custom_template_is_actually_used(self, tmp_path):
        src = str(tmp_path / "photo.jpg")
        out = generate_output_path(
            src, str(tmp_path),
            filename_template="wm_{original}",
            output_format="jpeg",
        )
        assert os.path.basename(out) == "wm_photo.jpg"

    def test_date_time_seq_variables(self, tmp_path):
        src = str(tmp_path / "photo.jpg")
        out = generate_output_path(
            src, str(tmp_path),
            filename_template="{date}_{time}_{seq}_{original}",
            output_format="jpeg",
            seq_counter=7,
        )
        name = os.path.basename(out)
        assert name.startswith("7_") is False  # 顺序：date_time_seq_original
        assert name.endswith("_photo.jpg")
        assert "_7_" in name

    def test_blank_template_falls_back_to_source_name(self, tmp_path):
        """空模板会生成"只有扩展名"的文件，必须回落到源文件名"""
        src = str(tmp_path / "photo.jpg")
        out = generate_output_path(
            src, str(tmp_path), filename_template="   ", output_format="jpeg"
        )
        assert os.path.basename(out) == "photo.jpg"


# ==================== 同名冲突策略 ====================

class TestConflictPolicy:
    def _touch(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")

    def test_rename_never_overwrites(self, tmp_path):
        src = str(tmp_path / "a.jpg")
        existing = tmp_path / "a_watermarked.jpg"
        self._touch(str(existing))

        out = generate_output_path(
            src, str(tmp_path), output_format="jpeg", on_conflict=CONFLICT_RENAME
        )
        assert os.path.basename(out) == "a_watermarked_1.jpg"

    def test_overwrite_reuses_the_existing_name(self, tmp_path):
        src = str(tmp_path / "a.jpg")
        existing = tmp_path / "a_watermarked.jpg"
        self._touch(str(existing))

        out = generate_output_path(
            src, str(tmp_path), output_format="jpeg", on_conflict=CONFLICT_OVERWRITE
        )
        assert out == str(existing)

    def test_skip_returns_none(self, tmp_path):
        src = str(tmp_path / "a.jpg")
        self._touch(str(tmp_path / "a_watermarked.jpg"))

        out = generate_output_path(
            src, str(tmp_path), output_format="jpeg", on_conflict=CONFLICT_SKIP
        )
        assert out is None

    def test_overwrite_still_protects_earlier_results_in_same_batch(self, tmp_path):
        """
        覆盖模式下也不能自己覆盖自己。

        模板只含 {date} 时，同一批的两个文件会映射到同一个输出名 ——
        若直接覆盖，用户会发现"处理了 2 张只出来 1 张"。
        """
        a = str(tmp_path / "a.jpg")
        b = str(tmp_path / "b.jpg")
        used = set()

        p1 = generate_output_path(
            a, str(tmp_path), filename_template="same",
            output_format="jpeg", existing_names=used, on_conflict=CONFLICT_OVERWRITE,
        )
        p2 = generate_output_path(
            b, str(tmp_path), filename_template="same",
            output_format="jpeg", existing_names=used, on_conflict=CONFLICT_OVERWRITE,
        )
        assert p1 != p2
        assert os.path.basename(p1) == "same.jpg"
        assert os.path.basename(p2) == "same_1.jpg"

    def test_skip_still_allows_fresh_names(self, tmp_path):
        src = str(tmp_path / "a.jpg")
        out = generate_output_path(
            src, str(tmp_path), output_format="jpeg", on_conflict=CONFLICT_SKIP
        )
        assert out is not None


# ==================== 覆盖原图 ====================

class TestOverwriteOriginal:
    def test_output_path_is_the_source_path(self, tmp_path):
        src = str(tmp_path / "photo.jpg")
        out = generate_output_path(
            src, str(tmp_path), output_format="jpeg", overwrite_original=True
        )
        assert out == src

    def test_overwrite_original_ignores_output_dir(self, tmp_path):
        src = str(tmp_path / "photo.jpg")
        out = generate_output_path(
            src, str(tmp_path / "out"), output_format="jpeg", overwrite_original=True
        )
        assert out == src


# ==================== 多目录输出策略 ====================

class TestOutputTarget:
    def test_explicit_directory_wins(self, tmp_path):
        target, note = resolve_output_target(
            [str(tmp_path / "a.jpg")], str(tmp_path), merge=False
        )
        assert target == str(tmp_path)
        assert note == ""

    def test_single_directory_uses_watermarked_output_subdir(self, tmp_path):
        src_a = str(tmp_path / "a.jpg")
        src_b = str(tmp_path / "b.jpg")
        target, note = resolve_output_target([src_a, src_b], "", merge=False)
        assert target == os.path.join(str(tmp_path), "watermarked_output")
        assert note

    def test_multi_directory_leaves_target_empty(self, tmp_path):
        """
        多目录 + 逐文件输出 → 目录留空，让每个文件落到自己的源目录下。
        这正是界面提示「留空 → 源目录\\watermarked_output」的语义。
        """
        d1 = tmp_path / "one"
        d2 = tmp_path / "two"
        d1.mkdir()
        d2.mkdir()
        target, note = resolve_output_target(
            [str(d1 / "a.jpg"), str(d2 / "b.jpg")], "", merge=False
        )
        assert target == ""
        assert "各自源目录" in note

        # 留空后由 generate_output_path 各自落位
        out_a = generate_output_path(str(d1 / "a.jpg"), "", output_format="jpeg")
        out_b = generate_output_path(str(d2 / "b.jpg"), "", output_format="jpeg")
        assert out_a.startswith(str(d1 / "watermarked_output"))
        assert out_b.startswith(str(d2 / "watermarked_output"))

    def test_multi_directory_merge_gets_a_single_home(self, tmp_path):
        """合并成单个 PDF 时必须有唯一落点，否则会没有输出目录"""
        d1 = tmp_path / "one"
        d2 = tmp_path / "two"
        d1.mkdir()
        d2.mkdir()
        target, note = resolve_output_target(
            [str(d1 / "a.jpg"), str(d2 / "b.jpg")], "", merge=True
        )
        assert target
        assert "合并" in note

    def test_missing_directory_string_is_treated_as_blank(self, tmp_path):
        target, _ = resolve_output_target(
            [str(tmp_path / "a.jpg")], str(tmp_path / "does_not_exist"), merge=False
        )
        assert target == os.path.join(str(tmp_path), "watermarked_output")


# ==================== 原子写出 ====================

class TestAtomicOutput:
    def test_success_replaces_target(self, tmp_path):
        final = str(tmp_path / "out.txt")
        with atomic_output(final) as tmp:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("done")
            assert tmp != final
        assert open(final, encoding="utf-8").read() == "done"

    def test_failure_leaves_no_file_at_all(self, tmp_path):
        """中途异常不能留下半成品 —— 那种"看着存在其实是坏的"最难排查"""
        final = str(tmp_path / "out.bin")

        with pytest.raises(RuntimeError):
            with atomic_output(final) as tmp:
                with open(tmp, "wb") as f:
                    f.write(b"partial")
                raise RuntimeError("boom")

        assert not os.path.exists(final)
        assert not os.path.exists(make_temp_output_path(final))

    def test_temp_path_keeps_extension(self, tmp_path):
        """临时文件必须保留扩展名，否则 Pillow 会按错误格式编码"""
        final = str(tmp_path / "photo.jpg")
        tmp = make_temp_output_path(final)
        assert tmp.endswith(".jpg")
        assert os.path.dirname(tmp) == str(tmp_path)

    def test_existing_target_survives_a_failed_write(self, tmp_path):
        """目标已存在时，失败必须保住旧文件（覆盖原图模式就靠这个）"""
        final = str(tmp_path / "photo.jpg")
        with open(final, "w", encoding="utf-8") as f:
            f.write("ORIGINAL")

        with pytest.raises(RuntimeError):
            with atomic_output(final) as tmp:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("BROKEN")
                raise RuntimeError("boom")

        assert open(final, encoding="utf-8").read() == "ORIGINAL"

    def test_execute_task_is_atomic_on_failure(self, tmp_path):
        """
        端到端：execute_task 失败时输出目录里不应出现任何文件
        （既没有最终文件，也没有 .part 残骸）
        """
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image at all")

        target = str(tmp_path / "out" / "broken_watermarked.jpg")
        task = FileTask(path=str(broken), config=WatermarkConfig(text="X"))

        with pytest.raises(Exception):
            execute_task(task, target)

        assert not os.path.exists(target)
        assert not os.path.exists(make_temp_output_path(target))
        leftovers = []
        for root, _dirs, names in os.walk(str(tmp_path)):
            leftovers += [n for n in names if ".part" in n]
        assert leftovers == []


# ==================== 任务级取消 ====================

class TestTaskCancellation:
    def test_execute_task_honours_cancel_before_start(self, tmp_path):
        src = tmp_path / "a.png"
        Image.new("RGB", (60, 40), (0, 0, 0)).save(str(src))
        target = str(tmp_path / "a_out.png")

        task = FileTask(path=str(src), config=WatermarkConfig(text="X"))
        with pytest.raises(TaskCancelled):
            execute_task(task, target, should_cancel=lambda: True)

        # 取消发生在解码之前：不该产出任何文件
        assert not os.path.exists(target)

    def test_execute_task_runs_when_not_cancelled(self, tmp_path):
        src = tmp_path / "a.png"
        Image.new("RGB", (60, 40), (0, 0, 0)).save(str(src))
        target = str(tmp_path / "a_out.png")

        task = FileTask(path=str(src), config=WatermarkConfig(text="X"))
        pages = execute_task(task, target, should_cancel=lambda: False)

        assert pages == 1
        assert os.path.exists(target)


# ==================== plan_output_paths ====================

class TestPlanOutputPaths:
    def _plan(self, paths):
        tasks = [
            FileTask(path=p, config=WatermarkConfig(text="X"), output_format="jpeg")
            for p in paths
        ]
        return BatchPlan(tasks=tasks, merge=False, out_format="jpeg")

    def test_returns_one_path_per_task(self, tmp_path):
        plan = self._plan([str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")])
        paths, merged = plan_output_paths(plan, str(tmp_path))
        assert merged == ""
        assert len(paths) == 2
        assert all(paths)

    def test_template_reaches_the_plan(self, tmp_path):
        plan = self._plan([str(tmp_path / "a.jpg")])
        paths, _ = plan_output_paths(
            plan, str(tmp_path), filename_template="OUT_{original}"
        )
        assert os.path.basename(paths[0]) == "OUT_a.jpg"

    def test_skip_policy_marks_task_as_none(self, tmp_path):
        existing = tmp_path / "a_watermarked.jpg"
        with open(str(existing), "w", encoding="utf-8") as f:
            f.write("x")
        plan = self._plan([str(tmp_path / "a.jpg")])
        paths, _ = plan_output_paths(plan, str(tmp_path), on_conflict=CONFLICT_SKIP)
        assert paths == [None]

    def test_merge_returns_a_single_pdf_path(self, tmp_path):
        plan = BatchPlan(tasks=[], merge=True, out_format="pdf")
        paths, merged = plan_output_paths(plan, str(tmp_path))
        assert paths == []
        assert merged.endswith(".pdf")
        assert merged.startswith(str(tmp_path))

    def test_overwrite_original_paths_are_the_sources(self, tmp_path):
        src = str(tmp_path / "a.jpg")
        plan = self._plan([src])
        paths, _ = plan_output_paths(plan, str(tmp_path), overwrite_original=True)
        assert paths == [src]


# ==================== 默认值单源 ====================

class TestConfigDefaultsSingleSource:
    def test_default_template_matches_config(self):
        assert WatermarkConfig().filename_template == DEFAULT_FILENAME_TEMPLATE

    def test_default_conflict_is_rename(self):
        """默认必须是"绝不覆盖已有文件"，覆盖得由用户显式选"""
        assert WatermarkConfig().on_conflict == CONFLICT_RENAME
        assert WatermarkConfig().resolved_on_conflict() == CONFLICT_RENAME

    def test_unknown_conflict_falls_back_to_rename(self):
        assert WatermarkConfig(on_conflict="whatever").resolved_on_conflict() == CONFLICT_RENAME


class TestOpacitySingleSource:
    """
    文字不透明度只能有一个权威源。

    旧实现面板写 opacity（滑块）、引擎读 color_rgba 的 alpha，两边各算一遍，
    于是出现「滑块显示 80%、实际按 128 渲染」。现在构造期就把两者对齐，
    并且兼容「只用 alpha 表达不透明度」的历史写法。
    """

    def test_default_is_consistent(self):
        cfg = WatermarkConfig()
        assert cfg.opacity == DEFAULT_OPACITY
        assert cfg.color_rgba[3] == opacity_to_alpha(DEFAULT_OPACITY)

    def test_opacity_drives_alpha(self):
        cfg = WatermarkConfig(opacity=80)
        assert cfg.color_rgba[3] == opacity_to_alpha(80)
        assert cfg.resolved_color_rgba() == cfg.color_rgba
        assert cfg.text_alpha() == cfg.color_rgba[3]

    def test_alpha_only_still_honoured(self):
        """老写法 WatermarkConfig(color_rgba=(r,g,b,255)) 必须仍然是完全不透明"""
        assert WatermarkConfig(color_rgba=(255, 255, 255, 255)).opacity == 100
        assert WatermarkConfig(color_rgba=(255, 255, 255, 128)).opacity == 50
        assert WatermarkConfig(color_rgba=(255, 255, 255, 0)).opacity == 0

    def test_explicit_opacity_wins_over_alpha(self):
        """两者都写时以 opacity 为准，不允许出现两个来源"""
        cfg = WatermarkConfig(color_rgba=(255, 0, 0, 255), opacity=20)
        assert cfg.opacity == 20
        assert cfg.color_rgba == (255, 0, 0, opacity_to_alpha(20))

    def test_rgb_only_means_opaque(self):
        cfg = WatermarkConfig(color_rgba=(10, 20, 30))
        assert cfg.opacity == 100
        assert cfg.color_rgba == (10, 20, 30, 255)

    def test_garbage_values_do_not_crash(self):
        cfg = WatermarkConfig(color_rgba=("x", None, 300), opacity="??")
        assert cfg.opacity == DEFAULT_OPACITY
        r, g, b, a = cfg.color_rgba
        assert 0 <= r <= 255 and 0 <= g <= 255 and b == 255
        assert a == opacity_to_alpha(DEFAULT_OPACITY)

    def test_out_of_range_opacity_is_clamped(self):
        assert WatermarkConfig(opacity=180).opacity == 100
        assert WatermarkConfig(opacity=-40).opacity == 0

    def test_legacy_preset_without_opacity_keeps_alpha(self):
        """旧预设文件里只有 color_rgba 的 alpha，不能被默认值顶掉"""
        cfg = WatermarkConfig.from_dict({"text": "旧预设", "color_rgba": [255, 255, 0, 204]})
        assert cfg.opacity == 80
        assert cfg.color_rgba == (255, 255, 0, 204)

    def test_panel_roundtrip_keeps_opacity(self):
        """面板提交 → 存储 → 回填，不透明度不能漂移"""
        source = WatermarkConfig(text="内部", opacity=37)
        back = WatermarkConfig.from_dict(source.to_dict())
        assert back.opacity == 37
        assert back.color_rgba == source.color_rgba
