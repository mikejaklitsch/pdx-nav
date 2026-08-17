# pdx-nav

IDE-style navigation for EU5 PDX script and GUI, across mod + vanilla. Built
for agent workflows: every result carries the enclosing definition and its
exact line span, so the next step is a surgical read instead of another broad
grep.

## Commands

```bash
pdx-nav def <name>...        # definitions in all sources, overrides (INJECT/REPLACE) included
pdx-nav show <name>          # print the block (--vanilla/--mod, -n max lines)
pdx-nav context <name>       # show definition with parent hierarchy (indentation walk-up)
pdx-nav refs <name>...       # all references, role-tagged (definition/use/string/override)
pdx-nav callers <name>       # definitions whose block references <name>
pdx-nav stack <name>         # transitive caller tree up to on_action entry points
pdx-nav vars <name>          # variable write/read sites (set_variable family vs var:/has_variable)
pdx-nav list <glob> [--kind] # enumerate definitions
pdx-nav list --file <path>   # list all definitions in one file (file order)
pdx-nav grep <pattern>       # full-text search over indexed symbol names
pdx-nav graph                # dependency graph: trees, orphans, cross-system edges
pdx-nav rebuild              # force reindex (run after a game patch if the sentinel misses it)
```

`stack` reconstructs dispatch chains end to end, e.g.:

```
sul_estate_wages_pay_all (effect .../sul_estate_wages.txt:19-25, mod)
^ sul_wealth_monthly_update (effect .../sul_wealth_core.txt:292-329, mod)
  ^ sul_economy.10 (event .../sul_economy_events.txt:6-14, mod)
    ^ sul_monthly_country_pulse (on_action .../sul_parallel_on_actions.txt:5-14, mod) [entry point]
      ^ monthly_country_pulse (on_action .../sul_hardcoded.txt:59-65, mod) [entry point]
```

`refs`/`vars` also detect `$param$`-templated constructions
(`sul_pay_frac_$ESTATE$`) that expand to the queried name. Without this,
generated names look like they are never written.

`graph` builds a color-coded dependency tree of the full mod: which on_actions
the engine calls, what they dispatch, where subsystems connect, and what is
orphaned. Drill into any definition with `graph <name>`, or trace callers
upstream with `graph <name> --up`.

## What it indexes

Scripts (.txt), GUI (.gui), and localization (.yml) across all three EU5
domains (in_game, main_menu, loading_screen). Definition kinds:

- **Script**: effects, triggers, script_values, on_actions, events, buildings,
  production methods, reforms, privileges, and all other `common/` directories
- **GUI**: templates, types, local_templates, types blocks, `@variable`
  definitions (gui_var)
- **Localization**: loc keys from `l_english` yml files

Top-level blocks only; `INJECT:`/`REPLACE:`/`TRY_INJECT:` prefixes are
recorded as override flags.

## How it works

- **Index** (definitions, kinds, line spans): built once per source. Cached
  at `<mod>/.claude/pdx_nav/` and `~/.cache/pdx-nav/`.
- **Corpus**: a local concatenated copy (`fileid:line:text`) of all indexed
  files, so reference queries ripgrep one native-filesystem file instead of
  crawling thousands of files on a slow /mnt drive.
- **Cache validation**: vanilla uses a patch sentinel
  (`<install>/binaries/checksum.txt` changes every patch) when available,
  otherwise a file-hash check. The mod side re-stats its tree per invocation.
  `pdx-nav rebuild` forces a full reindex.
- **Vanilla tracker**: when a bare git repo is found at
  `$PDX_VANILLA_REPO` or `<mod-parent>/vanilla-tracker/repo.git`, it is used
  instead of the live game install. `--at <version>` selects a specific tag.
- **Hit classification** uses a comment/string-aware scanner: hits inside
  comments are hidden by default, hits inside string literals are tagged
  `string`, code hits are `use`. Enclosure is resolved from the span index.
- Excluded from indexing: `gfx/map` (generated map-object blobs),
  tool/VCS directories.

## Package structure

```
pdx_nav/
  constants.py   # paths, regex patterns, kind maps
  scanner.py     # comment/string-aware line parser (split_line)
  paths.py       # mod/vanilla root discovery, file collection
  parsing.py     # definition parsing (parse_defs, kind_for_path)
  source.py      # Source/GitSource classes, index building, vanilla-tracker
  classify.py    # hit classification, reference gathering, template matching
  output.py      # formatting, color, suggestions
  commands.py    # def, show, context, refs, callers, stack, vars, list, grep, rebuild
  graph.py       # dependency graph building, reachability, graph command
  cli.py         # argument parsing and dispatch
pdx_nav.py       # thin shim for symlink compatibility
```

## Setup

```bash
ln -sf "$(pwd)/pdx_nav.py" ~/.local/bin/pdx-nav
# vanilla root: defaults to the Steam install; override with PDX_GAME_ROOT
```

First run builds the vanilla index (~5 min over /mnt; one-time per patch).
Warm queries run in about 1-3s.

## Known limitations

- Variable write detection is heuristic: a `name = X` line counts as a write
  when a `*_variable*`-family opener is on the same line or within the 4
  lines above (pdx-format's compact style makes this reliable in practice).
- Multi-line string literals are classified per-line during hit
  classification (they only occur in excluded generated gfx files).
- Nested definitions (laws inside law groups, PMs inside buildings) index as
  part of their top-level block, not separately.
