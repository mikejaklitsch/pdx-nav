# pdx-nav

IDE-style navigation for EU5 PDX script, across mod + vanilla. Built for
agent workflows: every result carries the enclosing definition and its exact
line span, so the next step is a surgical read — never another broad grep.

## Commands

```bash
pdx-nav def <name>...        # definitions in all sources, overrides (INJECT/REPLACE) included
pdx-nav show <name>          # print the block (--vanilla/--mod, -n max lines)
pdx-nav refs <name>...       # all references, role-tagged (definition/use/string/override)
pdx-nav callers <name>       # definitions whose block references <name>
pdx-nav stack <name>         # transitive caller tree up to on_action entry points
pdx-nav vars <name>          # variable write/read sites (set_variable family vs var:/has_variable)
pdx-nav list <glob> [--kind] # enumerate definitions
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
(`sul_pay_frac_$ESTATE$`) that expand to the queried name — without this,
generated names look like they are never written.

## How it works

- **Index** (definitions, kinds, line spans): built once per source. Kind is
  derived from the directory (`common/scripted_effects/` → effect, `events/`
  → event, localization ymls → loc, .gui → template/types). Top-level blocks
  only; `INJECT:`/`REPLACE:`/`TRY_INJECT:` prefixes are recorded as override
  flags. Cached at `<mod>/.claude/pdx_nav/` and `~/.cache/pdx-nav/`.
- **Corpus**: a local concatenated copy (`fileid:line:text`) of all indexed
  files, so reference queries ripgrep one native-filesystem file instead of
  crawling thousands of files on a slow /mnt drive.
- **Cache validation**: vanilla is trusted via a patch sentinel
  (`<install>/binaries/checksum.txt` changes every patch); the mod side
  re-stats its tree with a 30s TTL so query bursts stay fast. Recent edits
  may take up to 30s to appear; `pdx-nav rebuild` forces it.
- **Hit classification** uses the same comment/string-aware scanner as
  pdx-tag/pdx-format: hits inside comments are hidden by default, hits inside
  string literals are tagged `string` (loc keys, tooltips), code hits are
  `use`. Enclosure is resolved from the span index.
- Excluded from indexing: `gfx/map` (generated map-object blobs),
  tool/VCS directories.

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
