from .constants import DEF_RE, GUI_DEF_RE, KIND_DISPLAY, LOC_DEF_RE
from .scanner import split_line


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
    pending = None
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
                defs.append((pending[0], pending[1], i, i, pending[2]))
                pending = None
        elif pending and new_depth == 0 and depth > 0:
            defs.append((pending[0], pending[1], pending[3], i, pending[2]))
            pending = None
        depth = max(new_depth, 0)
    if pending:
        defs.append((pending[0], pending[1], pending[3], pending[3], pending[2]))
    return defs
