"""
界面冒烟测试 —— 真实装配一次 AppWindow，走一遍完整操作流程

为什么需要它：
    单元测试各自都对，不代表"在窗口里点一遍"也是对的。
    作用范围三档、专属标记、翻页取值、PDF 输出与合并这些能力，
    胶水全在 AppWindow 里，只有把窗口真的建起来才能验证。
    另外还覆盖两处"界面里点一遍才会暴露"的问题：
      - 预览的工作分辨率与 scale 换算（决定预览与导出是否一致）
      - 完成对话框不持有模态抓取（持有会让窗口最小化后唤不醒）

安全约定：
    1. 窗口创建后立刻 withdraw + 全透明，不打扰正在用电脑的人
    2. 所有弹窗（messagebox / 完成对话框）都被替换成记录器，不会阻塞
    3. APPDATA 指向临时目录，绝不读写用户真实的会话/窗口位置文件

用法:
    python tests/smoke_gui.py
退出码 0 表示全部通过。
"""

import os
import sys
import tempfile
import time

# CI（英文 Windows）的 stdout 默认是 cp1252 —— 第一句中文就会 UnicodeEncodeError
# 直接崩掉、一条检查都跑不到。与 build_exe.py 同一处理：显式切成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------
# 1. 环境隔离与弹窗屏蔽（必须在 import 界面代码之前）
# ---------------------------------------------------------------

# 全部中间产物放系统临时目录，不在仓库里留垃圾
_WORK = tempfile.mkdtemp(prefix="wm_smoke_")

# app_window 在模块级用 APPDATA 拼 SESSION_FILE / 窗口位置文件
os.environ["APPDATA"] = os.path.join(_WORK, "appdata")
os.makedirs(os.environ["APPDATA"], exist_ok=True)

import tkinter.messagebox as _mb  # noqa: E402

for _fn in ("showinfo", "showwarning", "showerror"):
    setattr(_mb, _fn, lambda *a, **k: None)
_mb.askyesno = lambda *a, **k: False  # 崩溃恢复一律选"不恢复"

from PIL import Image  # noqa: E402
from reportlab.pdfgen import canvas as rl_canvas  # noqa: E402
import customtkinter as ctk  # noqa: E402

from src.config import WatermarkConfig  # noqa: E402
from src.config import (  # noqa: E402
    CONFLICT_LABELS,
    CONFLICT_OVERWRITE,
    CONFLICT_RENAME,
    CONFLICT_SKIP,
    DEFAULT_FILENAME_TEMPLATE,
)
from src.overrides import SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE  # noqa: E402
from src.pdf_engine import (  # noqa: E402
    get_pdf_page_count,
    pdf_page_reference_size,
    render_pdf_page,
)
from src.ui.app_window import AppWindow  # noqa: E402


# ---------------------------------------------------------------
# 2. 断言收集
# ---------------------------------------------------------------

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not cond:
        line += f"  <- {detail}"
    print(line, flush=True)
    return bool(cond)


def head(title):
    print(f"\n=== {title} ===", flush=True)


# ---------------------------------------------------------------
# 3. 像素工具
# ---------------------------------------------------------------

def count(img, channel):
    """通道占优像素数"""
    px = img.convert("RGB").get_flattened_data()
    if channel == "red":
        return sum(1 for q in px if q[0] > 200 and q[1] < 100 and q[2] < 100)
    if channel == "green":
        return sum(1 for q in px if q[1] > 200 and q[0] < 100 and q[2] < 100)
    return sum(1 for q in px if q[2] > 200 and q[0] < 100 and q[1] < 100)


def make_image(path, size=(800, 480), color=(0, 0, 0)):
    Image.new("RGB", size, color).save(path)


def make_pdf(path, pages=2, size=(595, 842)):
    c = rl_canvas.Canvas(path, pagesize=size)
    for _ in range(pages):
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, 0, size[0], size[1], stroke=0, fill=1)
        c.showPage()
    c.save()


def pump(app, dialogs, timeout=180.0):
    """
    启动批处理并等它结束。

    必须用 mainloop 等待，不能手工 app.update() 泵事件：
    工作线程通过 self.after() 把进度送回主线程，而 tkinter 只在
    主线程处于 mainloop 里时才允许跨线程 after，
    否则工作线程会抛 "main thread is not in main loop" 并直接死掉。

    返回 True 表示在超时前恢复可用。
    """
    state = {"done": False, "timeout": False}
    deadline = time.time() + timeout

    def _poll():
        # _on_done 在主线程里把开始按钮恢复成 normal，用它作为结束信号
        try:
            idle = str(app._start_btn.cget("state")) == "normal"
        except Exception:
            idle = False

        if idle:
            state["done"] = True
            app.quit()
            return
        if time.time() > deadline:
            state["timeout"] = True
            app.quit()
            return
        app.after(60, _poll)

    app.after(60, _poll)
    app.mainloop()
    app.update()
    return state["done"]


def is_managed(w) -> bool:
    """控件是否被 pack/grid 管理。

    窗口处于 withdraw 状态时 winfo_ismapped() 恒为 0，
    判断显示/隐藏要用 winfo_manager()（pack 后为 "pack"，
    pack_forget 后为空串）。
    """
    try:
        return bool(w.winfo_manager())
    except Exception:
        return False


