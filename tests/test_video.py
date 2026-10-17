"""
单元测试 — 视频水印引擎

覆盖原则：
- 编码决策 / 滤镜链 / 命令构建全部是纯函数，不依赖 ffmpeg 是否安装
- 只有真实转码的用例才 skipif，CI 上没有 ffmpeg 时自动跳过而不是失败
- 重点守住三类回归：
  1. HDR 掉成 8bit（最容易被"顺手优化"改坏）
  2. overlay 的 format 值写成 yuv420p（会导致 ffmpeg 直接报错）
  3. 把色彩标记改回命令行参数（实测对 x264/x265 不生效）

运行: python -m pytest tests/test_video.py -v
"""

import os
import shutil
import subprocess
import sys

import pytest

from src import video_engine as ve
from src.config import WatermarkConfig
from src.video_engine import VideoInfo


# ==================== 测试用构造 ====================

def make_info(**kwargs) -> VideoInfo:
    """构造一个默认 1080p / 8bit / 无旋转的探测结果，按需覆盖字段"""
    defaults = dict(
        path="dummy.mp4",
        width=1920,
        height=1080,
        duration=60.0,
        fps=30.0,
        pix_fmt="yuv420p",
        bit_depth=8,
        video_codec="h264",
        audio_codec="aac",
        has_audio=True,
    )
    defaults.update(kwargs)
    return VideoInfo(**defaults)


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """
    把 ffmpeg 定位换成固定字符串。

    build_ffmpeg_command 只负责拼参数、不执行，所以路径写什么都行 ——
    这样命令构建的测试在任何机器上都能跑，不需要装 ffmpeg。
    """
    monkeypatch.setattr(ve, "ffmpeg_path", lambda: "ffmpeg")


# ==================== 几何：旋转与像素形状 ====================

class TestVideoGeometry:
    """显示尺寸换算 —— 竖拍视频水印错位的根源都在这"""

    def test_no_rotation(self):
        assert make_info().display_size == (1920, 1080)

    def test_rotation_90_swaps(self):
        """竖拍：编码是横的，显示要交换宽高"""
        assert make_info(rotation=90).display_size == (1080, 1920)

    def test_rotation_270_swaps(self):
        assert make_info(rotation=270).display_size == (1080, 1920)

    def test_rotation_180_keeps(self):
        assert make_info(rotation=180).display_size == (1920, 1080)

    def test_square_pixel_flag(self):
        assert make_info().needs_square_pixel_scale is False
        assert make_info(sar_num=4, sar_den=3).needs_square_pixel_scale is True

    def test_sar_widens_before_rotation(self):
        """
        SAR 作用于编码宽高，旋转在显示时交换 —— 顺序反了会算出一个
        "看起来合理但其实是错的"尺寸。
        """
        info = make_info(width=720, height=576, sar_num=16, sar_den=15)
        assert info.display_size == (768, 576)

        rotated = make_info(width=720, height=576, sar_num=16, sar_den=15, rotation=90)
        assert rotated.display_size == (576, 768)

    def test_zero_sar_is_ignored(self):
        """ffprobe 对未知 SAR 给 0:1，不能拿去做除法"""
        assert make_info(width=640, height=480, sar_num=0, sar_den=1).display_size == (640, 480)


class TestRotationParsing:
    """从 ffprobe 的 stream 结构里读旋转"""

    def test_side_data_rotation(self):
        stream = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}]}
        assert ve._rotation_from_stream(stream) == 90

    def test_negative_rotation_normalized(self):
        stream = {"side_data_list": [{"rotation": -90}]}
        assert ve._rotation_from_stream(stream) == 270

    def test_tags_rotate_fallback(self):
        assert ve._rotation_from_stream({"tags": {"rotate": "180"}}) == 180

    def test_no_rotation(self):
        assert ve._rotation_from_stream({}) == 0


# ==================== 位深 / 像素格式 ====================

