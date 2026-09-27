import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

from pdx_utilities.paths import find_mod_root, vanilla_root  # noqa: F401
from .constants import (CACHE_DIR, CODE_EXTS, EXCLUDE_PARTS, EXCLUDE_SUBTREES,
                        SCAN_TOPDIRS)

NAV_EXTS = CODE_EXTS | {".yml"}

# Loc files are named *_l_<language>.yml; only English carries definitions.
LOC_LANG_RE = re.compile(r'_l_(\w+)\.yml$', re.IGNORECASE)

# /mnt drives are latency bound (9P), so stats and reads fan out to threads.
IO_THREADS = 16


def cache_dir_for(source, root):
    """Every cache lives on the native filesystem: ripgrep over a local corpus
    is far faster than over /mnt, and rewrites stay cheap."""
    key = hashlib.md5(str(root).encode()).hexdigest()[:12]
    d = CACHE_DIR / (f"mod-{key}" if source == "mod" else key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_foreign_loc(rel):
    m = LOC_LANG_RE.search(rel)
    return bool(m) and m.group(1).lower() != "english"


def keep_path(rel, english_only=False):
    """Shared filter for filesystem walks and git tree listings."""
    parts = rel.split("/")
    if parts[0] not in SCAN_TOPDIRS:
        return False
    if os.path.splitext(rel)[1].lower() not in NAV_EXTS:
        return False
    if EXCLUDE_PARTS.intersection(parts):
        return False
    if any(sub in rel for sub in EXCLUDE_SUBTREES):
        return False
    if english_only and rel.endswith(".yml") and is_foreign_loc(rel):
        return False
    return True


def path_order(rel):
    """Sort key matching pathlib ordering (by component, not raw string)."""
    return rel.split("/")


def _walk(root, top):
    out = []
    stack = [top]
    while stack:
        rel_dir = stack.pop()
        try:
            it = os.scandir(os.path.join(root, rel_dir))
        except OSError:
            continue
        with it:
            for e in it:
                rel = f"{rel_dir}/{e.name}"
                if e.is_dir(follow_symlinks=False):
                    if e.name not in EXCLUDE_PARTS:
                        stack.append(rel)
                elif e.is_file():
                    out.append(rel)
    return out


def scan_tree(root, english_only=False):
    """Return [(rel, (mtime_ns, size))] for every indexable file, in index
    order. One scandir walk plus threaded stats: the stat pass is what makes
    change detection cheap enough to run on every invocation."""
    root = str(root)
    rels = []
    for top in SCAN_TOPDIRS:
        if os.path.isdir(os.path.join(root, top)):
            rels.extend(r for r in _walk(root, top)
                        if keep_path(r, english_only))
    rels.sort(key=path_order)

    def sig(rel):
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    with ThreadPoolExecutor(IO_THREADS) as ex:
        sigs = list(ex.map(sig, rels))
    return [(r, s) for r, s in zip(rels, sigs) if s is not None]


def read_texts(root, rels):
    """Read many files concurrently. Returns {rel: text}; unreadable files
    are left out."""
    def read(rel):
        try:
            return rel, (root / rel).read_text(encoding="utf-8-sig",
                                               errors="replace")
        except OSError:
            return rel, None

    if len(rels) == 1:
        pairs = [read(rels[0])]
    else:
        with ThreadPoolExecutor(IO_THREADS) as ex:
            pairs = list(ex.map(read, rels))
    return {rel: text for rel, text in pairs if text is not None}
