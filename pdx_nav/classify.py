import re

from .constants import TEMPLATE_TOKEN_RE
from .scanner import split_line


def template_matches(tok, name):
    """Does a $param$-templated token expand to <name> for some substitution?"""
    rx = re.escape(tok)
    rx = re.sub(r'\\\$\w+\\\$', r'\\w+', rx)
    return re.fullmatch(rx, name) is not None


def classify_hit(name, text, src, fi, line_no):
    code, comment, _, _ = split_line(text)
    tok = re.compile(rf'(?<![\w.]){re.escape(name)}(?![\w])')
    in_code = bool(tok.search(code))
    in_comment = bool(tok.search(comment))
    role = None
    if in_code:
        for d in src.by_name.get(name, []):
            if d[2] == fi and d[3] == line_no:
                role = "override" if d[5] else "definition"
                break
        if role is None:
            _, _, masked, _ = split_line(text)
            role = "use" if tok.search(masked) else "string"
    elif in_comment:
        role = "comment"
    else:
        role = "string"
    return role


def gather_refs(name, sources, include_roles=None):
    """Return [(src, fi, line, role, text, encl)] for every occurrence."""
    out = []
    for src in sources:
        for fi, ln, text in src.rg(name, fixed=True, word=True):
            if not re.search(rf'(?<![\w.]){re.escape(name)}(?!\w)', text):
                continue
            role = classify_hit(name, text, src, fi, ln)
            if include_roles and role not in include_roles:
                continue
            encl = src.enclosing(fi, ln)
            out.append((src, fi, ln, role, text.strip(), encl))
    return out


def templated_hits(name, sources):
    """Find $param$-templated constructions that could expand to <name>."""
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
