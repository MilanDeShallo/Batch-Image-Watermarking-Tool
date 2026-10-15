"""
PDF 输出能力测试：逐页参数 / 图片转 PDF / 多输入合并

运行: python -m pytest tests/test_pdf_output.py -v
"""

import os
import sys
import tempfile


from PIL import Image, ImageChops, ImageStat
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas

from src.config import WatermarkConfig
from src.pdf_engine import (
    MergeCancelled,
    apply_pdf_watermark,
    build_merged_pdf,
    config_fingerprint,
    get_overlay_scale,
    get_pdf_page_count,
    image_page_size,
    image_to_pdf_bytes,
    image_file_to_pdf_bytes,
    overlay_reference_size,
    render_pdf_page,
    set_overlay_scale,
    watermark_pdf_bytes,
)


# ==================== 素材工具 ====================

def _make_pdf(path: str, pages: int = 3, page_size=(595, 842), fill=None):
    """创建测试 PDF；fill 为 (r, g, b) 时铺满整页底色"""
    c = rl_canvas.Canvas(path, pagesize=page_size)
    for _ in range(pages):
        if fill:
            c.setFillColorRGB(*fill)
            c.rect(0, 0, page_size[0], page_size[1], stroke=0, fill=1)
        c.showPage()
    c.save()


def _make_image(path: str, size=(960, 480), color=(0, 0, 0)):
    Image.new("RGB", size, color).save(path)


def _mean(path: str, page: int = 0, scale: float = 1.0) -> float:
    """渲染指定页并返回平均亮度（0-255）"""
    img = render_pdf_page(path, page_num=page, scale=scale).convert("L")
    return ImageStat.Stat(img).mean[0]


def _max_diff(path_a: str, path_b: str, page: int = 0) -> int:
    a = render_pdf_page(path_a, page_num=page, scale=1.0)
    b = render_pdf_page(path_b, page_num=page, scale=1.0)
    return ImageChops.difference(a, b).convert("L").getextrema()[1]


def _page_sizes(path: str) -> list[tuple[float, float]]:
    reader = PdfReader(path)
    return [
        (float(p.mediabox.width), float(p.mediabox.height))
        for p in reader.pages
    ]


# ==================== 配置指纹 ====================

class TestConfigFingerprint:
    def test_same_config_same_fingerprint(self):
        a = WatermarkConfig(text="内部", opacity=50, color_rgba=(255, 0, 0, 128))
        b = WatermarkConfig(text="内部", opacity=50, color_rgba=(255, 0, 0, 128))
        assert config_fingerprint(a) == config_fingerprint(b)

    def test_different_config_different_fingerprint(self):
        a = WatermarkConfig(text="内部")
        b = WatermarkConfig(text="样片")
        assert config_fingerprint(a) != config_fingerprint(b)

    def test_list_vs_tuple_color_is_stable(self):
        """JSON 往返后 color_rgba 是 list，指纹必须与 tuple 一致"""
        a = WatermarkConfig(color_rgba=(255, 0, 0, 128))
        b = WatermarkConfig.from_dict({**a.to_dict(), "color_rgba": [255, 0, 0, 128]})
        assert config_fingerprint(a) == config_fingerprint(b)


# ==================== 逐页参数 ====================

