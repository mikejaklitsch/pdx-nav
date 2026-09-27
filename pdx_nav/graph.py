import os
import pickle
import re
from bisect import bisect_left
from collections import deque

from .classify import template_matches, template_regex
from .constants import (
    ENGINE_EVALUATED_KINDS, GRAPH_CONCATENATE_RE, GRAPH_DEFAULT_KINDS,
    GRAPH_GUI_KINDS, GRAPH_LIBRARY_KINDS, GRAPH_NOISE_NAMES,
    GRAPH_TEMPLATE_RE, GRAPH_VAR_HAS_RE, GRAPH_VAR_NAME_RE, GRAPH_VAR_REF_RE,
    GRAPH_VAR_WRITE_KW, GRAPH_VARLIST_RE, GRAPH_VARMAP_RE,
    LOC_CONVENTION_PREFIXES, LOC_DEF_SUFFIXES, SCAN_TOPDIRS, WORD_RE,
)
from .output import c_kind, color_enabled, suggest, C_DIM, C_RESET
from .paths import cache_dir_for, read_texts
from .scanner import split_line
from .source import NOISY_REFRESH, note

GRAPH_CACHE = "graph.pkl"
GRAPH_SCHEMA = 3
LIST_PREFIXES = ("every_", "ordered_", "any_", "random_")
BARE_PARAM_RE = re.compile(r'\$\w+\$')
# Data-binding expressions ("[GetScriptValue('x')]", "[TC.GetTag]") live in
# strings, which script scanning otherwise masks out.
BINDING_RE = re.compile(r'\[([^\[\]]*)\]')
BINDING_WORD_RE = re.compile(r'[A-Za-z_]\w*')
LOC_REF_RE = re.compile(r'\$([A-Za-z_][\w.]*)\$')


# --- per-file token extraction (cached by file signature) ---

def _scan_blocks(lines, spans, is_gui, is_loc=False):
    """Tokens, $param$ templates and variable touches for each definition
    block in one file. spans: [(name, start, end)]."""
    out = {}
    for name, start, end in spans:
        tokens, templates, block_vars = set(), set(), []
        in_string = False
        in_var_write = False
        for line in lines[start - 1:end]:
            code, _, masked, in_string = split_line(line, in_string)
            tokens.update(WORD_RE.findall(code if is_gui else masked))
            if "[" in code and not is_gui:
                for seg in BINDING_RE.findall(code):
                    tokens.update(BINDING_WORD_RE.findall(seg))
            if is_loc:
                # Loc text: $key$ nests another loc key; it never builds a
                # script name, so skip template and variable parsing.
                tokens.update(LOC_REF_RE.findall(code))
                continue
            if "$" in code:
                templates.update(GRAPH_TEMPLATE_RE.findall(code))
            if GRAPH_VAR_WRITE_KW.search(masked):
                in_var_write = True
            if in_var_write:
                for m in GRAPH_VAR_NAME_RE.finditer(masked):
                    block_vars.append((m.group(1), "W"))
                if "}" in masked:
                    in_var_write = False
            for m in GRAPH_VAR_HAS_RE.finditer(masked):
                block_vars.append((m.group(1), "R"))
            for m in GRAPH_VAR_REF_RE.finditer(masked):
                block_vars.append((m.group(1), "R"))
            for m in GRAPH_VARMAP_RE.finditer(masked):
                block_vars.append((m.group(1), "M"))
            for m in GRAPH_VARLIST_RE.finditer(masked):
                block_vars.append((m.group(1), "L"))
        tokens.discard(name)
        templates = {t for t in templates if not BARE_PARAM_RE.fullmatch(t)}
        out[(name, start, end)] = (frozenset(tokens), tuple(sorted(templates)),
                                   tuple(block_vars))
    return out


def _scan_whole(text, want_templates):
    """Every code token in a file (GUI data binding, map modes), plus the
    $param$ templates and Concatenate() prefixes that build names at runtime."""
    tokens, templates, concat = set(), set(), set()
    in_string = False
    for line in text.split("\n"):
        code, _, _, in_string = split_line(line, in_string)
        tokens.update(WORD_RE.findall(code))
        if want_templates:
            if "$" in code:
                templates.update(GRAPH_TEMPLATE_RE.findall(code))
            if "Concatenate" in code:
                concat.update(m.group(1)
                              for m in GRAPH_CONCATENATE_RE.finditer(code))
    return frozenset(tokens), templates, concat


