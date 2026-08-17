BOM = '﻿'


def read_text_preserve(path):
    """Read UTF-8 (BOM-tolerant) without newline translation, so CRLF
    files round-trip byte-exact."""
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return f.read()


def write_text_exact(path, content):
    """Write UTF-8 without newline translation."""
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(content)


def ensure_trailing_newline(content):
    return content if content.endswith('\n') else content + '\n'
