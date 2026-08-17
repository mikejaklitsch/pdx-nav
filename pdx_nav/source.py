import hashlib
import json
import os
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path

from .constants import CODE_EXTS, SCAN_TOPDIRS, EXCLUDE_PARTS, EXCLUDE_SUBTREES, VANILLA_CACHE_DIR
from .parsing import parse_defs
from .paths import cache_dir_for, collect_files, files_hash, find_mod_root, vanilla_root


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


def vanilla_sentinel(root):
    """Cheap patch detector: the install's checksum.txt changes every patch."""
    p = (root / ".." / "binaries" / "checksum.txt").resolve()
    if not p.is_file():
        return None
    st = p.stat()
    return [str(p), st.st_mtime_ns, st.st_size,
            p.read_text(errors="replace")[:512]]


def build_index(source, root):
    cache = cache_dir_for(source, root)
    idx_path = cache / "index.json"
    corpus_path = cache / "corpus.txt"

    files = None
    if idx_path.exists() and corpus_path.exists():
        try:
            idx = json.loads(idx_path.read_text())
        except (json.JSONDecodeError, KeyError):
            idx = None
        if idx:
            if source == "vanilla":
                sent = vanilla_sentinel(root)
                if sent is not None:
                    if idx.get("sentinel") == sent:
                        return idx, corpus_path
                else:
                    files = collect_files(root)
                    if idx.get("hash") == files_hash(root, files):
                        return idx, corpus_path
            else:
                files = collect_files(root)
                if idx.get("hash") == files_hash(root, files):
                    return idx, corpus_path

    if files is None:
        files = collect_files(root)
    fhash = files_hash(root, files)

    sys.stderr.write(f"pdx-nav: indexing {source} ({len(files)} files)...\n")
    defs = []
    corpus_lines = []
    for fi, rel in enumerate(files):
        try:
            text = (root / rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for (name, kind, start, end, flag) in parse_defs(text, rel):
            defs.append([name, kind, fi, start, end, flag])
        if Path(rel).suffix.lower() in CODE_EXTS or rel.endswith(".yml"):
            for ln, line in enumerate(text.split("\n"), 1):
                if line.strip():
                    corpus_lines.append(f"{fi}:{ln}:{line}")

    idx = {"hash": fhash, "root": str(root), "files": files, "defs": defs}
    if source == "vanilla":
        idx["sentinel"] = vanilla_sentinel(root)
    idx_path.write_text(json.dumps(idx, separators=(",", ":")))
    corpus_path.write_text("\n".join(corpus_lines), encoding="utf-8")
    return idx, corpus_path


class Source:
    def __init__(self, name, root):
        self.name = name
        self.root = root
        self.idx, self.corpus = build_index(name, root)
        self._init_lookups()

    def _init_lookups(self):
        self.files = self.idx["files"]
        self.by_name = {}
        self.spans_by_file = {}
        for d in self.idx["defs"]:
            self.by_name.setdefault(d[0], []).append(d)
            self.spans_by_file.setdefault(d[2], []).append((d[3], d[4], d[0], d[1]))
        for spans in self.spans_by_file.values():
            spans.sort()

    def read_file(self, fi):
        path = self.root / self.files[fi]
        return path.read_text(encoding="utf-8-sig", errors="replace")

    def enclosing(self, file_i, line):
        spans = self.spans_by_file.get(file_i, [])
        starts = [s[0] for s in spans]
        i = bisect_right(starts, line) - 1
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
        cmd += [pattern, str(self.corpus)]
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


# --- vanilla-tracker (bare git repo) ---

def find_vanilla_tracker(mod_root=None):
    """Discover the vanilla-tracker bare git repo."""
    env = os.environ.get("PDX_VANILLA_REPO")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p

    if mod_root:
        candidate = mod_root.parent / "vanilla-tracker" / "repo.git"
        if candidate.exists():
            return candidate

    return None


def tracker_tags(repo):
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "tag", "-l",
             "--format=%(refname:short) %(objectname:short)"],
            capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    tags = {}
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            tags[parts[0]] = parts[1]
    return tags


def tracker_latest_tag(repo):
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "tag", "--sort=-creatordate"],
            capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in r.stdout.strip().split("\n"):
        if line.strip():
            return line.strip()
    return None


def tracker_resolve_ref(repo, version):
    tags = tracker_tags(repo)
    if version in tags:
        return version
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "rev-parse", "--verify",
             f"{version}^{{commit}}"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return version
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    import difflib
    close = difflib.get_close_matches(version, list(tags.keys()), n=3, cutoff=0.5)
    hint = f"; did you mean: {', '.join(close)}" if close else \
        f"; available: {', '.join(sorted(tags.keys()))}"
    print(f"Error: unknown version '{version}'{hint}", file=sys.stderr)
    return None