# ---------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------

def main():
    print(f"工作目录（系统临时目录）: {_WORK}", flush=True)

    src_dir = os.path.join(_WORK, "src")
    out_a = os.path.join(_WORK, "out_single")
    out_b = os.path.join(_WORK, "out_merged")
    for d in (src_dir, out_a, out_b):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))

    A = os.path.join(src_dir, "A.png")
    B = os.path.join(src_dir, "B.png")
    C = os.path.join(src_dir, "C.pdf")
    make_image(A)
    make_image(B)
    make_pdf(C, pages=2)

    head("创建窗口")
    app = AppWindow()
    app.withdraw()
    try:
        app.attributes("-alpha", 0.0)
    except Exception:
        pass
    app.update()

    # 弹窗一律记录，不显示
    dialogs = []
    app._show_message = lambda m: dialogs.append(("msg", m))
    # 桩的参数名与真实方法保持一致（含 report）。
    # 少了 report 时，_on_done 里的 report=self._last_failure_report 会抛
    # TypeError —— 而它发生在 tkinter 回调里，只进 stderr，冒烟照样全绿，
    # "失败清单入口"这条链路等于从来没被验证过。
    app._show_completion_dialog = lambda output_dir, success, fail, note=None, report=None: dialogs.append(
        ("done", output_dir, success, fail, note, report)
    )

    # 上面这条注释不可靠，用签名本身兜底：真实方法日后新增关键字参数时，
    # 这行会立刻失败，而不是等到某个回调偷偷 TypeError。
    import inspect as _inspect
    _real_params = set(_inspect.signature(AppWindow._show_completion_dialog).parameters) - {"self"}
    _stub_params = set(_inspect.signature(app._show_completion_dialog).parameters)
    check("完成对话框桩覆盖真实方法的全部参数",
          _real_params <= _stub_params,
          f"真实={sorted(_real_params)} 桩={sorted(_stub_params)}")

    # 关掉预览的后台渲染线程：它在工作线程里调 self.after()，
    # 而本次冒烟在批处理之外是同步驱动（不在 mainloop 里），
    # 跨线程 after 会抛 "main thread is not in main loop"。
    # 预览像素不是本次验证对象，直接不渲染，顺带排除线程干扰。
    app._preview_canvas._render_in_background = lambda *a, **k: None

    check("窗口已建成且隐藏", app.winfo_exists() == 1)

    # ---------- 加文件 ----------
    head("添加文件")
    app._file_list.add_files([A, B, C])
    app._update_count()
    app.update()
    check("文件列表 3 项", app._file_list.count == 3, app._file_list.count)

    # ---------- 列表选中与拖拽排序 ----------
    head("文件列表：选中与拖拽排序")
    # 点行会走 select_by_index。历史上它末尾调了 CTkFrame 没有的 see()，
    # 异常把 on_press 里后面的拖拽状态赋值一并跳过 —— 表现为"能选中但拖不动"，
    # 而且异常只进 stderr，打包成 exe 后完全看不见
    sel_err = ""
    try:
        app._file_list.select_by_index(0)
    except Exception as e:
        sel_err = repr(e)
    check("点击选中不抛异常", not sel_err, sel_err)

    before = list(app._file_list.files)
    app._file_list._reorder(0, 2)
    mid = list(app._file_list.files)
    check("拖拽重排改变了顺序",
          sorted(mid) == sorted(before) and mid != before, mid)
    app._file_list._reorder(2, 0)
    check("顺序可还原", list(app._file_list.files) == before, app._file_list.files)
    app.update()

    # ---------- 设全局默认 ----------
    head("作用范围：全部文件")
    base_cfg = WatermarkConfig(
        watermark_type="text", text="全局", font_ratio=0.12,
        color_rgba=(255, 0, 0, 255), opacity=100,
        position="bottom-right", margin=20,
    )
    app._on_scope_changed(SCOPE_ALL)
    app._text_panel.set_from_config(base_cfg)
    app.update()
    app._commit_current_layer()
    app.update()

    check("当前档位=全部文件", app._scope == SCOPE_ALL, app._scope)
    check("全局默认已记录文字", app._scopes.config_for(None).text == "全局",
          app._scopes.config_for(None).text)
    check("全局默认字号正确",
          abs(app._scopes.config_for(None).font_ratio - 0.12) < 1e-6,
          app._scopes.config_for(None).font_ratio)

    # ---------- B 文件设专属 ----------
    head("作用范围：仅本文件（B）")
    app._file_list.select_by_index(1)
    app.update()
    check("选中项为 B", app._file_list.selected_file == B, app._file_list.selected_file)
    check("B 无专属时档位未自动改变", app._scope == SCOPE_ALL, app._scope)

    app._on_scope_changed(SCOPE_FILE)
    b_cfg = WatermarkConfig(
        watermark_type="text", text="仅B", font_ratio=0.12,
        color_rgba=(0, 0, 255, 255), opacity=100,
        position="bottom-right", margin=20,
    )
    app._text_panel.set_from_config(b_cfg)
    app.update()
    app._commit_current_layer()
    app.update()

    check("B 有文件层专属", app._scopes.has_file(B))
    patch = app._scopes.to_dict()["files"].get(B, {})
    check("文件层只存差异字段（非整份快照）",
          set(patch) <= {"text", "color_rgba"},
          sorted(patch))
    check("B 生效文字=仅B", app._scopes.config_for(B).text == "仅B",
          app._scopes.config_for(B).text)
    marker = app._file_list._marker_labels[1].cget("text")
    check("B 行显示专属标记", marker == "专属", marker)

    # ---------- A 不受影响 ----------
    app._file_list.select_by_index(0)
    app.update()
    check("A 仍走全局默认", app._scopes.config_for(A).text == "全局",
          app._scopes.config_for(A).text)
    check("A 行无专属标记",
          app._file_list._marker_labels[0].cget("text") == "",
          app._file_list._marker_labels[0].cget("text"))

    # ---------- C 的页层专属 ----------
    head("作用范围：仅本页（C.pdf）")
    app._file_list.select_by_index(2)
    app.update()
    check("选中项为 C", app._file_list.selected_file == C, app._file_list.selected_file)
    check("C 预览当前页=0", app._preview_canvas.current_pdf_page == 0)

    # 第 1 页：不加（文字置空）
    app._on_scope_changed(SCOPE_PAGE)
    blank = dict(base_cfg.to_dict())
    blank["text"] = ""
    app._text_panel.set_from_config(WatermarkConfig.from_dict(blank))
    app.update()
    app._commit_current_layer()
    app.update()
    check("C 第 1 页有页层专属", app._scopes.has_page(C, 0))
    check("第 1 页专属只有 text 差异",
          set(app._scopes.to_dict()["pages"][0]["data"]) == {"text"},
          app._scopes.to_dict()["pages"][0]["data"])

    # 翻到第 2 页：面板应回填「全局默认」（C 无文件层）
    app._preview_canvas._set_pdf_page(1)
    app.update()
    check("翻页后当前页=1", app._preview_canvas.current_pdf_page == 1)
    panel_text = app._text_panel.get_config_dict().get("text")
    check("翻页后面板回填的是该页生效参数", panel_text == "全局", repr(panel_text))

    c2_cfg = WatermarkConfig(
        watermark_type="text", text="仅C2", font_ratio=0.12,
        color_rgba=(0, 255, 0, 255), opacity=100,
        position="bottom-right", margin=20,
    )
    app._text_panel.set_from_config(c2_cfg)
    app.update()
    app._commit_current_layer()
    app.update()

    check("C 第 2 页有页层专属", app._scopes.has_page(C, 1))
    check("C 两页都有专属", app._scopes.page_numbers(C) == [0, 1],
          app._scopes.page_numbers(C))
    marker = app._file_list._marker_labels[2].cget("text")
    check("C 行标记显示页数", marker == "专属 2页", marker)
    marked = app._preview_canvas._pdf_marked_label.cget("text")
    check("翻页栏显示专属页", "专属页" in marked and "1" in marked and "2" in marked,
          marked)

    # ---------- 自动定位到最深一层 ----------
    head("自动定位作用层")
    app._scope = SCOPE_ALL
    app._on_file_selected(B)
    app.update()
    check("选中带文件层专属的 B → 自动切到「仅本文件」",
          app._scope == SCOPE_FILE, app._scope)

    app._scope = SCOPE_ALL
    app._on_file_selected(C)
    app.update()
    check("选中带页层专属的 C → 自动切到「仅本页」",
          app._scope == SCOPE_PAGE, app._scope)

    # ---------- 清除专属 ----------
    head("清除专属参数")
    app._file_list.select_by_index(1)  # 必须先选中 B：清除作用于"当前选中"
    app.update()
    check("清除前选中项为 B", app._file_list.selected_file == B,
          app._file_list.selected_file)
    app._on_scope_changed(SCOPE_FILE)
    app._on_clear_scope_override()
    app.update()
    check("清除 B 的文件层", not app._scopes.has_file(B))
    check("B 行标记已消失",
          app._file_list._marker_labels[1].cget("text") == "",
          app._file_list._marker_labels[1].cget("text"))
    # 还原 B 的专属，供后面导出验证
    app._scopes.set_file(B, {"text": "仅B", "color_rgba": [0, 0, 255, 255]})
    app._refresh_scope_marks()
    app.update()

    # ---------- 格式切换 ----------
    head("输出格式：PDF")
    app._format_var.set("PDF")
    app._on_format_changed("PDF")
    app.update()
    check("选 PDF 后出现合并开关", is_managed(app._merge_pdf_cb))
    app._format_var.set("PNG")
    app._on_format_changed("PNG")
    app.update()
    check("切回 PNG 后合并开关隐藏且复位",
          not is_managed(app._merge_pdf_cb) and not app._merge_pdf_var.get())

    # ---------- 导出：PDF + 合并 ----------
    head("导出：PDF（合并为单个）")
    app._format_var.set("PDF")
    app._on_format_changed("PDF")
    app._merge_pdf_var.set(True)
    app._output_dir_var.set(out_b)
    app.update()

    app._on_start_processing()
    ok = pump(app, dialogs)
    check("批处理在超时前结束", ok)
    app.update()

    merged = [f for f in os.listdir(out_b) if f.lower().endswith(".pdf")]
    check("合并输出恰好 1 个 PDF", len(merged) == 1, merged)
    if merged:
        mp = os.path.join(out_b, merged[0])
        check("输出名带「合并水印」前缀", merged[0].startswith("合并水印"), merged[0])
        pages = get_pdf_page_count(mp)
        check("总页数 = 1(A) + 1(B) + 2(C) = 4", pages == 4, pages)
        if pages == 4:
            p0 = render_pdf_page(mp, 0, scale=1.0)
            p1 = render_pdf_page(mp, 1, scale=1.0)
            p2 = render_pdf_page(mp, 2, scale=1.0)
            p3 = render_pdf_page(mp, 3, scale=1.0)
            check("第1页(A)=红色全局水印", count(p0, "red") > 100 and count(p0, "blue") == 0,
                  (count(p0, "red"), count(p0, "blue")))
            check("第2页(B)=蓝色文件层水印", count(p1, "blue") > 100 and count(p1, "red") == 0,
                  (count(p1, "blue"), count(p1, "red")))
            check("第3页(C 第1页)=无彩色水印",
                  count(p2, "red") == 0 and count(p2, "green") == 0 and count(p2, "blue") == 0,
                  (count(p2, "red"), count(p2, "green"), count(p2, "blue")))
            check("第4页(C 第2页)=绿色页层水印", count(p3, "green") > 50,
                  count(p3, "green"))
    check("完成对话框已弹出（合并文案带页数）",
          any(d[0] == "done" and d[4] and "页" in str(d[4]) for d in dialogs),
          dialogs[-1:] if dialogs else [])

    # ---------- 导出：PDF 不合并 ----------
    head("导出：PDF（每图一个单页 PDF）")
    dialogs.clear()
    app._merge_pdf_var.set(False)
    app._output_dir_var.set(out_a)
    app.update()

    app._on_start_processing()
    ok = pump(app, dialogs)
    app.update()
    check("批处理在超时前结束", ok)

    made = sorted(f for f in os.listdir(out_a) if f.lower().endswith(".pdf"))
    check("逐文件输出 3 个 PDF", len(made) == 3, made)
    counts = {}
    for f in made:
        counts[f] = get_pdf_page_count(os.path.join(out_a, f))
    check("A/B 各 1 页、C 为 2 页",
          sorted(counts.values()) == [1, 1, 2], counts)
    a_pdf = os.path.join(out_a, "A_watermarked.pdf")
    b_pdf = os.path.join(out_a, "B_watermarked.pdf")
    if os.path.exists(a_pdf) and os.path.exists(b_pdf):
        check("A 输出红色水印", count(render_pdf_page(a_pdf, 0, scale=1.0), "red") > 100)
        check("B 输出蓝色水印", count(render_pdf_page(b_pdf, 0, scale=1.0), "blue") > 100)
    else:
        check("输出文件名符合模板", False, made)

    # ---------- 会话往返 ----------
    head("会话持久化")
    app._save_session()
    session_file = os.path.join(os.environ["APPDATA"], "WatermarkTool", "session.json")
    check("会话文件已写出", os.path.exists(session_file), session_file)
    if os.path.exists(session_file):
        import json
        with open(session_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        check("会话包含 scopes", isinstance(data.get("scopes"), dict))
        check("会话包含 merge_pdf 开关", "merge_pdf" in data)
        check("会话里的文件层保留了 B",
              B in (data.get("scopes", {}).get("files") or {}),
              list((data.get("scopes", {}).get("files") or {}).keys()))

    # ---------- 预览：工作分辨率与 scale ----------
    head("预览管线：工作分辨率与按比例渲染")
    pc = app._preview_canvas

    # 预览必须在缩小后的图上渲染（画布只有几百像素），同时把
    # 「工作尺寸 / 导出尺寸」交给引擎换算间距与边距 —— 否则预览与导出不一致
    big_path = os.path.join(src_dir, "big.jpg")
    Image.new("RGB", (6000, 4000), (10, 40, 80)).save(big_path, quality=92)

    work, ref, scale = pc._prepare_base(big_path, 0, 1200)
    check("大图底图被缩到工作分辨率", max(work.size) <= 1200, work.size)
    check("参考分辨率=原图像素尺寸", ref == (6000, 4000), ref)
    check("scale=工作宽度/参考宽度", abs(scale - work.width / 6000) < 1e-6,
          (scale, work.width))

    work2, ref2, scale2 = pc._prepare_base(big_path, 0, 1200)
    check("同一(路径,页,尺寸)命中底图缓存", work2 is work)

    small_path = os.path.join(src_dir, "small.jpg")
    Image.new("RGB", (640, 480), (10, 40, 80)).save(small_path, quality=92)
    swork, sref, sscale = pc._prepare_base(small_path, 0, 1200)
    check("小图不放大，scale=1.0", swork.size == (640, 480) and abs(sscale - 1.0) < 1e-9,
          (swork.size, sscale))

    pdf_ref = pdf_page_reference_size(C, 0)
    pwork, pref, pscale = pc._prepare_base(C, 0, 800)
    check("PDF 参考尺寸=覆盖层口径", pref == pdf_ref, (pref, pdf_ref))
    check("PDF 工作尺寸<=目标", max(pwork.size) <= 800, pwork.size)
    check("PDF scale=工作宽度/参考宽度", abs(pscale - pwork.width / pdf_ref[0]) < 1e-6,
          (pscale, pwork.width, pdf_ref))

    # ---------- 预览：缩放与拖动缓存 ----------
    head("预览：缩放与拖动只重采样一次")
    pc._cached_render = Image.new("RGB", (1600, 1200), (20, 20, 20))
    pc._cached_orig_size = (6000, 4000)
    pc._display_key = None
    # 放大到 200%：隐藏窗口里画布只有 100px 见方，不放大就没有可拖动的余量
    pc._zoom_var.set(2.0)
    pc._apply_zoom(force=True)
    key_after_zoom = pc._display_key
    tk_before = pc._display_image_tk
    check("缩放后生成了显示图",
          tk_before is not None and pc._display_size[0] > 0, pc._display_size)
    check("显示图大于画布（有可拖动余量）", pc._display_size[0] > 100, pc._display_size)

    class _Ev:
        def __init__(self, x, y):
            self.x, self.y = x, y

    pc._on_pan_start(_Ev(10, 10))
    pc._on_pan_motion(_Ev(60, 40))
    pc._on_pan_end(_Ev(60, 40))
    app.update()
    check("拖动没有重新采样（显示图与缓存键未变）",
          pc._display_key == key_after_zoom and pc._display_image_tk is tk_before,
          (pc._display_key, key_after_zoom))
    check("拖动确实移动了视图", pc._pan_offset != (0, 0), pc._pan_offset)

    pc._zoom_timer = None
    pc._last_zoom_apply = time.monotonic()   # 假装刚算过，触发节流分支
    pc._on_zoom_slider(1.2)
    pc._on_zoom_slider(1.3)
    check("滑块连续回调被节流（未每帧重采样）", pc._zoom_timer is not None)
    if pc._zoom_timer:
        pc.after_cancel(pc._zoom_timer)
        pc._zoom_timer = None
    pc._cached_render = None
    pc._show_placeholder("（冒烟测试结束预览）")

    # ---------- 完成对话框与僵尸抓取 ----------
    head("完成对话框：不持有模态抓取")
    # 前面把弹窗换成了记录器，这里调用一次真实实现
    AppWindow._show_completion_dialog(app, out_a, 3, 0)
    app.update()
    dlg = getattr(app, "_completion_dialog", None)
    check("完成对话框已创建", dlg is not None and dlg.winfo_exists())
    check("对话框没有 grab（最小化后不会吞掉输入）",
          app.grab_current() is None, app.grab_current())

    # 安全网：万一有不可见窗口抓着模态，主窗口重新出现时必须清掉它
    zombie = ctk.CTkToplevel(app)
    zombie.withdraw()
    app.update()
    zombie.grab_set()
    app.update()
    check("已构造出僵尸抓取（前置条件）", app.grab_current() is zombie)
    app._release_stale_grab()
    app.update()
    check("安全网释放了不可见窗口的抓取", app.grab_current() is None,
          app.grab_current())
    try:
        zombie.destroy()
    except Exception:
        pass

    if dlg is not None:
        app._close_completion_dialog(dlg)
        app.update()
        check("关闭后清掉单例引用",
              getattr(app, "_completion_dialog", None) is None)

    # ---------- 两个 Tab 合并成一份完整参数 ----------
    head("水印类型由内容推导（切 Tab 不再丢另一种水印）")
    app._scope = SCOPE_ALL   # 让后续提交只作用于全局默认，不污染文件/页层
    logo_path = os.path.join(src_dir, "logo.png")
    Image.new("RGBA", (200, 80), (255, 255, 255, 255)).save(logo_path)

    app._text_panel.set_from_config(WatermarkConfig(watermark_type="text", text="文字"))
    app._image_panel.set_from_config(
        WatermarkConfig(watermark_type="image", logo_path=logo_path)
    )
    app.update()

    app._settings_tabview.set("文字水印")
    app.update()
    cfg_text_tab = app._get_current_config()
    check("文字 Tab 下类型=both（Logo 没被关掉）",
          cfg_text_tab.watermark_type == "both", cfg_text_tab.watermark_type)

    app._settings_tabview.set("图片水印")
    app.update()
    cfg_image_tab = app._get_current_config()
    check("图片 Tab 下类型仍=both（切 Tab 不改变类型）",
          cfg_image_tab.watermark_type == "both", cfg_image_tab.watermark_type)
    check("完整参数同时含文字与 Logo",
          cfg_image_tab.text == "文字" and cfg_image_tab.logo_path == logo_path,
          (cfg_image_tab.text, cfg_image_tab.logo_path))

    app._text_panel.set_from_config(WatermarkConfig(watermark_type="text", text=""))
    app.update()
    check("只有 Logo 没有文字时类型=image",
          app._get_current_config().watermark_type == "image",
          app._get_current_config().watermark_type)

    # 复原，避免影响后续（如有）检查
    app._image_panel.set_from_config(WatermarkConfig(watermark_type="image", logo_path=""))
    app._text_panel.set_from_config(base_cfg)
    app.update()

    # ============================================================
    # 预设加载：Logo 必须显式落地（无 Logo / 失效 Logo 都要清空）
    # ============================================================
    head("加载预设：无 Logo / Logo 失效时必须清空旧 Logo")
    # 先让面板处于「有 Logo」状态
    app._image_panel.set_from_config(
        WatermarkConfig(watermark_type="image", logo_path=logo_path)
    )
    app._text_panel.set_from_config(WatermarkConfig(watermark_type="text", text="文字"))
    app.update()
    check("前置条件：面板已装上 Logo",
          app._image_panel.logo_path == logo_path, app._image_panel.logo_path)

    # 1) 加载「纯文字」预设（logo_path 为空）→ 旧 Logo 必须被清掉。
    #    旧实现只在「路径存在」时才动作，其余情况什么都不做，于是面板上
    #    仍留着上一张 Logo，导出结果变成「以为纯文字、实际带 Logo」。
    app._image_panel.set_from_config(WatermarkConfig(watermark_type="text", logo_path=""))
    app.update()
    check("加载无 Logo 预设后 logo_path 被清空",
          not app._image_panel.logo_path, app._image_panel.logo_path)
    check("加载无 Logo 预设后预览缩略图同步清掉",
          app._image_panel.logo_preview_tk is None, app._image_panel.logo_preview_tk)
    check("加载无 Logo 预设后水印类型回落 text",
          app._get_current_config().watermark_type == "text",
          app._get_current_config().watermark_type)

    # 2) 加载 Logo 路径失效的预设 → 同样不能沿用上一张
    app._image_panel.set_from_config(
        WatermarkConfig(watermark_type="image", logo_path=logo_path)
    )
    app.update()
    check("前置条件：重新装上 Logo", app._image_panel.logo_path == logo_path)

    missing_logo = os.path.join(src_dir, "logo-does-not-exist.png")
    app._image_panel.set_from_config(
        WatermarkConfig(watermark_type="image", logo_path=missing_logo)
    )
    app.update()
    check("加载失效 Logo 预设后不保留旧 Logo",
          app._image_panel.logo_path != logo_path, app._image_panel.logo_path)
    check("失效 Logo 不会污染最终配置",
          app._get_current_config().logo_path != logo_path,
          app._get_current_config().logo_path)

    # ============================================================
    # 界面初值必须来自配置类（不再各处硬编码）
    # ============================================================
    head("界面初值取自配置类（唯一来源）")
    from src.ui.image_watermark_panel import ImageWatermarkPanel  # noqa: E402
    from src.ui.text_watermark_panel import TextWatermarkPanel  # noqa: E402

    defaults = WatermarkConfig()
    fresh_text = TextWatermarkPanel(app)
    fresh_image = ImageWatermarkPanel(app)
    app.update()
    try:
        t0 = fresh_text.get_config_dict()
        i0 = fresh_image.get_config_dict()
        check("文字面板初值文本 = 配置类默认（不再预填「内部资料」）",
              t0.get("text") == defaults.text, repr(t0.get("text")))
        check("文字面板初值不透明度 = 配置类默认",
              t0.get("opacity") == defaults.opacity, t0.get("opacity"))
        check("文字面板初值边距 = 配置类默认",
              t0.get("margin") == defaults.margin, t0.get("margin"))
        check("图片面板初值 Logo 缩放 = 配置类默认",
              abs(float(i0.get("scale_ratio")) - defaults.scale_ratio) < 1e-9,
              i0.get("scale_ratio"))
        check("图片面板初值 Logo 边距 = 配置类默认",
              i0.get("logo_margin") == defaults.logo_margin, i0.get("logo_margin"))
    finally:
        fresh_text.destroy()
        fresh_image.destroy()
        app.update()

    # ============================================================
    # 输出设置控件（文件名模板 / 同名策略 / 覆盖原图）
    # ============================================================
    head("输出设置：文件名模板 / 同名策略 / 覆盖原图")
    check("存在文件名模板输入框", is_managed(app._template_entry))
    check("存在同名冲突下拉", is_managed(app._conflict_menu))
    check("存在「覆盖原图」勾选框", is_managed(app._overwrite_original_cb))
    check("模板初值 = 配置类默认",
          app._template_var.get() == DEFAULT_FILENAME_TEMPLATE, app._template_var.get())
    check("同名策略初值 = 自动重命名",
          app._conflict_mode() == CONFLICT_RENAME, app._conflict_mode())
    check("覆盖原图默认不勾选", app._is_overwrite_original() is False)

    mode_ok = []
    for code in (CONFLICT_SKIP, CONFLICT_OVERWRITE, CONFLICT_RENAME):
        app._conflict_var.set(CONFLICT_LABELS[code])
        app.update()
        mode_ok.append(app._conflict_mode() == code)
    check("下拉三种策略都能正确映射成代码", all(mode_ok), mode_ok)

    # 空模板必须回落默认，否则所有文件会撞成同一个名字
    app._template_var.set("   ")
    app._on_template_changed()
    app.update()
    check("空模板回落为默认模板",
          app._template_var.get() == DEFAULT_FILENAME_TEMPLATE, app._template_var.get())
    app._template_var.set("{original}_tpl")
    app._on_template_changed()
    app.update()
    check("自定义模板被保留",
          app._template_var.get() == "{original}_tpl", app._template_var.get())
    app._template_var.set(DEFAULT_FILENAME_TEMPLATE)
    app._on_template_changed()
    app.update()

    # 只切控件状态，不真正跑批处理 —— 覆盖原图会弹两次确认框
    app._overwrite_original_var.set(True)
    app._refresh_output_hint()
    app.update()
    check("勾选「覆盖原图」后状态为真", app._is_overwrite_original() is True)
    check("勾选后输出提示出现覆盖警告",
          "覆盖原图" in app._output_hint_label.cget("text"),
          app._output_hint_label.cget("text"))
    app._overwrite_original_var.set(False)
    app._refresh_output_hint()
    app.update()
    check("取消勾选后恢复", app._is_overwrite_original() is False)

    # ---------- 位置口径 / 主题持久化 / 状态标志 ----------
    head("位置口径 / 主题持久化 / 关窗保护")

    import json as _json
    import tempfile as _tempfile

    import src.ui.app_window as aw
    from src.ui.image_watermark_panel import ImageWatermarkPanel
    from src.ui.text_watermark_panel import _POSITIONS

    check(
        "两面板九宫格键完全一致",
        [k for k, _ in _POSITIONS] == list(ImageWatermarkPanel._POS_NAMES),
        f"text={[k for k, _ in _POSITIONS]}",
    )

    # 旧预设里的 center-left 回填后必须归一化：
    # 不归一化则按钮不高亮，渲染时还会滑到正中
    try:
        app._image_panel.set_from_config(
            WatermarkConfig(logo_position="center-left")
        )
        _pos = app._image_panel.get_config_dict().get("logo_position")
    except Exception as exc:
        _pos = f"<异常 {exc}>"
    check(
        "center-left 回填后归一化为 middle-left",
        _pos == "middle-left",
        f"实际 {_pos!r}",
    )

    # 皮肤：固定浅色、界面上无处可切（用户要求去掉深色版）。
    # 冒烟会真的写盘，所以先把 LAYOUT_FILE 指到临时文件，别污染用户的界面偏好。
    _orig_layout = aw.LAYOUT_FILE
    aw.LAYOUT_FILE = os.path.join(
        _tempfile.gettempdir(), "smoke_ui_layout.json"
    )
    try:
        check(
            "外观模式固定为浅色",
            ctk.get_appearance_mode() == "Light",
            f"实际 {ctk.get_appearance_mode()!r}",
        )
        check("顶栏没有主题切换按钮（深色版已移除）", not hasattr(app, "_theme_btn"))
        check(
            "皮肤来自自定义主题文件",
            str(getattr(app, "_theme_source", "")).endswith("ctk_theme.json"),
            f"实际 {getattr(app, '_theme_source', None)!r}",
        )

        # 预置一个旧版布局文件：带已废弃的 appearance 字段。
        # 保存后该字段要被清掉，同一文件里的布局比例必须原样保留（读-改-写）
        with open(aw.LAYOUT_FILE, "w", encoding="utf-8") as _f:
            _json.dump({"appearance": "Dark", "left_ratio": 0.31}, _f)
        app._save_layout()
        with open(aw.LAYOUT_FILE, encoding="utf-8") as _f:
            _raw = _json.load(_f)
        check("旧的外观字段已清除", "appearance" not in _raw, f"实际 {_raw!r}")
        check(
            "旧布局比例被保留（读-改-写没被破坏）",
            _raw.get("left_ratio") == 0.31,
            f"实际 {_raw!r}",
        )
    finally:
        try:
            os.remove(aw.LAYOUT_FILE)
        except Exception:
            pass
        aw.LAYOUT_FILE = _orig_layout

    # 文件名截断：列表里 .png/.jpg/.pdf 是用户第一眼要确认的信息，
    # 截断必须优先保住扩展名，而不是从尾巴上切（"人像竖版.…"）
    _elided = app._file_list._elide_name(
        "一个特别长的测试文件名用来验证截断行为.png", 120
    )
    check(
        "超长文件名被截断且保留扩展名",
        "…" in _elided and _elided.endswith(".png"),
        f"实际 {_elided!r}",
    )
    check(
        "放得下的文件名原样返回",
        app._file_list._elide_name("a.png", 400) == "a.png",
    )

    # 原生窗口外观（图标 + 标题栏配色）：都只是"画"，失败也不能拦住启动，
    # 但"悄悄全失效"同样要能发现 —— 所以逐项确认真的执行了
    check(
        "应用图标文件存在",
        os.path.isfile(aw._asset_path("app.ico")),
        aw._asset_path("app.ico"),
    )
    check(
        "窗口图标已设置",
        aw._apply_window_icon(app) is True,
        aw._asset_path("app.ico"),
    )
    _tb = aw.style_titlebar(app)
    check(
        "标题栏配色已应用（非 Windows 环境返回 False 属正常）",
        _tb is True or not sys.platform.startswith("win"),
        f"实际 {_tb!r}",
    )

    # 品牌名接线：窗口标题必须是「APP_NAME — 批量图片水印工具」
    from src import APP_NAME

    check(
        "窗口标题带品牌名",
        APP_NAME in app.title(),
        app.title(),
    )

    # 顶栏状态与底栏状态必须是两个不同控件：曾撞名（都叫 _status_label），
    # 结果底栏的"处理进度/就绪"文案被顶栏状态覆盖，两处显示同一句话
    check(
        "顶栏状态与底栏状态是两个标签",
        app._top_status_label is not app._status_label,
        f"top={app._top_status_label} bottom={app._status_label}",
    )
    app._file_list.select_by_index(0)
    _top_text = app._top_status_label.cget("text")
    check(
        "选中文件后顶栏显示文件名",
        "1/1" in _top_text or "第 1" in _top_text,
        f"实际 {_top_text!r}",
    )

    # 输入框焦点态：隐藏窗口下 Tk 不派发 <FocusIn>（event_generate 也无效），
    # 所以只能确认"绑定还在"。CTkEntry 自己也会绑 FocusIn/FocusOut，
    # 因此判据要落到我们那个 lambda 上（真变色由截图与人工验证）
    _inner = getattr(app._output_dir_entry, "_entry", None)
    _script = (_inner.bind("<FocusIn>") if _inner is not None else "") or ""
    check(
        "输入框已绑定焦点态（FocusIn 上挂着我们的回调）",
        "<lambda>" in _script,
        f"实际绑定脚本 {_script!r}",
    )

    # 质量标签：IntVar.set 不触发 CTkSlider 的 command，必须手动同步
    app._quality_var.set(72)
    app._quality_value_label.configure(text="72")
    check(
        "质量数字标签可独立刷新",
        app._quality_value_label.cget("text") == "72",
    )

    head("负路径：只测成功路径才会漏掉的那些")
    # 本节四条都对应一次真实事故。共同点：异常发生在 tkinter 回调或工作线程里，
    # 只进 stderr —— 冒烟即使全绿也发现不了，必须主动构造失败场景来验。
    # 关窗保护靠这个标志；处理中为 True 时才会弹确认
    check("空闲时不处于处理中", app._processing is False)
    check("关窗回调已注册", callable(app._on_window_close))

    # 处理中真的走一遍关窗回调。
    # 只断言"回调可调用"是恒真的，等于什么都没验 —— 正因如此，
    # _on_window_close 里漏导入 messagebox 造成的 NameError 长期没被发现
    # （表现：处理中点关闭既不弹确认、窗口也关不掉，只能结束进程）。
    # 弹窗在前面已被替换成记录器，askyesno 恒返回 False，所以走"用户点了否"分支。
    app._processing = True
    try:
        app._on_window_close()
        check("处理中点关闭不抛异常", True)
    except Exception as exc:                      # noqa: BLE001 - 冒烟要抓到全部异常
        check("处理中点关闭不抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        app._processing = False
    check("确认被拒后窗口仍然存在", bool(app.winfo_exists()))

    # 提前返回路径必须复位按钮三态。
    # 用空文件列表走到「没有可处理的任务」那条 early return（旧实现只复位了
    # 开始按钮，取消按钮会留在"可点"状态 —— 点了显示"正在取消…"，其实没任务）。
    app._format_var.set("PNG")
    app._merge_pdf_var.set(False)
    app._set_processing_ui(True)      # 先摆成"处理中"，看 early return 能否自己收拾干净
    app._run_batch([], {})
    check("提前返回后开始按钮恢复可用",
          str(app._start_btn.cget("state")) == "normal",
          str(app._start_btn.cget("state")))
    check("提前返回后取消按钮已禁用",
          str(app._cancel_btn.cget("state")) == "disabled",
          str(app._cancel_btn.cget("state")))
    check("提前返回后未标记为处理中", app._processing is False)

    # 预检线程里抛异常也必须能回到可用状态。
    # 旧实现把 oversized_files / large_images 裸在 try 外面，抛出后线程静默死掉、
    # after(0) 不执行 → 界面永久停在"正在检查 N 个文件…"、开始按钮永久 disabled。
    import src.resource_budget as _rb  # noqa: PLC0415 - 就地打补丁，便于 finally 还原
    _orig_over, _orig_large = _rb.oversized_files, _rb.large_images

    def _boom(*_a, **_k):
        raise OSError("模拟预检失败（文件被占用）")

    _rb.oversized_files = _boom
    _rb.large_images = _boom
    try:
        app._file_list.add_files([A])   # 确保预检真的会被驱动起来
        app._on_start_processing()
        scan_ok = pump(app, dialogs, timeout=45.0)
        check("预检抛异常后界面仍回到可用状态", scan_ok, "开始按钮未恢复")
        # 这一批是有成功文件的，所以 _on_done 一定走了"成功>0"分支调完成对话框。
        # 无失败时 _write_failure_report 返回 ""，因此这里的 report 必须是 ""，
        # 而不是桩的默认值 None —— 用后者就说明 _on_done 根本没把 report 传下去。
        _done = [d for d in dialogs if d[0] == "done"]
        _rep = _done[-1][5] if _done else "（没有完成对话框）"
        check("完成对话框收到了失败清单参数（未传即 None）",
              bool(_done) and _rep is not None,
              f"done 对话框 {len(_done)} 个，report={_rep!r}")
    finally:
        _rb.oversized_files, _rb.large_images = _orig_over, _orig_large

    # ---------- 收尾 ----------
    head("收尾")
    app._clear_session()
    try:
        app.destroy()
    except Exception:
        pass
    check("窗口已销毁", True)

    # ---------- 汇总 ----------
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 56, flush=True)
    print(f"冒烟结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过", flush=True)
    if failed:
        print("失败项:", flush=True)
        for name, _, detail in failed:
            print(f"  - {name}  {detail}", flush=True)
    print("=" * 56, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
