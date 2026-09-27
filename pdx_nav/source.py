"""Indexes (definitions + ripgrep corpus) for the mod, the game install, and
vanilla-tracker versions.

Every index records a per-file signature (mtime/size on disk, blob sha in
git). Refreshes reparse only files whose signature changed and splice the
rest of the corpus from the previous build, so an edit costs one file's
parse rather than a full reindex. Vanilla refreshes (game patch, new tracker
tag) run in a detached background process while queries keep using the
previous index.
"""
import fcntl
import json
import os
import pickle
import subprocess
import sys
import time
from bisect import bisect_right
from contextlib import contextmanager
from pathlib import Path

from pdx_utilities.fileio import strip_bom_bytes
from pdx_utilities.git import git as _git, git_read_file, git_resolve_version
from pdx_utilities.paths import find_vanilla_repo

from .constants import CACHE_DIR
from .parsing import parse_defs
from .paths import (cache_dir_for, find_mod_root, keep_path, path_order,
                    read_texts, scan_tree, vanilla_root)

INDEX_VERSION = 2
INDEX_FILE = "index.pkl"
LOCK_FILE = ".lock"
LOG_FILE = "refresh.log"
# Without a patch sentinel, the game install is re-verified in the background
# at most this often.
REVALIDATE_SECS = 600
# Refreshes that touch fewer files than this stay quiet on stderr.
NOISY_REFRESH = 200

_SEARCHER = None


def resolve_searcher():
    """Return (executable, argv0, is_rg). Prefers real ripgrep, then Claude
    Code's bundled ripgrep (claude binary invoked with argv0='rg'), then
    grep -P as a slow fallback."""
    global _SEARCHER
    if _SEARCHER:
        return _SEARCHER
    import shutil
    rg = shutil.which("rg")
    if rg:
        _SEARCHER = (rg, "rg", True)
        return _SEARCHER
    claude = os.environ.get("CLAUDE_CODE_EXECPATH") or \
        str(Path.home() / ".local" / "bin" / "claude")
    if os.access(claude, os.X_OK):
        _SEARCHER = (claude, "rg", True)
        return _SEARCHER
    _SEARCHER = (shutil.which("grep") or "grep", "grep", False)
    return _SEARCHER


def note(msg):
    sys.stderr.write(f"pdx-nav: {msg}\n")


# --- cache files ---

@contextmanager
def cache_lock(cache, blocking=True):
    """Exclusive per-cache lock so concurrent invocations never build the same
    index twice. Yields False when non-blocking and already held."""
    fd = os.open(cache / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def lock_held(cache):
    with cache_lock(cache, blocking=False) as got:
        return not got


def load_index(cache):
    try:
        with open(cache / INDEX_FILE, "rb") as f:
            idx = pickle.load(f)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError,
            ImportError, ValueError):
        return None
    if not isinstance(idx, dict) or idx.get("schema") != INDEX_VERSION:
        return None
    if not (cache / idx["corpus"]).exists():
        return None
    return idx


def load_legacy_index(cache):
    """Version-1 caches (index.json + corpus.txt) are still servable while a
    background refresh replaces them."""
    try:
        idx = json.loads((cache / "index.json").read_text())
    except (OSError, ValueError):
        return None
    if not (cache / "corpus.txt").exists():
        return None
    idx["defs"] = [tuple(d) for d in idx.get("defs", [])]
    idx["corpus"] = "corpus.txt"
    idx["legacy"] = True
    return idx


def _write_index(cache, idx, chunks):
    """Atomically publish a corpus generation, then the index that points at
    it. The previous generation is kept for readers still mid-query."""
    gen = idx["gen"]
    if chunks is not None:
        name = f"corpus.{gen}.txt"
        tmp = cache / f"{name}.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.writelines(chunks)
        os.replace(tmp, cache / name)
        idx["corpus"] = name
    tmp = cache / f"{INDEX_FILE}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, cache / INDEX_FILE)
    for p in cache.glob("corpus.*.txt"):
        try:
            g = int(p.name.split(".")[1])
        except (IndexError, ValueError):
            continue
        if g < gen - 1:
            p.unlink(missing_ok=True)
    for legacy in ("index.json", "corpus.txt"):
        (cache / legacy).unlink(missing_ok=True)


