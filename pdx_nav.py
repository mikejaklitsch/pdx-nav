#!/usr/bin/env python3
"""pdx-nav: IDE-style navigation for EU5 PDX script (mod + vanilla).

Definitions, references, callers, call stacks, and variable flows — every
result carries the enclosing definition and its exact line span so the next
step (Read / show) is surgical, never another broad grep.

Usage:
    pdx-nav def <name>...             Where is it defined (all sources, overrides included)
    pdx-nav show <name>               Print the definition block (--vanilla/--mod, -n max lines)
    pdx-nav refs <name>...            All references, role-tagged, grouped by file
    pdx-nav callers <name>            Definitions that reference <name> (one level up)
    pdx-nav stack <name>              Transitive caller tree up to engine entry points
    pdx-nav vars <name>               Variable write/read sites
    pdx-nav list <glob>               List definitions matching a glob (--kind to filter)
    pdx-nav list --file <path>        List all definitions in one file (file order)
    pdx-nav grep <pattern>            Full-text search over indexed symbol names
    pdx-nav graph                      Dependency graph: trees, orphans, cross-system edges
    pdx-nav rebuild                   Force index + corpus rebuild

    pdx-nav --at 1.2.5 def <name>    Navigate vanilla at a specific version

Vanilla source: when a vanilla-tracker bare git repo is found ($PDX_VANILLA_REPO
or <mod-parent>/vanilla-tracker/repo.git), it is used instead of the live game
install. --at <version> selects a tracker tag; without it, the latest tag is
used. Falls back to the game install when no tracker is found.

Architecture: definitions and block spans are indexed once and cached
(mod: <mod>/.claude/pdx_nav/, vanilla: ~/.cache/pdx-nav/tracker-<version>/,
invalidated by commit hash). Reference queries run ripgrep over a locally
cached corpus copy, then each hit is classified (code/string/comment) and
enriched with its enclosing block.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path

DEFAULT_VANILLA_ROOT = Path(
    "/mnt/d/Program Files (x86)/Steam/steamapps/common/Europa Universalis V/game")
VANILLA_CACHE_DIR = Path.home() / ".cache" / "pdx-nav"

SCAN_TOPDIRS = ("in_game", "main_menu", "loading_screen")
EXCLUDE_PARTS = {".git", ".claude", ".gui_workspace", "__pycache__", "tools",
                 ".metadata", ".dev-mods"}
EXCLUDE_SUBTREES = ("gfx/map",)  # generated map-object blobs: huge, nav-useless
CODE_EXTS = {".txt", ".gui"}

KIND_DISPLAY = {
    "scripted_effects": "effect",
    "scripted_triggers": "trigger",
    "script_values": "script_value",
    "on_action": "on_action",
    "scripted_guis": "scripted_gui",
    "building_types": "building",
    "production_methods": "production_method",
    "government_reforms": "reform",
    "estate_privileges": "privilege",
    "casus_belli": "casus_belli",
    "subject_types": "subject_type",
    "generic_actions": "generic_action",
}

DEF_RE = re.compile(r'^\s*(INJECT:|REPLACE:|TRY_INJECT:)?([A-Za-z_@][\w.]*)\s*[={]')
GUI_DEF_RE = re.compile(r'^\s*(template|types|type|local_template)\s+(\w+)')
LOC_DEF_RE = re.compile(r'^\s*([\w.\-]+):\d*\s*"')

VAR_WRITE_FAMILY = re.compile(
    r'\b(?:set|change|round|clamp|divide|multiply|subtract_from|add_to|remove'
    r'|remove_from|cmf_change)_?\w*variable\w*\s*=')

ENTRY_KINDS = {"on_action"}
ENGINE_EVALUATED_KINDS = {"on_action", "auto_modifiers", "static_modifiers",
                          "pop_types", "location_ranks", "modifier_type_definitions",
                          "game_concepts", "modifier_icons", "data_binding",
                          "types"}


# --- shared scanner (same semantics as pdx-tag / pdx-format --brace) ---

def split_line(line, in_string=False):
    """Split into (code, comment, masked, in_string_after). Comment starts at
    any '#' outside a string; masked blanks string contents (same length as
    code) so brace/word positions index into code."""
    masked = []
    for i, ch in enumerate(line):
        if ch == '"':
            in_string = not in_string
            masked.append(' ')
        elif in_string:
            masked.append(' ')
        elif ch == '#':
            return line[:i], line[i:], ''.join(masked), in_string
        else:
            masked.append(ch)
    return line, '', ''.join(masked), in_string


# --- roots & cache paths ---

def find_mod_root(start=None):
    p = (start or Path.cwd()).resolve()
    while p != p.parent:
        if (p / ".metadata").is_dir():
            return p
        p = p.parent
    return None


def vanilla_root():
    env = os.environ.get("PDX_GAME_ROOT")
    return Path(env) if env else DEFAULT_VANILLA_ROOT


def cache_dir_for(source, root):
    if source == "mod":
        d = root / ".claude" / "pdx_nav"
    else:
        key = hashlib.md5(str(root).encode()).hexdigest()[:12]
        d = VANILLA_CACHE_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- file collection ---

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


# --- definition parsing ---

def kind_for_path(rel):
    parts = rel.split("/")
    if len(parts) >= 2 and parts[1] == "localization":
        return "loc"
    if len(parts) >= 2 and parts[1] == "events":
        return "event"
    if rel.endswith(".gui"):
        return "gui"
    if len(parts) >= 3 and parts[1] == "common":
        return KIND_DISPLAY.get(parts[2], parts[2])
    return parts[1] if len(parts) >= 2 else parts[0]


def parse_defs(text, rel):
    """Return list of (name, kind, start, end, flag). Top-level blocks only;
    span end is the line where the block's brace closes."""
    kind = kind_for_path(rel)
    defs = []

    if kind == "loc":
        if "l_english" not in rel:
            return []
        for i, line in enumerate(text.split("\n"), 1):
            m = LOC_DEF_RE.match(line)
            if m and not line.lstrip().startswith("l_"):
                defs.append((m.group(1), "loc", i, i, ""))
        return defs

    depth = 0
    in_string = False
    pending = None  # (name, kindname, flag, start_line)
    for i, line in enumerate(text.split("\n"), 1):
        code, _, masked, in_string = split_line(line, in_string)
        if depth == 0:
            if kind == "gui":
                m = GUI_DEF_RE.match(code)
                if m:
                    pending = (m.group(2), m.group(1), "", i)
            else:
                m = DEF_RE.match(code)
                if m:
                    flag = (m.group(1) or "").rstrip(":")
                    pending = (m.group(2), kind, flag, i)
        opens, closes = masked.count("{"), masked.count("}")
        new_depth = depth + opens - closes
        if pending and depth == 0:
            if opens == 0 or new_depth == 0:
                # scalar (`@x = 5`, `namespace = y`) or one-line block
                defs.append((pending[0], pending[1], i, i, pending[2]))
                pending = None
            # else: block stays open; close it when depth returns to 0
        elif pending and new_depth == 0 and depth > 0:
            defs.append((pending[0], pending[1], pending[3], i, pending[2]))
            pending = None
        depth = max(new_depth, 0)
    if pending:
        defs.append((pending[0], pending[1], pending[3], pending[3], pending[2]))
    return defs


