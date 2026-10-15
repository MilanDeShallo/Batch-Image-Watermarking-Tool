"""
三层作用域 → 实际输出  的端到端测试

覆盖链路：
    ScopeStore 存补丁 → plan_batch 解析 → execute_task / build_merged_pdf 写出
    → 把结果读回来数像素，验证「该用哪套参数」真的落到了文件上

这一层专门盯「胶水」：单元测试各自的模块都对，不代表串起来是对的。
所有断言都落在输出文件的实际内容上，不看中间对象。

运行: python -m pytest tests/test_batch_pipeline.py -v
"""

import os
import sys
import tempfile

import pytest


from PIL import Image, ImageChops
from reportlab.pdfgen import canvas as rl_canvas

from src.batch_planner import (
    TaskCancelled,
    execute_task,
    normalize_format,
    plan_batch,
)
from src.config import WatermarkConfig
from src.overrides import ScopeStore
from src.pdf_engine import build_merged_pdf, get_pdf_page_count, render_pdf_page


# ==================== 素材与工具 ====================

RED = (255, 0, 0, 255)
BLUE = (0, 0, 255, 255)
GREEN = (0, 255, 0, 255)


def _base_cfg() -> WatermarkConfig:
    """全局默认：红色「全局」"""
    return WatermarkConfig(
        watermark_type="text",
        text="全局",
        font_ratio=0.12,
        color_rgba=RED,
        opacity=100,
        position="bottom-right",
        margin=20,
    )


def _make_image(path: str, size=(800, 480), color=(0, 0, 0)):
    """纯色底图（默认黑底，方便数彩色水印像素）"""
    Image.new("RGB", size, color).save(path)


def _make_pdf(path: str, pages: int = 2, size=(595, 842), fill=(1, 1, 1)):
    c = rl_canvas.Canvas(path, pagesize=size)
    for _ in range(pages):
        c.setFillColorRGB(*fill)
        c.rect(0, 0, size[0], size[1], stroke=0, fill=1)
        c.showPage()
    c.save()


def _count(img: Image.Image, channel: str) -> int:
    """统计「通道占优」像素数：指定通道 >200 且另两个通道 <100"""
    px = img.convert("RGB").get_flattened_data()
    if channel == "red":
        return sum(1 for q in px if q[0] > 200 and q[1] < 100 and q[2] < 100)
    if channel == "green":
        return sum(1 for q in px if q[1] > 200 and q[0] < 100 and q[2] < 100)
    return sum(1 for q in px if q[2] > 200 and q[0] < 100 and q[1] < 100)


def _max_diff(path_a: str, path_b: str, page: int = 0) -> int:
    """渲染同一页并取最大通道差（0 表示视觉上完全没改）"""
    a = render_pdf_page(path_a, page_num=page, scale=1.0)
    b = render_pdf_page(path_b, page_num=page, scale=1.0)
    return ImageChops.difference(a, b).convert("L").getextrema()[1]


def _load(path: str) -> Image.Image:
    return Image.open(path).convert("RGB")


def _scopes_for(a: str, b: str, c: str) -> ScopeStore:
    """
    复刻用户举的例子：

    - 全局默认：红字「全局」
    - B.png 文件层：改成蓝字「仅B」
    - C.pdf 第 1 页：不加（text 置空）
    - C.pdf 第 2 页：改成绿字「仅C2」
    """
    store = ScopeStore()
    store.set_base(_base_cfg().to_dict())
    store.update_file(b, {"text": "仅B", "color_rgba": [0, 0, 255, 255]})
    store.update_page(c, 0, {"text": ""})
    store.update_page(c, 1, {"text": "仅C2", "color_rgba": [0, 255, 0, 255]})
    return store