class TestPixelFormat:
    def test_8bit_formats(self):
        for fmt in ("yuv420p", "yuv422p", "nv12", "gray", "rgb24"):
            assert ve._bit_depth_from_pix_fmt(fmt) == 8, fmt

    def test_10bit_formats(self):
        for fmt in ("yuv420p10le", "p010le", "yuv422p10le", "gbrp10le"):
            assert ve._bit_depth_from_pix_fmt(fmt) == 10, fmt

    def test_12bit(self):
        assert ve._bit_depth_from_pix_fmt("yuv444p12le") == 12

    def test_empty(self):
        assert ve._bit_depth_from_pix_fmt("") == 8
        assert ve._bit_depth_from_pix_fmt(None) == 8

    def test_overlay_format_never_uses_p_suffix(self):
        """
        回归：overlay 滤镜的 format 取值是 yuv420 / yuv420p10，
        写成 yuv420p 会让 ffmpeg 报 "Unable to parse format option value"。
        """
        assert ve.overlay_filter_format("yuv420p") == "yuv420"
        assert ve.overlay_filter_format("yuv420p10le") == "yuv420p10"
        for value in (ve.overlay_filter_format("yuv420p"), ve.overlay_filter_format("yuv420p10le")):
            assert not value.endswith("p"), value


# ==================== 编码决策 ====================

class TestEncoderDecision:
    def test_hdr_forces_x265(self):
        """HDR 无条件走 x265 —— H.264 的 10bit HDR 没有标准 signaling"""
        hdr = make_info(color_transfer="arib-std-b67")
        assert hdr.is_hdr is True
        assert ve.target_encoder(hdr, ve.OUTPUT_MODE_MP4) == "libx265"
        assert ve.target_encoder(hdr, ve.OUTPUT_MODE_SOURCE) == "libx265"

    def test_pq_is_hdr(self):
        assert make_info(color_transfer="smpte2084").is_hdr is True

    def test_sdr_is_not_hdr(self):
        assert make_info(color_transfer="bt709").is_hdr is False
        assert make_info(color_transfer="").is_hdr is False

    def test_mp4_mode_always_x264_for_sdr(self):
        info = make_info(video_codec="hevc")
        assert ve.target_encoder(info, ve.OUTPUT_MODE_MP4) == "libx264"

    def test_source_mode_keeps_encoder(self):
        assert ve.target_encoder(make_info(video_codec="h264"), ve.OUTPUT_MODE_SOURCE) == "libx264"
        assert ve.target_encoder(make_info(video_codec="hevc"), ve.OUTPUT_MODE_SOURCE) == "libx265"

    def test_source_mode_falls_back_for_exotic_codecs(self):
        """VP9 / AV1 / ProRes 一律回落 H.264（精简 ffmpeg 不带那些编码器）"""
        for codec in ("vp9", "av1", "prores", "mpeg4", ""):
            assert ve.target_encoder(
                make_info(video_codec=codec), ve.OUTPUT_MODE_SOURCE
            ) == "libx264", codec

    def test_pix_fmt_follows_source_depth(self):
        assert ve.target_pix_fmt(make_info(), "libx264") == "yuv420p"
        assert ve.target_pix_fmt(make_info(bit_depth=10, pix_fmt="yuv420p10le"), "libx264") == "yuv420p10le"
        assert ve.target_pix_fmt(make_info(bit_depth=12, pix_fmt="yuv444p12le"), "libx265") == "yuv420p10le"

    def test_hdr_8bit_source_still_10bit(self):
        """HLG 在 8bit 下必然出色带，所以要强制抬到 10bit"""
        info = make_info(bit_depth=8, color_transfer="arib-std-b67")
        assert ve.target_pix_fmt(info, "libx265") == "yuv420p10le"


# ==================== 滤镜链 ====================