# --- index build / load ---

def vanilla_sentinel(root):
    """Cheap patch detector: the install's checksum.txt changes every patch.
    Beats re-statting 9k files on a slow /mnt drive per invocation."""
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
                # mod: trust for a short TTL so query bursts don't re-stat
                # the tree on the slow /mnt drive every call
                import time
                marker = cache / "validated_at"
                try:
                    if time.time() - float(marker.read_text()) < 30:
                        return idx, corpus_path
                except (OSError, ValueError):
                    pass
                files = collect_files(root)
                if idx.get("hash") == files_hash(root, files):
                    try:
                        marker.write_text(str(time.time()))
                    except OSError:
                        pass
                    return idx, corpus_path

    if files is None:
        files = collect_files(root)
    fhash = files_hash(root, files)

    sys.stderr.write(f"pdx-nav: indexing {source} ({len(files)} files)...\n")
    defs = []          # [name, kind, file_i, start, end, flag]
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
    """Discover the vanilla-tracker bare git repo.
    Precedence: $PDX_VANILLA_REPO > <mod-parent>/vanilla-tracker/repo.git."""
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
    """Return {tag: commit_hash} from the bare repo."""
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
    """Return the most recent tag (by commit date)."""
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
    """Resolve a version string to a git ref. Tries exact tag first, then
    as a raw commit/branch ref. Returns None with a hint on failure."""
    tags = tracker_tags(repo)
    if version in tags:
        return version
    # try as a raw ref
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
    """List files from a git tree, filtered like collect_files."""
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
    """Read a single file from the bare repo at the given ref."""
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
    """Build or load a cached index for a vanilla-tracker ref."""
    cache = VANILLA_CACHE_DIR / f"tracker-{version_label}"
    cache.mkdir(parents=True, exist_ok=True)
    idx_path = cache / "index.json"
    corpus_path = cache / "corpus.txt"

    # resolve ref to full hash for cache validity
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

    # batch-read all files in one git archive + tar, much faster than N git-show calls
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
        except (subprocess.TimeoutExpired, FileNotFoundError, tarfile.TarError):
            pass

    # fallback to individual reads for any files not in archive
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


def load_sources(need_mod=True, at_version=None):
    sources = []
    mod = find_mod_root()
    if mod and need_mod:
        sources.append(Source("mod", mod))

    # prefer vanilla-tracker when available (faster than /mnt, version-taggable)
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


# --- hit classification ---

def classify_hit(name, text, src, fi, line_no):
    code, comment, _, _ = split_line(text)
    tok = re.compile(rf'(?<![\w.]){re.escape(name)}(?![\w])')
    in_code = bool(tok.search(code))
    in_comment = bool(tok.search(comment))
    role = None
    if in_code:
        # definition line?
        for d in src.by_name.get(name, []):
            if d[2] == fi and d[3] == line_no:
                role = "override" if d[5] else "definition"
                break
        if role is None:
            # crude string detection: is every code occurrence inside quotes?
            _, _, masked, _ = split_line(text)
            role = "use" if tok.search(masked) else "string"
    elif in_comment:
        role = "comment"
    else:
        role = "string"
    return role


# --- output helpers ---

def fmt_def(d, src):
    name, kind, fi, s, e, flag = d
    loc = f"{src.files[fi]}:{s}-{e}" if e > s else f"{src.files[fi]}:{s}"
    flag_s = f" [{flag}]" if flag else ""
    return f"  {kind:<18} {loc}  ({src.name}){flag_s}"


def suggest(name, sources):
    import difflib
    names = set()
    for src in sources:
        names.update(src.by_name.keys())
    close = difflib.get_close_matches(name, list(names), n=5, cutoff=0.6)
    if close:
        print("Did you mean:")
        for c in close:
            kinds = {d[1] for src in sources for d in src.by_name.get(c, [])}
            print(f"  - {c}  [{', '.join(sorted(kinds))}]")
    if re.fullmatch(r"[a-z0-9_]+", name):
        print("Note: engine tokens (effects/triggers/modifiers) live in "
              "pdx-syntax; GUI internals in pdx-gui.")


# --- commands ---

def cmd_def(args, sources):
    missing = 0
    for name in args.names:
        found = [(src, d) for src in sources for d in src.by_name.get(name, [])]
        if not found:
            print(f"No definition found for '{name}'")
            suggest(name, sources)
            missing += 1
            continue
        print(f"{name}:")
        for src, d in found:
            print(fmt_def(d, src))
    return 1 if missing else 0


def pick_def(name, sources, prefer=None):
    found = [(src, d) for src in sources for d in src.by_name.get(name, [])]
    if not found:
        return None
    if prefer:
        pref = [(s, d) for s, d in found if s.name == prefer]
        if pref:
            found = pref
    return found[0]