class _Workspace:
    """一个临时的 A.png / B.png / C.pdf 工作区"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self.a = os.path.join(tmp, "A.png")
        self.b = os.path.join(tmp, "B.png")
        self.c = os.path.join(tmp, "C.pdf")
        self.out_dir = os.path.join(tmp, "out")
        os.makedirs(self.out_dir, exist_ok=True)

    @property
    def files(self):
        return [self.a, self.b, self.c]

    def build(self):
        _make_image(self.a)
        _make_image(self.b)
        _make_pdf(self.c, pages=2)
        return self


def _run_per_file(plan, output_dir: str) -> list:
    """按 plan 逐文件执行（与界面 _on_start_processing 的逐文件分支同一条路径）"""
    from src.file_manager import generate_output_path

    names: set = set()
    outs = []
    for i, task in enumerate(plan.tasks):
        out = generate_output_path(
            task.path, output_dir,
            output_format=task.output_format,
            seq_counter=i + 1,
            existing_names=names,
        )
        execute_task(task, out, jpeg_quality=90)
        outs.append(out)
    return outs


# ==================== 规划层 ====================

class TestPlanning:
    def test_format_aliases(self):
        assert normalize_format("自动") == "auto"
        assert normalize_format("PNG") == "png"
        assert normalize_format("JPEG") == "jpeg"
        assert normalize_format("JPG") == "jpeg"
        assert normalize_format("PDF") == "pdf"
        assert normalize_format("") == "auto"
        assert normalize_format("随便什么") == "auto"  # 未知值不崩，回落

    def test_merge_only_takes_effect_for_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)

            assert plan_batch(ws.files, store, "PDF", True).merge is True
            # 格式不是 PDF 时，勾了合并不生效
            assert plan_batch(ws.files, store, "PNG", True).merge is False
            assert plan_batch(ws.files, store, "自动", True).merge is False

    def test_pdf_source_forces_pdf_output(self):
        """PDF 源即使选了 PNG，也必须输出 PDF（否则文字可搜性丢失）"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PNG")

            assert plan.tasks[0].output_format == "png"
            assert plan.tasks[1].output_format == "png"
            assert plan.tasks[2].output_format == "pdf"

    def test_patch_inherits_untouched_fields(self):
        """B 只改了 text 和颜色，字号等应继续沿用全局默认（补丁而非快照）"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store)

            b_cfg = plan.tasks[1].config
            assert b_cfg.text == "仅B"
            assert b_cfg.font_ratio == _base_cfg().font_ratio
            assert b_cfg.margin == _base_cfg().margin

            # A 没有任何覆盖 → 与全局默认一致
            assert plan.tasks[0].config.text == "全局"

    def test_page_configs_only_list_overridden_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store)

            # 图片没有页层数据
            assert plan.tasks[0].page_configs == {}
            # PDF 只收录被改过的两页
            assert set(plan.tasks[2].page_configs.keys()) == {0, 1}
            assert plan.tasks[2].page_configs[0].text == ""
            assert plan.tasks[2].page_configs[1].text == "仅C2"

    def test_json_roundtrip_keeps_scope_semantics(self):
        """会话往返（to_dict → from_dict）后，解析结果必须完全一致"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)

            restored = ScopeStore.from_dict(
                __import__("json").loads(__import__("json").dumps(store.to_dict()))
            )
            a = plan_batch(ws.files, store)
            b = plan_batch(ws.files, restored)

            for x, y in zip(a.tasks, b.tasks):
                assert x.config.text == y.config.text
                assert x.config.color_rgba == y.config.color_rgba
                assert set(x.page_configs) == set(y.page_configs)


# ==================== 逐文件执行 ====================