def _corpus_chunk(fi, text):
    prefix = f"{fi}:"
    return "".join(f"{prefix}{ln}:{line.rstrip(chr(13))}\n"
                   for ln, line in enumerate(text.split("\n"), 1)
                   if line.strip()).encode("utf-8")


def _renumber(chunk, old, new):
    old_p, new_p = f"{old}:".encode(), f"{new}:".encode()
    return new_p + bytes(chunk[len(old_p):]).replace(b"\n" + old_p,
                                                     b"\n" + new_p)


def build(cache, listing, read_many, label, meta, prev=None, prev_cache=None):
    """Write an index for `listing` ([(rel, sig)]). Files whose sig matches
    `prev` reuse its defs and corpus lines; the rest are read and parsed."""
    prev_cache = prev_cache or cache
    files = [rel for rel, _ in listing]
    sigs = [sig for _, sig in listing]
    prev_pos = {}
    if prev and "sigs" in prev:
        prev_pos = {rel: j for j, rel in enumerate(prev["files"])}
    reuse, parse = {}, []
    for fi, (rel, sig) in enumerate(listing):
        j = prev_pos.get(rel)
        if j is not None and prev["sigs"][j] == sig:
            reuse[fi] = j
        else:
            parse.append(fi)

    own = prev if prev is not None and prev_cache == cache else load_index(cache)
    gen = (own["gen"] + 1) if own else 1
    idx = {"schema": INDEX_VERSION, "files": files, "sigs": sigs,
           "gen": gen, "built": time.time(), **meta}

    if (not parse and own is prev and prev is not None
            and files == prev["files"]):
        idx.update(defs=prev["defs"], offsets=prev["offsets"],
                   corpus=prev["corpus"], gen=prev["gen"])
        _write_index(cache, idx, None)
        return idx

    if len(parse) >= NOISY_REFRESH:
        note(f"indexing {label} ({len(parse)} of {len(files)} files)...")
    texts = read_many([files[fi] for fi in parse]) if parse else {}

    prev_corpus = b""
    prev_defs = {}
    if reuse:
        prev_corpus = memoryview((prev_cache / prev["corpus"]).read_bytes())
        for d in prev["defs"]:
            prev_defs.setdefault(d[2], []).append(d)

    defs, offsets, chunks = [], [], []
    pos = 0
    for fi, rel in enumerate(files):
        j = reuse.get(fi)
        if j is not None:
            s, e = prev["offsets"][j]
            chunk = prev_corpus[s:e]
            if j != fi and e > s:
                chunk = _renumber(chunk, j, fi)
            for d in prev_defs.get(j, ()):
                defs.append((d[0], d[1], fi, d[3], d[4], d[5]))
        else:
            text = texts.get(rel)
            if text is None:
                chunk = b""
            else:
                for name, kind, start, end, flag in parse_defs(text, rel):
                    defs.append((name, kind, fi, start, end, flag))
                chunk = _corpus_chunk(fi, text)
        offsets.append((pos, pos + len(chunk)))
        pos += len(chunk)
        chunks.append(chunk)

    idx.update(defs=defs, offsets=offsets)
    _write_index(cache, idx, chunks)
    return idx


# --- background refresh ---

