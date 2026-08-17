# pdx-common

Shared utilities for PDX modding tools (pdx-nav, pdx-gui, pdx-format, etc.).

Vendored into each tool repo via `git subtree` so every tool is
self-contained and cloneable. Edit here, then push updates to each tool:

```bash
# Pull updates into a tool repo:
cd ../pdx-nav
git subtree pull --prefix=pdx_common ../pdx-common master --squash
```

## Modules

| Module | Contents |
|--------|----------|
| `scanner.py` | `split_line` (comment/string-aware line parser), `structural_balance` |
| `paths.py` | `find_mod_root`, `vanilla_root`, `DEFAULT_VANILLA_ROOT` |
| `fileio.py` | BOM-aware `read_text_preserve`/`write_text_exact`, `ensure_trailing_newline` |