class TestPerPageConfigs:
    def test_only_targeted_page_gets_watermark(self):
        """page_configs 指定第 2 页：只有该页变化，第 1 页保持原样"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            dst = os.path.join(tmp, "out.pdf")
            _make_pdf(src, pages=2, fill=(1, 1, 1))  # 白底

            base = WatermarkConfig(text="默认", color_rgba=(255, 255, 255, 255))
            page_cfgs = {
                1: WatermarkConfig(
                    text="第二页专属", font_ratio=0.15,
                    color_rgba=(255, 0, 0, 255), opacity=100,
                )
            }
            pages = apply_pdf_watermark(src, base, dst, page_configs=page_cfgs)
            assert pages == 2

            # 第 2 页（索引 1）有红色水印 → 与原始渲染不同
            assert _max_diff(src, dst, page=1) > 10
            # 第 1 页只用了「白字白底」的默认水印 → 视觉上应完全无变化。
            # 这一条同时是透明覆盖层的回归防线：覆盖层透明处的 RGB 若没被填成
            # 墨迹色（仍是 (0,0,0)），阅读器重采样会在笔画边缘留下灰边，
            # 这里就会量出非 0 差异
            assert _max_diff(src, dst, page=0) == 0, "未指定专属参数的页面不该被改动"

    def test_page_config_overrides_whole_document(self):
        """专属页参数与默认参数不同时，两页渲染结果需有差异"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            dst = os.path.join(tmp, "out.pdf")
            _make_pdf(src, pages=2, fill=(1, 1, 1))

            base = WatermarkConfig(text="A", font_ratio=0.15, color_rgba=(0, 0, 0, 255), opacity=100)
            page_cfgs = {0: WatermarkConfig(
                text="B", font_ratio=0.15, color_rgba=(255, 0, 0, 255), opacity=100,
            )}
            apply_pdf_watermark(src, base, dst, page_configs=page_cfgs)
            assert _max_diff(src, dst, page=0) > 10
            assert _max_diff(src, dst, page=1) > 10

    def test_overlay_cache_separates_different_configs(self):
        """同尺寸页面用不同配置：不能被缓存串味"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            dst = os.path.join(tmp, "both.pdf")
            _make_pdf(src, pages=2, fill=(1, 1, 1))

            cfg = WatermarkConfig(text="X", font_ratio=0.2, color_rgba=(255, 0, 0, 255), opacity=100)
            # 两页尺寸完全相同、只有配置不同 —— 专门验证覆盖层缓存键包含配置指纹
            page_cfgs = {0: cfg, 1: WatermarkConfig(text="Y", font_ratio=0.2,
                                                    color_rgba=(0, 0, 255, 255), opacity=100)}
            apply_pdf_watermark(src, WatermarkConfig(), dst, page_configs=page_cfgs)

            p0 = render_pdf_page(dst, page_num=0, scale=1.0).convert("RGB")
            p1 = render_pdf_page(dst, page_num=1, scale=1.0).convert("RGB")

            # 第 1 页应出现红色像素、且完全没有蓝色；第 2 页反之。
            # 断言用「通道占优」而不是"通道极差"，因为极差会被透明边缘干扰
            def count(p, target):
                if target == "red":
                    return sum(1 for q in p.get_flattened_data()
                               if q[0] > 200 and q[1] < 120 and q[2] < 120)
                return sum(1 for q in p.get_flattened_data()
                           if q[2] > 200 and q[0] < 120 and q[1] < 120)

            assert count(p0, "red") > 200, "第 1 页未出现红色水印"
            assert count(p0, "blue") == 0, "第 1 页混入了第 2 页的蓝色水印（缓存串味）"
            assert count(p1, "blue") > 200, "第 2 页未出现蓝色水印"
            assert count(p1, "red") == 0, "第 2 页混入了第 1 页的红色水印"

    def test_watermark_pdf_bytes_returns_bytes_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            _make_pdf(src, pages=3)
            data, pages = watermark_pdf_bytes(src, WatermarkConfig(text="X"))
            assert pages == 3
            assert data[:4] == b"%PDF"
            out = os.path.join(tmp, "out.pdf")
            with open(out, "wb") as f:
                f.write(data)
            assert get_pdf_page_count(out) == 3


# ==================== 图片 → PDF ====================

class TestImageToPdf:
    def test_page_size_matches_image_ratio(self):
        """页面尺寸 = 像素 × 72/96，与图片同比例"""
        assert image_page_size((960, 480)) == (720.0, 360.0)
        assert image_page_size((1, 1)) == (1.0, 1.0)  # 极小图不塌成 0

    def test_image_to_pdf_bytes_single_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Image.new("RGB", (960, 480), (10, 20, 30))
            data = image_to_pdf_bytes(img)
            out = os.path.join(tmp, "img.pdf")
            with open(out, "wb") as f:
                f.write(data)

            assert get_pdf_page_count(out) == 1
            w, h = _page_sizes(out)[0]
            assert abs(w - 720) < 0.5 and abs(h - 360) < 0.5
            # 图片内容被放进页面：渲染后颜色应接近原图
            mean = _mean(out)
            assert 5 < mean < 60

    def test_image_file_to_pdf_applies_watermark(self):
        """黑底图 + 白色平铺水印：转 PDF 后亮度显著上升"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "photo.png")
            _make_image(src, (800, 600), color=(0, 0, 0))

            plain = os.path.join(tmp, "plain.pdf")
            with open(plain, "wb") as f:
                f.write(image_file_to_pdf_bytes(src, None))

            cfg = WatermarkConfig(
                text="内部资料", font_ratio=0.08,
                color_rgba=(255, 255, 255, 255), opacity=100,
                tile=True, spacing_x=60, spacing_y=60,
            )
            marked = os.path.join(tmp, "marked.pdf")
            with open(marked, "wb") as f:
                f.write(image_file_to_pdf_bytes(src, cfg))

            assert _mean(marked) > _mean(plain) + 5, "水印未写入图片 PDF"
            assert _max_diff(plain, marked) > 50

    def test_alpha_image_keeps_transparency(self):
        """带 alpha 的图走 PNG 分支：不应报错且页数正确"""
        with tempfile.TemporaryDirectory() as tmp:
            img = Image.new("RGBA", (400, 300), (255, 0, 0, 128))
            out = os.path.join(tmp, "alpha.pdf")
            with open(out, "wb") as f:
                f.write(image_to_pdf_bytes(img))
            assert get_pdf_page_count(out) == 1

    def test_opaque_rgba_still_uses_jpeg(self):
        """
        整张图不透明的 RGBA 必须走 JPEG。

        apply_watermark 会把任意图统一转成 RGBA，所以「加过水印的图」几乎都是
        不透明的 RGBA。若按 mode 判断就会全部走 PNG —— 体积大得多，
        而且底部栏的质量设置被完全忽略。
        """
        opaque = Image.new("RGBA", (600, 400), (10, 20, 30, 255))
        assert b"DCTDecode" in image_to_pdf_bytes(opaque), \
            "不透明的 RGBA 走了 PNG 分支，质量设置会失效"

    def test_quality_affects_jpeg_payload(self):
        """质量参数真的落到编码上（用有纹理的图，纯色看不出差别）"""
        import random

        rnd = random.Random(20260918)
        noisy = Image.new("RGB", (400, 300))
        noisy.putdata([
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(400 * 300)
        ])
        low = image_to_pdf_bytes(noisy, jpeg_quality=30)
        high = image_to_pdf_bytes(noisy, jpeg_quality=95)
        assert len(low) < len(high), "质量参数没传到编码器"

    def test_real_transparency_keeps_png(self):
        """真有透明像素的图仍走 PNG（无损，alpha 不丢）"""
        src = Image.new("RGBA", (400, 300), (255, 0, 0, 128))
        assert b"DCTDecode" not in image_to_pdf_bytes(src), \
            "有透明像素时应走 PNG，不能被压成有损 JPEG"


