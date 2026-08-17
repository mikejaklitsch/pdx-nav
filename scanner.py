def split_line(line, in_string=False):
    """Split into (code, comment, masked, in_string_after).

    Comment starts at any '#' outside a string; masked blanks string
    contents (same length as code) so brace/word positions index into
    code."""
    masked = []
    for i, ch in enumerate(line):
        if ch == '"':
            in_string = not in_string
            masked.append(' ')
        elif in_string:
            masked.append(' ')
        elif ch == '#':
            return line[:i], line[i:], ''.join(masked), in_string
        else:
            masked.append(ch)
    return line, '', ''.join(masked), in_string


def structural_balance(content):
    """Net open-minus-close brace count, ignoring comments and strings."""
    net = 0
    in_string = False
    for line in content.split('\n'):
        _, _, masked, in_string = split_line(line, in_string)
        net += masked.count('{') - masked.count('}')
    return net
