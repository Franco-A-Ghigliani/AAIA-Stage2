"""Bounded, language-aware repository navigation helpers."""

import ast
import os
import subprocess
import tomllib
from collections import Counter
from pathlib import Path


MAX_ENTRIES = 300
MAX_SYMBOLS = 100
MAX_DEPENDENCIES = 100
MAX_SOURCE_BYTES = 1_000_000
IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "traces"}


def _visible_files(root, limit=MAX_ENTRIES):
    root = Path(root)
    found = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in IGNORED_DIRS
            and not name.startswith(".")
            and not (Path(directory) / name).is_symlink()
            and not (Path(directory) / name).is_junction()
        )
        for name in sorted(filenames):
            path = Path(directory) / name
            if name.startswith(".") or path.is_symlink() or path.is_junction():
                continue
            if path.is_file():
                found.append(path.relative_to(root).as_posix())
                if len(found) >= limit:
                    return found, True
    return found, False


def repository_map(root):
    """Return a compact path and language summary."""
    paths, truncated = _visible_files(root)
    counts = Counter(Path(path).suffix.lower() or "[no extension]" for path in paths)
    return {
        "files": paths,
        "file_count": len(paths),
        "truncated": truncated,
        "languages": dict(sorted(counts.items())),
        "modules": sorted({str(Path(path).parent).replace("\\", "/") for path in paths
                           if Path(path).parent != Path(".")}),
    }


def find_python_symbols(root, query):
    """Find matching Python classes and functions with source line numbers."""
    if not query.strip():
        raise ValueError("query must not be empty")
    paths, _ = _visible_files(root)
    results = []
    query_folded = query.casefold()
    for relative in paths:
        if Path(relative).suffix != ".py":
            continue
        try:
            path = Path(root) / relative
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if query_folded in node.name.casefold():
                    results.append({"path": relative, "name": node.name,
                                    "kind": type(node).__name__,
                                    "line": node.lineno})
                    if len(results) >= MAX_SYMBOLS:
                        return results
    return results


def discover_dependencies(root):
    """Read common Python dependency declarations without executing project code."""
    root = Path(root)
    dependencies = []
    manifests = []
    for candidate in ("pyproject.toml", "setup.cfg", "setup.py"):
        path = root / candidate
        if path.is_file():
            manifests.append(candidate)
            if candidate == "pyproject.toml":
                try:
                    if path.stat().st_size > MAX_SOURCE_BYTES:
                        continue
                    data = tomllib.loads(path.read_text(encoding="utf-8"))
                    project = data.get("project", {})
                    dependencies.extend(project.get("dependencies", []))
                    optional = project.get("optional-dependencies", {})
                    for values in optional.values():
                        dependencies.extend(values)
                except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                    pass
    for path in sorted(root.glob("requirements*.txt")):
        if path.is_file() and not path.is_symlink():
            manifests.append(path.name)
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    continue
                dependencies.extend(
                    line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith(("#", "-"))
                )
            except (OSError, UnicodeError):
                pass
    return {"manifests": sorted(set(manifests))[:MAX_DEPENDENCIES],
            "dependencies": sorted(set(str(item)[:300] for item in dependencies))[:MAX_DEPENDENCIES]}


def git_state(root):
    """Read Git identity and tracked working-tree status without running hooks."""
    root = Path(root).resolve(strict=True)

    def git(*args):
        command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                   "-c", "core.hooksPath=/dev/null", "-c", f"safe.directory={root}",
                   "-C", str(root), *args]
        return subprocess.run(command, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10, check=True, text=True, encoding="utf-8")

    commit = git("rev-parse", "HEAD").stdout.strip()
    branch = git("branch", "--show-current").stdout.strip()
    status = git("status", "--short", "--untracked-files=no").stdout.splitlines()
    return {"root": str(root), "commit": commit, "branch": branch,
            "tracked_changes": status[:200], "status_truncated": len(status) > 200,
            "clean": not status}
