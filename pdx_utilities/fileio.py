BOM = '﻿'
BOM_BYTES = b'\xef\xbb\xbf'


def read_text_preserve(path):
    """Read UTF-8 (BOM-tolerant) without newline translation, so CRLF
    files round-trip byte-exact."""
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return f.read()


def read_with_bom(path):
    """Read a file, returning (content, has_bom) for round-tripping."""
    with open(path, 'rb') as f:
        has_bom = f.read(3) == BOM_BYTES
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read()
    return content, has_bom


def write_text_exact(path, content):
    """Write UTF-8 without newline translation."""
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(content)


def write_with_bom(path, content, bom=False):
    """Write content with optional BOM, LF line endings."""
    encoding = 'utf-8-sig' if bom else 'utf-8'
    with open(path, 'w', encoding=encoding, newline='\n') as f:
        f.write(content)


def strip_bom_bytes(raw):
    """Strip UTF-8 BOM from raw bytes if present, decode to str."""
    if raw[:3] == BOM_BYTES:
        raw = raw[3:]
    return raw.decode('utf-8', errors='replace')


def ensure_trailing_newline(content):
    return content if content.endswith('\n') else content + '\n'