def cmd_show(args, sources):
    hit = pick_def(args.name, sources, args.source)
    if not hit:
        print(f"No definition found for '{args.name}'")
        suggest(args.name, sources)
        return 1
    src, d = hit
    name, kind, fi, s, e, flag = d
    lines = src.read_file(fi).split("\n")
    block = lines[s - 1:e]
    print(f"# {src.files[fi]}:{s}-{e}  ({src.name} {kind}, {len(block)} lines)")
    maxn = args.lines
    if len(block) <= maxn:
        for i, line in enumerate(block):
            print(f"{s + i:5d}  {line}")
    else:
        head = maxn * 2 // 3
        tail = maxn - head
        for i in range(head):
            print(f"{s + i:5d}  {block[i]}")
        print(f"       ... ({len(block) - head - tail} lines omitted; "
              f"full span {s}-{e}) ...")
        for i in range(len(block) - tail, len(block)):
            print(f"{s + i:5d}  {block[i]}")
    return 0


def gather_refs(name, sources, include_roles=None):
    """Return [(src, fi, line, role, text, encl)] for every occurrence."""
    out = []
    for src in sources:
        for fi, ln, text in src.rg(name, fixed=True, word=True):
            # guard: -w uses \w boundaries; reject dotted-prefix matches like x.name
            if not re.search(rf'(?<![\w.]){re.escape(name)}(?!\w)', text):
                continue
            role = classify_hit(name, text, src, fi, ln)
            if include_roles and role not in include_roles:
                continue
            encl = src.enclosing(fi, ln)
            out.append((src, fi, ln, role, text.strip(), encl))
    return out


TEMPLATE_TOKEN_RE = re.compile(r'[A-Za-z_][\w.]*(?:\$\w+\$[\w.]*)+')


def template_matches(tok, name):
    """Does a $param$-templated token (e.g. sul_pay_frac_$ESTATE$) expand
    to <name> for some substitution?"""
    rx = re.escape(tok)
    rx = re.sub(r'\\\$\w+\\\$', r'\\w+', rx)
    return re.fullmatch(rx, name) is not None


def templated_hits(name, sources):
    """Find $param$-templated constructions that could expand to <name>.
    Without this, generated variable/effect names look 'never written'."""
    if len(name) < 4:
        return []
    pat = re.escape(name[:4]) + r'[\w.]*\$\w+\$'
    out = []
    for src in sources:
        for fi, ln, text in src.rg(pat, fixed=False, word=False):
            code, _, _, _ = split_line(text)
            toks = {t for t in TEMPLATE_TOKEN_RE.findall(code)
                    if template_matches(t, name)}
            if toks:
                encl = src.enclosing(fi, ln)
                out.append((src, fi, ln, ", ".join(sorted(toks)),
                            text.strip(), encl))
    return out


def print_templated(hits, limit=10):
    if not hits:
        return
    print(f"  templated matches via $param$ ({len(hits)}):")
    for src, fi, ln, toks, text, encl in hits[:limit]:
        encl_s = f" in {encl[2]} ({encl[3]} L{encl[0]}-{encl[1]})" if encl else ""
        print(f"    {src.files[fi]}:{ln} ({src.name}){encl_s}: {text[:80]}")
    if len(hits) > limit:
        print(f"    ... {len(hits) - limit} more")


def cmd_refs(args, sources):
    status = 0
    for name in args.names:
        roles = None if args.all_roles else {"use", "definition", "override", "string"}
        hits = gather_refs(name, sources, roles)
        if args.mod_only:
            hits = [h for h in hits if h[0].name == "mod"]
        if not hits:
            tmpl = templated_hits(name, sources)
            if tmpl:
                print(f"{name}: no literal references, but templated "
                      f"constructions expand to it:")
                print_templated(tmpl)
                continue
            print(f"No references found for '{name}'")
            suggest(name, sources)
            status = 1
            continue
        by_role = {}
        for h in hits:
            by_role[h[3]] = by_role.get(h[3], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(by_role.items()))
        print(f"{name}: {len(hits)} references ({summary})")
        cur_file = None
        shown = 0
        for src, fi, ln, role, text, encl in hits:
            if shown >= args.limit:
                print(f"  ... {len(hits) - shown} more (use -n {len(hits)} to see all)")
                break
            fkey = (src.name, fi)
            if fkey != cur_file:
                cur_file = fkey
                print(f"  {src.files[fi]} ({src.name})")
            encl_s = ""
            if encl and not (encl[2] == name and encl[0] == ln):
                encl_s = f" in {encl[2]} ({encl[3]} L{encl[0]}-{encl[1]})"
            print(f"    L{ln:<5} [{role}]{encl_s}: {text[:90]}")
            shown += 1
        print_templated(templated_hits(name, sources))
        print()
    return status


def direct_callers(name, sources):
    """Definitions whose block references <name> (code role only)."""
    callers = {}
    for src, fi, ln, role, text, encl in gather_refs(name, sources, {"use"}):
        if encl is None or encl[2] == name:
            continue
        key = (src.name, encl[2], encl[3], src.files[fi], encl[0], encl[1])
        callers[key] = callers.get(key, 0) + 1
    return callers


def cmd_callers(args, sources):
    callers = direct_callers(args.name, sources)
    if not callers:
        print(f"No callers found for '{args.name}'")
        suggest(args.name, sources)
        return 1
    print(f"{args.name}: called from {len(callers)} definitions")
    for (srcname, cname, ckind, f, s, e), n in sorted(callers.items()):
        times = f" x{n}" if n > 1 else ""
        print(f"  {cname:<40} {ckind:<14} {f}:{s}-{e} ({srcname}){times}")
    return 0


def cmd_stack(args, sources):
    name = args.name
    if not any(name in src.by_name for src in sources):
        print(f"No definition found for '{name}'")
        suggest(name, sources)
        return 1

    seen = set()

    def walk(sym, depth, prefix):
        if depth > args.depth:
            print(f"{prefix}... (depth limit)")
            return
        callers = direct_callers(sym, sources)
        items = sorted(callers.keys())
        if not items:
            return
        for i, (srcname, cname, ckind, f, s, e) in enumerate(items[:args.fanout]):
            tag = " [entry point]" if ckind in ENTRY_KINDS else ""
            print(f"{prefix}^ {cname} ({ckind} {f}:{s}-{e}, {srcname}){tag}")
            if cname in seen:
                print(f"{prefix}  (already expanded)")
                continue
            seen.add(cname)
            if ckind not in ENTRY_KINDS or args.through_entries:
                walk(cname, depth + 1, prefix + "  ")
            else:
                walk(cname, depth + 1, prefix + "  ")
        if len(items) > args.fanout:
            print(f"{prefix}... {len(items) - args.fanout} more callers")

    d = pick_def(name, sources)
    src, dd = d
    print(f"{name} ({dd[1]} {src.files[dd[2]]}:{dd[3]}-{dd[4]}, {src.name})")
    seen.add(name)
    walk(name, 1, "")
    return 0


