import argparse
import sys

from .commands import (cmd_def, cmd_show, cmd_context, cmd_refs, cmd_callers,
                       cmd_stack, cmd_vars, cmd_list, cmd_grep, cmd_rename,
                       cmd_rebuild)
from .graph import cmd_graph
from .source import load_sources


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

    ctx = sub.add_parser("context", help="Show definition with parent hierarchy")
    ctx.add_argument("name")
    ctx.add_argument("-n", "--lines", type=int, default=60)
    ctx.add_argument("--vanilla", dest="source", action="store_const", const="vanilla")
    ctx.add_argument("--mod", dest="source", action="store_const", const="mod")

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

    rn = sub.add_parser("rename", help="Rename a symbol across all mod files")
    rn.add_argument("old", help="Current name")
    rn.add_argument("new", help="New name")
    rn.add_argument("--dry-run", action="store_true",
                    help="Show what would change without writing files")

    sub.add_parser("rebuild", help="Force index rebuild")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0

    if args.cmd == "rebuild":
        return cmd_rebuild(args, None)

    sources = load_sources(at_version=args.at)

    if hasattr(args, "source") and args.source == "vanilla":
        matches = [s for s in sources if s.name.startswith("vanilla")]
        if matches:
            args.source = matches[0].name

    return {
        "def": cmd_def, "show": cmd_show, "context": cmd_context,
        "refs": cmd_refs, "callers": cmd_callers, "stack": cmd_stack,
        "vars": cmd_vars, "list": cmd_list, "grep": cmd_grep,
        "rename": cmd_rename, "graph": cmd_graph,
    }[args.cmd](args, sources)


if __name__ == "__main__":
    sys.exit(main() or 0)