class TestFilterChain:
    def test_plain_chain(self):
        chain = ve.build_video_filter(make_info(), "yuv420p")
        assert chain == "[0:v][1:v]overlay=0:0:eof_action=repeat:format=yuv420[outv]"

    def test_10bit_chain_keeps_10bit(self):
        """回归：overlay 默认 yuv420 会把 10bit 悄悄降成 8bit"""
        chain = ve.build_video_filter(make_info(bit_depth=10), "yuv420p10le")
        assert "format=yuv420p10" in chain
        assert "format=yuv420[" not in chain

    def test_color_params_go_through_setparams(self):
        """
        回归：色彩标记必须走 setparams 滤镜。
        实测命令行 -color_primaries / -color_trc 对 x264/x265 不生效，
        改回命令行参数 = HDR 静默丢标记。
        """
        info = make_info(
            color_primaries="bt2020",
            color_transfer="arib-std-b67",
            color_space="bt2020nc",
            color_range="tv",
        )
        chain = ve.build_video_filter(info, "yuv420p10le")
        assert "setparams=" in chain
        assert "color_primaries=bt2020" in chain
        assert "color_trc=arib-std-b67" in chain
        assert "colorspace=bt2020nc" in chain
        assert "range=tv" in chain
        assert chain.endswith("[outv]")

    def test_no_color_params_no_setparams(self):
        chain = ve.build_video_filter(make_info(), "yuv420p")
        assert "setparams" not in chain

    def test_unknown_color_values_filtered(self):
        """setparams 拿到 unknown 会报错，必须在拼串前滤掉"""
        info = make_info(color_primaries="unknown", color_transfer="reserved")
        assert ve._setparams_clause(info) == ""

    def test_sar_mismatch_inserts_scale(self):
        chain = ve.build_video_filter(make_info(sar_num=4, sar_den=3), "yuv420p")
        assert "scale=" in chain
        assert "setsar=1" in chain
        assert "flags=lanczos" in chain

    def test_normal_source_has_no_scaling(self):
        """正常源不该有任何缩放 —— 多一次重采样就是白丢画质"""
        chain = ve.build_video_filter(make_info(), "yuv420p")
        assert "scale" not in chain


# ==================== 命令构建 ====================

