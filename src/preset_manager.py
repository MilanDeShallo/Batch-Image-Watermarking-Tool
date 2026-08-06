"""
预设管理器 — 保存/加载/删除水印配置预设

存储格式：JSON 文件，每个预设一个文件
存储位置：项目目录下 presets/
文件名：{预设名}.json（预设名中的非法字符会被替换为下划线）
"""

from __future__ import annotations

import os
import re
from typing import Optional

from src.config import WatermarkConfig, load_preset_safe


class PresetManager:
    """预设管理器 — 管理 presets/ 目录下的 JSON 配置文件"""

    # 预设存储目录（相对于项目根目录）
    PRESETS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "presets"
    )

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
        os.makedirs(cls.PRESETS_DIR, exist_ok=True)
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
        safe_name = cls._sanitize_name(name)
        filepath = os.path.join(cls.PRESETS_DIR, f"{safe_name}.json")
        if os.path.isfile(filepath):
            os.remove(filepath)
            return True
        return False

    @classmethod
    def preset_exists(cls, name: str) -> bool:
        """检查预设是否已存在"""
        safe_name = cls._sanitize_name(name)
        filepath = os.path.join(cls.PRESETS_DIR, f"{safe_name}.json")
        return os.path.isfile(filepath)