def cmd_vars(args, sources):
    n = re.escape(args.name)
    read_pat = rf"(?:global_|local_)?var:{n}\b|has_(?:local_|global_)?variable\s*=\s*{n}\b|variable_map\({n}\|"
    write_pat = rf"name\s*=\s*{n}\b"

    reads, writes = [], []
    for src in sources:
        for fi, ln, text in src.rg(read_pat, fixed=False, word=False):
            code, _, _, _ = split_line(text)
            if re.search(read_pat, code) or "variable_map(" in text:
                reads.append((src, fi, ln, text.strip()))
        for fi, ln, text in src.rg(write_pat, fixed=False, word=False):
            code, _, _, _ = split_line(text)
            if not re.search(write_pat, code):
                continue
            if VAR_WRITE_FAMILY.search(code):
                writes.append((src, fi, ln, text.strip()))
            else:
                # opener may be on a nearby earlier line
                try:
                    lines = src.read_file(fi).split("\n")
                except OSError:
                    continue
                window = "\n".join(lines[max(0, ln - 5):ln - 1])
                if VAR_WRITE_FAMILY.search(window):
                    writes.append((src, fi, ln, text.strip()))

    tmpl = templated_hits(args.name, sources)
    if not reads and not writes and not tmpl:
        print(f"No variable activity found for '{args.name}'")
        return 1

    def show(title, rows):
        print(f"{title} ({len(rows)}):")
        for src, fi, ln, text in rows[:args.limit]:
            encl = src.enclosing(fi, ln)
            encl_s = f" in {encl[2]} ({encl[3]} L{encl[0]}-{encl[1]})" if encl else ""
            print(f"  {src.files[fi]}:{ln} ({src.name}){encl_s}: {text[:80]}")
        if len(rows) > args.limit:
            print(f"  ... {len(rows) - args.limit} more")

    show("writes", writes)
    show("reads", reads)
    print_templated(tmpl)
    return 0


def cmd_list(args, sources):
    # Dispatch to file-listing mode if --file given
    if args.file:
        return cmd_list_file(args, sources)

    pat = args.pattern
    if not pat:
        print("Error: pattern required (or use --file <path>)")
        return 1
    pat = pat.lower()
    rows = []
    for src in sources:
        for d in src.idx["defs"]:
            if fnmatch.fnmatch(d[0].lower(), pat):
                if args.kind and d[1] != args.kind:
                    continue
                rows.append((d[1], d[0], src, d))
    if not rows:
        print(f"No definitions matching '{args.pattern}'")
        return 1
    rows.sort(key=lambda r: (r[0], r[1]))
    cur = None
    for kind, name, src, d in rows[:500]:
        if kind != cur:
            cur = kind
            print(f"\n[{kind}]")
        print(f"  {name}  — {src.files[d[2]]}:{d[3]}-{d[4]} ({src.name})")
    if len(rows) > 500:
        print(f"\n... {len(rows) - 500} more")
    return 0


def cmd_grep(args, sources):
    pattern = args.pattern
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        # Fall back to literal substring match if not a valid regex
        rx = re.compile(re.escape(pattern), re.IGNORECASE)

    cap = args.limit
    rows = []
    for src in sources:
        for d in src.idx["defs"]:
            if rx.search(d[0]):
                if args.kind and d[1] != args.kind:
                    continue
                rows.append((d[1], d[0], src, d))

    if not rows:
        print(f"No indexed symbols matching '{pattern}'")
        suggest(pattern, sources)
        return 1

    rows.sort(key=lambda r: (r[0], r[1]))
    cur = None
    shown = 0
    for kind, name, src, d in rows:
        if shown >= cap:
            remaining = len(rows) - shown
            print(f"\n+{remaining} more (use -n {len(rows)} to see all)")
            break
        if kind != cur:
            cur = kind
            print(f"\n[{kind}]")
        print(f"  {name}  — {src.files[d[2]]}:{d[3]}-{d[4]} ({src.name})")
        shown += 1
    return 0


def resolve_file_path(path_str, sources):
    """Resolve a file path (absolute, cwd-relative, or root-relative) to
    [(src, file_index)] pairs. Accepts mod-relative or vanilla-relative
    paths like 'in_game/common/generic_actions/italian_wars.txt'."""
    results = []
    # Normalize to forward slashes
    path_str = path_str.replace("\\", "/")

    for src in sources:
        # Try as absolute path
        try:
            p = Path(path_str).resolve()
            rel = p.relative_to(src.root).as_posix()
            if rel in src.files:
                fi = src.files.index(rel)
                results.append((src, fi))
                continue
        except (ValueError, OSError):
            pass

        # Try as cwd-relative
        try:
            p = (Path.cwd() / path_str).resolve()
            rel = p.relative_to(src.root).as_posix()
            if rel in src.files:
                fi = src.files.index(rel)
                results.append((src, fi))
                continue
        except (ValueError, OSError):
            pass

        # Try as root-relative (e.g. in_game/common/...)
        norm = path_str.lstrip("/")
        if norm in src.files:
            fi = src.files.index(norm)
            results.append((src, fi))

    return results


def cmd_list_file(args, sources):
    """List all definitions in a specific file, in file order."""
    matches = resolve_file_path(args.file, sources)
    if not matches:
        print(f"File not found in index: '{args.file}'")
        print("Searched mod and vanilla roots for matching paths.")
        return 1

    for src, fi in matches:
        spans = src.spans_by_file.get(fi, [])
        if args.kind:
            spans = [(s, e, name, kind) for s, e, name, kind in spans
                     if kind == args.kind]
        if args.pattern:
            pat = args.pattern.lower()
            spans = [(s, e, name, kind) for s, e, name, kind in spans
                     if fnmatch.fnmatch(name.lower(), pat)]
        print(f"# {src.files[fi]} ({src.name}, {len(spans)} definitions)")
        for start, end, name, kind in spans:
            loc = f"L{start}-{end}" if end > start else f"L{start}"
            print(f"  {kind:<18} {name:<40} {loc}")
    return 0


