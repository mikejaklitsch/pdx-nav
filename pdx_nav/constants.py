import re
from pathlib import Path

VANILLA_CACHE_DIR = Path.home() / ".cache" / "pdx-nav"

SCAN_TOPDIRS = ("in_game", "main_menu", "loading_screen")
EXCLUDE_PARTS = {".git", ".claude", ".gui_workspace", "__pycache__", "tools",
                 ".metadata", ".dev-mods"}
EXCLUDE_SUBTREES = ("gfx/map",)
CODE_EXTS = {".txt", ".gui"}

KIND_DISPLAY = {
    "scripted_effects": "effect",
    "scripted_triggers": "trigger",
    "script_values": "script_value",
    "on_action": "on_action",
    "scripted_guis": "scripted_gui",
    "building_types": "building",
    "production_methods": "production_method",
    "government_reforms": "reform",
    "estate_privileges": "privilege",
    "casus_belli": "casus_belli",
    "subject_types": "subject_type",
    "generic_actions": "generic_action",
}

DEF_RE = re.compile(r'^\s*(INJECT:|REPLACE:|TRY_INJECT:)?([A-Za-z_@][\w.]*)\s*[={]')
GUI_DEF_RE = re.compile(r'^\s*(template|types|type|local_template)\s+(\w+)')
GUI_VAR_RE = re.compile(r'^\s*@(\w+)\s*=\s*(.+)')
LOC_DEF_RE = re.compile(r'^\s*([\w.\-]+):\d*\s*"')

VAR_WRITE_FAMILY = re.compile(
    r'\b(?:set|change|round|clamp|divide|multiply|subtract_from|add_to|remove'
    r'|remove_from|cmf_change)_?\w*variable\w*\s*=')

ENTRY_KINDS = {"on_action"}
ENGINE_EVALUATED_KINDS = {"on_action", "auto_modifiers", "static_modifiers",
                          "pop_types", "location_ranks", "modifier_type_definitions",
                          "game_concepts", "modifier_icons", "data_binding",
                          "types"}

WORD_RE = re.compile(r'[A-Za-z_@][\w.]*')
TEMPLATE_TOKEN_RE = re.compile(r'[A-Za-z_][\w.]*(?:\$\w+\$[\w.]*)+')

GRAPH_EXCLUDE_KINDS = {"loc", "gui", "gui_var", "template", "type", "types",
                        "local_template", "map_modes"}
GRAPH_DEFAULT_KINDS = {"on_action", "effect", "event", "script_value"}
GRAPH_TEMPLATE_RE = re.compile(
    r'(?:[A-Za-z_][\w.]*)?'
    r'(?:\$\w+\$[\w.]*)'
    r'(?:[\w.]*\$\w+\$[\w.]*)*'
)
GRAPH_NOISE_NAMES = {"namespace", "l_english"}
GRAPH_VAR_WRITE_KW = re.compile(
    r'\b(?:set|change|round|clamp|divide|multiply|subtract_from|add_to'
    r'|remove_from|cmf_change)_?\w*variable\w*\s*=')
GRAPH_VAR_HAS_RE = re.compile(r'\bhas_\w*variable\s*=\s*(\w+)')
GRAPH_VAR_NAME_RE = re.compile(r'\bname\s*=\s*(\w+)')
GRAPH_VAR_REF_RE = re.compile(r'\b(?:global_|local_)?var:(\w+)')
GRAPH_VARMAP_RE = re.compile(r'variable_map\(\s*(\w+)\s*\|')
GRAPH_VARLIST_RE = re.compile(
    r'(?:add_to_variable_list|remove_from_variable_list'
    r'|is_in_variable_list|clear_variable_list'
    r'|every_in_variable_list|any_in_variable_list'
    r'|ordered_in_variable_list|variable_list_size)\s*=\s*(\w+)')
LOC_CONVENTION_PREFIXES = (
    "MODIFIER_TYPE_NAME_", "MODIFIER_TYPE_DESC_",
    "STATIC_MODIFIER_NAME_", "STATIC_MODIFIER_DESC_",
    "game_concept_", "mapmode_", "MAPMODE_",
)