# ==================== 合并为单个 PDF ====================

class TestMergedPdf:
    def test_mixed_inputs_merged_in_order(self):
        """2 张图片（红/蓝）+ 1 份 3 页 PDF → 共 5 页，顺序与列表一致"""
        with tempfile.TemporaryDirectory() as tmp:
            red = os.path.join(tmp, "red.png")
            blue = os.path.join(tmp, "blue.png")
            _make_image(red, (400, 300), color=(255, 0, 0))
            _make_image(blue, (400, 300), color=(0, 0, 255))

            doc = os.path.join(tmp, "doc.pdf")
            _make_pdf(doc, pages=3, fill=(1, 1, 1))

            out = os.path.join(tmp, "merged.pdf")
            items = [
                {"path": red, "config": None},
                {"path": blue, "config": None},
                {"path": doc, "config": WatermarkConfig(text="默认")},
            ]
            pages = build_merged_pdf(items, out)

            assert pages == 5
            assert get_pdf_page_count(out) == 5

            # 第 1 页偏红、第 2 页偏蓝、后 3 页为文档页
            p0 = render_pdf_page(out, page_num=0, scale=0.5).convert("RGB")
            p1 = render_pdf_page(out, page_num=1, scale=0.5).convert("RGB")
            assert p0.split()[0].getextrema()[1] > 200 and p0.split()[2].getextrema()[1] < 60
            assert p1.split()[2].getextrema()[1] > 200 and p1.split()[0].getextrema()[1] < 60

            # 文档页保持 A4 尺寸
            sizes = _page_sizes(out)
            assert abs(sizes[2][0] - 595) < 1 and abs(sizes[2][1] - 842) < 1

    def test_merged_applies_watermark_to_each_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            dark = os.path.join(tmp, "dark.png")
            _make_image(dark, (600, 400), color=(0, 0, 0))

            plain = os.path.join(tmp, "plain.pdf")
            build_merged_pdf([{"path": dark, "config": None}], plain)

            cfg = WatermarkConfig(
                text="合并", font_ratio=0.1, color_rgba=(255, 255, 255, 255),
                opacity=100, tile=True, spacing_x=40, spacing_y=40,
            )
            marked = os.path.join(tmp, "marked.pdf")
            build_merged_pdf([{"path": dark, "config": cfg}], marked)

            assert _mean(marked) > _mean(plain) + 5

    def test_merged_honours_page_configs(self):
        """合并时 PDF 的逐页参数同样生效"""
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, "doc.pdf")
            _make_pdf(doc, pages=2, fill=(1, 1, 1))

            out = os.path.join(tmp, "merged.pdf")
            items = [{
                "path": doc,
                "config": WatermarkConfig(),
                "page_configs": {1: WatermarkConfig(
                    text="第二页", font_ratio=0.2,
                    color_rgba=(0, 0, 0, 255), opacity=100,
                )},
            }]
            build_merged_pdf(items, out)

            assert _max_diff(doc, out, page=0) == 0
            assert _max_diff(doc, out, page=1) > 10

    def test_cancel_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "a.png")
            _make_image(img, (200, 200))
            out = os.path.join(tmp, "merged.pdf")

            try:
                build_merged_pdf(
                    [{"path": img, "config": None}], out,
                    should_cancel=lambda: True,
                )
                assert False, "应抛出 MergeCancelled"
            except MergeCancelled:
                pass
            assert not os.path.exists(out), "取消后不该留下半成品文件"

    def test_cancel_between_pages_also_aborts(self):
        """
        合并到一半才取消：必须以 MergeCancelled 抛出，且不留半成品。

        与上一条的区别：那次一进函数 should_cancel 就返回 True，命中的是
        **循环前**的检查（旧版本也有）。本条让回调先放行、到第 3 次调用才返回
        True，命中的只能是 watermark_pdf_bytes 里的**逐页**检查 ——
        旧版 build_merged_pdf 调它时漏传 should_cancel，这条路径永远不触发：
        一本几百页的 PDF 点了取消仍会跑完整本，界面却已经显示"已取消"。

        调用次数是这条用例的判据：循环前 1 次 + 每页 1 次。只调到 1 次
        就说明参数又丢了。
        """
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, "doc.pdf")
            _make_pdf(doc, pages=3, fill=(1, 1, 1))
            out = os.path.join(tmp, "merged.pdf")

            calls = {"n": 0}

            def cancel_after_two():
                calls["n"] += 1
                return calls["n"] > 2

            try:
                build_merged_pdf(
                    [{"path": doc, "config": WatermarkConfig()}], out,
                    should_cancel=cancel_after_two,
                )
                assert False, "处理到第 2 页前取消，应抛出 MergeCancelled"
            except MergeCancelled:
                pass

            assert calls["n"] > 2, (
                f"should_cancel 没传到底层逐页检查（只被调用 {calls['n']} 次）"
            )
            assert not os.path.exists(out), "取消后不该留下半成品文件"

    def test_progress_callback_reports_each_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "a.png")
            _make_image(img, (200, 200))
            out = os.path.join(tmp, "merged.pdf")

            seen = []
            build_merged_pdf(
                [{"path": img, "config": None}] * 2, out,
                progress=lambda i, total, name: seen.append((i, total)),
            )
            assert seen == [(0, 2), (1, 2), (2, 2)]

    def test_merged_output_url_safe_by_reader(self):
        """输出可被 pypdf 正常解析且不含加密标记"""
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "a.png")
            _make_image(img, (200, 200))
            out = os.path.join(tmp, "merged.pdf")
            build_merged_pdf([{"path": img, "config": None}], out)
            reader = PdfReader(out)
            assert reader.is_encrypted is False
            assert len(reader.pages) == 1


