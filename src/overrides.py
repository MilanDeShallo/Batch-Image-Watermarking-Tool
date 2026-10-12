"""
参数作用域 — 全局默认 / 本文件专属 / 本页专属 三层参数

背景：
    一批文件往往不是同一套水印参数。有的图要打「样片」、有的要打「内部」、
    有的 PDF 只有某一页需要处理。因此把参数分成三层，
    导出时逐文件、逐页向上查找：页 > 文件 > 全局。

设计要点：
1. 每一层存的是「补丁」（patch）而不是完整配置快照：
   - 只记录该层被改过的字段，未改动的字段继续向上继承
   - 于是修改全局默认时，没被单独设置过的字段会跟着变，不会被旧快照冻住
2. 输出类字段（输出目录/格式/质量/文件名模板）不进入任何一层：
   它们由底部栏统一管理，跟着单个文件走没有意义
3. 本模块不 import 任何 GUI 库，可独立单元测试

解析顺序（后面的覆盖前面的）：
    resolve(path, page) = base  ∪  file[path]  ∪  page[(path, page)]
"""

from __future__ import annotations

from typing import Any, Optional

# 作用范围取值
SCOPE_ALL = "all"    # 全局默认：所有文件共用
SCOPE_FILE = "file"  # 仅本文件：只记给选中的文件
SCOPE_PAGE = "page"  # 仅本页：只记给 PDF 的当前页

SCOPES = (SCOPE_ALL, SCOPE_FILE, SCOPE_PAGE)

SCOPE_LABELS = {
    SCOPE_ALL: "全部文件",
    SCOPE_FILE: "仅本文件",
    SCOPE_PAGE: "仅本页",
}

# 不进入作用域的字段：由底部栏 / 会话统一管理
OUTPUT_FIELDS = frozenset({
    "output_dir",
    "output_format",
    "jpeg_quality",
    "filename_template",
    "overwrite_original",
    "version",
})


def strip_output_fields(data: dict) -> dict:
    """
    剔除输出类字段，只保留水印参数。

    作用域只管"水印长什么样"，不管"存到哪、存成什么格式"。
    """
    return {k: v for k, v in dict(data or {}).items() if k not in OUTPUT_FIELDS}


def _same_value(a: Any, b: Any) -> bool:
    """
    比较两个字段值是否等价。

    JSON 往返会把 tuple 变成 list（color_rgba 就是这种情况），
    若按类型严格比较，会把"其实没改"的字段误判成改动，
    平白多存一条补丁。所以先把 list/tuple 归一化。
    """
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def _page_key(path: str, page: int) -> tuple[str, int]:
    """页码键：统一为 (绝对路径, 页码 0 起)"""
    return (path, int(page))