def spawn_refresh(cache, *argv):
    """Start a detached refresher unless one already holds the cache lock."""
    if lock_held(cache):
        return
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (pkg_parent, env.get("PYTHONPATH")) if p)
    try:
        log = open(cache / LOG_FILE, "wb")
        subprocess.Popen(
            [sys.executable, "-m", "pdx_nav", "_refresh", *map(str, argv)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=env, cwd=str(cache))
    except OSError:
        pass


def refresh_main(argv):
    """Entry point for `pdx-nav _refresh <kind> ...` (background process)."""
    kind = argv[0] if argv else ""
    if kind == "vanilla":
        root = Path(argv[1])
        cache = cache_dir_for("vanilla", root)
        with cache_lock(cache, blocking=False) as got:
            if got:
                _refresh_vanilla(root, cache)
    elif kind == "tracker":
        repo, version, commit = Path(argv[1]), argv[2], argv[3]
        cache = tracker_cache(version)
        with cache_lock(cache, blocking=False) as got:
            if got:
                _refresh_tracker(repo, version, commit, cache)
    else:
        return 2
    return 0


# --- mod ---

def _cleanup_legacy_mod_cache(root):
    """Version 1 kept mod caches inside the mod (<mod>/.claude/pdx_nav)."""
    old = root / ".claude" / "pdx_nav"
    if not old.is_dir():
        return
    for name in ("index.json", "corpus.txt", "graph.json", "validated_at"):
        try:
            (old / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        old.rmdir()
    except OSError:
        pass


def mod_index(root, force=False):
    """The mod index is always exact: stat every file (cheap, threaded) and
    reparse only what changed since the last build."""
    cache = cache_dir_for("mod", root)
    _cleanup_legacy_mod_cache(root)
    listing = scan_tree(root)
    files = [r for r, _ in listing]
    sigs = [s for _, s in listing]

    def fresh(idx):
        return (idx and not force and idx["files"] == files
                and idx["sigs"] == sigs)

    idx = load_index(cache)
    if fresh(idx):
        return idx, cache
    with cache_lock(cache):
        idx = load_index(cache)
        if fresh(idx):
            return idx, cache
        idx = build(cache, listing, lambda rels: read_texts(root, rels),
                    "mod", {"root": str(root)}, prev=None if force else idx)
    return idx, cache


# --- game install ---

def vanilla_sentinel(root):
    """Cheap patch detector: the install's checksum.txt changes every patch."""
    p = (root / ".." / "binaries" / "checksum.txt").resolve()
    if not p.is_file():
        return None
    st = p.stat()
    return [str(p), st.st_mtime_ns, st.st_size,
            p.read_text(errors="replace")[:512]]


def _refresh_vanilla(root, cache, force=False):
    prev = None if force else load_index(cache)
    sentinel = vanilla_sentinel(root)
    listing = scan_tree(root, english_only=True)
    return build(cache, listing, lambda rels: read_texts(root, rels),
                 "vanilla", {"root": str(root), "sentinel": sentinel,
                             "validated": time.time()}, prev=prev)


def vanilla_index(root, force=False):
    """Serve the existing install index immediately. A patch (sentinel change)
    or periodic re-verification refreshes it in the background; only the
    very first build blocks."""
    cache = cache_dir_for("vanilla", root)
    if not force:
        idx = load_index(cache)
        if idx:
            sentinel = vanilla_sentinel(root)
            if sentinel is not None:
                if idx.get("sentinel") == sentinel:
                    return idx, cache
                note("game patch detected; vanilla index refreshing in the "
                     "background (results may lag until it finishes)")
                spawn_refresh(cache, "vanilla", root)
                return idx, cache
            if time.time() - idx.get("validated", 0) > REVALIDATE_SECS:
                spawn_refresh(cache, "vanilla", root)
            return idx, cache
        legacy = load_legacy_index(cache)
        if legacy:
            spawn_refresh(cache, "vanilla", root)
            return legacy, cache
    with cache_lock(cache):
        idx = None if force else load_index(cache)
        if idx is None:
            idx = _refresh_vanilla(root, cache, force=force)
    return idx, cache


# --- vanilla-tracker (bare git repo) ---

def tracker_cache(version):
    d = CACHE_DIR / f"tracker-{version}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tracker_tags(repo):
    """[(tag, commit)] newest first. Cached against the repo's ref files so
    ordinary queries skip the git subprocesses entirely."""
    key = []
    for p in (repo / "packed-refs", repo / "refs" / "tags"):
        try:
            key.append(p.stat().st_mtime_ns)
        except OSError:
            key.append(0)
    path = CACHE_DIR / "tracker-refs.json"
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = {}
    entry = cached.get(str(repo))
    if entry and entry.get("key") == key:
        return [tuple(t) for t in entry["tags"]]
    out = _git(repo, "for-each-ref", "--sort=-creatordate",
               "--format=%(refname:short)\t%(objectname)\t%(*objectname)",
               "refs/tags")
    tags = []
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) == 3 and parts[0]:
            tags.append((parts[0], parts[2] or parts[1]))
    if not tags:
        return tags
    cached[str(repo)] = {"key": key, "tags": tags}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(cached))
        os.replace(tmp, path)
    except OSError:
        pass
    return tags