class TestPerFileOutput:
    def test_each_file_uses_its_own_config(self):
        """A 用全局红字，B 用文件层蓝字，互不串味"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PNG")

            out_a, out_b, _ = _run_per_file(plan, ws.out_dir)

            assert out_a.lower().endswith(".png")
            img_a = _load(out_a)
            assert _count(img_a, "red") > 200, "A 应带全局红色水印"
            assert _count(img_a, "blue") == 0, "A 混入了 B 的蓝色水印"

            img_b = _load(out_b)
            assert _count(img_b, "blue") > 200, "B 应带文件层蓝色水印"
            assert _count(img_b, "red") == 0, "B 混入了全局红色水印"

    def test_page_override_only_affects_that_page(self):
        """C 第 1 页不加、第 2 页绿字：第 1 页必须与原件逐像素相同"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PNG")

            _, _, out_c = _run_per_file(plan, ws.out_dir)

            # 第 1 页：text 置空 → 完全没被改动
            assert _max_diff(ws.c, out_c, page=0) == 0, "第 1 页不该有水印"
            # 第 2 页：绿字水印
            assert _max_diff(ws.c, out_c, page=1) > 10, "第 2 页应有水印"
            p1 = render_pdf_page(out_c, page_num=1, scale=1.0)
            assert _count(p1, "green") > 100
            assert _count(p1, "red") == 0, "第 2 页混入了全局红色水印"

    def test_images_become_single_page_pdfs(self):
        """格式选 PDF、不勾合并 → 每个图片一个单页 PDF"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PDF", merge_pdf=False)

            assert plan.merge is False
            out_a, out_b, out_c = _run_per_file(plan, ws.out_dir)

            assert out_a.lower().endswith(".pdf")
            assert out_b.lower().endswith(".pdf")
            assert out_c.lower().endswith(".pdf")
            assert get_pdf_page_count(out_a) == 1
            assert get_pdf_page_count(out_b) == 1
            assert get_pdf_page_count(out_c) == 2  # PDF 源页数保持

            # 水印同样落到 PDF 里
            assert _count(render_pdf_page(out_a, 0, scale=1.0), "red") > 100
            assert _count(render_pdf_page(out_b, 0, scale=1.0), "blue") > 100

    def test_image_pdf_page_keeps_aspect_ratio(self):
        """图片转 PDF 后页面比例与原图一致，不留白边"""
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PDF")

            out_a, _, _ = _run_per_file(plan, ws.out_dir)
            page = PdfReader(out_a).pages[0]
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            assert abs(w / h - 800 / 480) < 0.01


# ==================== 合并执行 ====================

class TestMergedOutput:
    def test_merge_in_list_order_with_per_file_configs(self):
        """A + B + C(2页) → 4 页，页序与列表一致，各页参数各自生效"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PDF", merge_pdf=True)

            assert plan.merge is True
            assert plan.count == 3

            out = os.path.join(ws.out_dir, "merged.pdf")
            pages = build_merged_pdf(plan.merge_items(), out, jpeg_quality=90)

            assert pages == 4
            assert get_pdf_page_count(out) == 4

            p0 = render_pdf_page(out, page_num=0, scale=1.0)
            p1 = render_pdf_page(out, page_num=1, scale=1.0)
            p3 = render_pdf_page(out, page_num=3, scale=1.0)

            assert _count(p0, "red") > 100, "第 1 页（A）应是全局红字"
            assert _count(p0, "blue") == 0
            assert _count(p1, "blue") > 100, "第 2 页（B）应是文件层蓝字"
            assert _count(p1, "red") == 0
            assert _count(p3, "green") > 50, "第 4 页（C 第 2 页）应是绿字"

    def test_merged_page_with_empty_text_stays_clean(self):
        """合并时页层「不加」同样生效：C 第 1 页（合并后第 3 页）保持白底"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)
            plan = plan_batch(ws.files, store, "PDF", merge_pdf=True)

            out = os.path.join(ws.out_dir, "merged.pdf")
            build_merged_pdf(plan.merge_items(), out)

            p2 = render_pdf_page(out, page_num=2, scale=1.0).convert("RGB")
            # A4 白底页：不该出现任何彩色水印像素
            assert _count(p2, "red") == 0
            assert _count(p2, "green") == 0
            assert _count(p2, "blue") == 0

    def test_quality_setting_reaches_merged_images(self):
        """底部栏质量设置要能传到图片编码（体积随质量下降）"""
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Workspace(tmp).build()
            store = _scopes_for(ws.a, ws.b, ws.c)

            high = os.path.join(ws.out_dir, "q95.pdf")
            low = os.path.join(ws.out_dir, "q30.pdf")
            plan = plan_batch(ws.files, store, "PDF", merge_pdf=True)

            build_merged_pdf(plan.merge_items(), high, jpeg_quality=95)
            build_merged_pdf(plan.merge_items(), low, jpeg_quality=30)

            assert os.path.getsize(low) < os.path.getsize(high)


class TestCancellationPropagation:
    """
    取消必须真的传到底层引擎 —— 机制写好了却没接上线，表现就是"点了取消没反应"。

    背景：execute_task 的 PDF 分支曾经只传 4 个位置参数，
    should_cancel 没进 apply_pdf_watermark，于是 pdf_engine 里的逐页检查
    在批处理路径形同虚设：800 页的 PDF 会把整本跑完才响应取消。
    """

    def test_pdf_branch_forwards_cancel_to_engine(self, tmp_path):
        """入口放行 + 页间检查取消 → 抛 TaskCancelled，且不留任何半成品"""
        ws = _Workspace(str(tmp_path)).build()
        plan = plan_batch([ws.c], _scopes_for(ws.a, ws.b, ws.c))
        task = plan.tasks[0]
        out = os.path.join(ws.out_dir, "cancelled.pdf")

        calls = []

        def should_cancel():
            calls.append(1)
            # 第 1 次来自 execute_task 的入口检查（放行）；
            # 第 2 次来自 apply_pdf_watermark 的页间检查 —— 只有透传成功才会出现
            return len(calls) >= 2

        with pytest.raises(TaskCancelled):
            execute_task(task, out, should_cancel=should_cancel)

        assert len(calls) >= 2, (
            "should_cancel 没被透传到 apply_pdf_watermark："
            "逐页取消在批处理路径失效"
        )
        assert not os.path.exists(out), "取消后不该留下输出文件"
        leftovers = [n for n in os.listdir(ws.out_dir) if ".part" in n]
        assert leftovers == [], f"原子写出应清理临时文件，残留：{leftovers}"

    def test_cancel_at_entry_writes_nothing(self, tmp_path):
        """入口就取消：一次都不该往下走"""
        ws = _Workspace(str(tmp_path)).build()
        plan = plan_batch([ws.c], _scopes_for(ws.a, ws.b, ws.c))
        out = os.path.join(ws.out_dir, "never.pdf")

        with pytest.raises(TaskCancelled):
            execute_task(plan.tasks[0], out, should_cancel=lambda: True)

        assert not os.path.exists(out)

    def test_no_cancel_callback_still_completes(self, tmp_path):
        """不传 should_cancel 时行为不变（向后兼容）"""
        ws = _Workspace(str(tmp_path)).build()
        plan = plan_batch([ws.c], _scopes_for(ws.a, ws.b, ws.c))
        out = os.path.join(ws.out_dir, "done.pdf")

        pages = execute_task(plan.tasks[0], out)

        assert pages == 2
        assert os.path.exists(out)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