class TestCommandBuild:
    def test_output_path_is_last(self, fake_ffmpeg):
        cmd = ve.build_ffmpeg_command(make_info(), "in.mp4", "ov.png", "out.mp4")
        assert cmd[-1] == "out.mp4"

    def test_required_flags(self, fake_ffmpeg):
        cmd = ve.build_ffmpeg_command(make_info(), "in.mp4", "ov.png", "out.mp4")
        joined = " ".join(cmd)
        assert "-nostdin" in cmd                  # GUI 进程别去抢 stdin
        assert "passthrough" in cmd               # 不丢帧不补帧
        assert "+faststart" in cmd                # 网页/微信边下边播
        assert "pipe:1" in cmd                    # 进度走 stdout
        assert "-filter_complex" in cmd

    def test_default_quality_is_visually_lossless(self, fake_ffmpeg):
        cmd = ve.build_ffmpeg_command(make_info(), "in.mp4", "ov.png", "out.mp4")
        assert cmd[cmd.index("-crf") + 1] == str(ve.DEFAULT_CRF)
        assert cmd[cmd.index("-preset") + 1] == ve.DEFAULT_PRESET
        assert ve.DEFAULT_CRF <= 16, "CRF 高于 16 就不是视觉无损档了"
        assert ve.DEFAULT_PRESET in ("slow", "slower", "veryslow")

    def test_audio_copy_for_aac(self, fake_ffmpeg):
        cmd = ve.build_ffmpeg_command(make_info(audio_codec="aac"), "i.mp4", "o.png", "o.mp4")
        assert cmd[cmd.index("-c:a") + 1] == "copy"

    def test_pcm_audio_transcoded(self, fake_ffmpeg):
        """mov 里的 PCM 在 mp4 容器里装不下，copy 会直接报错"""
        cmd = ve.build_ffmpeg_command(make_info(audio_codec="pcm_s16le"), "i.mov", "o.png", "o.mp4")
        assert cmd[cmd.index("-c:a") + 1] == "aac"

    def test_opus_audio_transcoded(self, fake_ffmpeg):
        cmd = ve.build_ffmpeg_command(make_info(audio_codec="opus"), "i.webm", "o.png", "o.mp4")
        assert cmd[cmd.index("-c:a") + 1] == "aac"

    def test_no_audio_no_map(self, fake_ffmpeg):
        info = make_info(has_audio=False, audio_codec="")
        cmd = ve.build_ffmpeg_command(info, "i.mp4", "o.png", "o.mp4")
        assert "0:a?" not in cmd
        assert "-c:a" not in cmd

    def test_srt_subtitle_skipped(self, fake_ffmpeg):
        """srt 塞不进 mp4，硬映射会让整条命令失败"""
        info = make_info(has_subtitle=True, subtitle_codec="subrip")
        cmd = ve.build_ffmpeg_command(info, "i.mkv", "o.png", "o.mp4")
        assert "0:s?" not in cmd

    def test_mov_text_subtitle_copied(self, fake_ffmpeg):
        info = make_info(has_subtitle=True, subtitle_codec="mov_text")
        cmd = ve.build_ffmpeg_command(info, "i.mov", "o.png", "o.mp4")
        assert "0:s?" in cmd
        assert cmd[cmd.index("-c:s") + 1] == "copy"

    def test_hdr_adds_x265_params(self, fake_ffmpeg):
        cmd = ve.build_ffmpeg_command(
            make_info(color_transfer="arib-std-b67"), "i.mp4", "o.png", "o.mp4"
        )
        assert "libx265" in cmd
        assert "-x265-params" in cmd

    def test_no_color_flag_on_command_line(self, fake_ffmpeg):
        """
        回归守卫：色彩标记不能走命令行选项。
        这两个参数在 x264/x265 上实测无效，只会给人"已经保住了"的错觉。
        """
        info = make_info(color_primaries="bt2020", color_transfer="arib-std-b67")
        cmd = ve.build_ffmpeg_command(info, "i.mp4", "o.png", "o.mp4")
        assert "-color_primaries" not in cmd
        assert "-color_transfer" not in cmd
        assert "-color_trc" not in cmd


# ==================== 覆盖层 ====================

class TestOverlay:
    def test_overlay_size_and_mode(self):
        cfg = WatermarkConfig(text="测试水印", font_ratio=0.05)
        overlay = ve.build_overlay((640, 360), cfg)
        assert overlay.size == (640, 360)
        assert overlay.mode == "RGBA"

    def test_overlay_has_content(self):
        overlay = ve.build_overlay((640, 360), WatermarkConfig(text="水印", font_ratio=0.06))
        assert ve.overlay_has_content(overlay) is True

    def test_empty_text_has_no_content(self):
        """空文字 + 无 Logo = 无内容，不该白跑一遍重编码"""
        overlay = ve.build_overlay((640, 360), WatermarkConfig(text=""))
        assert ve.overlay_has_content(overlay) is False

    def test_overlay_transparent_background(self):
        """覆盖层其余区域必须完全透明，否则整段视频会被盖一层色"""
        cfg = WatermarkConfig(text="水印", font_ratio=0.06, position="bottom-right")
        overlay = ve.build_overlay((640, 360), cfg)
        alpha = overlay.getchannel("A")
        # 左上角远离水印，必须全透明
        assert alpha.getpixel((5, 5)) == 0
        # 水印区域必须有内容
        assert alpha.getbbox() is not None

    def test_illegal_size_rejected(self):
        with pytest.raises(ValueError):
            ve.build_overlay((0, 100), WatermarkConfig(text="x"))


# ==================== 工具函数 ====================

