"""
预设管理器 — 保存/加载/删除水印配置预设

存储格式：JSON 文件，每个预设一个文件
存储位置：%APPDATA%/WatermarkTool/presets/
文件名：{预设名}.json（预设名中的非法字符会被替换为下划线）

为什么放在用户目录而不是项目目录：
PyInstaller 打包后 __file__ 落在 _internal/ 下，程序若装到 Program Files，
那里是只读的 —— 用户一保存就报"保存预设失败"。界面布局(ui_layout.json)
与日志都已经走 APPDATA，这里统一口径。旧版存在项目 presets/ 的预设
会在首次访问时复制过来。
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Optional

from src.config import WatermarkConfig, load_preset_safe


class PresetManager:
    """预设管理器 — 管理 presets/ 目录下的 JSON 配置文件"""

    # 预设是**用户数据**，写在用户目录
    PRESETS_DIR = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "WatermarkTool",
        "presets",
    )

    # 旧版本把预设写在项目目录下的 presets/
    LEGACY_PRESETS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "presets"
    )

    _ready = False   # 目录准备与迁移在进程内只做一次

    @classmethod
    def _ensure_dirs(cls) -> None:
        """
        确保用户预设目录存在，并在首次运行时把旧版预设搬过来。

        迁移用**复制**而不是移动：旧目录保持原样，用户随时可以退回旧版本。
        同名文件不覆盖 —— 用户目录里的那份更新。
        """
        try:
            os.makedirs(cls.PRESETS_DIR, exist_ok=True)
        except Exception:
            return

        if cls._ready:
            return
        cls._ready = True

        legacy = cls.LEGACY_PRESETS_DIR
        try:
            if not os.path.isdir(legacy):
                return
            if os.path.abspath(legacy) == os.path.abspath(cls.PRESETS_DIR):
                return
            for name in os.listdir(legacy):
                if not name.endswith(".json"):
                    continue
                dst = os.path.join(cls.PRESETS_DIR, name)
                if os.path.exists(dst):
                    continue
                try:
                    shutil.copy2(os.path.join(legacy, name), dst)
                except Exception:
                    pass        # 单个文件失败不影响其他
        except Exception:
            pass

    @classmethod
    def _sanitize_name(cls, name: str) -> str:
        """将预设名转换为安全的文件名（去除非法字符）"""
        # 替换 Windows 文件名非法字符
        safe = re.sub(r'[<>:"/\\|?*]', '_', name.strip())
        # 去除首尾空格和点
        safe = safe.strip('. ')
        return safe or "unnamed"

    @classmethod
    def list_presets(cls) -> list[str]:
        """列出所有已保存的预设名称（按修改时间倒序）"""
        cls._ensure_dirs()
        if not os.path.isdir(cls.PRESETS_DIR):
            return []

        presets = []
        for f in os.listdir(cls.PRESETS_DIR):
            if f.endswith('.json') and not f.startswith('.'):
                name = f[:-5]  # 去掉 .json 后缀
                filepath = os.path.join(cls.PRESETS_DIR, f)
                mtime = os.path.getmtime(filepath)
                presets.append((name, mtime))

        # 按修改时间倒序排列
        presets.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in presets]

    @classmethod
    def save_preset(cls, name: str, config: WatermarkConfig) -> str:
        """
        保存预设到 JSON 文件。

        Returns:
            保存的文件完整路径
        Raises:
            ValueError: 预设名为空
        """
        if not name or not name.strip():
            raise ValueError("预设名不能为空")

        safe_name = cls._sanitize_name(name)
        cls._ensure_dirs()
        filepath = os.path.join(cls.PRESETS_DIR, f"{safe_name}.json")
        config.save_to_json(filepath)
        return filepath

    @classmethod
    def load_preset(cls, name: str) -> tuple[WatermarkConfig, list[str]]:
        """
        加载预设。

        Returns:
            (config, warnings) 元组。warnings 非空表示有非致命问题（如 Logo 路径失效）。
        Raises:
            FileNotFoundError: 预设不存在
        """
        cls._ensure_dirs()
        safe_name = cls._sanitize_name(name)
        filepath = os.path.join(cls.PRESETS_DIR, f"{safe_name}.json")
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"预设不存在: {name}")

        return load_preset_safe(filepath)

    @classmethod
    def delete_preset(cls, name: str) -> bool:
        """
        删除预设。

        Returns:
            True 如果删除成功，False 如果文件不存在
        """
        cls._ensure_dirs()
        safe_name = cls._sanitize_name(name)
        filepath = os.path.join(cls.PRESETS_DIR, f"{safe_name}.json")
        if os.path.isfile(filepath):
            os.remove(filepath)
            return True
        return False

    @classmethod
    def preset_exists(cls, name: str) -> bool:
        """检查预设是否已存在"""
        cls._ensure_dirs()
        safe_name = cls._sanitize_name(name)
        filepath = os.path.join(cls.PRESETS_DIR, f"{safe_name}.json")
        return os.path.isfile(filepath)