def cmd_rebuild(args, _sources):
    mod = find_mod_root()
    if mod:
        cache = cache_dir_for("mod", mod)
        for f in ("index.json", "corpus.txt"):
            p = cache / f
            if p.exists():
                p.unlink()
        idx, _ = build_index("mod", mod)
        print(f"mod: {len(idx['defs'])} definitions from "
              f"{len(idx['files'])} files")

    tracker = find_vanilla_tracker(mod)
    if tracker:
        version = getattr(args, "at", None) or tracker_latest_tag(tracker)
        if version:
            cache = VANILLA_CACHE_DIR / f"tracker-{version}"
            if cache.exists():
                for f in ("index.json", "corpus.txt"):
                    p = cache / f
                    if p.exists():
                        p.unlink()
            idx, _ = build_tracker_index(tracker, version, version)
            print(f"vanilla@{version}: {len(idx['defs'])} definitions from "
                  f"{len(idx['files'])} files")
    else:
        v = vanilla_root()
        if v.is_dir():
            cache = cache_dir_for("vanilla", v)
            for f in ("index.json", "corpus.txt"):
                p = cache / f
                if p.exists():
                    p.unlink()
            idx, _ = build_index("vanilla", v)
            print(f"vanilla: {len(idx['defs'])} definitions from "
                  f"{len(idx['files'])} files")
    return 0


WORD_RE = re.compile(r'[A-Za-z_@][\w.]*')
GRAPH_EXCLUDE_KINDS = {"loc", "gui", "template", "type", "types",
                        "local_template", "map_modes"}
GRAPH_DEFAULT_KINDS = {"on_action", "effect", "event", "script_value"}
GRAPH_TEMPLATE_RE = re.compile(
    r'(?:[A-Za-z_][\w.]*)?'     # optional static prefix
    r'(?:\$\w+\$[\w.]*)'        # first $param$ segment
    r'(?:[\w.]*\$\w+\$[\w.]*)*' # additional segments
)
GRAPH_NOISE_NAMES = {"namespace", "l_english"}
GRAPH_VAR_WRITE_KW = re.compile(
    r'\b(?:set|change|round|clamp|divide|multiply|subtract_from|add_to'
    r'|remove_from|cmf_change)_?\w*variable\w*\s*=')
GRAPH_VAR_HAS_RE = re.compile(r'\bhas_\w*variable\s*=\s*(\w+)')
GRAPH_VAR_NAME_RE = re.compile(r'\bname\s*=\s*(\w+)')
GRAPH_VAR_REF_RE = re.compile(r'\b(?:global_|local_)?var:(\w+)')
GRAPH_VARMAP_RE = re.compile(r'variable_map\(\s*(\w+)\s*\|')
GRAPH_VARLIST_RE = re.compile(
    r'(?:add_to_variable_list|remove_from_variable_list'
    r'|is_in_variable_list|clear_variable_list'
    r'|every_in_variable_list|any_in_variable_list'
    r'|ordered_in_variable_list|variable_list_size)\s*=\s*(\w+)')
LOC_CONVENTION_PREFIXES = (
    "MODIFIER_TYPE_NAME_", "MODIFIER_TYPE_DESC_",
    "STATIC_MODIFIER_NAME_", "STATIC_MODIFIER_DESC_",
    "game_concept_", "mapmode_", "MAPMODE_",
)


def build_graph(mod_src, all_sources, include_kinds=None, exclude_kinds=None,
                track_vars=False):
    """Build a directed graph of mod definitions.
    Returns (nodes, edges, entry_points, gui_refs) where:
      nodes = {name: (kind, file, start, end)}
      edges = {name: set(target_names)}  (name references target)
      entry_points = set(names) that the engine evaluates directly
    """
    skip = exclude_kinds or set()
    nodes = {}
    for d in mod_src.idx["defs"]:
        name, kind, fi, start, end, flag = d
        if name in GRAPH_NOISE_NAMES:
            continue
        if include_kinds and kind not in include_kinds:
            continue
        if kind in skip:
            continue
        nodes[name] = (kind, mod_src.files[fi], start, end)

    name_set = set(nodes.keys())
    list_names = {n for n, (k, *_) in nodes.items() if k == "scripted_lists"}
    LIST_PREFIXES = ("every_", "ordered_", "any_", "random_")
    edges = {n: set() for n in nodes}
    entry_points = set()
    var_refs = {}  # {def_name: {(var_name, role)}} where role = W/R/M/L

    file_cache = {}
    for d in mod_src.idx["defs"]:
        name, kind, fi, start, end, flag = d
        if name not in nodes:
            continue
        if kind in ENGINE_EVALUATED_KINDS:
            entry_points.add(name)
        if kind == "loc" and any(name.startswith(p) for p in LOC_CONVENTION_PREFIXES):
            entry_points.add(name)

        if fi not in file_cache:
            try:
                file_cache[fi] = mod_src.read_file(fi).split("\n")
            except OSError:
                file_cache[fi] = []
        lines = file_cache[fi]
        is_gui = mod_src.files[fi].endswith(".gui")
        block_tokens = set()
        template_tokens = set()
        block_vars = set()
        in_string = False
        in_var_write = False
        for line in lines[start - 1:end]:
            code, _, masked, in_string = split_line(line, in_string)
            if is_gui:
                block_tokens.update(WORD_RE.findall(code))
            else:
                block_tokens.update(WORD_RE.findall(masked))
            if "$" in code:
                template_tokens.update(GRAPH_TEMPLATE_RE.findall(code))
            if track_vars:
                if GRAPH_VAR_WRITE_KW.search(masked):
                    in_var_write = True
                if in_var_write:
                    for m in GRAPH_VAR_NAME_RE.finditer(masked):
                        block_vars.add((m.group(1), "W"))
                    if "}" in masked:
                        in_var_write = False
                for m in GRAPH_VAR_HAS_RE.finditer(masked):
                    block_vars.add((m.group(1), "R"))
                for m in GRAPH_VAR_REF_RE.finditer(masked):
                    block_vars.add((m.group(1), "R"))
                for m in GRAPH_VARMAP_RE.finditer(masked):
                    block_vars.add((m.group(1), "M"))
                for m in GRAPH_VARLIST_RE.finditer(masked):
                    block_vars.add((m.group(1), "L"))
        block_tokens.discard(name)
        matched = block_tokens & name_set
        for tok in block_tokens - name_set:
            if list_names:
                for pfx in LIST_PREFIXES:
                    if tok.startswith(pfx):
                        base = tok[len(pfx):]
                        if base in list_names:
                            matched.add(base)
        for tok in template_tokens:
            if re.fullmatch(r'\$\w+\$', tok):
                continue
            prefix = tok.split("$", 1)[0]
            for candidate in name_set:
                if candidate.startswith(prefix) and template_matches(tok, candidate):
                    matched.add(candidate)
        edges[name] = matched
        if block_vars:
            var_refs[name] = block_vars

    if track_vars and var_refs:
        all_vars = set()
        for pairs in var_refs.values():
            for vn, role in pairs:
                all_vars.add(vn)
        for vn in all_vars:
            var_key = f"var:{vn}"
            nodes[var_key] = ("variable", "", 0, 0)
            edges[var_key] = set()
        for def_name, pairs in var_refs.items():
            for vn, role in pairs:
                var_key = f"var:{vn}"
                if role == "W":
                    edges[def_name].add(var_key)
                else:
                    edges[var_key].add(def_name)
        name_set = set(nodes.keys())

    gui_refs = scan_gui_references(mod_src, name_set)
    entry_points.update(gui_refs)

    return nodes, edges, entry_points, gui_refs