class TestHelpers:
    def test_is_video_file(self):
        for name in ("a.mp4", "a.MOV", "a.mkv", "a.webm", "a.m4v"):
            assert ve.is_video_file(name) is True, name
        for name in ("a.jpg", "a.pdf", "a.png", "noext"):
            assert ve.is_video_file(name) is False, name

    def test_format_time(self):
        assert ve.format_time(0) == "00:00"
        assert ve.format_time(65) == "01:05"
        assert ve.format_time(3725) == "1:02:05"
        assert ve.format_time(None) == "00:00"

    def test_parse_fraction(self):
        assert ve._parse_fraction("30000/1001") == pytest.approx(29.97, abs=0.01)
        assert ve._parse_fraction("25/1") == 25.0
        assert ve._parse_fraction(None) == 0.0
        assert ve._parse_fraction("0/0", default=30.0) == 30.0

    def test_parse_sar(self):
        assert ve._parse_sar("1:1") == (1, 1)
        assert ve._parse_sar("4:3") == (4, 3)
        assert ve._parse_sar("0:1") == (0, 1)
        assert ve._parse_sar(None) == (1, 1)

    def test_summary_marks_hdr_and_rotation(self):
        info = make_info(rotation=90, color_transfer="arib-std-b67")
        text = info.summary()
        assert "1080x1920" in text
        assert "HDR" in text
        assert "旋转90" in text


class TestToolDiscovery:
    """ffmpeg 定位失败必须给出可操作的提示，而不是 WinError 2"""

    def test_missing_tool_raises_readable_error(self, monkeypatch):
        monkeypatch.delenv("WATERMARK_FFMPEG_DIR", raising=False)
        monkeypatch.setattr(ve.shutil, "which", lambda name: None)
        if getattr(sys, "frozen", False):  # pragma: no cover - 打包环境下跳过
            pytest.skip("打包环境里 _MEIPASS 可能真的存在 ffmpeg")
        with pytest.raises(FileNotFoundError) as excinfo:
            ve.ffmpeg_path()
        message = str(excinfo.value)
        assert "ffmpeg" in message
        assert "WATERMARK_FFMPEG_DIR" in message

    def test_env_override_wins(self, monkeypatch, tmp_path):
        exe = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        exe.write_bytes(b"stub")
        monkeypatch.setenv("WATERMARK_FFMPEG_DIR", str(tmp_path))
        assert ve.ffmpeg_path() == str(exe)


# ==================== 批处理接入 ====================

class _FakeScopes:
    """替身 ScopeStore：只实现 plan_batch 用到的两个方法"""

    def __init__(self, config=None):
        self._config = config or WatermarkConfig(text="水印", font_ratio=0.06)

    def config_for(self, path, page=None):
        return self._config

    def page_numbers(self, path):
        return []