def tracker_listing(repo, commit):
    out = _git(repo, "ls-tree", "-r", "--full-tree", commit, timeout=60)
    listing = []
    for line in out.split("\n"):
        meta, _, rel = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        if keep_path(rel, english_only=True):
            listing.append((rel, parts[2]))
    listing.sort(key=lambda x: path_order(x[0]))
    return listing


def git_read_blobs(repo, shas):
    """{sha: text} via one `git cat-file --batch` round trip."""
    if not shas:
        return {}
    try:
        r = subprocess.run(["git", f"--git-dir={repo}", "cat-file", "--batch"],
                           input="\n".join(shas).encode() + b"\n",
                           capture_output=True, timeout=600)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    data, pos, out = r.stdout, 0, {}
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].split()
        pos = nl + 1
        if len(header) != 3:
            continue
        size = int(header[2])
        out[header[0].decode()] = strip_bom_bytes(data[pos:pos + size])
        pos += size + 1
    return out


def _newest_tracker_index(exclude=None):
    best = None
    for d in CACHE_DIR.glob("tracker-*"):
        if d == exclude or not d.is_dir():
            continue
        idx = load_index(d)
        if idx and (best is None or idx["built"] > best[0]["built"]):
            best = (idx, d)
    return best


def _refresh_tracker(repo, version, commit, cache, force=False):
    prev, prev_cache = None, None
    if not force:
        prev = load_index(cache)
        prev_cache = cache
        if prev is None:
            # Blob shas are content hashes, so another version's index can
            # donate every file this version didn't change.
            other = _newest_tracker_index(exclude=cache)
            if other:
                prev, prev_cache = other
        elif prev.get("commit") == commit:
            return prev
    listing = tracker_listing(repo, commit)
    sha_of = dict(listing)

    def read_many(rels):
        blobs = git_read_blobs(repo, [sha_of[r] for r in rels])
        return {r: blobs[sha_of[r]] for r in rels if sha_of[r] in blobs}

    return build(cache, listing, read_many, f"vanilla-tracker {version}",
                 {"commit": commit, "repo": str(repo), "ref": version,
                  "version": version},
                 prev=prev, prev_cache=prev_cache)


def tracker_index(repo, version, commit, explicit=False, force=False):
    """Return (idx, cache, served_version). A moved tag or new latest version
    builds in the background while the closest existing index answers."""
    cache = tracker_cache(version)
    if not force:
        idx = load_index(cache)
        if idx and idx.get("commit") == commit:
            return idx, cache, version
        stale = (idx or load_legacy_index(cache), cache, version)
        if stale[0] is None and not explicit:
            other = _newest_tracker_index(exclude=cache)
            if other:
                stale = (other[0], other[1], other[0].get("version", "?"))
        if stale[0] is not None:
            if stale[2] != version:
                note(f"vanilla@{version} index building in the background; "
                     f"answering from vanilla@{stale[2]} until it finishes")
            spawn_refresh(cache, "tracker", repo, version, commit)
            return stale
    with cache_lock(cache):
        idx = _refresh_tracker(repo, version, commit, cache, force=force)
    return idx, cache, version


# --- sources ---

