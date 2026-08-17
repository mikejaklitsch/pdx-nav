import re
import sys

KIND_COLORS = {
    "effect": "\033[32m",
    "event": "\033[33m",
    "script_value": "\033[36m",
    "on_action": "\033[91m",
    "static_modifiers": "\033[35m",
    "auto_modifiers": "\033[35m",
    "modifier_type_definitions": "\033[35m",
    "variable": "\033[2m",
    "types": "\033[1;34m",
    "type": "\033[36m",
    "template": "\033[32m",
    "local_template": "\033[2;32m",
    "block": "\033[33m",
    "blockoverride": "\033[2;33m",
    "@var": "\033[35m",
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