def _map_mode_files(root):
    """Map mode scripts sit under the excluded gfx/map subtree but name
    script values, so they are scanned on the side."""
    out = []
    for top in SCAN_TOPDIRS:
        mm = root / top / "gfx" / "map" / "map_modes"
        if not mm.is_dir():
            continue
        for f in sorted(mm.glob("*.txt")):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append((f.relative_to(root).as_posix(),
                        (st.st_mtime_ns, st.st_size)))
    return out


def scan_mod(mod_src):
    """Per-file graph data for the whole mod. Only files whose signature
    changed since the last graph call are rescanned."""
    cache = cache_dir_for("mod", mod_src.root) / GRAPH_CACHE
    try:
        with open(cache, "rb") as f:
            old = pickle.load(f)
        if old.get("schema") != GRAPH_SCHEMA:
            old = {}
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError,
            ValueError):
        old = {}
    old_files = old.get("files", {})

    spans_by_fi = {}
    for name, kind, fi, start, end, flag in mod_src.idx["defs"]:
        if name not in GRAPH_NOISE_NAMES:
            spans_by_fi.setdefault(fi, []).append((name, start, end))

    wanted = {}
    for fi, rel in enumerate(mod_src.files):
        if fi in spans_by_fi or rel.endswith(".gui"):
            wanted[rel] = (mod_src.idx["sigs"][fi], fi)
    for rel, sig in _map_mode_files(mod_src.root):
        wanted[rel] = (sig, None)

    files, stale = {}, []
    for rel, (sig, fi) in wanted.items():
        hit = old_files.get(rel)
        if hit and hit[0] == sig:
            files[rel] = hit
        else:
            stale.append(rel)

    if stale:
        if len(stale) >= NOISY_REFRESH:
            note(f"scanning {len(stale)} files for graph edges...")
        texts = read_texts(mod_src.root, stale)
        for rel in stale:
            text = texts.get(rel)
            if text is None:
                continue
            sig, fi = wanted[rel]
            is_gui = rel.endswith(".gui")
            blocks = {}
            if fi is not None and fi in spans_by_fi:
                blocks = _scan_blocks(text.split("\n"), spans_by_fi[fi], is_gui,
                                      is_loc=rel.endswith(".yml"))
            whole = None
            if is_gui or fi is None:
                whole = _scan_whole(text, want_templates=is_gui)
            files[rel] = (sig, blocks, whole)
        tmp = cache.with_suffix(f".tmp{os.getpid()}")
        try:
            with open(tmp, "wb") as f:
                pickle.dump({"schema": GRAPH_SCHEMA, "files": files}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache)
        except OSError:
            pass
    return files


# --- graph assembly ---