class TestOverlayScaleSnapshot:
    """
    覆盖层倍率必须是「一批一次」的快照。

    背景：OVERLAY_SCALE 是模块级全局变量，而批处理跑在多个工作线程里。
    如果每页都去读全局值，用户在处理途中拖动倍率滑块，
    同一份 PDF 的前后几页就会按不同参考尺寸渲染 ——
    同一个文件里出现两种水印大小，界面上完全看不出是哪一步出的问题。
    修法是批处理入口取一次快照，一路传到最底层。
    """

    def setup_method(self):
        self._before = get_overlay_scale()

    def teardown_method(self):
        # 全局值是进程级状态，测试必须还原，否则会污染同一进程里的其他用例
        set_overlay_scale(self._before)

    def test_reference_size_honours_explicit_scale(self):
        set_overlay_scale(2.0)
        assert overlay_reference_size(595, 842, 0, 1.0) == (595, 842)
        assert overlay_reference_size(595, 842, 0, 3.0) == (1785, 2526)

    def test_reference_size_falls_back_to_global(self):
        set_overlay_scale(2.0)
        assert overlay_reference_size(595, 842, 0) == \
            overlay_reference_size(595, 842, 0, 2.0)

    def test_explicit_scale_beats_global_change(self):
        """关键回归：传了快照之后，全局值怎么变都不影响本次结果"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "p.pdf")
            _make_pdf(pdf, pages=2)
            cfg = WatermarkConfig(text="SNAP", opacity=100)

            set_overlay_scale(1.0)
            baseline, _ = watermark_pdf_bytes(pdf, cfg)

            set_overlay_scale(3.0)     # 模拟"处理途中用户拖了滑块"
            with_snapshot, _ = watermark_pdf_bytes(pdf, cfg, overlay_scale=1.0)

            assert with_snapshot == baseline, "快照没生效：全局倍率仍渗透进来了"

    def test_scaling_actually_reaches_rendering(self):
        """倍率确实是体积旋钮（顺带确认参数没被吞掉）"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "p.pdf")
            _make_pdf(pdf, pages=1)
            cfg = WatermarkConfig(text="SIZE", opacity=100)

            small, _ = watermark_pdf_bytes(pdf, cfg, overlay_scale=1.0)
            large, _ = watermark_pdf_bytes(pdf, cfg, overlay_scale=3.0)

            assert len(large) > len(small)

    def test_execute_task_forwards_snapshot(self):
        """批处理路径（execute_task）也要把快照带到底层"""
        from src.batch_planner import execute_task, plan_batch
        from src.overrides import ScopeStore

        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "p.pdf")
            _make_pdf(pdf, pages=1)

            store = ScopeStore()
            store.set_base(WatermarkConfig(text="E2E", opacity=100).to_dict())
            plan = plan_batch([pdf], store)

            set_overlay_scale(1.0)
            out_a = os.path.join(tmp, "a.pdf")
            execute_task(plan.tasks[0], out_a, overlay_scale=1.0)

            set_overlay_scale(3.0)
            out_b = os.path.join(tmp, "b.pdf")
            execute_task(plan.tasks[0], out_b, overlay_scale=1.0)

            assert os.path.getsize(out_a) == os.path.getsize(out_b)

    def test_snapshot_does_not_mutate_global(self):
        """快照是只读的：批处理不该顺手把全局值改掉"""
        set_overlay_scale(2.5)
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "p.pdf")
            _make_pdf(pdf, pages=1)

            watermark_pdf_bytes(
                pdf, WatermarkConfig(text="X", opacity=100), overlay_scale=1.0
            )

        assert get_overlay_scale() == 2.5