class Source:
    def __init__(self, name, root, idx, cache):
        self.name = name
        self.root = root
        self.idx = idx
        self.corpus = cache / idx["corpus"]
        self.files = idx["files"]
        self.by_name = {}
        for d in idx["defs"]:
            self.by_name.setdefault(d[0], []).append(d)
        self._spans = None
        self._outer = None

    @property
    def spans_by_file(self):
        if self._spans is None:
            spans = {}
            for d in self.idx["defs"]:
                spans.setdefault(d[2], []).append((d[3], d[4], d[0], d[1]))
            for v in spans.values():
                v.sort()
            self._spans = spans
        return self._spans

    def _outer_spans(self, file_i):
        """Spans not nested in another (GUI `types` blocks hold inner types)."""
        if self._outer is None:
            self._outer = {}
        out = self._outer.get(file_i)
        if out is None:
            out, end = [], 0
            for sp in self.spans_by_file.get(file_i, []):
                if sp[0] > end:
                    out.append(sp)
                    end = sp[1]
            self._outer[file_i] = out
        return out

    def read_file(self, fi):
        path = self.root / self.files[fi]
        return path.read_text(encoding="utf-8-sig", errors="replace")

    def enclosing(self, file_i, line):
        """Innermost definition span containing `line`, or None."""
        key = (line, float("inf"))
        for spans in (self.spans_by_file.get(file_i, []),
                      self._outer_spans(file_i)):
            i = bisect_right(spans, key) - 1
            if i >= 0 and spans[i][0] <= line <= spans[i][1]:
                return spans[i]
        return None

    def rg(self, pattern, fixed=False, word=True):
        exe, argv0, is_rg = resolve_searcher()
        if is_rg:
            cmd = [argv0, "--no-heading", "-N"]
            if word:
                cmd.append("-w")
            if fixed:
                cmd.append("-F")
        else:
            cmd = [argv0, "-P" if not fixed else "-F"]
            if word:
                cmd.append("-w")
        cmd += ["-e", pattern, str(self.corpus)]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=60, executable=exe)
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
            return []
        hits = []
        for row in out.stdout.split("\n"):
            if not row:
                continue
            try:
                fi, ln, text = row.split(":", 2)
                hits.append((int(fi), int(ln), text))
            except ValueError:
                continue
        return hits


class GitSource(Source):
    """A Source backed by a bare git repo (vanilla-tracker) at a specific ref."""

    def __init__(self, repo, version_label, idx, cache):
        super().__init__(f"vanilla@{version_label}", None, idx, cache)
        self._repo = repo
        self._ref = idx.get("commit") or version_label
        self._version = version_label

    def read_file(self, fi):
        text = git_read_file(self._repo, self._ref, self.files[fi])
        return text if text is not None else ""


def resolve_tracker_version(repo, at_version):
    """Return (version, commit) or (None, None) when the tracker has no tags.
    Exits on an unknown --at version."""
    tags = tracker_tags(repo)
    if not at_version:
        return tags[0] if tags else (None, None)
    for tag, commit in tags:
        if tag == at_version:
            return tag, commit
    if git_resolve_version(repo, at_version) is None:
        sys.exit(1)
    out = _git(repo, "rev-parse", "--verify", f"{at_version}^{{commit}}")
    return at_version, out.strip() or at_version


def load_sources(need_mod=True, at_version=None):
    sources = []
    mod = find_mod_root()
    if mod and need_mod:
        idx, cache = mod_index(mod)
        sources.append(Source("mod", mod, idx, cache))

    tracker = find_vanilla_repo(mod)
    if tracker:
        version, commit = resolve_tracker_version(tracker, at_version)
        if version:
            idx, cache, served = tracker_index(tracker, version, commit,
                                               explicit=bool(at_version))
            sources.append(GitSource(tracker, served, idx, cache))
        else:
            note("vanilla-tracker has no tags, falling back to game install")
            v = vanilla_root()
            if v.is_dir():
                idx, cache = vanilla_index(v)
                sources.append(Source("vanilla", v, idx, cache))
    else:
        if at_version:
            print("Error: --at requires a vanilla-tracker repo. None found.\n"
                  "  Set $PDX_VANILLA_REPO or place repo.git at "
                  "<mod-parent>/vanilla-tracker/repo.git", file=sys.stderr)
            sys.exit(1)
        v = vanilla_root()
        if v.is_dir():
            idx, cache = vanilla_index(v)
            sources.append(Source("vanilla", v, idx, cache))

    if not sources:
        print("Error: no mod root (.metadata/) found and vanilla root missing",
              file=sys.stderr)
        sys.exit(1)
    return sources
