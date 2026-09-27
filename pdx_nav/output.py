import re

from pdx_utilities.terminal import color_enabled, C_RESET, C_DIM  # noqa: F401

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


def c_kind(kind):
    if not color_enabled():
        return "", ""
    return KIND_COLORS.get(kind, ""), C_RESET


def fmt_def(d, src):
    name, kind, fi, s, e, flag = d
    loc = f"{src.files[fi]}:{s}-{e}" if e > s else f"{src.files[fi]}:{s}"
    flag_s = f" [{flag}]" if flag else ""
    return f"  {kind:<18} {loc}  ({src.name}){flag_s}"


def _suggest_pool(name, names, cap=3000, grams=False):
    """Narrow ~10^5 indexed names to plausible candidates before difflib
    (which costs a SequenceMatcher per candidate). Candidates must fit the
    length bound implied by the 0.6 cutoff and share name segments (or, with
    grams, 3-letter substrings) with the query; the most-overlapping ones
    are kept."""
    n = len(name)
    lo, hi = n * 3 / 7, n * 7 / 3
    if grams:
        segs = {name[i:i + 3] for i in range(max(n - 2, 1))}
    else:
        segs = {s for s in re.split(r'[_.\-:]+', name) if len(s) >= 3}
    if not segs:
        return []
    scored = []
    for c in names:
        if lo <= len(c) <= hi:
            hits = sum(1 for s in segs if s in c)
            if hits:
                scored.append((hits, c))
    if len(scored) > cap:
        scored.sort(key=lambda x: -x[0])
        scored = scored[:cap]
    return [c for _, c in scored]


def suggest(name, sources):
    import difflib
    names = set()
    for src in sources:
        names.update(src.by_name.keys())
    close = difflib.get_close_matches(name, _suggest_pool(name, names),
                                      n=5, cutoff=0.6)
    if not close:
        close = difflib.get_close_matches(
            name, _suggest_pool(name, names, grams=True), n=5, cutoff=0.6)
    if close:
        print("Did you mean:")
        for c in close:
            kinds = {d[1] for src in sources for d in src.by_name.get(c, [])}
            print(f"  - {c}  [{', '.join(sorted(kinds))}]")
    if re.fullmatch(r"[a-z0-9_]+", name):
        print("Note: engine tokens (effects/triggers/modifiers) live in "
              "pdx-syntax; GUI internals in pdx-gui.")
