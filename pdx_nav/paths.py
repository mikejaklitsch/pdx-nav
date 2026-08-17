import hashlib
from pathlib import Path

from pdx_utilities.paths import find_mod_root, vanilla_root  # noqa: F401
from pdx_utilities.files import collect_files as _collect_files
from .constants import (CODE_EXTS, EXCLUDE_SUBTREES, VANILLA_CACHE_DIR)

NAV_EXTS = CODE_EXTS | {".yml"}


def cache_dir_for(source, root):
    if source == "mod":
        d = root / ".claude" / "pdx_nav"
    else:
        key = hashlib.md5(str(root).encode()).hexdigest()[:12]
        d = VANILLA_CACHE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def collect_files(root):
    files = _collect_files(root, exts=NAV_EXTS)
    if EXCLUDE_SUBTREES:
        files = [f for f in files
                 if not any(sub in f for sub in EXCLUDE_SUBTREES)]
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