class ScopeStore:
    """
    三层参数存储。

    - base：全局默认补丁
    - file：{文件路径: 补丁}
    - page：{(文件路径, 页码): 补丁}
    """

    def __init__(self, base: Optional[dict] = None):
        self._base: dict[str, Any] = strip_output_fields(base or {})
        self._file: dict[str, dict[str, Any]] = {}
        self._page: dict[tuple[str, int], dict[str, Any]] = {}

    # ==================== 全局默认层 ====================

    @property
    def base(self) -> dict:
        """全局默认补丁（只读副本）"""
        return dict(self._base)

    def set_base(self, data: dict) -> None:
        """整体替换全局默认（用于启动时登记面板初始状态）"""
        self._base = strip_output_fields(data)

    def update_base(self, patch: dict) -> None:
        """按字段合并进全局默认（保留其他字段）"""
        self._base.update(strip_output_fields(patch))

    # ==================== 本文件 / 本页 层 ====================

    def update_file(self, path: str, patch: dict) -> None:
        """把补丁记到某个文件身上"""
        if not path:
            return
        clean = strip_output_fields(patch)
        if not clean:
            return
        self._file.setdefault(path, {}).update(clean)

    def update_page(self, path: str, page: int, patch: dict) -> None:
        """把补丁记到某个文件的某一页上"""
        if not path:
            return
        clean = strip_output_fields(patch)
        if not clean:
            return
        self._page.setdefault(_page_key(path, page), {}).update(clean)

    def set_file(self, path: str, data: dict) -> None:
        """
        用一份「完整参数」替换该文件的专属参数，只保留与全局默认不同的字段。

        与 update_file 的区别（很重要）：
        面板每次提交的都是完整参数。若像 update_file 那样整份存下来，
        这一层就等于把当时的全局默认冻成快照 —— 用户之后修改全局默认时，
        这个文件不会跟着变（哪怕他只改过文字颜色）。
        只存差异字段，没动过的字段才能继续继承全局。
        """
        if not path:
            return
        patch = self._diff(self._base, data)
        if patch:
            self._file[path] = patch
        else:
            self._file.pop(path, None)

    def set_page(self, path: str, page: int, data: dict) -> None:
        """
        用一份「完整参数」替换某页的专属参数，只保留与父层不同的字段。

        差异是相对"父层"（全局默认 + 本文件层）算的：
        本页没动过的字段继续继承文件层，文件层没动过的继续继承全局默认。
        """
        if not path:
            return
        patch = self._diff(self.resolve(path), data)
        key = _page_key(path, page)
        if patch:
            self._page[key] = patch
        else:
            self._page.pop(key, None)

    @staticmethod
    def _diff(parent: dict, data: dict) -> dict:
        """data 中与 parent 不同的字段（已剔除输出类字段）"""
        clean = strip_output_fields(data)
        return {k: v for k, v in clean.items() if not _same_value(parent.get(k), v)}

    def clear_file(self, path: str) -> bool:
        """清除某文件的专属参数（含其各页的专属参数）。返回是否清掉了东西。"""
        removed = self._file.pop(path, None) is not None
        for key in [k for k in self._page if k[0] == path]:
            del self._page[key]
            removed = True
        return removed

    def clear_page(self, path: str, page: int) -> bool:
        """清除某文件某页的专属参数"""
        return self._page.pop(_page_key(path, page), None) is not None

    def clear_overrides(self) -> None:
        """清空全部文件/页专属参数（保留全局默认）"""
        self._file.clear()
        self._page.clear()

    # ==================== 查询 ====================

    def has_file(self, path: str) -> bool:
        """该文件是否有专属参数"""
        return bool(path) and bool(self._file.get(path))

    def has_page(self, path: str, page: int) -> bool:
        """该文件该页是否有专属参数"""
        return bool(path) and bool(self._page.get(_page_key(path, page)))

    def has_override(self, path: str) -> bool:
        """该文件是否存在任何层级的专属参数（文件层或任意页层）"""
        return self.has_file(path) or bool(self.page_numbers(path))

    def page_numbers(self, path: str) -> list[int]:
        """该文件被单独设置过的页码（升序，0 起）"""
        if not path:
            return []
        return sorted(k[1] for k in self._page if k[0] == path)

    def override_paths(self) -> list[str]:
        """有专属参数的文件路径列表（文件层 + 页层去重）"""
        paths = set(self._file)
        paths.update(k[0] for k in self._page)
        return sorted(paths)

    def deepest_scope_for(self, path: Optional[str], page: Optional[int] = None) -> Optional[str]:
        """
        该目标上"最深"的已存在层级：page > file > None。

        用于选中文件/翻页后自动把作用范围切到那一层，
        避免出现"面板显示的是全局参数，实际导出用的是专属参数"的错觉。
        """
        if not path:
            return None
        if page is not None and self.has_page(path, page):
            return SCOPE_PAGE
        if self.has_file(path):
            return SCOPE_FILE
        return None

    def marker_text(self, path: str) -> str:
        """文件列表里显示的专属标记文案（无专属返回空字符串）"""
        pages = self.page_numbers(path)
        if pages:
            if len(pages) == 1:
                return f"专属 第{pages[0] + 1}页"
            return f"专属 {len(pages)}页"
        if self.has_file(path):
            return "专属"
        return ""

    def markers(self) -> dict[str, str]:
        """{路径: 标记文案}，供文件列表一次性刷新"""
        return {p: self.marker_text(p) for p in self.override_paths()}

    # ==================== 解析 ====================

    def resolve(self, path: Optional[str] = None, page: Optional[int] = None) -> dict:
        """
        解析该目标最终生效的参数字典。

        page > file > base；不存在的层直接跳过。
        缺失字段由 WatermarkConfig 的默认值补齐（在 config_for 中完成）。
        """
        merged: dict[str, Any] = dict(self._base)
        if path:
            merged.update(self._file.get(path, {}))
            if page is not None:
                merged.update(self._page.get(_page_key(path, page), {}))
        return merged

    def config_for(self, path: Optional[str] = None, page: Optional[int] = None):
        """
        解析该目标最终生效的 WatermarkConfig。

        注意：这里是唯一把补丁变成完整配置的地方，
        导出与预览都应当用它，保证两者取到同一套参数。
        """
        from src.config import WatermarkConfig

        return WatermarkConfig.from_dict(self.resolve(path, page))

    # ==================== 清理与持久化 ====================

    def prune(self, valid_paths) -> int:
        """
        丢弃已不在文件列表里的专属参数。

        返回被清理的条目数（文件层 + 页层）。
        """
        keep = set(valid_paths or ())
        removed = 0
        for path in [p for p in self._file if p not in keep]:
            del self._file[path]
            removed += 1
        for key in [k for k in self._page if k[0] not in keep]:
            del self._page[key]
            removed += 1
        return removed

    def to_dict(self) -> dict:
        """序列化为 JSON 可写结构（用于崩溃恢复会话）"""
        return {
            "base": dict(self._base),
            "files": {p: dict(d) for p, d in self._file.items()},
            "pages": [
                {"path": p, "page": n, "data": dict(d)}
                for (p, n), d in self._page.items()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScopeStore":
        """从会话数据恢复（对残缺/非法数据保持容错）"""
        store = cls()
        if not isinstance(data, dict):
            return store

        base = data.get("base")
        if isinstance(base, dict):
            store.set_base(base)

        files = data.get("files")
        if isinstance(files, dict):
            for path, patch in files.items():
                if isinstance(patch, dict):
                    store.update_file(str(path), patch)

        pages = data.get("pages")
        if isinstance(pages, list):
            for item in pages:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                page = item.get("page")
                patch = item.get("data")
                if path is None or not isinstance(patch, dict):
                    continue
                try:
                    page_num = int(page)
                except (TypeError, ValueError):
                    continue
                store.update_page(str(path), page_num, patch)

        return store

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"ScopeStore(base_fields={len(self._base)}, "
            f"files={len(self._file)}, pages={len(self._page)})"
        )