def scan_gui_references(mod_src, name_set):
    """Scan .gui files and excluded-but-scriptful files (map_modes) for
    references to script definitions. GUI data-binding lives inside strings,
    so we scan the full code portion (including string interiors) for tokens."""
    gui_referenced = set()
    for fi, rel in enumerate(mod_src.files):
        if not rel.endswith(".gui"):
            continue
        try:
            text = mod_src.read_file(fi)
        except OSError:
            continue
        in_string = False
        for line in text.split("\n"):
            code, _, _, in_string = split_line(line, in_string)
            tokens = set(WORD_RE.findall(code))
            gui_referenced.update(tokens & name_set)

    if mod_src.root:
        for top in SCAN_TOPDIRS:
            mm = mod_src.root / top / "gfx" / "map" / "map_modes"
            if not mm.is_dir():
                continue
            for f in sorted(mm.glob("*.txt")):
                try:
                    text = f.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                in_string = False
                for line in text.split("\n"):
                    code, _, _, in_string = split_line(line, in_string)
                    tokens = set(WORD_RE.findall(code))
                    gui_referenced.update(tokens & name_set)

    return gui_referenced


def compute_reachability(nodes, edges, entry_points):
    """BFS from entry points. Returns (reachable, components).
    components = [(entry_name, set_of_reachable_nodes)]"""
    all_reachable = set()
    components = []

    reverse = {n: set() for n in nodes}
    for src, targets in edges.items():
        for t in targets:
            if t in reverse:
                reverse[t].add(src)

    for ep in sorted(entry_points):
        visited = set()
        queue = [ep]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            for t in edges.get(cur, set()):
                if t not in visited:
                    queue.append(t)
        if len(visited) > 1 or edges.get(ep):
            components.append((ep, visited))
        all_reachable.update(visited)

    orphans = set(nodes.keys()) - all_reachable
    return all_reachable, components, orphans


def find_connected_clusters(nodes, edges, orphans):
    """Among orphans, find clusters that reference each other."""
    undirected = {n: set() for n in orphans}
    for n in orphans:
        for t in edges.get(n, set()):
            if t in orphans:
                undirected[n].add(t)
                undirected[t].add(n)

    visited = set()
    clusters = []
    for n in sorted(orphans):
        if n in visited:
            continue
        cluster = set()
        queue = [n]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            cluster.add(cur)
            for t in undirected.get(cur, set()):
                if t not in visited:
                    queue.append(t)
        clusters.append(cluster)
    return clusters


KIND_COLORS = {
    "effect": "\033[32m",       # green
    "event": "\033[33m",        # yellow
    "script_value": "\033[36m", # cyan
    "on_action": "\033[91m",    # bright red
    "static_modifiers": "\033[35m",           # magenta
    "auto_modifiers": "\033[35m",
    "modifier_type_definitions": "\033[35m",
    "variable": "\033[2m",      # dim
}
C_RESET = "\033[0m"
C_DIM = "\033[2m"
_use_color = None


def color_enabled():
    global _use_color
    if _use_color is None:
        _use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    return _use_color


def c_kind(kind):
    if not color_enabled():
        return "", ""
    return KIND_COLORS.get(kind, ""), C_RESET


def dep_tree(name, edges, nodes, depth, seen, prefix, max_depth,
             cycle_counts=None):
    """Recursively print a dependency tree, collapsing repeat cycles."""
    if cycle_counts is None:
        cycle_counts = {}
    targets = sorted(edges.get(name, set()))
    new_targets = []
    for t in targets:
        if t in seen:
            cycle_counts[t] = cycle_counts.get(t, 0) + 1
        else:
            new_targets.append(t)

    items = new_targets
    if not items and cycle_counts:
        return

    for i, t in enumerate(items):
        is_last = (i == len(items) - 1)
        connector = "└── " if is_last else "├── "
        kind = nodes[t][0] if t in nodes else "?"
        cs, ce = c_kind(kind)
        print(f"{prefix}{connector}{cs}{t}{ce} ({kind})")
        if depth < max_depth:
            seen.add(t)
            child_prefix = prefix + ("    " if is_last else "│   ")
            dep_tree(t, edges, nodes, depth + 1, seen, child_prefix,
                     max_depth, cycle_counts)