class TestBatchIntegration:
    """视频接入批处理的规划层行为"""

    def test_video_output_is_container_not_image_format(self):
        """PDF/PNG 选项对视频没有意义，输出必须是视频容器扩展名"""
        plan = ve_batch_plan(["a.mp4", "b.jpg"], _FakeScopes(), out_format="jpeg")
        formats = {t.path: t.output_format for t in plan.tasks}
        assert formats["a.mp4"] == "mp4"
        assert formats["b.jpg"] == "jpeg"

    def test_video_ignores_pdf_output_choice(self):
        plan = ve_batch_plan(["a.mov"], _FakeScopes(), out_format="pdf")
        assert plan.tasks[0].output_format == "mp4"

    def test_source_mode_keeps_container(self):
        for name, expected in (("a.mp4", "mp4"), ("a.mov", "mov"), ("a.mkv", "mkv")):
            plan = ve_batch_plan([name], _FakeScopes(), video_output_mode=ve.OUTPUT_MODE_SOURCE)
            assert plan.tasks[0].output_format == expected, name

    def test_source_mode_maps_exotic_containers_to_mp4(self):
        for name in ("a.avi", "a.webm", "a.flv", "a.wmv", "a.ts"):
            plan = ve_batch_plan([name], _FakeScopes(), video_output_mode=ve.OUTPUT_MODE_SOURCE)
            assert plan.tasks[0].output_format == "mp4", name

    def test_video_blocks_merge(self):
        """视频没有"页"的概念，混进合并 PDF 会在打开文件时失败"""
        plan = ve_batch_plan(
            ["a.jpg", "b.mp4"], _FakeScopes(), out_format="pdf", merge_pdf=True
        )
        assert plan.merge is False
        assert plan.has_video is True

    def test_merge_still_works_without_video(self):
        plan = ve_batch_plan(
            ["a.jpg", "b.png"], _FakeScopes(), out_format="pdf", merge_pdf=True
        )
        assert plan.merge is True
        assert plan.has_video is False

    def test_merge_items_excludes_video(self):
        plan = ve_batch_plan(["a.jpg", "b.mp4"], _FakeScopes(), out_format="pdf")
        paths = [item["path"] for item in plan.merge_items()]
        assert paths == ["a.jpg"]

    def test_video_never_overwrites_source(self, tmp_path):
        """视频永不就地覆盖 —— 一次编码失败就毁掉原始素材"""
        from src.batch_planner import plan_output_paths

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"stub")
        plan = ve_batch_plan([str(video)], _FakeScopes(), out_format="auto")

        paths, _ = plan_output_paths(
            plan, str(tmp_path), on_conflict="rename", overwrite_original=True
        )
        assert paths[0] != str(video), "视频被就地覆盖了"
        assert paths[0].endswith(".mp4")

    def test_output_path_uses_video_extension(self, tmp_path):
        from src.batch_planner import plan_output_paths

        video = tmp_path / "clip.mkv"
        video.write_bytes(b"stub")
        plan = ve_batch_plan([str(video)], _FakeScopes(), out_format="auto")
        paths, _ = plan_output_paths(plan, str(tmp_path), on_conflict="rename")
        assert paths[0].endswith(".mp4"), paths[0]

    def test_execute_task_routes_video_to_engine(self, tmp_path, monkeypatch):
        """execute_task 必须把视频交给 watermark_video，而不是 Pillow"""
        from src.batch_planner import execute_task

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"stub")
        plan = ve_batch_plan([str(video)], _FakeScopes(), out_format="auto")
        out = str(tmp_path / "out.mp4")

        calls = {}

        def fake_watermark_video(source_path, output_path, cfg, **kwargs):
            calls["source"] = source_path
            calls["output"] = output_path
            calls["kwargs"] = kwargs
            return output_path

        monkeypatch.setattr(ve, "watermark_video", fake_watermark_video)
        execute_task(plan.tasks[0], out, video_output_mode=ve.OUTPUT_MODE_SOURCE)

        assert calls["source"] == str(video)
        assert calls["output"] == out
        assert calls["kwargs"]["output_mode"] == ve.OUTPUT_MODE_SOURCE

    def test_execute_task_converts_video_cancel(self, tmp_path, monkeypatch):
        """video_engine 的取消要翻译成批处理的 TaskCancelled"""
        from src.batch_planner import TaskCancelled, execute_task

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"stub")
        plan = ve_batch_plan([str(video)], _FakeScopes(), out_format="auto")

        def fake_watermark_video(*args, **kwargs):
            raise ve.VideoCancelled()

        monkeypatch.setattr(ve, "watermark_video", fake_watermark_video)
        with pytest.raises(TaskCancelled):
            execute_task(plan.tasks[0], str(tmp_path / "out.mp4"))


class TestFileCollection:
    def test_collect_files_accepts_video(self, tmp_path):
        from src.file_manager import collect_files

        video = tmp_path / "a.mp4"
        video.write_bytes(b"stub")
        image = tmp_path / "b.jpg"
        image.write_bytes(b"stub")
        junk = tmp_path / "c.txt"
        junk.write_text("x", encoding="utf-8")

        added, skipped = collect_files([str(video), str(image), str(junk)])
        assert str(video) in added
        assert str(image) in added
        assert any("c.txt" in path for path, _ in skipped), "txt 应被跳过"
        assert len(added) == 2

    def test_is_image_file_still_excludes_video(self):
        """is_image_file 是"能不能交给 Pillow"的判断，不能把视频算进去"""
        from src.file_manager import is_image_file, is_media_file

        assert is_image_file("a.mp4") is False
        assert is_media_file("a.mp4") is True
        assert is_image_file("a.jpg") is True
        assert is_image_file("a.pdf") is True


