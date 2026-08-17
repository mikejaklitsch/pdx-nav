import json
import re
import sys

from .classify import template_matches
from .constants import (
    ENGINE_EVALUATED_KINDS, GRAPH_DEFAULT_KINDS, GRAPH_NOISE_NAMES,
    GRAPH_TEMPLATE_RE, GRAPH_VAR_HAS_RE, GRAPH_VAR_NAME_RE,
    GRAPH_VAR_REF_RE, GRAPH_VAR_WRITE_KW, GRAPH_VARLIST_RE, GRAPH_VARMAP_RE,
    LOC_CONVENTION_PREFIXES, SCAN_TOPDIRS, WORD_RE,
)
from .output import c_kind, color_enabled, suggest, C_DIM, C_RESET
from .paths import cache_dir_for
from .scanner import split_line


def build_raw_graph(mod_src):
    """Scan all definitions and extract per-block token sets, template tokens,
    and variable references. Results are cached alongside the index."""
    cache = cache_dir_for("mod", mod_src.root)
    cache_path = cache / "graph.json"
    idx_hash = mod_src.idx.get("hash", "")

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("hash") == idx_hash:
                return cached
        except (json.JSONDecodeError, KeyError):
            pass

    sys.stderr.write("pdx-nav: scanning blocks for graph edges...\n")
    all_names = set()
    all_defs = {}
    for d in mod_src.idx["defs"]:
        name, kind, fi, start, end, flag = d
        if name in GRAPH_NOISE_NAMES:
            continue
        all_names.add(name)
        all_defs[name] = (kind, fi, start, end)

    list_names = {n for n, (k, *_) in all_defs.items() if k == "scripted_lists"}
    LIST_PREFIXES = ("every_", "ordered_", "any_", "random_")

    raw_edges = {}
    raw_vars = {}
    file_cache = {}
    for name, (kind, fi, start, end) in all_defs.items():
        if fi not in file_cache:
            try:
                file_cache[fi] = mod_src.read_file(fi).split("\n")
            except OSError:
                file_cache[fi] = []
        lines = file_cache[fi]
        is_gui = mod_src.files[fi].endswith(".gui")
        block_tokens = set()
        template_tokens = set()
        block_vars = []
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
            if GRAPH_VAR_WRITE_KW.search(masked):
                in_var_write = True
            if in_var_write:
                for m in GRAPH_VAR_NAME_RE.finditer(masked):
                    block_vars.append([m.group(1), "W"])
                if "}" in masked:
                    in_var_write = False
            for m in GRAPH_VAR_HAS_RE.finditer(masked):
                block_vars.append([m.group(1), "R"])
            for m in GRAPH_VAR_REF_RE.finditer(masked):
                block_vars.append([m.group(1), "R"])
            for m in GRAPH_VARMAP_RE.finditer(masked):
                block_vars.append([m.group(1), "M"])
            for m in GRAPH_VARLIST_RE.finditer(masked):
                block_vars.append([m.group(1), "L"])

        block_tokens.discard(name)
        matched = list(block_tokens & all_names)
        for tok in block_tokens - all_names:
            if list_names:
                for pfx in LIST_PREFIXES:
                    if tok.startswith(pfx):
                        base = tok[len(pfx):]
                        if base in list_names:
                            matched.append(base)
        for tok in template_tokens:
            if re.fullmatch(r'\$\w+\$', tok):
                continue
            prefix = tok.split("$", 1)[0]
            for candidate in all_names:
                if candidate.startswith(prefix) and template_matches(tok, candidate):
                    matched.append(candidate)
        raw_edges[name] = sorted(set(matched))
        if block_vars:
            raw_vars[name] = block_vars

    gui_tokens = set()
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
            gui_tokens.update(WORD_RE.findall(code))

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
                    gui_tokens.update(WORD_RE.findall(code))

    result = {"hash": idx_hash, "edges": raw_edges, "vars": raw_vars,
              "gui_tokens": sorted(gui_tokens & all_names)}
    cache_path.write_text(json.dumps(result, separators=(",", ":")))
    return result


def build_graph(mod_src, all_sources, include_kinds=None, exclude_kinds=None,
                track_vars=False):
    raw = build_raw_graph(mod_src)

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
    edges = {}
    entry_points = set()
    for name in nodes:
        edges[name] = set(t for t in raw["edges"].get(name, []) if t in name_set)
        kind = nodes[name][0]
        if kind in ENGINE_EVALUATED_KINDS:
            entry_points.add(name)
        if kind == "loc" and any(name.startswith(p) for p in LOC_CONVENTION_PREFIXES):
            entry_points.add(name)

    if track_vars:
        all_vars = set()
        for name in nodes:
            for vn, role in raw["vars"].get(name, []):
                all_vars.add(vn)
        for vn in all_vars:
            var_key = f"var:{vn}"
            nodes[var_key] = ("variable", "", 0, 0)
            edges[var_key] = set()
        for name in list(nodes):
            for vn, role in raw["vars"].get(name, []):
                var_key = f"var:{vn}"
                if var_key not in nodes:
                    continue
                if role == "W":
                    edges.setdefault(name, set()).add(var_key)
                else:
                    edges[var_key].add(name)
        name_set = set(nodes.keys())

    gui_refs = set(t for t in raw.get("gui_tokens", []) if t in name_set)
    entry_points.update(gui_refs)

    return nodes, edges, entry_points, gui_refs


def compute_reachability(nodes, edges, entry_points):
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

    # Drill-down mode
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

    sections = []
    if not args.orphans_only:
        sections.append("entry_points")
        if gui_only_eps:
            sections.append("gui_referenced")
    sections.append("orphans")

    edge_count = sum(len(t) for t in edges.values())
    print(f"DEPENDENCY GRAPH  "
          f"{len(nodes)} defs, {edge_count} edges, "
          f"{len(engine_eps)} engine, {len(gui_refs)} gui, "
          f"{len(reachable)} reachable, {len(orphans)} orphans")

    for si, section in enumerate(sections):
        s_last = (si == len(sections) - 1)
        s_conn = "└── " if s_last else "├── "
        s_pre = "    " if s_last else "│   "

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
