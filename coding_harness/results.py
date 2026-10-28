"""Preserve changes from a disposable workspace for human review."""

import difflib
import json
import os
import stat
from pathlib import Path


def snapshot_files(root):
    """Read visible regular files without following links or special files."""
    root = Path(root)
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith('.')
                         and not (Path(directory) / name).is_symlink()
                         and not (Path(directory) / name).is_junction())
        for name in sorted(names):
            path = Path(directory) / name
            if (name.startswith('.') or path.is_symlink() or path.is_junction()
                    or not stat.S_ISREG(path.lstat().st_mode)):
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def export_results(before, root, destination):
    """Save changed file contents, a unified diff, and a deletion manifest."""
    after = snapshot_files(root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    changes = []
    diff = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        status = 'added' if old is None else 'deleted' if new is None else 'modified'
        changes.append({'path': path, 'status': status})
        if new is not None:
            target = destination / 'files' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new)
        try:
            if b'\0' in (old or b'') or b'\0' in (new or b''):
                raise UnicodeError()
            old_lines = (old or b'').decode('utf-8').splitlines(keepends=True)
            new_lines = (new or b'').decode('utf-8').splitlines(keepends=True)
        except UnicodeError:
            diff.append(f'Binary files differ: {path} ({status})\n')
            continue
        for line in difflib.unified_diff(
                old_lines, new_lines,
                fromfile='/dev/null' if old is None else 'a/' + path,
                tofile='/dev/null' if new is None else 'b/' + path):
            diff.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    (destination / 'changes.diff').write_text(''.join(diff), encoding='utf-8')
    (destination / 'changes.json').write_text(
        json.dumps(changes, indent=2) + '\n', encoding='utf-8')
    return changes
