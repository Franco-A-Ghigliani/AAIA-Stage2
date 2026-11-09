"""Validate replacement text and commit file edits atomically."""

import os
import tempfile
from pathlib import Path

from .errors import ToolError


def _leading_space(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def align_replacement(text: str, old: str, new: str) -> str | None:
    """Find one block ignoring indentation and shift its replacement to the original margin."""
    needle_lines = old.strip("\n").split("\n")
    want = [line.strip() for line in needle_lines]
    lines = text.split("\n")
    hits = [
        i
        for i in range(len(lines) - len(want) + 1)
        if [line.strip() for line in lines[i : i + len(want)]] == want
    ]
    if len(hits) != 1 or not any(want):
        return None
    start = hits[0]
    source_indent, target_indent = _leading_space(needle_lines[0]), _leading_space(lines[start])
    new_lines = (
        []
        if not new.strip()
        else [
            line
            if not line.strip()
            else target_indent
            + (line[len(source_indent) :] if line.startswith(source_indent) else line.lstrip())
            for line in new.strip("\n").split("\n")
        ]
    )
    return "\n".join(lines[:start] + new_lines + lines[start + len(want) :])


def validate_source(path: str, source: str) -> None:
    """Compile Python text without executing it; reject invalid edits with syntax details."""
    if not path.endswith(".py"):
        return
    try:
        compile(source, path, "exec")
    except SyntaxError as exc:
        lines = source.splitlines()
        near = lines[exc.lineno - 1] if exc.lineno and exc.lineno <= len(lines) else ""
        raise ToolError(
            {
                "error": f"{type(exc).__name__}: {exc.msg}; file NOT changed. "
                "Fix the indentation/syntax and retry.",
                "line": exc.lineno,
                "text": near,
            }
        ) from None


def atomic_write(real: Path, text: str) -> None:
    """Write a sibling temporary file, preserve permissions, then replace the destination."""
    handle, temporary = tempfile.mkstemp(dir=real.parent, prefix=".edit-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, real.stat().st_mode)
        os.replace(temporary, real)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