def print_cycles(cycle_counts, prefix):
    """Print collapsed cycle summary."""
    if not cycle_counts:
        return
    items = sorted(cycle_counts.items(), key=lambda x: -x[1])
    dim_on = C_DIM if color_enabled() else ""
    dim_off = C_RESET if color_enabled() else ""
    count_label = f"{len(items)} cycles" if len(items) > 1 else "1 cycle"
    total = sum(v for _, v in items)
    names = ", ".join(f"{n} x{c}" if c > 1 else n for n, c in items[:5])
    if len(items) > 5:
        names += f" +{len(items)-5}"
    print(f"{prefix}{dim_on}[{count_label}, {total} refs: {names}]{dim_off}")


def cmd_graph(args, sources):
    mod_src = None
    for s in sources:
        if s.name == "mod":
            mod_src = s
            break
    if not mod_src:
        print("Error: no mod root found")
        return 1

    sys.stderr.write("pdx-nav: building dependency graph...\n")
    if args.kind:
        include = set()
        for k in args.kind:
            include.update(k.replace(",", " ").split())
        exclude = None
    elif args.all:
        include, exclude = None, set()
    else:
        include, exclude = set(GRAPH_DEFAULT_KINDS), None
    if exclude is not None:
        if args.include_loc:
            exclude.discard("loc")
        if args.include_gui:
            exclude -= {"gui", "template", "type", "types", "local_template"}
    if args.vars and include is not None:
        include.add("variable")
    nodes, edges, entry_points, gui_refs = build_graph(
        mod_src, sources, include_kinds=include, exclude_kinds=exclude,
        track_vars=args.vars)
    reachable, components, orphans = compute_reachability(nodes, edges, entry_points)

    engine_eps = entry_points - gui_refs
    gui_only_eps = gui_refs - engine_eps

    if args.depth is None:
        args.depth = 6 if args.name else 2

    in_edges = {n: set() for n in nodes}
    for src, targets in edges.items():
        for t in targets:
            if t in in_edges:
                in_edges[t].add(src)

    # --- Drill-down mode ---
    if args.name:
        name = args.name
        if name not in nodes:
            print(f"No definition found for '{name}'")
            suggest(name, sources)
            return 1
        kind = nodes[name][0]
        cs, ce = c_kind(kind)
        loc = ""
        if args.paths and nodes[name][1]:
            loc = f"  {nodes[name][1]}:{nodes[name][2]}-{nodes[name][3]}"
        ep_tag = " [entry point]" if name in entry_points else ""
        gui_tag = " [gui]" if name in gui_refs else ""
        print(f"{cs}{name}{ce} ({kind}){ep_tag}{gui_tag}{loc}")

        depth = args.depth

        if args.up:
            seen = {name}
            cycle_counts = {}
            dep_tree(name, in_edges, nodes, 1, seen, "", depth, cycle_counts)
            print_cycles(cycle_counts, "")
        else:
            callers = sorted(in_edges.get(name, set()))
            if callers:
                dim = C_DIM if color_enabled() else ""
                rst = C_RESET if color_enabled() else ""
                print(f"├── {dim}called by: {', '.join(callers[:10])}"
                      + (f" +{len(callers)-10}" if len(callers) > 10 else "")
                      + rst)
            seen = {name}
            cycle_counts = {}
            dep_tree(name, edges, nodes, 1, seen, "", depth, cycle_counts)
            print_cycles(cycle_counts, "")
        return 0

    # collect sections
    sections = []
    if not args.orphans_only:
        sections.append("entry_points")
        if gui_only_eps:
            sections.append("gui_referenced")
    sections.append("orphans")

    # root
    edge_count = sum(len(t) for t in edges.values())
    print(f"DEPENDENCY GRAPH  "
          f"{len(nodes)} defs, {edge_count} edges, "
          f"{len(engine_eps)} engine, {len(gui_refs)} gui, "
          f"{len(reachable)} reachable, {len(orphans)} orphans")

    for si, section in enumerate(sections):
        s_last = (si == len(sections) - 1)
        s_conn = "└── " if s_last else "├── "
        s_pre = "    " if s_last else "│   "

        # --- ENTRY POINTS ---
        if section == "entry_points":
            print(f"{s_conn}ENTRY POINTS (engine-evaluated)")
            eps = sorted(engine_eps)
            for ei, ep in enumerate(eps):
                e_last = (ei == len(eps) - 1)
                e_conn = "└── " if e_last else "├── "
                e_pre = s_pre + ("    " if e_last else "│   ")
                kind = nodes[ep][0]
                cs, ce = c_kind(kind)
                loc = ""
                if args.paths:
                    loc = f"  {nodes[ep][1]}:{nodes[ep][2]}-{nodes[ep][3]}"
                print(f"{s_pre}{e_conn}{cs}{ep}{ce} ({kind}){loc}")
                targets = edges.get(ep, set())
                if targets:
                    seen = {ep}
                    cycle_counts = {}
                    dep_tree(ep, edges, nodes, 1, seen, e_pre, args.depth,
                             cycle_counts)
                    print_cycles(cycle_counts, e_pre)

        # --- GUI-REFERENCED ---
        elif section == "gui_referenced":
            print(f"{s_conn}GUI-REFERENCED (used in .gui data binding)")
            by_kind = {}
            for ep in gui_only_eps:
                by_kind.setdefault(nodes[ep][0], []).append(ep)
            kinds = sorted(by_kind)
            for ki, kind in enumerate(kinds):
                k_last = (ki == len(kinds) - 1)
                k_conn = "└── " if k_last else "├── "
                k_pre = s_pre + ("    " if k_last else "│   ")
                items = sorted(by_kind[kind])
                print(f"{s_pre}{k_conn}{kind} ({len(items)})")
                for ii, ep in enumerate(items):
                    i_last = (ii == len(items) - 1)
                    i_conn = "└── " if i_last else "├── "
                    cs, ce = c_kind(kind)
                    targets = edges.get(ep, set())
                    dep_s = ""
                    if targets:
                        dep_s = f" -> {', '.join(sorted(targets))}"
                    print(f"{k_pre}{i_conn}{cs}{ep}{ce}{dep_s}")


        # --- ORPHANS ---
        elif section == "orphans":
            print(f"{s_conn}ORPHANS (unreachable from entry points)")
            if not orphans:
                print(f"{s_pre}└── (none)")
            else:
                clusters = find_connected_clusters(nodes, edges, orphans)
                connected = [c for c in clusters if len(c) > 1]
                isolated = sorted(n for c in clusters if len(c) == 1
                                  for n in c)
                parts = []
                for i, cluster in enumerate(connected, 1):
                    parts.append(("cluster", i, cluster))
                if isolated:
                    parts.append(("isolated", 0, isolated))

                for pi, (ptype, idx, data) in enumerate(parts):
                    p_last = (pi == len(parts) - 1)
                    p_conn = "└── " if p_last else "├── "
                    p_pre = s_pre + ("    " if p_last else "│   ")

                    if ptype == "cluster":
                        print(f"{s_pre}{p_conn}Cluster {idx} "
                              f"({len(data)} nodes, disconnected)")
                        items = sorted(data)
                        for ii, name in enumerate(items):
                            i_last = (ii == len(items) - 1)
                            i_conn = "└── " if i_last else "├── "
                            kind = nodes[name][0]
                            targets_in = edges[name] & data
                            arrow = ""
                            if targets_in:
                                arrow = f" -> {', '.join(sorted(targets_in))}"
                            cs, ce = c_kind(kind)
                            print(f"{p_pre}{i_conn}{cs}{name}{ce} ({kind}){arrow}")
                    else:
                        print(f"{s_pre}{p_conn}Isolated ({len(data)})")
                        by_kind = {}
                        for name in data:
                            by_kind.setdefault(nodes[name][0], []).append(name)
                        kinds = sorted(by_kind)
                        for ki, kind in enumerate(kinds):
                            k_last = (ki == len(kinds) - 1)
                            k_conn = "└── " if k_last else "├── "
                            k_pre = p_pre + ("    " if k_last else "│   ")
                            items = sorted(by_kind[kind])
                            cs, ce = c_kind(kind)
                            print(f"{p_pre}{k_conn}{kind} ({len(items)})")
                            for ii, name in enumerate(items):
                                i_last = (ii == len(items) - 1)
                                i_conn = "└── " if i_last else "├── "
                                loc = ""
                                if args.paths:
                                    loc = f"  {nodes[name][1]}:{nodes[name][2]}"
                                print(f"{k_pre}{i_conn}{cs}{name}{ce}{loc}")

    return 0


