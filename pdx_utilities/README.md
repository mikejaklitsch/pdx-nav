# pdx-utilities

Shared utilities for PDX modding tools (pdx-nav, pdx-gui, pdx-format, etc.).

Vendored into each tool repo via `git subtree` so every tool is
self-contained and cloneable. Edit here, then push updates to each tool:

```bash
# Pull updates into a tool repo:
cd ../pdx-nav
git subtree pull --prefix=pdx_utilities ../pdx-utilities master --squash
```

## Modules

| Module | Contents |
|--------|----------|
| `scanner.py` | `split_line` (comment/string-aware line parser), `structural_balance` |
| `paths.py` | `find_mod_root`, `find_mod_root_or_exit`, `vanilla_root`, `find_vanilla_repo` |
| `fileio.py` | BOM-aware I/O: `read_text_preserve`, `read_with_bom`, `write_text_exact`, `write_with_bom`, `strip_bom_bytes` |
| `constants.py` | `SCAN_TOPDIRS`, `CODE_EXTS`, `EXCLUDE_PARTS` |
| `files.py` | `collect_files` (enumerate script files under a game/mod root) |
| `git.py` | Bare-repo helpers: `git`, `git_tags`, `git_latest_tag`, `git_archive`, `git_extract_files`, `git_read_file` |
| `terminal.py` | `color_enabled` (NO_COLOR/TERM=dumb aware), ANSI constants |
