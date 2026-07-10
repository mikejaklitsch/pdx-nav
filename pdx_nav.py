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
    pdx-nav rebuild                   Force index + corpus rebuild

Architecture: definitions and block spans are indexed once and cached
(mod: <mod>/.claude/pdx_nav/, vanilla: ~/.cache/pdx-nav/, invalidated by
mtime hash). Reference queries run ripgrep over a locally cached corpus
copy (fast even though the game lives on a slow /mnt drive), then each hit
is classified (code/string/comment) and enriched with its enclosing block.
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
        self.files = self.idx["files"]
        self.by_name = {}
        self.spans_by_file = {}
        for d in self.idx["defs"]:
            self.by_name.setdefault(d[0], []).append(d)
            self.spans_by_file.setdefault(d[2], []).append((d[3], d[4], d[0], d[1]))
        for spans in self.spans_by_file.values():
            spans.sort()

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


def load_sources(need_mod=True):
    sources = []
    mod = find_mod_root()
    if mod and need_mod:
        sources.append(Source("mod", mod))
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
    path = src.root / src.files[fi]
    lines = path.read_text(encoding="utf-8-sig", errors="replace").split("\n")
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
                path = src.root / src.files[fi]
                try:
                    lines = path.read_text(encoding="utf-8-sig",
                                           errors="replace").split("\n")
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
    pat = args.pattern.lower()
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


def cmd_rebuild(_args, _sources):
    mod = find_mod_root()
    targets = []
    if mod:
        targets.append(("mod", mod))
    targets.append(("vanilla", vanilla_root()))
    for source, root in targets:
        cache = cache_dir_for(source, root)
        for f in ("index.json", "corpus.txt"):
            p = cache / f
            if p.exists():
                p.unlink()
        idx, _ = build_index(source, root)
        print(f"{source}: {len(idx['defs'])} definitions from "
              f"{len(idx['files'])} files")
    return 0


def main():
    p = argparse.ArgumentParser(description="EU5 PDX script navigation",
                                epilog="Misses print did-you-mean suggestions.")
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

    ls = sub.add_parser("list", help="List definitions by glob")
    ls.add_argument("pattern")
    ls.add_argument("--kind")

    sub.add_parser("rebuild", help="Force index rebuild")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0

    if args.cmd == "rebuild":
        return cmd_rebuild(args, None)

    sources = load_sources()
    return {
        "def": cmd_def, "show": cmd_show, "refs": cmd_refs,
        "callers": cmd_callers, "stack": cmd_stack, "vars": cmd_vars,
        "list": cmd_list,
    }[args.cmd](args, sources)


if __name__ == "__main__":
    sys.exit(main() or 0)
