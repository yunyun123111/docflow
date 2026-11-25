"""Windows 长路径与文件名安全工具。"""
from __future__ import annotations

import os
import re
from pathlib import Path

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_LONG_PREFIX = "\\\\?\\"


def fs_path(p) -> str:
    r"""长路径（>240）自动加 \\?\ 前缀，Windows 下不再受 260 限制。"""
    s = str(p)
    if os.name == "nt" and len(s) >= 240 and not s.startswith(_LONG_PREFIX):
        s = _LONG_PREFIX + (s[2:] if s[1:3] in (":\\", ":/") else s.replace("/", "\\"))
    return s


def safe_filename(name: str, ext: str = "", max_len: int = 120) -> str:
    """清洗为合法文件名：去非法字符/保留字/首尾点空格，限长度。"""
    stem = _ILLEGAL.sub("_", (name or "").strip()).strip(" ._")
    if not stem:
        stem = "未命名"
    stem = stem[:max_len]
    if stem.upper().split(".")[0] in _RESERVED:
        stem = "_" + stem
    ext = (ext or "").lower().strip()
    if ext and not ext.startswith("."):
        ext = "." + ext
    ext = _ILLEGAL.sub("", ext)[:12]
    return stem + ext


def unique_filename(dir_path: Path, name: str) -> Path:
    """目录内防重名：_001/_002 递增，绝不覆盖。"""
    candidate = dir_path / name
    if not candidate.exists():
        return candidate
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    for i in range(1, 10000):
        candidate = dir_path / f"{stem}_{i:03d}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法在 {dir_path} 生成唯一文件名: {name}")


def safe_join(base: Path, *parts: str) -> Path:
    """把用户可控相对路径安全拼接进 base，越界即抛错（防目录穿越/zip-slip）。"""
    base = base.resolve()
    target = Path(base, *parts).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"非法路径（越出 {base}）: {target}")
    return target