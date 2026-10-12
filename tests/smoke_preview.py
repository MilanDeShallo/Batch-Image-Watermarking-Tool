"""
预览渲染冒烟测试 —— 跑真实线程 + 真实 mainloop

为什么单独一个文件：
    smoke_gui.py 是同步驱动事件的（不在 mainloop 里），而预览的后台线程
    依赖跨线程 self.after()，所以那里必须把预览渲染屏蔽掉。
    于是「预览渲染线程 + 单飞 + 结果回主线程」这条链路就没人覆盖了 ——
    这里补上，并且顺带验证最要紧的一件事：
    **预览画出来的水印，几何必须和导出一致。**

安全约定：窗口 withdraw + 全透明，弹窗替换成记录器，APPDATA 指向临时目录。

用法: python tests/smoke_preview.py      退出码 0 表示全部通过
"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_WORK = tempfile.mkdtemp(prefix="wm_preview_")
os.environ["APPDATA"] = os.path.join(_WORK, "appdata")
os.makedirs(os.environ["APPDATA"], exist_ok=True)

import tkinter.messagebox as _mb  # noqa: E402

for _fn in ("showinfo", "showwarning", "showerror"):
    setattr(_mb, _fn, lambda *a, **k: None)
_mb.askyesno = lambda *a, **k: False

from PIL import Image  # noqa: E402
from reportlab.pdfgen import canvas as rl_canvas  # noqa: E402

from src.config import WatermarkConfig  # noqa: E402
from src.ui.app_window import AppWindow  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + ("" if cond or not detail else f"  <- {detail}"), flush=True)
    return bool(cond)


def head(title):
    print(f"\n=== {title} ===", flush=True)


# ---------------------------------------------------------------
# 像素工具
# ---------------------------------------------------------------

def ink_bbox_norm(img: Image.Image, thresh: int = 160):
    """
    墨迹外框（归一化）。

    阈值取 160：底色是 (30,60,90)，亮度约 54；不透明白字约 255。
    若水印被设成半透明，亮度会落到 150 附近 — 所以测试里用 opacity=100。
    """
    box = img.convert("L").point(lambda v: 255 if v >= thresh else 0).getbbox()
    if not box:
        return None
    w, h = img.size
    return (box[0] / w, box[1] / h, box[2] / w, box[3] / h)


# ---------------------------------------------------------------
# 步骤机：在 mainloop 里推进
# ---------------------------------------------------------------

def run_steps(app, steps, total_timeout=180.0):
    """依次执行 steps，每步返回 True 表示完成；全部用 after 轮询，保持 mainloop 存活"""
    state = {"i": 0, "deadline": time.time() + total_timeout, "timeout": False}

    def tick():
        if time.time() > state["deadline"]:
            state["timeout"] = True
            app.quit()
            return
        try:
            done = steps[state["i"]]()
        except Exception as e:  # 步骤内部异常也要中断，避免静默超时
            print(f"  步骤 {state['i']} 抛异常: {e!r}", flush=True)
            done = True
            RESULTS.append((f"步骤 {state['i']} 无异常", False, repr(e)))
        if done:
            state["i"] += 1
            if state["i"] >= len(steps):
                app.quit()
                return
        app.after(50, tick)

    app.after(50, tick)
    app.mainloop()
    return state["timeout"]


def wait_until(cond, note=""):
    """返回一个"条件成立即完成"的步骤函数"""
    def _step():
        return cond()
    _step.__doc__ = note
    return _step


def after_frames(n=6):
    """等若干次轮询周期，让 after 队列里排队的刷新真正跑起来"""
    box = {"n": 0}

    def _step():
        box["n"] += 1
        return box["n"] > n
    return _step


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------

def main():
    print(f"工作目录（系统临时目录）: {_WORK}", flush=True)

    img_path = os.path.join(_WORK, "big.jpg")
    # 6000x4000：旧实现会在原图上渲染，预览因此与导出不一致
    Image.new("RGB", (6000, 4000), (30, 60, 90)).save(img_path, quality=92)

    pdf_path = os.path.join(_WORK, "doc.pdf")
    c = rl_canvas.Canvas(pdf_path, pagesize=(595, 842))
    for i in range(2):
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, 0, 595, 842, stroke=0, fill=1)
        c.showPage()
    c.save()

    head("创建窗口")
    app = AppWindow()
    app.withdraw()
    try:
        app.attributes("-alpha", 0.0)
    except Exception:
        pass
    app.update()
    app._show_message = lambda m: None
    app._show_completion_dialog = lambda o, s, f, note=None: None

    pc = app._preview_canvas
    render_calls = {"n": 0}
    real_render = pc._render_in_background

    def counting_render(*a, **k):
        render_calls["n"] += 1
        return real_render(*a, **k)

    pc._render_in_background = counting_render

    check("窗口已建成且隐藏", app.winfo_exists() == 1)

    # ---------- 参数：单次定位，便于量右下角距离 ----------
    head("准备参数与文件")
    cfg = WatermarkConfig(
        watermark_type="text", text="WATERMARK", color_rgba=(255, 255, 255, 255),
        opacity=100, font_ratio=0.06, angle=0, tile=False,
        position="bottom-right", margin=20,
    )
    app._on_scope_changed(app._scope)
    app._text_panel.set_from_config(cfg)
    app._file_list.add_files([img_path, pdf_path])
    app.update()
    check("文件已加入列表", app._file_list.count == 2, app._file_list.count)

    token_before = pc._render_token

    # ---------- 选中图片：触发真实后台渲染 ----------
    head("选中图片 → 后台渲染完成")
    app._file_list.select_by_index(0)
    app.update()

    steps = [
        wait_until(lambda: pc._cached_render is not None),
        after_frames(4),
    ]
    timed_out = run_steps(app, steps, total_timeout=90)
    check("预览渲染在超时前完成", not timed_out)
    check("已产出渲染结果", pc._cached_render is not None)
    check("渲染版本号已推进", pc._render_token > token_before,
          (pc._render_token, token_before))
    check("渲染单飞标记已复位（不会永久卡住）", pc._render_busy is False,
          pc._render_busy)
    check("渲染期间没有堆积线程（只跑了一次）", render_calls["n"] <= 2,
          render_calls["n"])

    if pc._cached_render is not None:
        cached = pc._cached_render
        check("预览工作分辨率不超过上限",
              max(cached.size) <= pc.WORK_MAX_DIM, cached.size)
        check("预览工作分辨率不低于下限",
              max(cached.size) >= pc.WORK_MIN_DIM, cached.size)

        # ---------- 核心断言：预览与导出几何一致 ----------
        head("预览 vs 导出：几何必须一致")
        from src.watermark_engine import apply_watermark, safe_open_image

        live_cfg = app._get_current_config()
        export = apply_watermark(safe_open_image(img_path), live_cfg)   # scale=1.0
        e = ink_bbox_norm(export)
        p = ink_bbox_norm(cached)
        check("导出与预览都能取到墨迹", bool(e) and bool(p), (e, p))
        if e and p:
            for idx, name in ((2, "右边距"), (3, "下边距")):
                dev = abs(e[idx] - p[idx])
                check(f"{name}与导出一致（偏差 <= 0.004）", dev <= 0.004,
                      f"导出 {e[idx]:.4f} vs 预览 {p[idx]:.4f}，偏差 {dev:.4f}")

        # 旧实现（不换算 scale）会偏多少，一并打印出来做对照。
        # 注意必须用「未加过水印的底图」，否则是在已加水印的图上再叠一层，
        # 量出来的外框被已有墨迹主导，数字会假性偏小
        from src.watermark_engine import apply_watermark as aw
        base_work = pc._base_work
        if base_work is not None:
            naive = aw(base_work.copy(), live_cfg)   # 就是旧预览的行为
            n = ink_bbox_norm(naive)
            if n and e:
                print(f"    对照：旧预览（不换算 scale）右边距 {n[2]:.4f}"
                      f"（偏差 {abs(e[2] - n[2]):.4f}），本次预览偏差 "
                      f"{abs(e[2] - p[2]):.4f}", flush=True)

    # ---------- 参数微调 → 重新渲染 ----------
    head("参数微调 → 重新渲染")
    token_before = pc._render_token
    tweak = dict(cfg.to_dict())
    tweak["font_ratio"] = 0.09
    app._text_panel.set_from_config(WatermarkConfig.from_dict(tweak))
    app.update()   # 面板回调 → 提交作用层 + 请求刷新

    steps = [
        wait_until(lambda: pc._render_token > token_before),
        after_frames(4),
    ]
    timed_out = run_steps(app, steps, total_timeout=90)
    check("微调后完成了一次新渲染", not timed_out and pc._render_token > token_before,
          pc._render_token)

    # ---------- 连续刷新：请求必须被合并 ----------
    head("连续刷新 → 单飞合并")
    token_before = pc._render_token
    calls_before = render_calls["n"]
    for _ in range(6):
        pc.request_refresh()
    app.update()

    steps = [
        wait_until(lambda: pc._render_token > token_before),
        after_frames(6),
    ]
    timed_out = run_steps(app, steps, total_timeout=90)
    extra = render_calls["n"] - calls_before
    check("连续 6 次请求后仍完成了渲染", not timed_out and pc._render_token > token_before)
    check("连续请求被合并（渲染次数远少于 6）", extra <= 3, f"实际渲染 {extra} 次")
    check("完成后单飞标记复位", pc._render_busy is False, pc._render_busy)

    # ---------- PDF：翻页渲染 ----------
    head("PDF 预览：翻页")
    token_before = pc._render_token
    app._file_list.select_by_index(1)
    app.update()

    steps = [
        wait_until(lambda: pc._pdf_page_count == 2),
        after_frames(4),
    ]
    timed_out = run_steps(app, steps, total_timeout=90)
    check("PDF 页数已回填", not timed_out and pc._pdf_page_count == 2,
          pc._pdf_page_count)
    check("PDF 预览已产出", pc._cached_render is not None)

    token_before = pc._render_token
    pc._set_pdf_page(1)
    steps = [
        wait_until(lambda: pc._render_token > token_before),
        after_frames(4),
    ]
    timed_out = run_steps(app, steps, total_timeout=90)
    check("翻到第 2 页会重新渲染", not timed_out and pc._render_token > token_before,
          pc._render_token)
    check("翻页后单飞标记复位", pc._render_busy is False, pc._render_busy)

    # ---------- 清空：底图缓存要丢 ----------
    head("清空预览 → 丢弃底图缓存")
    pc.clear_preview()
    app.update()
    check("清空后底图缓存已丢弃", pc._base_key is None and pc._base_work is None,
          pc._base_key)
    check("清空后不再有渲染结果", pc._cached_render is None)

    # ---------- 收尾 ----------
    head("收尾")
    app._clear_session()
    try:
        app.destroy()
    except Exception:
        pass
    check("窗口已销毁", True)

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 56, flush=True)
    print(f"预览冒烟结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过", flush=True)
    for name, _, detail in failed:
        print(f"  - {name}  {detail}", flush=True)
    print("=" * 56, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