def ve_batch_plan(files, scopes, **kwargs):
    """薄封装：避免每个用例都写一遍 import"""
    from src.batch_planner import plan_batch

    return plan_batch(files, scopes, **kwargs)


# ==================== 真实转码（需要 ffmpeg）====================

_available, _reason = ve.ffmpeg_available()
requires_ffmpeg = pytest.mark.skipif(not _available, reason=f"本机无 ffmpeg：{_reason}")


@requires_ffmpeg
class TestRealTranscode:
    """真实跑一遍 ffmpeg。慢但不可替代 —— 参数拼错了只有这里能发现。"""

    @staticmethod
    def _make_clip(path: str, width=320, height=180, seconds=1, extra=None):
        command = [
            ve.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
        ]
        command += list(extra or [])
        command.append(path)
        subprocess.run(command, check=True, capture_output=True)
        return path

    def test_probe_and_watermark(self, tmp_path):
        src = self._make_clip(str(tmp_path / "src.mp4"))
        info = ve.probe_video(src)
        assert info.width == 320 and info.height == 180
        assert info.duration == pytest.approx(1.0, abs=0.2)
        assert info.has_audio is True

        cfg = WatermarkConfig(text="水印", font_ratio=0.06)
        out = str(tmp_path / "out.mp4")
        ve.watermark_video(src, out, cfg, info=info)
        assert os.path.isfile(out)

        result = ve.probe_video(out)
        assert result.width == 320 and result.height == 180
        assert result.duration == pytest.approx(1.0, abs=0.2)
        assert result.has_audio is True, "音轨丢了"

    def test_rotation_output_is_upright(self, tmp_path):
        """竖拍视频输出必须是转正后的尺寸"""
        raw = self._make_clip(str(tmp_path / "raw.mp4"), width=320, height=180)
        rotated = str(tmp_path / "rot.mp4")
        subprocess.run(
            [ve.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
             "-display_rotation", "90", "-i", raw, "-c", "copy", rotated],
            check=True, capture_output=True,
        )

        info = ve.probe_video(rotated)
        assert info.rotation == 90
        assert info.display_size == (180, 320)

        out = str(tmp_path / "out.mp4")
        ve.watermark_video(rotated, out, WatermarkConfig(text="水印", font_ratio=0.06), info=info)
        result = ve.probe_video(out)
        assert (result.width, result.height) == (180, 320)

    def test_cancel_leaves_no_artifact(self, tmp_path):
        src = self._make_clip(str(tmp_path / "src.mp4"), seconds=3)
        out = str(tmp_path / "cancel.mp4")
        state = {"flag": False}

        def on_progress(percent, _text):
            if percent > 5:
                state["flag"] = True

        with pytest.raises(ve.VideoCancelled):
            ve.watermark_video(
                src, out, WatermarkConfig(text="水印", font_ratio=0.06),
                progress=on_progress, should_cancel=lambda: state["flag"],
            )
        assert not os.path.exists(out), "取消后留下了半成品"
        assert not [n for n in os.listdir(tmp_path) if ".part" in n]

    def test_empty_watermark_rejected_before_encoding(self, tmp_path):
        """空水印要在开跑之前就拒掉，别浪费一整条视频的编码时间"""
        src = self._make_clip(str(tmp_path / "src.mp4"))
        with pytest.raises(ValueError, match="没有可添加的水印"):
            ve.watermark_video(src, str(tmp_path / "out.mp4"), WatermarkConfig(text=""))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
