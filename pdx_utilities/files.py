from pathlib import Path

from .constants import CODE_EXTS, EXCLUDE_PARTS, SCAN_TOPDIRS


def collect_files(root, exts=None, exclude=None, topdirs=None):
    """Enumerate script files under a mod/vanilla root.
    Walks SCAN_TOPDIRS, filters by extension, skips excluded directory parts."""
    exts = exts if exts is not None else CODE_EXTS
    exclude = exclude if exclude is not None else EXCLUDE_PARTS
    topdirs = topdirs if topdirs is not None else SCAN_TOPDIRS
    files = []
    for top in topdirs:
        base = root / top
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in exts:
                continue
            rel = f.relative_to(root).as_posix()
            if exclude.intersection(rel.split("/")):
                continue
            files.append(rel)
    return files
