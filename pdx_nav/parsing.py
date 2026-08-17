from .constants import DEF_RE, GUI_DEF_RE, GUI_VAR_RE, KIND_DISPLAY, LOC_DEF_RE, MACRO_DEF_RE
from .scanner import split_line


def kind_for_path(rel):
    parts = rel.split("/")
    if len(parts) >= 2 and parts[1] == "localization":
        return "loc"
    if len(parts) >= 2 and parts[1] == "events":
        return "event"
    if len(parts) >= 2 and parts[1] == "data_binding":
        return "data_binding"
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

    is_macro_file = kind == "data_binding"
    depth = 0
    in_string = False
    pending = None
    macro_block_start = None
    for i, line in enumerate(text.split("\n"), 1):
        code, _, masked, in_string = split_line(line, in_string)
        if depth == 0:
            if kind == "gui":
                m = GUI_VAR_RE.match(code)
                if m:
                    defs.append((f"@{m.group(1)}", "gui_var", i, i, ""))
                elif (m := GUI_DEF_RE.match(code)):
                    pending = (m.group(2), m.group(1), "", i)
            elif is_macro_file:
                m = DEF_RE.match(code)
                if m and m.group(2) == "macro":
                    macro_block_start = i
                    pending = None
                elif m:
                    flag = (m.group(1) or "").rstrip(":")
                    pending = (m.group(2), kind, flag, i)
            else:
                m = DEF_RE.match(code)
                if m:
                    flag = (m.group(1) or "").rstrip(":")
                    pending = (m.group(2), kind, flag, i)
        elif is_macro_file and macro_block_start and depth == 1:
            m = MACRO_DEF_RE.match(code)
            if m:
                pending = (m.group(1), "macro", "", macro_block_start)
        opens, closes = masked.count("{"), masked.count("}")
        new_depth = depth + opens - closes
        if pending and depth == 0:
            if opens == 0 or new_depth == 0:
                defs.append((pending[0], pending[1], i, i, pending[2]))
                pending = None
                macro_block_start = None
        elif pending and new_depth == 0 and depth > 0:
            defs.append((pending[0], pending[1], pending[3], i, pending[2]))
            pending = None
            macro_block_start = None
        elif not pending and new_depth == 0 and depth > 0:
            macro_block_start = None
        depth = max(new_depth, 0)
    if pending:
        defs.append((pending[0], pending[1], pending[3], pending[3], pending[2]))
    return defs