def tracker_collect_files(repo, ref):
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "ls-tree", "-r", "--name-only", ref],
            capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    files = []
    for line in r.stdout.split("\n"):
        rel = line.strip()
        if not rel:
            continue
        parts = rel.split("/")
        if parts[0] not in SCAN_TOPDIRS:
            continue
        ext = Path(rel).suffix.lower()
        if ext not in CODE_EXTS and ext != ".yml":
            continue
        if EXCLUDE_PARTS.intersection(parts):
            continue
        if any(sub in rel for sub in EXCLUDE_SUBTREES):
            continue
        files.append(rel)
    return sorted(files)


def tracker_read_file(repo, ref, rel):
    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "show", f"{ref}:{rel}"],
            capture_output=True, timeout=30)
        if r.returncode != 0:
            return None
        raw = r.stdout
        if raw[:3] == b'\xef\xbb\xbf':
            raw = raw[3:]
        return raw.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def build_tracker_index(repo, ref, version_label):
    cache = VANILLA_CACHE_DIR / f"tracker-{version_label}"
    cache.mkdir(parents=True, exist_ok=True)
    idx_path = cache / "index.json"
    corpus_path = cache / "corpus.txt"

    try:
        r = subprocess.run(
            ["git", f"--git-dir={repo}", "rev-parse", ref],
            capture_output=True, text=True, timeout=10)
        commit_hash = r.stdout.strip() if r.returncode == 0 else ref
    except (subprocess.TimeoutExpired, FileNotFoundError):
        commit_hash = ref

    if idx_path.exists() and corpus_path.exists():
        try:
            idx = json.loads(idx_path.read_text())
            if idx.get("commit") == commit_hash:
                return idx, corpus_path
        except (json.JSONDecodeError, KeyError):
            pass

    files = tracker_collect_files(repo, ref)
    sys.stderr.write(
        f"pdx-nav: indexing vanilla-tracker {version_label} ({len(files)} files)...\n")

    import tarfile
    import io
    file_contents = {}
    if files:
        try:
            r = subprocess.run(
                ["git", f"--git-dir={repo}", "archive", ref] + files,
                capture_output=True, timeout=120)
            if r.returncode == 0:
                tf = tarfile.open(fileobj=io.BytesIO(r.stdout))
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    raw = tf.extractfile(member).read()
                    if raw[:3] == b'\xef\xbb\xbf':
                        raw = raw[3:]
                    file_contents[member.name] = raw.decode("utf-8", errors="replace")
                tf.close()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        except Exception:
            pass

    for rel in files:
        if rel not in file_contents:
            text = tracker_read_file(repo, ref, rel)
            if text is not None:
                file_contents[rel] = text

    defs = []
    corpus_lines = []
    for fi, rel in enumerate(files):
        text = file_contents.get(rel)
        if text is None:
            continue
        for (name, kind, start, end, flag) in parse_defs(text, rel):
            defs.append([name, kind, fi, start, end, flag])
        if Path(rel).suffix.lower() in CODE_EXTS or rel.endswith(".yml"):
            for ln, line in enumerate(text.split("\n"), 1):
                if line.strip():
                    corpus_lines.append(f"{fi}:{ln}:{line}")

    idx = {"commit": commit_hash, "repo": str(repo), "ref": ref,
           "version": version_label, "files": files, "defs": defs}
    idx_path.write_text(json.dumps(idx, separators=(",", ":")))
    corpus_path.write_text("\n".join(corpus_lines), encoding="utf-8")
    return idx, corpus_path


class GitSource(Source):
    """A Source backed by a bare git repo (vanilla-tracker) at a specific ref."""

    def __init__(self, repo, ref, version_label):
        self.name = f"vanilla@{version_label}"
        self.root = None
        self._repo = repo
        self._ref = ref
        self._version = version_label
        self.idx, self.corpus = build_tracker_index(repo, ref, version_label)
        self._init_lookups()

    def read_file(self, fi):
        text = tracker_read_file(self._repo, self._ref, self.files[fi])
        return text if text is not None else ""


def load_sources(need_mod=True, at_version=None):
    sources = []
    mod = find_mod_root()
    if mod and need_mod:
        sources.append(Source("mod", mod))

    tracker = find_vanilla_tracker(mod)
    if tracker:
        version = at_version
        if not version:
            version = tracker_latest_tag(tracker)
        if version:
            ref = tracker_resolve_ref(tracker, version)
            if ref is None:
                sys.exit(1)
            sources.append(GitSource(tracker, ref, version))
        else:
            sys.stderr.write("pdx-nav: vanilla-tracker has no tags, "
                             "falling back to game install\n")
            v = vanilla_root()
            if v.is_dir():
                sources.append(Source("vanilla", v))
    else:
        if at_version:
            print("Error: --at requires a vanilla-tracker repo. None found.\n"
                  "  Set $PDX_VANILLA_REPO or place repo.git at "
                  "<mod-parent>/vanilla-tracker/repo.git", file=sys.stderr)
            sys.exit(1)
        v = vanilla_root()
        if v.is_dir():
            sources.append(Source("vanilla", v))

    if not sources:
        print("Error: no mod root (.metadata/) found and vanilla root missing",
              file=sys.stderr)
        sys.exit(1)
    return sources
