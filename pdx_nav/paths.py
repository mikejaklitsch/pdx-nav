import hashlib
from pathlib import Path

from pdx_utilities.paths import find_mod_root, vanilla_root  # noqa: F401
from .constants import (CODE_EXTS, EXCLUDE_PARTS, EXCLUDE_SUBTREES,
                        SCAN_TOPDIRS, VANILLA_CACHE_DIR)


def cache_dir_for(source, root):
    if source == "mod":
        d = root / ".claude" / "pdx_nav"
    else:
        key = hashlib.md5(str(root).encode()).hexdigest()[:12]
        d = VANILLA_CACHE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def collect_files(root):
    files = []
    for top in SCAN_TOPDIRS:
        base = root / top
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in CODE_EXTS and not (ext == ".yml"):
                continue
            rel = f.relative_to(root).as_posix()
            if EXCLUDE_PARTS.intersection(rel.split("/")):
                continue
            if any(sub in rel for sub in EXCLUDE_SUBTREES):
                continue
            files.append(rel)
    return files


def files_hash(root, files):
    h = hashlib.md5()
    for rel in files:
        try:
            st = (root / rel).stat()
            h.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            pass
    return h.hexdigest()