class Graph:
    """Full dependency graph of the mod. Reachability is always computed over
    every definition; kind filters only choose what gets displayed."""

    def __init__(self, mod_src, sources=(), track_vars=False):
        scanned = scan_mod(mod_src)
        self.nodes = {}
        defs = mod_src.idx["defs"]
        # A loc key sharing a definition's name is that definition's display
        # text, so the definition keeps the node.
        for loc_pass in (False, True):
            for name, kind, fi, start, end, flag in defs:
                if name in GRAPH_NOISE_NAMES or (kind == "loc") != loc_pass:
                    continue
                if loc_pass and name in self.nodes:
                    continue
                self.nodes[name] = (kind, mod_src.files[fi], start, end)

        names = set(self.nodes)
        sorted_names = sorted(names)
        list_names = {n for n, v in self.nodes.items()
                      if v[0] == "scripted_lists"}
        template_memo = {}

        def expand(tok):
            hit = template_memo.get(tok)
            if hit is None:
                prefix = tok.split("$", 1)[0]
                lo = bisect_left(sorted_names, prefix)
                hi = bisect_left(sorted_names, prefix + "\U0010ffff")
                rx = template_regex(tok)
                hit = [c for c in sorted_names[lo:hi] if rx.fullmatch(c)]
                template_memo[tok] = hit
            return hit

        self.edges = {}
        block_vars = {}
        self.template_tokens = set()
        gui_tokens = set()
        concat = set()
        for rel, (sig, blocks, whole) in scanned.items():
            if whole is not None:
                gui_tokens |= whole[0]
                self.template_tokens |= whole[1]
                concat |= whole[2]
            for (name, start, end), (tokens, templates, bvars) in blocks.items():
                node = self.nodes.get(name)
                if node is None or node[1] != rel or node[2] != start:
                    continue
                targets = set(tokens & names)
                if list_names:
                    for tok in tokens:
                        for pfx in LIST_PREFIXES:
                            if tok.startswith(pfx) and tok[len(pfx):] in list_names:
                                targets.add(tok[len(pfx):])
                for tok in templates:
                    targets.update(expand(tok))
                    self.template_tokens.add(tok)
                targets.discard(name)
                self.edges[name] = targets
                if bvars:
                    block_vars[name] = bvars
        for name in self.nodes:
            self.edges.setdefault(name, set())

        for name, (kind, *_rest) in self.nodes.items():
            if kind != "loc":
                continue
            for sfx in LOC_DEF_SUFFIXES:
                if name.endswith(sfx):
                    base = name[:-len(sfx)]
                    if base in self.nodes and self.nodes[base][0] != "loc":
                        self.edges[base].add(name)

        self.template_tokens = {t for t in self.template_tokens
                                if not BARE_PARAM_RE.fullmatch(t)}
        self.concat_prefixes = concat

        if track_vars:
            for name, bvars in block_vars.items():
                for vn, role in bvars:
                    key = f"var:{vn}"
                    if key not in self.nodes:
                        self.nodes[key] = ("variable", "", 0, 0)
                        self.edges[key] = set()
                    if role == "W":
                        self.edges[name].add(key)
                    else:
                        self.edges[key].add(name)

        self.script_eps = set()
        self.db_roots = set()
        for name, (kind, *_rest) in self.nodes.items():
            if kind in ENGINE_EVALUATED_KINDS:
                self.script_eps.add(name)
            elif kind == "loc":
                if name.startswith(LOC_CONVENTION_PREFIXES):
                    self.script_eps.add(name)
            elif kind not in GRAPH_LIBRARY_KINDS:
                self.db_roots.add(name)
        # Redefining a vanilla name means vanilla's callers now run the mod's
        # version, so an override is live even with no caller in the mod.
        vanilla = [s for s in sources if s is not mod_src]
        self.overrides = {n for n in self.nodes
                          if any(n in s.by_name for s in vanilla)}
        self.gui_refs = gui_tokens & set(self.nodes)
        self.reachable = self._reach(self.script_eps | self.db_roots
                                     | self.gui_refs | self.overrides)

    def _reach(self, roots):
        seen = set(roots)
        queue = deque(roots)
        while queue:
            for t in self.edges.get(queue.popleft(), ()):
                if t not in seen:
                    seen.add(t)
                    queue.append(t)
        return seen

    def possibly_dynamic(self, names):
        """Names that $param$ templates or Concatenate() prefixes could
        build at runtime."""
        out = set()
        for name in names:
            if any(name.startswith(p) and len(name) > len(p)
                   for p in self.concat_prefixes):
                out.add(name)
                continue
            for tok in self.template_tokens:
                if name.startswith(tok.split("$", 1)[0]) and \
                        template_matches(tok, name):
                    out.add(name)
                    break
        return out


