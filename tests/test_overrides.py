"""
参数作用域（三层参数）单元测试

运行: python -m pytest tests/test_overrides.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import opacity_to_alpha
from src.overrides import (
    SCOPE_ALL,
    SCOPE_FILE,
    SCOPE_PAGE,
    ScopeStore,
    strip_output_fields,
)


A = r"C:\pics\a.jpg"
B = r"C:\pics\b.jpg"
C = r"C:\pics\c.pdf"


# ==================== 字段过滤 ====================

class TestStripOutputFields:
    def test_removes_output_keys(self):
        data = {
            "text": "内部资料",
            "output_dir": r"C:\out",
            "output_format": "pdf",
            "jpeg_quality": 90,
            "filename_template": "{original}_watermarked",
            "overwrite_original": False,
            "version": 1,
        }
        assert strip_output_fields(data) == {"text": "内部资料"}

    def test_keeps_watermark_keys(self):
        data = {"text": "x", "opacity": 80, "color_rgba": (255, 0, 0, 255)}
        assert strip_output_fields(data) == data

    def test_handles_empty(self):
        assert strip_output_fields({}) == {}
        assert strip_output_fields(None) == {}


# ==================== 三层解析 ====================

class TestResolve:
    def test_defaults_when_empty(self):
        """空存储解析出的是 WatermarkConfig 默认值"""
        cfg = ScopeStore().config_for(B)
        assert cfg.text == ""
        assert cfg.opacity == 50

    def test_base_layer_applies_to_every_file(self):
        store = ScopeStore()
        store.update_base({"text": "全局", "opacity": 60})
        for path in (A, B, C):
            cfg = store.config_for(path)
            assert cfg.text == "全局"
            assert cfg.opacity == 60

    def test_file_layer_overrides_base(self):
        store = ScopeStore()
        store.update_base({"text": "全局", "opacity": 60})
        store.update_file(B, {"text": "样片"})
        # B 的文件层只改了 text，opacity 仍从全局继承
        assert store.config_for(B).text == "样片"
        assert store.config_for(B).opacity == 60
        # 其他文件不受影响
        assert store.config_for(A).text == "全局"

    def test_page_layer_overrides_file_and_base(self):
        store = ScopeStore()
        store.update_base({"text": "全局", "opacity": 60})
        store.update_file(C, {"text": "文件层"})
        store.update_page(C, 2, {"text": "第3页", "opacity": 20})

        assert store.config_for(C, 2).text == "第3页"
        assert store.config_for(C, 2).opacity == 20
        # 同文件其他页仍然走文件层
        assert store.config_for(C, 0).text == "文件层"
        assert store.config_for(C, 0).opacity == 60

    def test_patch_does_not_freeze_inherited_fields(self):
        """文件层只记改过的字段：之后改全局默认，未单独设置的字段要跟着变"""
        store = ScopeStore()
        store.update_base({"text": "全局", "opacity": 60})
        store.update_file(B, {"text": "样片"})
        store.update_base({"opacity": 30})
        assert store.config_for(B).opacity == 30, "文件层不该把 opacity 冻在 60"

    def test_output_fields_never_enter_layers(self):
        store = ScopeStore()
        store.update_file(B, {"text": "x", "output_format": "pdf", "jpeg_quality": 70})
        store.update_page(C, 1, {"text": "y", "output_dir": r"C:\somewhere"})
        assert "output_format" not in store.base
        assert "output_format" not in store.resolve(B)
        assert "output_dir" not in store.resolve(C, 1)
        assert store.config_for(B).output_format == "auto"  # 回落到默认

    def test_empty_patch_is_ignored(self):
        """只含输出字段的补丁不该凭空造出一个专属层"""
        store = ScopeStore()
        store.update_file(B, {"output_format": "pdf"})
        store.update_page(C, 0, {"jpeg_quality": 80})
        assert not store.has_file(B)
        assert not store.has_page(C, 0)
        assert store.override_paths() == []


# ==================== 差异化写入（面板提交走这条路） ====================

class TestDiffBasedSet:
    """
    面板每次提交的都是「完整参数」，不是用户手写的小补丁。
    这一组用例盯的是：完整提交同样只能存下差异，不能把继承冻成快照。
    """

    def test_set_file_stores_only_differences(self):
        store = ScopeStore()
        base = {"text": "全局", "opacity": 60, "margin": 20}
        store.set_base(base)

        store.set_file(B, dict(base, text="样片"))  # 完整提交，只改了 text

        assert store.has_file(B)
        stored = store.to_dict()["files"][B]
        assert stored == {"text": "样片"}, f"应只存差异字段，实际存了 {stored}"

    def test_set_file_keeps_inheritance_after_base_change(self):
        """提交完整参数后，之后再改全局默认，没动过的字段必须跟着变"""
        store = ScopeStore()
        store.set_base({"text": "全局", "opacity": 60})
        store.set_file(B, {"text": "样片", "opacity": 60})

        store.update_base({"opacity": 30})  # 之后改全局默认

        assert store.config_for(B).opacity == 30, "完整提交把 opacity 冻在 60 了"
        assert store.config_for(B).text == "样片"

    def test_set_file_drops_layer_when_nothing_differs(self):
        """把文件层改回与全局一致：专属层应消失，不留一个空壳"""
        store = ScopeStore()
        store.set_base({"text": "全局", "opacity": 60})
        store.set_file(B, {"text": "样片", "opacity": 60})
        assert store.has_file(B)

        store.set_file(B, {"text": "全局", "opacity": 60})
        assert not store.has_file(B), "没有差异就不该还挂着专属层"

    def test_set_page_diffs_against_file_layer(self):
        """页层的差异相对「全局 + 本文件」算，父层没动过的继续继承"""
        store = ScopeStore()
        store.set_base({"text": "全局", "opacity": 60, "margin": 20})
        store.set_file(C, {"text": "文件层", "opacity": 60, "margin": 20})

        # 这一页只改 margin，其余与父层相同
        store.set_page(C, 1, {"text": "文件层", "opacity": 60, "margin": 44})

        entry = [p for p in store.to_dict()["pages"] if p["page"] == 1][0]
        assert entry["data"] == {"margin": 44}, entry

        # 父层之后变化：页层没动过的字段跟着变，动过的保持
        store.update_base({"opacity": 15})
        assert store.config_for(C, 1).opacity == 15
        assert store.config_for(C, 1).margin == 44
        assert store.config_for(C, 1).text == "文件层"

    def test_list_vs_tuple_is_not_treated_as_change(self):
        """JSON 往返会把 tuple 变 list，不该被误判成改动而多存一条补丁"""
        from src.config import WatermarkConfig

        store = ScopeStore()
        cfg = WatermarkConfig()
        store.set_base(cfg.to_dict())  # color_rgba 是 tuple

        data = cfg.to_dict()
        data["color_rgba"] = list(cfg.color_rgba)  # 模拟 JSON 往返后的 list
        store.set_file(B, data)

        assert not store.has_file(B), "只有 list/tuple 的类型差异，不该产生专属层"


# ==================== 查询与清除 ====================

class TestQueryAndClear:
    def test_has_file_and_page(self):
        store = ScopeStore()
        store.update_file(B, {"text": "x"})
        store.update_page(C, 3, {"text": "y"})
        assert store.has_file(B) is True
        assert store.has_file(A) is False
        assert store.has_page(C, 3) is True
        assert store.has_page(C, 2) is False
        assert store.has_override(C) is True
        assert store.has_override(A) is False

    def test_page_numbers_sorted(self):
        store = ScopeStore()
        for p in (4, 0, 2):
            store.update_page(C, p, {"text": str(p)})
        store.update_page(B, 1, {"text": "b"})
        assert store.page_numbers(C) == [0, 2, 4]
        assert store.page_numbers(B) == [1]
        assert store.page_numbers(A) == []

    def test_clear_page_keeps_file_layer(self):
        store = ScopeStore()
        store.update_file(C, {"text": "文件层"})
        store.update_page(C, 1, {"text": "第2页"})
        assert store.clear_page(C, 1) is True
        assert store.has_page(C, 1) is False
        assert store.config_for(C, 1).text == "文件层"

    def test_clear_file_also_removes_its_pages(self):
        store = ScopeStore()
        store.update_file(C, {"text": "文件层"})
        store.update_page(C, 1, {"text": "第2页"})
        store.update_page(C, 3, {"text": "第4页"})
        assert store.clear_file(C) is True
        assert store.page_numbers(C) == []
        assert store.has_file(C) is False
        assert store.clear_file(C) is False

    def test_marker_text(self):
        store = ScopeStore()
        assert store.marker_text(A) == ""
        store.update_file(A, {"text": "x"})
        assert store.marker_text(A) == "专属"
        store.update_page(A, 0, {"text": "y"})
        assert store.marker_text(A) == "专属 第1页"
        store.update_page(A, 3, {"text": "z"})
        assert store.marker_text(A) == "专属 2页"

    def test_deepest_scope_for(self):
        store = ScopeStore()
        assert store.deepest_scope_for(A) is None
        store.update_file(A, {"text": "x"})
        assert store.deepest_scope_for(A) == SCOPE_FILE
        store.update_page(A, 5, {"text": "y"})
        assert store.deepest_scope_for(A, 5) == SCOPE_PAGE
        assert store.deepest_scope_for(A, 0) == SCOPE_FILE
        assert store.deepest_scope_for(None) is None


# ==================== 清理与持久化 ====================

class TestPrune:
    def test_prune_drops_files_out_of_list(self):
        store = ScopeStore()
        store.update_file(A, {"text": "a"})
        store.update_file(B, {"text": "b"})
        store.update_page(B, 1, {"text": "b1"})
        store.update_page(C, 1, {"text": "c1"})

        # 列表里只剩 A 和 C：B 的文件层与页层都应被丢弃
        assert store.prune([A, C]) == 2
        assert store.has_file(B) is False
        assert store.page_numbers(B) == []
        assert store.has_file(A) is True
        assert store.page_numbers(C) == [1]

    def test_prune_keeps_pages_of_kept_files(self):
        store = ScopeStore()
        store.update_page(C, 1, {"text": "c"})
        store.update_page(B, 1, {"text": "b"})
        assert store.prune([C]) == 1
        assert store.page_numbers(C) == [1]
        assert store.page_numbers(B) == []


class TestPersistence:
    def test_round_trip(self):
        store = ScopeStore()
        # 历史写法：只用 alpha 表达不透明度。入口处应被翻译成 opacity=80
        store.update_base({"text": "全局", "color_rgba": (255, 0, 0, 204)})
        store.update_file(B, {"text": "样片"})
        store.update_page(C, 4, {"text": "第5页", "opacity": 25})

        restored = ScopeStore.from_dict(store.to_dict())

        assert restored.base["text"] == "全局"
        assert restored.base["opacity"] == 80   # alpha 204 → 80%，只此一个来源
        assert restored.config_for(B).text == "样片"
        assert restored.config_for(C, 4).text == "第5页"
        assert restored.config_for(C, 4).opacity == 25
        # 颜色元组经 JSON 走一圈后要能还原成 tuple；
        # 本页只覆盖了不透明度，RGB 沿用全局，alpha 由 opacity 派生
        assert restored.config_for(C, 4).color_rgba == (255, 0, 0, opacity_to_alpha(25))

    def test_round_trip_through_json(self):
        import json
        store = ScopeStore()
        store.update_page(C, 2, {"text": "页", "color_rgba": (1, 2, 3, 128)})
        text = json.dumps(store.to_dict(), ensure_ascii=False)
        restored = ScopeStore.from_dict(json.loads(text))
        cfg = restored.config_for(C, 2)
        # tuple 经 JSON 走一圈后要还原成 tuple，
        # 否则 _same_value 会把「其实没改」的字段误判成改动，平白多存一条补丁
        assert isinstance(cfg.color_rgba, tuple)
        assert cfg.color_rgba == (1, 2, 3, 128)
        assert cfg.opacity == 50
        assert restored.page_numbers(C) == [2]

    def test_alpha_only_patch_migrates_to_opacity(self):
        """
        只给 alpha 的历史补丁必须翻译成 opacity。

        这是「不透明度只有一个权威源」的兼容面：内部认 opacity，
        但外部脚本与旧会话文件一直用 color_rgba 的第 4 位，
        入口不翻译的话它们会被默认值顶掉，半透明水印静默变淡。
        """
        assert strip_output_fields({"color_rgba": (10, 20, 30, 255)})["opacity"] == 100
        assert strip_output_fields({"color_rgba": (10, 20, 30, 128)})["opacity"] == 50
        # 显式给了 opacity 就以它为准，不再被 alpha 覆盖
        both = strip_output_fields({"color_rgba": (10, 20, 30, 255), "opacity": 30})
        assert both["opacity"] == 30

        store = ScopeStore()
        store.update_base({"text": "只给 alpha", "color_rgba": (255, 0, 0, 128)})
        assert store.base["opacity"] == 50
        assert store.config_for().color_rgba == (255, 0, 0, 128)

    def test_from_dict_tolerates_garbage(self):
        assert ScopeStore.from_dict(None).override_paths() == []
        assert ScopeStore.from_dict({"base": "oops", "files": 5, "pages": "x"}).base == {}

        store = ScopeStore.from_dict({
            "files": {"p1": "not-a-dict"},
            "pages": [
                "not-a-dict",
                {"path": "p2", "page": "abc", "data": {"text": "x"}},  # 页码非法
                {"path": "p3", "page": 1, "data": {"text": "ok"}},
            ],
        })
        assert store.has_file("p1") is False
        assert store.page_numbers("p2") == []
        assert store.config_for("p3", 1).text == "ok"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
