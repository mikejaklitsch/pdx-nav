import fnmatch
import re
from pathlib import Path

from .classify import gather_refs, print_templated, templated_hits
from .constants import ENTRY_KINDS, VAR_WRITE_FAMILY
from .output import fmt_def, suggest
from .paths import find_mod_root, cache_dir_for
from .scanner import split_line
from pdx_utilities.git import git_latest_tag
from pdx_utilities.paths import find_vanilla_repo
from .source import (Source, build_index, build_tracker_index, vanilla_root)
from .constants import VANILLA_CACHE_DIR


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


def cmd_context(args, sources):
    """Show a definition with its parent hierarchy (indentation-based)."""
    hit = pick_def(args.name, sources, args.source)
    if not hit:
        print(f"No definition found for '{args.name}'")
        suggest(args.name, sources)
        return 1
    src, d = hit
    name, kind, fi, s, e, flag = d
    lines = src.read_file(fi).split("\n")
    target_line = s - 1
    indent_at_target = len(lines[target_line]) - len(lines[target_line].lstrip())

    parents = []
    for i in range(target_line - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent < indent_at_target:
            parents.append((i + 1, line.rstrip()))
            indent_at_target = indent
            if indent == 0:
                break
    parents.reverse()

    print(f"# {src.files[fi]}:{s}-{e}  ({src.name} {kind})")
    if parents:
        print("# Hierarchy:")
        for line_no, text in parents:
            print(f"#   L{line_no}: {text}")
        print()

    block = lines[s - 1:e]
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
    if args.file:
        return cmd_list_file(args, sources)

    pat = args.pattern
    if not pat:
        print("Error: pattern required (or use --file <path>)")
        return 1
    pat = pat.lower()
    excluded = set()
    if args.exclude:
        excluded = set(args.exclude.replace(",", " ").split())
    rows = []
    for src in sources:
        for d in src.idx["defs"]:
            if fnmatch.fnmatch(d[0].lower(), pat):
                if d[1] in excluded:
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
        rx = re.compile(re.escape(pattern), re.IGNORECASE)

    cap = args.limit
    excluded = set()
    if args.exclude:
        excluded = set(args.exclude.replace(",", " ").split())
    rows = []
    for src in sources:
        for d in src.idx["defs"]:
            if rx.search(d[0]):
                if d[1] in excluded:
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
    results = []
    path_str = path_str.replace("\\", "/")

    for src in sources:
        try:
            p = Path(path_str).resolve()
            rel = p.relative_to(src.root).as_posix()
            if rel in src.files:
                fi = src.files.index(rel)
                results.append((src, fi))
                continue
        except (ValueError, OSError, TypeError):
            pass

        try:
            p = (Path.cwd() / path_str).resolve()
            rel = p.relative_to(src.root).as_posix()
            if rel in src.files:
                fi = src.files.index(rel)
                results.append((src, fi))
                continue
        except (ValueError, OSError, TypeError):
            pass

        norm = path_str.lstrip("/")
        if norm in src.files:
            fi = src.files.index(norm)
            results.append((src, fi))

    return results


def cmd_list_file(args, sources):
    matches = resolve_file_path(args.file, sources)
    if not matches:
        print(f"File not found in index: '{args.file}'")
        print("Searched mod and vanilla roots for matching paths.")
        return 1

    for src, fi in matches:
        spans = src.spans_by_file.get(fi, [])
        if args.exclude:
            excluded = set(args.exclude.replace(",", " ").split())
            spans = [(s, e, name, kind) for s, e, name, kind in spans
                     if kind not in excluded]
        if args.pattern:
            pat = args.pattern.lower()
            spans = [(s, e, name, kind) for s, e, name, kind in spans
                     if fnmatch.fnmatch(name.lower(), pat)]
        print(f"# {src.files[fi]} ({src.name}, {len(spans)} definitions)")
        for start, end, name, kind in spans:
            loc = f"L{start}-{end}" if end > start else f"L{start}"
            print(f"  {kind:<18} {name:<40} {loc}")
    return 0


GUI_DEF_KINDS = re.compile(
    r'(?:^|\s)(template|local_template)\s+(\w+)'
    r'|(^|\s)(types)\s+(\w+)'
    r'|(^|\s)(type)\s+(\w+)\s*=\s*(\w+)'
    r'|(blockoverride)\s+"([^"]+)"'
    r'|(block)\s+"([^"]+)"',
    re.IGNORECASE)

GUI_VAR_LINE_RE = re.compile(r'^\s*(@\w+)\s*=\s*(.+)')

BLOCK_OPEN_RE = re.compile(r'Block_open:(\S+)')
BLOCK_CLOSE_RE = re.compile(r'Block_close:(\S+)')


def _tag_file(filepath):
    """Run pdx-gui tag --stdout on a file, return tagged content."""
    import subprocess, shutil
    gui = shutil.which("pdx-gui")
    if not gui:
        return None
    try:
        r = subprocess.run([gui, "tag", str(filepath), "--stdout"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _tag_text(text):
    """Run pdx-gui tag on stdin text, return tagged content."""
    import subprocess, shutil
    gui = shutil.which("pdx-gui")
    if not gui:
        return None
    try:
        r = subprocess.run([gui, "tag", "-", "--stdout"],
                           capture_output=True, text=True, timeout=30,
                           input=text)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _classify_gui_line(head):
    """Extract (kind, name) from the non-marker portion of a tagged line."""
    m_var = GUI_VAR_LINE_RE.match(head)
    if m_var:
        return "@var", f"{m_var.group(1)} = {m_var.group(2).strip()}"
    m_def = GUI_DEF_KINDS.search(head)
    if m_def:
        if m_def.group(1):
            return m_def.group(1).lower(), m_def.group(2)
        if m_def.group(4):
            return "types", m_def.group(5)
        if m_def.group(7):
            return "type", f"{m_def.group(8)} = {m_def.group(9)}"
        if m_def.group(10):
            return "blockoverride", m_def.group(11)
        if m_def.group(12):
            return "block", m_def.group(13)
    return None, None


def _parse_gui_tree(tagged_lines):
    """Build a tree from pdx-gui tagged output. Each node is
    (kind, name, line, children)."""
    root = []
    stack = [root]
    pending = None

    for i, line in enumerate(tagged_lines, 1):
        stripped = line.strip()
        if not stripped:
            continue

        m_close = BLOCK_CLOSE_RE.search(stripped)
        if m_close:
            pending = None
            if len(stack) > 1:
                stack.pop()
            continue

        m_open = BLOCK_OPEN_RE.search(stripped)
        head = stripped.split(" Block_open:")[0].strip() if m_open else stripped

        kind, name = _classify_gui_line(head) if head else (None, None)

        if kind:
            node = (kind, name, i, [])
            stack[-1].append(node)
            if m_open:
                stack.append(node[3])
                pending = None
            elif kind != "@var":
                pending = node
            else:
                pending = None
        elif m_open:
            if pending:
                stack.append(pending[3])
                pending = None
            else:
                stack.append([])

    return root


def _print_gui_tree(nodes, prefix, include_kinds):
    """Render a GUI tree with connectors and colors."""
    from .output import c_kind, color_enabled, C_DIM, C_RESET

    filtered = nodes
    if include_kinds is not None:
        filtered = []
        for kind, name, line, children in nodes:
            child_has = _tree_has_kinds(children, include_kinds)
            if kind in include_kinds or child_has:
                filtered.append((kind, name, line, children))

    for i, (kind, name, line, children) in enumerate(filtered):
        is_last = (i == len(filtered) - 1)
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

        cs, ce = c_kind(kind)
        dim = C_DIM if color_enabled() and kind == "blockoverride" else ""
        rst = C_RESET if dim else ""
        show_kind = kind not in include_kinds if include_kinds else False

        if show_kind:
            dim_on = C_DIM if color_enabled() else ""
            dim_off = C_RESET if color_enabled() else ""
            print(f"{prefix}{connector}{cs}{name}{ce} {dim_on}({kind} L{line}){dim_off}")
        else:
            print(f"{prefix}{connector}{dim}{cs}{name}{ce}{rst} ({kind} L{line})")

        _print_gui_tree(children, child_prefix, include_kinds)


def _tree_has_kinds(nodes, kinds):
    for kind, name, line, children in nodes:
        if kind in kinds or _tree_has_kinds(children, kinds):
            return True
    return False


def cmd_outline(args, sources):
    """Show the nested structure of a .gui file."""
    matches = resolve_file_path(args.file, sources)
    if not matches:
        print(f"File not found in index: '{args.file}'")
        return 1

    src, fi = matches[0]
    text = src.read_file(fi)

    if src.root:
        tagged = _tag_file(src.root / src.files[fi])
    else:
        tagged = _tag_text(text)
    if not tagged:
        print("Error: pdx-gui not found or tagging failed")
        return 1

    tree = _parse_gui_tree(tagged.split("\n"))

    include = None
    if args.kind:
        include = set()
        for k in args.kind:
            include.update(k.replace(",", " ").split())

    line_count = len(text.split("\n"))
    count = _count_nodes(tree, include)
    print(f"# {src.files[fi]} ({src.name}, {line_count} lines, {count} constructs)")
    _print_gui_tree(tree, "", include)
    return 0


def _count_nodes(nodes, include_kinds):
    n = 0
    for kind, name, line, children in nodes:
        if include_kinds is None or kind in include_kinds or \
                _tree_has_kinds(children, include_kinds):
            n += 1
        n += _count_nodes(children, include_kinds)
    return n


def cmd_rename(args, sources):
    old, new = args.old, args.new
    pat = re.compile(rf'(?<!\w){re.escape(old)}(?!\w)')

    mod_src = None
    for s in sources:
        if s.name == "mod":
            mod_src = s
            break
    if not mod_src:
        print("Error: no mod root found")
        return 1

    cwd = Path.cwd().resolve()
    try:
        scope = cwd.relative_to(mod_src.root).as_posix()
    except ValueError:
        print("Error: not inside the mod root")
        return 1
    if scope == ".":
        scope = ""

    changed = []
    total = 0
    for rel in mod_src.files:
        if scope and not rel.startswith(scope):
            continue
        path = mod_src.root / rel
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        from pdx_utilities.fileio import BOM_BYTES
        has_bom = raw[:3] == BOM_BYTES
        text = (raw[3:] if has_bom else raw).decode('utf-8', errors='replace')
        new_text, count = pat.subn(new, text)
        if count == 0:
            continue
        if not args.dry_run:
            out = (BOM_BYTES + new_text.encode('utf-8')) if has_bom else new_text.encode('utf-8')
            path.write_bytes(out)
        changed.append((rel, count))
        total += count

    verb = "would rename" if args.dry_run else "renamed"
    print(f"{verb} '{old}' -> '{new}': {total} occurrences in {len(changed)} files")
    for rel, count in changed:
        print(f"  {rel}: {count}")

    if not args.dry_run and total > 0:
        print(f"\nRun `pdx-nav rebuild` to update the index.")

    vanilla_defs = sum(len(s.by_name.get(old, []))
                       for s in sources if s.name != "mod")
    if vanilla_defs:
        print(f"\nNote: '{old}' also exists in vanilla "
              f"({vanilla_defs} definitions), not modified.")

    return 0 if total > 0 else 1


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

    tracker = find_vanilla_repo(mod)
    if tracker:
        version = getattr(args, "at", None) or git_latest_tag(tracker)
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