class TestOverlayCacheBounds:
    """
    覆盖层缓存必须有上限。

    背景：缓存键包含「配置指纹」。逐页设了不同参数的 PDF 会让每页指纹都不同，
    缓存于是退化成「把整本 PDF 每一页的覆盖层位图都留在内存里」——
    800 页就是 800 张整页位图，命中率本来就是 0，留着只剩内存代价。
    """

    @staticmethod
    def _count_overlay_builds(monkeypatch):
        """替换 _make_overlay_pdf 计数，返回 (模块, 调用记录)"""
        import src.pdf_engine as pe

        calls = []
        original = pe._make_overlay_pdf

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(pe, "_make_overlay_pdf", counting)
        return pe, calls

    @staticmethod
    def _make_pdf_with_sizes(path, sizes):
        """逐页不同页面尺寸的 PDF（用来制造不同的缓存指纹）"""
        c = rl_canvas.Canvas(path)
        for size in sizes:
            c.setPageSize(size)
            c.showPage()
        c.save()

    def test_same_size_pages_share_one_overlay(self, tmp_path, monkeypatch):
        pe, calls = self._count_overlay_builds(monkeypatch)

        pdf = str(tmp_path / "same.pdf")
        self._make_pdf_with_sizes(pdf, [(595, 842)] * 6)

        pe.watermark_pdf_bytes(pdf, WatermarkConfig(text="C", opacity=100))

        assert len(calls) == 1, "同规格页面应共用同一张覆盖层"

    def test_eviction_rebuilds_when_limit_exceeded(self, tmp_path, monkeypatch):
        """上限 2 + 三种尺寸轮流：第 4 页必须重建 A —— 证明淘汰真的发生了"""
        pe, calls = self._count_overlay_builds(monkeypatch)
        monkeypatch.setattr(pe, "OVERLAY_CACHE_MAX_ITEMS", 2)

        pdf = str(tmp_path / "mix.pdf")
        self._make_pdf_with_sizes(
            pdf, [(595, 842), (842, 595), (400, 600), (595, 842)]
        )

        pe.watermark_pdf_bytes(pdf, WatermarkConfig(text="C", opacity=100))

        assert len(calls) == 4

    def test_larger_limit_keeps_all_fingerprints(self, tmp_path, monkeypatch):
        """上限够大时不该发生淘汰（对照组，防止"一律不缓存"也能过前面的用例）"""
        pe, calls = self._count_overlay_builds(monkeypatch)
        monkeypatch.setattr(pe, "OVERLAY_CACHE_MAX_ITEMS", 8)

        pdf = str(tmp_path / "mix.pdf")
        self._make_pdf_with_sizes(
            pdf, [(595, 842), (842, 595), (400, 600), (595, 842)]
        )

        pe.watermark_pdf_bytes(pdf, WatermarkConfig(text="C", opacity=100))

        assert len(calls) == 3

    def test_output_correct_under_tiny_limit(self, tmp_path, monkeypatch):
        """淘汰只影响速度，不影响正确性：每页都得有水印"""
        pe, _ = self._count_overlay_builds(monkeypatch)
        monkeypatch.setattr(pe, "OVERLAY_CACHE_MAX_ITEMS", 1)

        pdf = str(tmp_path / "many.pdf")
        self._make_pdf_with_sizes(pdf, [(595, 842), (842, 595), (400, 600)])

        # 白底页面 + 黑字水印，才能用"最暗像素"判断水印在不在
        data, pages = pe.watermark_pdf_bytes(
            pdf, WatermarkConfig(text="MARK", color_rgba=(0, 0, 0), opacity=100)
        )
        assert pages == 3

        out = tmp_path / "out.pdf"
        out.write_bytes(data)
        for i in range(3):
            img = render_pdf_page(str(out), page_num=i, scale=1.0)
            assert img.convert("L").getextrema()[0] < 200, f"第 {i + 1} 页没有水印"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