def project(edges, visible):
    """Collapse hidden nodes: each visible node points at the visible nodes
    it reaches through hidden-only paths. Returns (projected edges,
    frontier(hidden node) -> visible set)."""
    frontier = {}
    index, low, on_stack, stack = {}, {}, set(), []
    counter = 0

    def direct(n):
        return edges.get(n, ())

    for root in edges:
        if root in visible or root in index:
            continue
        work = [(root, iter(direct(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for t in it:
                if t in visible:
                    continue
                if t not in index:
                    index[t] = low[t] = counter
                    counter += 1
                    stack.append(t)
                    on_stack.add(t)
                    work.append((t, iter(direct(t))))
                    advanced = True
                    break
                if t in on_stack:
                    low[node] = min(low[node], index[t])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    m = stack.pop()
                    on_stack.discard(m)
                    members.append(m)
                    if m == node:
                        break
                reach = set()
                member_set = set(members)
                for m in members:
                    for t in direct(m):
                        if t in visible:
                            reach.add(t)
                        elif t not in member_set:
                            reach |= frontier[t]
                for m in members:
                    frontier[m] = reach

    projected = {}
    for v in visible:
        out = set()
        for t in direct(v):
            if t in visible:
                out.add(t)
            else:
                out |= frontier.get(t, set())
        out.discard(v)
        projected[v] = out
    return projected, frontier


# --- rendering ---

def find_connected_clusters(edges, orphans):
    undirected = {n: set() for n in orphans}
    for n in orphans:
        for t in edges.get(n, ()):
            if t in orphans:
                undirected[n].add(t)
                undirected[t].add(n)

    visited = set()
    clusters = []
    for n in sorted(orphans):
        if n in visited:
            continue
        cluster = set()
        queue = deque([n])
        while queue:
            cur = queue.popleft()
            if cur in visited:
                continue
            visited.add(cur)
            cluster.add(cur)
            queue.extend(t for t in undirected[cur] if t not in visited)
        clusters.append(cluster)
    return clusters


def dep_tree(name, edges, nodes, depth, seen, prefix, max_depth,
             cycle_counts=None):
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


def _print_by_kind(names, nodes, prefix, args, dim=False, suffix=None):
    """Group names under kind headers. suffix(name) adds trailing text."""
    d_on = C_DIM if dim and color_enabled() else ""
    d_off = C_RESET if d_on else ""
    by_kind = {}
    for name in names:
        by_kind.setdefault(nodes[name][0], []).append(name)
    kinds = sorted(by_kind)
    for ki, kind in enumerate(kinds):
        k_last = (ki == len(kinds) - 1)
        k_conn = "└── " if k_last else "├── "
        k_pre = prefix + ("    " if k_last else "│   ")
        items = sorted(by_kind[kind])
        cs, ce = c_kind(kind)
        print(f"{prefix}{k_conn}{kind} ({len(items)})")
        for ii, name in enumerate(items):
            i_conn = "└── " if ii == len(items) - 1 else "├── "
            loc = ""
            if args.paths and nodes[name][1]:
                loc = f"  {nodes[name][1]}:{nodes[name][2]}"
            extra = suffix(name) if suffix else ""
            print(f"{k_pre}{i_conn}{d_on}{cs}{name}{ce}{extra}{loc}{d_off}")


def _print_orphan_group(orphan_set, nodes, edges, args, s_pre,
                        has_next=False):
    """Render a set of orphans as clusters + isolated, under s_pre."""
    if not orphan_set:
        if not has_next:
            print(f"{s_pre}└── (none)")
        return
    clusters = find_connected_clusters(edges, orphan_set)
    connected = [c for c in clusters if len(c) > 1]
    isolated = sorted(n for c in clusters if len(c) == 1 for n in c)
    parts = [("cluster", i, c) for i, c in enumerate(connected, 1)]
    if isolated:
        parts.append(("isolated", 0, isolated))

    for pi, (ptype, idx, data) in enumerate(parts):
        p_last = (pi == len(parts) - 1) and not has_next
        p_conn = "└── " if p_last else "├── "
        p_pre = s_pre + ("    " if p_last else "│   ")

        if ptype == "cluster":
            print(f"{s_pre}{p_conn}Cluster {idx} "
                  f"({len(data)} nodes, disconnected)")
            items = sorted(data)
            for ii, name in enumerate(items):
                i_conn = "└── " if ii == len(items) - 1 else "├── "
                kind = nodes[name][0]
                targets_in = edges[name] & data
                arrow = ""
                if targets_in:
                    arrow = f" -> {', '.join(sorted(targets_in))}"
                cs, ce = c_kind(kind)
                print(f"{p_pre}{i_conn}{cs}{name}{ce} ({kind}){arrow}")
        else:
            print(f"{s_pre}{p_conn}Isolated ({len(data)})")
            _print_by_kind(data, nodes, p_pre, args)


def visible_kinds(args):
    """None means every kind."""
    if args.kind:
        include = set()
        for k in args.kind:
            include.update(k.replace(",", " ").split())
    elif args.all:
        return None
    else:
        include = set(GRAPH_DEFAULT_KINDS)
    if args.include_loc:
        include.add("loc")
    if args.include_gui:
        include |= GRAPH_GUI_KINDS
    if args.vars:
        include.add("variable")
    return include


def cmd_graph(args, sources):
    mod_src = None
    for s in sources:
        if s.name == "mod":
            mod_src = s
            break
    if not mod_src:
        print("Error: no mod root found")
        return 1

    g = Graph(mod_src, sources, track_vars=args.vars)
    nodes = g.nodes
    include = visible_kinds(args)
    visible = {n for n, v in nodes.items()
               if include is None or v[0] in include}

    if args.depth is None:
        args.depth = 6 if args.name else 2

    # Drill-down mode
    if args.name:
        name = args.name
        if name not in nodes:
            print(f"No definition found for '{name}'")
            suggest(name, sources)
            return 1
        visible.add(name)
        edges, _ = project(g.edges, visible)
        in_edges = {n: set() for n in visible}
        for src, targets in edges.items():
            for t in targets:
                in_edges[t].add(src)
        kind = nodes[name][0]
        cs, ce = c_kind(kind)
        loc = ""
        if args.paths and nodes[name][1]:
            loc = f"  {nodes[name][1]}:{nodes[name][2]}-{nodes[name][3]}"
        tags = ""
        if name in g.script_eps:
            tags += " [entry point]"
        elif name in g.db_roots:
            tags += " [engine-loaded]"
        if name in g.overrides:
            tags += " [overrides vanilla]"
        if name in g.gui_refs:
            tags += " [gui]"
        if name not in g.reachable:
            tags += " [orphan]"
        print(f"{cs}{name}{ce} ({kind}){tags}{loc}")

        seen = {name}
        cycle_counts = {}
        if args.up:
            dep_tree(name, in_edges, nodes, 1, seen, "", args.depth,
                     cycle_counts)
        else:
            callers = sorted(in_edges.get(name, set()))
            if callers:
                dim = C_DIM if color_enabled() else ""
                rst = C_RESET if color_enabled() else ""
                print(f"├── {dim}called by: {', '.join(callers[:10])}"
                      + (f" +{len(callers)-10}" if len(callers) > 10 else "")
                      + rst)
            dep_tree(name, edges, nodes, 1, seen, "", args.depth,
                     cycle_counts)
        print_cycles(cycle_counts, "")
        return 0

    edges, frontier = project(g.edges, visible)
    gui_eps = g.gui_refs & visible
    engine_eps = (g.script_eps & visible) - gui_eps
    db_eps = (g.db_roots & visible) - gui_eps - engine_eps
    override_eps = (g.overrides & visible) - db_eps - gui_eps - engine_eps
    # Visible nodes reached through filtered-out on_actions (e.g. events the
    # engine fires under --kind event) stand in as the entry points.
    via = {}
    for ep in g.script_eps - visible:
        if nodes[ep][0] != "on_action":
            continue
        for v in frontier.get(ep, ()):
            if v not in engine_eps and v not in gui_eps:
                via.setdefault(v, set()).add(ep)
    orphans = visible - g.reachable
    dynamic = g.possibly_dynamic(orphans)
    true_orphans = orphans - dynamic

    sections = []
    if not args.orphans_only:
        if engine_eps or via:
            sections.append("entry_points")
        if gui_eps:
            sections.append("gui_referenced")
        if db_eps or override_eps:
            sections.append("engine_loaded")
    sections.append("orphans")

    edge_count = sum(len(t) for t in edges.values())
    dyn_s = f", {len(dynamic)} possibly dynamic" if dynamic else ""
    print(f"DEPENDENCY GRAPH  "
          f"{len(visible)} defs, {edge_count} edges, "
          f"{len(engine_eps)} engine, {len(gui_eps)} gui, "
          f"{len(visible & g.reachable)} reachable, "
          f"{len(true_orphans)} orphans{dyn_s}")
    if len(visible) < len(nodes):
        dim = C_DIM if color_enabled() else ""
        rst = C_RESET if color_enabled() else ""
        print(f"{dim}reachability traced through all {len(nodes)} mod "
              f"definitions; {len(nodes) - len(visible)} of other kinds "
              f"are hidden{rst}")

    for si, section in enumerate(sections):
        s_last = (si == len(sections) - 1)
        s_conn = "└── " if s_last else "├── "
        s_pre = "    " if s_last else "│   "

        if section == "entry_points":
            print(f"{s_conn}ENTRY POINTS (engine-evaluated)")
            eps = sorted(engine_eps) + sorted(via)
            for ei, ep in enumerate(eps):
                e_last = (ei == len(eps) - 1)
                e_conn = "└── " if e_last else "├── "
                e_pre = s_pre + ("    " if e_last else "│   ")
                kind = nodes[ep][0]
                cs, ce = c_kind(kind)
                loc = ""
                if args.paths:
                    loc = f"  {nodes[ep][1]}:{nodes[ep][2]}-{nodes[ep][3]}"
                via_s = ""
                if ep in via:
                    srcs = sorted(via[ep])
                    dim = C_DIM if color_enabled() else ""
                    rst = C_RESET if color_enabled() else ""
                    more = f" +{len(srcs) - 3}" if len(srcs) > 3 else ""
                    via_s = f" {dim}via {', '.join(srcs[:3])}{more}{rst}"
                print(f"{s_pre}{e_conn}{cs}{ep}{ce} ({kind}){via_s}{loc}")
                if edges.get(ep):
                    seen = {ep}
                    cycle_counts = {}
                    dep_tree(ep, edges, nodes, 1, seen, e_pre, args.depth,
                             cycle_counts)
                    print_cycles(cycle_counts, e_pre)

        elif section == "gui_referenced":
            print(f"{s_conn}GUI-REFERENCED (used in .gui data binding)")
            _print_by_kind(
                gui_eps, nodes, s_pre, args,
                suffix=lambda n: (f" -> {', '.join(sorted(edges[n]))}"
                                  if edges.get(n) else ""))

        elif section == "engine_loaded":
            print(f"{s_conn}ENGINE-LOADED (database definitions and vanilla "
                  f"overrides; `graph <name>` expands one)")
            rows = []
            for group, label in ((db_eps, "with dependencies"),
                                 (override_eps, "overriding vanilla")):
                by_kind = {}
                for n in group:
                    by_kind.setdefault(nodes[n][0], []).append(n)
                for kind, names in by_kind.items():
                    if group is db_eps:
                        count = sum(1 for n in names if edges.get(n))
                        rows.append(f"{kind} ({len(names)}, {count} {label})")
                    else:
                        rows.append(f"{kind} ({len(names)} {label})")
            rows.sort()
            for ri, row in enumerate(rows):
                r_conn = "└── " if ri == len(rows) - 1 else "├── "
                print(f"{s_pre}{r_conn}{row}")

        elif section == "orphans":
            dyn_label = (f", {len(dynamic)} possibly dynamic"
                         if dynamic else "")
            print(f"{s_conn}ORPHANS ({len(true_orphans)} unreachable"
                  f"{dyn_label})")
            if not orphans:
                print(f"{s_pre}└── (none)")
                continue
            _print_orphan_group(true_orphans, nodes, edges, args, s_pre,
                                has_next=bool(dynamic))
            if dynamic:
                print(f"{s_pre}└── POSSIBLY DYNAMIC "
                      f"({len(dynamic)} names matched by "
                      f"$param$ or Concatenate patterns)")
                _print_by_kind(dynamic, nodes, s_pre + "    ", args,
                               dim=True)

    return 0