def main():
    p = argparse.ArgumentParser(description="EU5 PDX script navigation",
                                epilog="Misses print did-you-mean suggestions.")
    p.add_argument("--at", metavar="VERSION",
                   help="Vanilla version tag (e.g. 1.3.11); requires vanilla-tracker")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("def", help="Find definitions")
    d.add_argument("names", nargs="+")

    s = sub.add_parser("show", help="Print a definition block")
    s.add_argument("name")
    s.add_argument("-n", "--lines", type=int, default=120)
    s.add_argument("--vanilla", dest="source", action="store_const", const="vanilla")
    s.add_argument("--mod", dest="source", action="store_const", const="mod")

    r = sub.add_parser("refs", help="Find all references")
    r.add_argument("names", nargs="+")
    r.add_argument("-n", "--limit", type=int, default=40)
    r.add_argument("--mod-only", action="store_true")
    r.add_argument("--all-roles", action="store_true",
                   help="Include comment hits")

    c = sub.add_parser("callers", help="Direct callers")
    c.add_argument("name")

    st = sub.add_parser("stack", help="Transitive caller tree")
    st.add_argument("name")
    st.add_argument("--depth", type=int, default=6)
    st.add_argument("--fanout", type=int, default=10)
    st.add_argument("--through-entries", action="store_true")

    v = sub.add_parser("vars", help="Variable writes/reads")
    v.add_argument("name")
    v.add_argument("-n", "--limit", type=int, default=30)

    ls = sub.add_parser("list", help="List definitions by glob or file")
    ls.add_argument("pattern", nargs="?", default=None)
    ls.add_argument("--kind")
    ls.add_argument("--file", metavar="PATH",
                    help="List all definitions in one file (file order)")

    g = sub.add_parser("grep", help="Search indexed symbol names by regex/substring")
    g.add_argument("pattern")
    g.add_argument("--kind", help="Filter by kind")
    g.add_argument("-n", "--limit", type=int, default=50)

    gr = sub.add_parser("graph", help="Dependency graph of all mod definitions")
    gr.add_argument("name", nargs="?", default=None,
                    help="Drill into a specific definition's subtree")
    gr.add_argument("--up", action="store_true",
                    help="Show upstream callers instead of downstream deps")
    gr.add_argument("--depth", type=int, default=None,
                    help="Max tree depth (default: 2 for full graph, 6 for drill-down)")
    gr.add_argument("--orphans-only", action="store_true",
                    help="Only show orphans section")
    gr.add_argument("--include-loc", action="store_true",
                    help="Include localization keys")
    gr.add_argument("--include-gui", action="store_true",
                    help="Include GUI templates/types")
    gr.add_argument("--all", action="store_true",
                    help="Include all definition kinds (loc, gui, etc.)")
    gr.add_argument("--kind", action="append", metavar="KIND",
                    help="Only include these kinds (comma or space "
                    "separated, e.g. --kind 'effect,event,script_value')")
    gr.add_argument("--vars", action="store_true",
                    help="Include variables, variable_maps, variable_lists")
    gr.add_argument("--paths", action="store_true",
                    help="Show file paths on definitions")

    sub.add_parser("rebuild", help="Force index rebuild")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0

    if args.cmd == "rebuild":
        return cmd_rebuild(args, None)

    sources = load_sources(at_version=args.at)

    # normalize --vanilla / --mod source filter to match GitSource naming
    if hasattr(args, "source") and args.source == "vanilla":
        matches = [s for s in sources if s.name.startswith("vanilla")]
        if matches:
            args.source = matches[0].name

    return {
        "def": cmd_def, "show": cmd_show, "refs": cmd_refs,
        "callers": cmd_callers, "stack": cmd_stack, "vars": cmd_vars,
        "list": cmd_list, "grep": cmd_grep, "graph": cmd_graph,
    }[args.cmd](args, sources)


if __name__ == "__main__":
    sys.exit(main() or 0)
