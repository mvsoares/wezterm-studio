"""Dual-mode cross-platform configuration engine, Lua validator, backups, and presets.

Supports two operating modes seamlessly:
1. Standalone Mode (standard WezTerm users on Linux, macOS, Windows):
   - Stores state in `wezterm_theme.json` and generates `wezterm_theme.lua`.
   - `wezterm-theme init` adds a safe, portable hook block to `wezterm.lua`
     (and `wezterm-theme uninstall` cleanly removes it).
2. Loader Mode (`linux-setup` JSON loader with `defaults.json` + `settings.json`):
   - Syncs fonts, window, cursor, colors, and tab metadata into `settings.json`
     and generates `tab_theme.lua`.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import find_wezterm_cmd, get_platform_name, resolve_wezterm_paths
from .themes import (
    THEMES,
    generate_lua_for_theme,
    get_theme_by_id,
)

MAX_AUTO_BACKUPS = 20
HOOK_START = "-- >>> wezterm-theme >>>"
HOOK_END = "-- <<< wezterm-theme <<<"

_KEEP = object()

DEFAULT_APPEARANCE: dict[str, Any] = {
    "font_family": None,
    "font_weight": "Regular",
    "font_size": None,
    "line_height": None,
    "tab_font_family": None,
    "tab_font_weight": "Medium",
    "tab_font_size": None,
    "color_scheme": None,
    "window_opacity": None,
    "window_padding": None,
    "window_decorations": None,
    "cursor_style": None,
    "cursor_blink_rate": None,
    "audible_bell": None,
    "scrollback_lines": None,
    "hide_tab_bar_if_only_one_tab": None,
    "tab_bar_at_bottom": None,
    "use_fancy_tab_bar": None,
    "tab_max_width": None,
}


def sanitize_legacy_lua(lua_code: str) -> str:
    """Normalize deprecated config keys found in older backups."""
    return lua_code.replace("show_tab_index_in_tab_title", "show_tab_index_in_tab_bar")


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def is_loader_mode(paths: dict[str, Path] | None = None) -> bool:
    """Return True if WezTerm config uses the `defaults.json` + `settings.json` JSON loader."""
    paths = paths or resolve_wezterm_paths()
    if paths["defaults_json"].is_file():
        return True
    cfg = paths["config_file"]
    if cfg.is_file():
        try:
            head = cfg.read_text(encoding="utf-8", errors="ignore")[:512]
            if "-- WezTerm loader" in head:
                return True
        except Exception:
            pass
    return False


def is_hook_installed(paths: dict[str, Path] | None = None) -> bool:
    """Return True if `wezterm.lua` loads the active theme module."""
    paths = paths or resolve_wezterm_paths()
    if is_loader_mode(paths):
        return True
    cfg = paths["config_file"]
    if not cfg.is_file():
        return False
    try:
        txt = cfg.read_text(encoding="utf-8", errors="ignore")
        return HOOK_START in txt or "wezterm_theme.lua" in txt or "tab_theme.lua" in txt
    except Exception:
        return False


def _parse_lua_metadata(lua_path: Path) -> tuple[int | None, int | None, dict[str, str] | None, bool]:
    """Extract (theme_id, tab_title_len, symbols, has_file) from a generated Lua module."""
    if not lua_path.is_file():
        return None, None, None, False
    try:
        content = lua_path.read_text(encoding="utf-8", errors="ignore")
        tid = None
        tlen = None
        syms = None
        m_id = re.search(r"-- Theme #(\d+):", content)
        if m_id:
            tid = int(m_id.group(1))
        m_len = re.search(r"TAB_TITLE_LEN\s*=\s*(\d+)", content)
        if m_len:
            tlen = int(m_len.group(1))
        m_sym = re.search(r"^-- Symbols: (\{.*\})$", content, re.MULTILINE)
        if m_sym:
            syms = json.loads(m_sym.group(1))
        return tid, tlen, syms, True
    except Exception:
        return None, None, None, True


def _extract_loader_appearance(merged: dict[str, Any]) -> dict[str, Any]:
    app = dict(DEFAULT_APPEARANCE)
    f = merged.get("font") if isinstance(merged.get("font"), dict) else {}
    c = merged.get("colors") if isinstance(merged.get("colors"), dict) else {}
    w = merged.get("window") if isinstance(merged.get("window"), dict) else {}
    tb = merged.get("tabs") if isinstance(merged.get("tabs"), dict) else {}
    cu = merged.get("cursor") if isinstance(merged.get("cursor"), dict) else {}
    be = merged.get("bell") if isinstance(merged.get("bell"), dict) else {}
    pf = merged.get("performance") if isinstance(merged.get("performance"), dict) else {}
    d = merged.get("display") if isinstance(merged.get("display"), dict) else {}

    app["font_family"] = f.get("family")
    app["font_weight"] = f.get("weight") or "Regular"
    app["font_size"] = f.get("size")
    app["line_height"] = f.get("line_height")

    ff = tb.get("frame_font") if isinstance(tb.get("frame_font"), dict) else {}
    app["tab_font_family"] = ff.get("family")
    app["tab_font_weight"] = ff.get("weight") or "Medium"
    app["tab_font_size"] = ff.get("size")

    app["color_scheme"] = c.get("scheme")
    app["window_opacity"] = w.get("opacity")

    pad = w.get("padding")
    if isinstance(pad, dict):
        vals = [pad.get(k) for k in ("left", "right", "top", "bottom") if isinstance(pad.get(k), (int, float))]
        app["window_padding"] = int(vals[0]) if vals and len(set(vals)) == 1 else pad
    elif isinstance(pad, (int, float)):
        app["window_padding"] = int(pad)

    app["window_decorations"] = w.get("decorations") or (
        "TITLE | RESIZE" if d.get("use_x11") else "INTEGRATED_BUTTONS | RESIZE"
    )
    app["cursor_style"] = cu.get("style")
    app["cursor_blink_rate"] = cu.get("blink_rate")
    app["audible_bell"] = be.get("audible")
    app["scrollback_lines"] = pf.get("scrollback_lines")
    app["hide_tab_bar_if_only_one_tab"] = tb.get("hide_if_only_one_tab")
    app["tab_bar_at_bottom"] = tb.get("at_bottom")
    app["use_fancy_tab_bar"] = None  # Controlled per-theme unless overridden
    app["tab_max_width"] = tb.get("max_width")
    return app


def get_current_state(paths: dict[str, Path] | None = None) -> dict[str, Any]:
    """Read complete unified state (mode, hook status, tab theme, symbols, font & appearance)."""
    paths = paths or resolve_wezterm_paths()
    loader = is_loader_mode(paths)

    if loader:
        defaults = _read_json_safe(paths["defaults_json"])
        settings = _read_json_safe(paths["settings_json"])
        merged = _deep_merge(defaults, settings)
        tabs_cfg = merged.get("tabs") if isinstance(merged.get("tabs"), dict) else {}

        tid, tlen, syms, has_lua = _parse_lua_metadata(paths["tab_theme_lua"])
        if tid is None:
            tid = tabs_cfg.get("theme_id") if isinstance(tabs_cfg.get("theme_id"), int) else 10
        if tlen is None:
            tlen = tabs_cfg.get("tab_title_len") if isinstance(tabs_cfg.get("tab_title_len"), int) else 15
        if not has_lua and syms is None and isinstance(tabs_cfg.get("symbols"), dict):
            syms = tabs_cfg.get("symbols")

        return {
            "mode": "loader",
            "initialized": True,
            "platform": get_platform_name(),
            "config_file": str(paths["config_file"]),
            "active_module": str(paths["tab_theme_lua"]),
            "theme_id": tid,
            "tab_title_len": tlen,
            "symbols": syms,
            "appearance": _extract_loader_appearance(merged),
        }

    # Standalone mode
    studio = _read_json_safe(paths["studio_json"])
    lua_target = paths["studio_lua"] if paths["studio_lua"].is_file() else paths["tab_theme_lua"]
    tid, tlen, syms, has_lua = _parse_lua_metadata(lua_target)

    if tid is None:
        tid = studio.get("theme_id") if isinstance(studio.get("theme_id"), int) else 10
    if tlen is None:
        tlen = studio.get("tab_title_len") if isinstance(studio.get("tab_title_len"), int) else 15
    if not has_lua and syms is None and isinstance(studio.get("symbols"), dict):
        syms = studio.get("symbols")

    saved_app = studio.get("appearance") if isinstance(studio.get("appearance"), dict) else {}
    appearance = {**DEFAULT_APPEARANCE, **saved_app}

    return {
        "mode": "standalone",
        "initialized": is_hook_installed(paths),
        "platform": get_platform_name(),
        "config_file": str(paths["config_file"]),
        "active_module": str(paths["studio_lua"]),
        "theme_id": tid,
        "tab_title_len": tlen,
        "symbols": syms,
        "appearance": appearance,
    }


def detect_current_theme_id() -> int:
    return int(get_current_state()["theme_id"])


def detect_current_tab_title_len() -> int:
    return int(get_current_state()["tab_title_len"])


def detect_current_symbols() -> dict[str, str] | None:
    return get_current_state()["symbols"]


def validate_config(config_path: Path) -> str | None:
    """Validate a WezTerm Lua configuration file using `wezterm ls-fonts --text a`."""
    wz = find_wezterm_cmd()
    if not wz:
        return None
    try:
        res = subprocess.run(
            [*wz, "--config-file", str(config_path), "ls-fonts", "--text", "a"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "o WezTerm demorou demais para validar a configuração"
    errors = [line for line in res.stderr.splitlines() if " ERROR " in line]
    return "\n".join(errors) or None


def validate_theme_lua(lua_code: str) -> str | None:
    """Validate a generated theme Lua module in an isolated temporary directory."""
    with tempfile.TemporaryDirectory() as tmp:
        mod_path = Path(tmp) / "wezterm_theme.lua"
        mod_path.write_text(lua_code, encoding="utf-8")
        wrapper = Path(tmp) / "wezterm.lua"
        wrapper.write_text(
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder and wezterm.config_builder() or {}\n"
            f"dofile([==[{mod_path.as_posix()}]==]).apply(config, wezterm)\n"
            "return config\n",
            encoding="utf-8",
        )
        return validate_config(wrapper)


def _build_hook_block(cfg_var: str = "config") -> str:
    return (
        f"{HOOK_START}\n"
        "do\n"
        "  local _wt_wz = require 'wezterm'\n"
        "  local _wt_p = (_wt_wz.config_dir or '') .. '/wezterm_theme.lua'\n"
        "  local _wt_f = io.open(_wt_p, 'r')\n"
        "  if not _wt_f and _wt_wz.home_dir then\n"
        "    _wt_p = _wt_wz.home_dir .. '/.config/wezterm/wezterm_theme.lua'\n"
        "    _wt_f = io.open(_wt_p, 'r')\n"
        "  end\n"
        "  if _wt_f then\n"
        "    _wt_f:close()\n"
        "    local _wt_ok, _wt_m = pcall(dofile, _wt_p)\n"
        "    if _wt_ok and type(_wt_m) == 'table' and type(_wt_m.apply) == 'function' then\n"
        f"      _wt_m.apply({cfg_var}, _wt_wz)\n"
        "    end\n"
        "  end\n"
        "end\n"
        f"{HOOK_END}"
    )


def init_wezterm_config(paths: dict[str, Path] | None = None) -> tuple[bool, str]:
    """Integrate `wezterm-theme` non-destructively into the user's `wezterm.lua`."""
    paths = paths or resolve_wezterm_paths()
    paths["config_dir"].mkdir(parents=True, exist_ok=True)

    if is_loader_mode(paths):
        # Ensure theme module exists
        if not paths["tab_theme_lua"].is_file():
            state = get_current_state(paths)
            theme = get_theme_by_id(state["theme_id"]) or THEMES[9]
            apply_theme(theme, tab_title_len=state["tab_title_len"], symbols=state["symbols"], paths=paths)
        return True, f"Modo WezTerm JSON Loader detectado e pronto em {paths['config_file']}"

    cfg_file = paths["config_file"]
    # Ensure studio_lua exists first
    if not paths["studio_lua"].is_file():
        state = get_current_state(paths)
        theme = get_theme_by_id(state["theme_id"]) or THEMES[9]
        lua_code = generate_lua_for_theme(
            theme,
            tab_title_len=state["tab_title_len"],
            symbols=state["symbols"],
            appearance=state["appearance"],
            watch_files=[paths["studio_lua"].as_posix(), paths["studio_json"].as_posix()],
        )
        paths["studio_lua"].write_text(lua_code, encoding="utf-8")
        paths["studio_json"].write_text(
            json.dumps(
                {
                    "theme_id": theme["id"],
                    "tab_title_len": state["tab_title_len"],
                    "symbols": state["symbols"],
                    "appearance": {k: v for k, v in state["appearance"].items() if v is not None},
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    if not cfg_file.is_file():
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        fresh = (
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder and wezterm.config_builder() or {}\n\n"
            f"{_build_hook_block('config')}\n\n"
            "return config\n"
        )
        cfg_file.write_text(fresh, encoding="utf-8")
        err = validate_config(cfg_file)
        if err:
            cfg_file.unlink(missing_ok=True)
            return False, f"Falha ao validar novo wezterm.lua: {err}"
        return True, f"Criado {cfg_file} integrado ao wezterm-theme!"

    original = cfg_file.read_text(encoding="utf-8")
    if HOOK_START in original:
        return True, f"wezterm-theme já está integrado em {cfg_file}"

    # Match `return <identifier>` near EOF
    m_ret = re.search(r"^([ \t]*)return[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:--.*)?$", original, re.MULTILINE)
    if m_ret:
        # Use the last top-level return identifier in the file
        matches = list(
            re.finditer(r"^([ \t]*)return[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:--.*)?$", original, re.MULTILINE)
        )
        last = matches[-1]
        cfg_var = last.group(2)
        hook = _build_hook_block(cfg_var)
        updated = original[: last.start()] + hook + "\n\n" + original[last.start() :]
    else:
        # Fallback for `return { ... }` style configs
        m_tbl = list(re.finditer(r"^([ \t]*)return[ \t]*\{", original, re.MULTILINE))
        if not m_tbl:
            return (
                False,
                f"Não foi possível localizar 'return config' em {cfg_file}. "
                f"Adicione manualmente antes do return:\n{_build_hook_block('config')}",
            )
        last = m_tbl[-1]
        hook = _build_hook_block("_wt_config")
        updated = (
            original[: last.start()]
            + f"{last.group(1)}local _wt_config = {{"
            + original[last.end() :]
            + f"\n\n{hook}\nreturn _wt_config\n"
        )

    # Backup original wezterm.lua before modifying
    bkp_dir = paths["backup_dir"]
    bkp_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(cfg_file, bkp_dir / f"wezterm.lua.{stamp}_pre_init.bak")

    cfg_file.write_text(updated, encoding="utf-8")
    err = validate_config(cfg_file)
    if err:
        cfg_file.write_text(original, encoding="utf-8")
        return False, f"O WezTerm rejeitou a alteração em {cfg_file} (revertido automaticamente): {err}"

    return True, f"Integração adicionada com sucesso em {cfg_file}"


def uninstall_wezterm_config(
    remove_files: bool = False,
    paths: dict[str, Path] | None = None,
) -> tuple[bool, str]:
    """Remove the `wezterm-theme` hook block from `wezterm.lua` cleanly."""
    paths = paths or resolve_wezterm_paths()
    cfg_file = paths["config_file"]
    removed_hook = False

    if cfg_file.is_file() and not is_loader_mode(paths):
        original = cfg_file.read_text(encoding="utf-8")
        if HOOK_START in original and HOOK_END in original:
            pattern = re.compile(
                r"\n?" + re.escape(HOOK_START) + r".*?" + re.escape(HOOK_END) + r"\n?",
                re.DOTALL,
            )
            cleaned = pattern.sub("\n", original)
            cfg_file.write_text(cleaned, encoding="utf-8")
            err = validate_config(cfg_file)
            if err:
                cfg_file.write_text(original, encoding="utf-8")
                return False, f"Erro ao remover bloco de {cfg_file} (revertido): {err}"
            removed_hook = True

    if remove_files:
        paths["studio_lua"].unlink(missing_ok=True)
        paths["studio_json"].unlink(missing_ok=True)

    if removed_hook:
        return True, f"Hook do wezterm-theme removido de {cfg_file}."
    return True, "Nenhum hook de modo standalone encontrado para remover."


def _sync_loader_settings(
    paths: dict[str, Path],
    theme_id: int | None = None,
    tab_title_len: int | None = None,
    symbols: Any = _KEEP,
    appearance_updates: dict[str, Any] | None = None,
) -> None:
    """Update `settings.json` in Loader mode while preserving all custom user sections."""
    settings_path = paths["settings_json"]
    data = _read_json_safe(settings_path)

    tabs = data.setdefault("tabs", {})
    if not isinstance(tabs, dict):
        tabs = {}
        data["tabs"] = tabs

    if theme_id is not None:
        tabs["theme_id"] = int(theme_id)
    if tab_title_len is not None:
        tabs["tab_title_len"] = int(tab_title_len)
    if symbols is not _KEEP:
        if symbols is None:
            tabs.pop("symbols", None)
        else:
            tabs["symbols"] = symbols

    if appearance_updates:
        if "font_family" in appearance_updates or "font_weight" in appearance_updates or "font_size" in appearance_updates or "line_height" in appearance_updates:
            f = data.setdefault("font", {})
            if appearance_updates.get("font_family"):
                f["family"] = str(appearance_updates["font_family"])
            if appearance_updates.get("font_weight"):
                f["weight"] = str(appearance_updates["font_weight"])
            if appearance_updates.get("font_size") is not None:
                f["size"] = float(appearance_updates["font_size"]) if float(appearance_updates["font_size"]) % 1 else int(float(appearance_updates["font_size"]))
            if appearance_updates.get("line_height") is not None:
                f["line_height"] = round(float(appearance_updates["line_height"]), 2)

        if "tab_font_family" in appearance_updates or "tab_font_weight" in appearance_updates or "tab_font_size" in appearance_updates:
            ff = tabs.setdefault("frame_font", {})
            if appearance_updates.get("tab_font_family"):
                ff["family"] = str(appearance_updates["tab_font_family"])
            if appearance_updates.get("tab_font_weight"):
                ff["weight"] = str(appearance_updates["tab_font_weight"])
            if appearance_updates.get("tab_font_size") is not None:
                val = float(appearance_updates["tab_font_size"])
                ff["size"] = val if val % 1 else int(val)

        if "color_scheme" in appearance_updates:
            c = data.setdefault("colors", {})
            scheme = appearance_updates["color_scheme"]
            if not scheme or scheme == "Default":
                c.pop("scheme", None)
            else:
                c["scheme"] = str(scheme)

        if "window_opacity" in appearance_updates or "window_padding" in appearance_updates or "window_decorations" in appearance_updates:
            w = data.setdefault("window", {})
            if appearance_updates.get("window_opacity") is not None:
                w["opacity"] = round(float(appearance_updates["window_opacity"]), 2)
            if appearance_updates.get("window_padding") is not None:
                pad = appearance_updates["window_padding"]
                if isinstance(pad, dict):
                    w["padding"] = {k: int(pad.get(k, 10)) for k in ("left", "right", "top", "bottom")}
                else:
                    p = int(float(pad))
                    w["padding"] = {"left": p, "right": p, "top": p, "bottom": p}
            if appearance_updates.get("window_decorations"):
                w["decorations"] = str(appearance_updates["window_decorations"])

        if "cursor_style" in appearance_updates or "cursor_blink_rate" in appearance_updates:
            cu = data.setdefault("cursor", {})
            if appearance_updates.get("cursor_style"):
                cu["style"] = str(appearance_updates["cursor_style"])
            if appearance_updates.get("cursor_blink_rate") is not None:
                cu["blink_rate"] = int(appearance_updates["cursor_blink_rate"])

        if "audible_bell" in appearance_updates:
            be = data.setdefault("bell", {})
            if appearance_updates.get("audible_bell"):
                be["audible"] = str(appearance_updates["audible_bell"])

        if "scrollback_lines" in appearance_updates and appearance_updates.get("scrollback_lines") is not None:
            pf = data.setdefault("performance", {})
            pf["scrollback_lines"] = int(appearance_updates["scrollback_lines"])

        if "hide_tab_bar_if_only_one_tab" in appearance_updates and appearance_updates["hide_tab_bar_if_only_one_tab"] is not None:
            tabs["hide_if_only_one_tab"] = bool(appearance_updates["hide_tab_bar_if_only_one_tab"])
        if "tab_bar_at_bottom" in appearance_updates and appearance_updates["tab_bar_at_bottom"] is not None:
            tabs["at_bottom"] = bool(appearance_updates["tab_bar_at_bottom"])
        if "use_fancy_tab_bar" in appearance_updates and appearance_updates["use_fancy_tab_bar"] is not None:
            tabs["fancy"] = bool(appearance_updates["use_fancy_tab_bar"])
        if "tab_max_width" in appearance_updates and appearance_updates["tab_max_width"] is not None:
            tabs["max_width"] = int(appearance_updates["tab_max_width"])

    paths["config_dir"].mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup_current_config(label: str | None = None, paths: dict[str, Path] | None = None) -> Path | None:
    """Create a collision-free timestamped backup of the active Lua module & state."""
    paths = paths or resolve_wezterm_paths()
    loader = is_loader_mode(paths)
    active_lua = paths["tab_theme_lua"] if loader else paths["studio_lua"]
    if not active_lua.is_file():
        return None

    bkp_dir = paths["backup_dir"]
    bkp_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    clean_label = f"_{re.sub(r'[^a-zA-Z0-9_-]', '_', label)}" if label else ""

    dest = bkp_dir / f"tab_theme.lua.{stamp}{clean_label}.bak"
    seq = 2
    while dest.exists():
        dest = bkp_dir / f"tab_theme.lua.{stamp}-{seq}{clean_label}.bak"
        seq += 1

    shutil.copy2(active_lua, dest)

    # Also save companion state JSON alongside .bak if settings/studio json exists
    state_src = paths["settings_json"] if loader else paths["studio_json"]
    if state_src.is_file():
        try:
            shutil.copy2(state_src, dest.with_suffix(".json"))
        except OSError:
            pass

    if not label:
        prune_auto_backups(paths=paths)
    return dest


def prune_auto_backups(keep: int = MAX_AUTO_BACKUPS, paths: dict[str, Path] | None = None) -> None:
    """Keep only the newest unlabeled automatic backups; labeled backups are never pruned."""
    paths = paths or resolve_wezterm_paths()
    bkp_dir = paths["backup_dir"]
    if not bkp_dir.is_dir():
        return
    autos = [
        f
        for f in bkp_dir.glob("tab_theme.lua.*.bak")
        if re.fullmatch(r"tab_theme\.lua\.\d{8}-\d{6}(?:-\d+)?\.bak", f.name)
    ]
    autos.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    for f in autos[keep:]:
        f.unlink(missing_ok=True)
        f.with_suffix(".json").unlink(missing_ok=True)


def get_available_backups(paths: dict[str, Path] | None = None) -> list[dict[str, Any]]:
    """Retrieve and parse all saved backups sorted newest first."""
    paths = paths or resolve_wezterm_paths()
    bkp_dir = paths["backup_dir"]
    if not bkp_dir.is_dir():
        return []

    backups: list[dict[str, Any]] = []
    for f in bkp_dir.glob("tab_theme.lua.*.bak"):
        m = re.fullmatch(r"tab_theme\.lua\.(\d{8}-\d{6})(?:-\d+)?(?:_(.+))?\.bak", f.name)
        label = ""
        if m:
            raw_stamp = m.group(1)
            label = m.group(2) or ""
            try:
                dt = datetime.strptime(raw_stamp, "%Y%m%d-%H%M%S")
                date_str = dt.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                date_str = raw_stamp
        else:
            date_str = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m/%Y %H:%M:%S")

        theme_info = "Configuração Personalizada"
        theme_id = None
        content_norm = ""
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            content_norm = sanitize_legacy_lua(content).strip()
            tm = re.search(r"-- Theme #(\d+):\s*([^\n\r]+)", content)
            lm = re.search(r"TAB_TITLE_LEN\s*=\s*(\d+)", content)
            len_suffix = f" · {lm.group(1)}c" if lm else ""
            if tm:
                theme_id = int(tm.group(1))
                theme_info = f"Tema #{theme_id:02d}: {tm.group(2).strip()}{len_suffix}"
        except Exception:
            pass

        backups.append({
            "path": f,
            "filename": f.name,
            "date_str": date_str,
            "label": label,
            "theme_info": theme_info,
            "theme_id": theme_id,
            "content_norm": content_norm,
            "mtime": f.stat().st_mtime,
        })

    backups.sort(key=lambda b: (b["mtime"], b["filename"]), reverse=True)
    return backups


def _config_watches_file(cfg_file: Path) -> bool:
    if not cfg_file.is_file():
        return False
    try:
        return "add_to_config_reload_watch_list" in cfg_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False


def _install_module_transactional(
    target_lua: Path,
    lua_code: str,
    paths: dict[str, Path],
) -> str | None:
    """Validate `lua_code` standalone and against `config_file`, rolling back on error."""
    lua_code = sanitize_legacy_lua(lua_code)
    err = validate_theme_lua(lua_code)
    if err:
        return err

    target_lua.parent.mkdir(parents=True, exist_ok=True)
    prev = target_lua.read_text(encoding="utf-8") if target_lua.is_file() else None
    target_lua.write_text(lua_code, encoding="utf-8")

    cfg_file = paths["config_file"]
    err = validate_config(cfg_file) if cfg_file.is_file() else None
    if err:
        if prev is None:
            target_lua.unlink(missing_ok=True)
        else:
            target_lua.write_text(prev, encoding="utf-8")
        return err

    if cfg_file.is_file() and (prev is None or not _config_watches_file(cfg_file)):
        try:
            cfg_file.touch()
        except OSError:
            pass
    return None


def apply_theme(
    theme: dict[str, Any],
    tab_title_len: int | None = None,
    symbols: Any = _KEEP,
    appearance_updates: dict[str, Any] | None = None,
    paths: dict[str, Path] | None = None,
) -> tuple[bool, Path | None, str | None]:
    """Apply a tab theme (and optional appearance/font updates) atomically with validation."""
    paths = paths or resolve_wezterm_paths()
    paths["config_dir"].mkdir(parents=True, exist_ok=True)

    state = get_current_state(paths)
    loader = state["mode"] == "loader"

    eff_len = int(tab_title_len) if tab_title_len is not None else int(state["tab_title_len"])
    eff_syms = state["symbols"] if symbols is _KEEP else symbols

    bkp_path = backup_current_config(paths=paths)

    if loader:
        prev_settings = paths["settings_json"].read_text(encoding="utf-8") if paths["settings_json"].is_file() else None
        lua_code = generate_lua_for_theme(theme, tab_title_len=eff_len, symbols=eff_syms)
        _sync_loader_settings(
            paths,
            theme_id=theme["id"],
            tab_title_len=eff_len,
            symbols=eff_syms,
            appearance_updates=appearance_updates,
        )
        err = _install_module_transactional(paths["tab_theme_lua"], lua_code, paths)
        if err is not None:
            if prev_settings is None:
                paths["settings_json"].unlink(missing_ok=True)
            else:
                paths["settings_json"].write_text(prev_settings, encoding="utf-8")
            return False, bkp_path, err
        return True, bkp_path, None

    # Standalone mode: if no wezterm.lua exists yet, auto-create it so first-time users work immediately!
    merged_app = dict(state["appearance"])
    if appearance_updates:
        for k, v in appearance_updates.items():
            if k in DEFAULT_APPEARANCE:
                merged_app[k] = v

    clean_app = {k: v for k, v in merged_app.items() if v is not None}
    lua_code = generate_lua_for_theme(
        theme,
        tab_title_len=eff_len,
        symbols=eff_syms,
        appearance=clean_app if clean_app else None,
        watch_files=[paths["studio_lua"].as_posix(), paths["studio_json"].as_posix()],
    )

    prev_studio = paths["studio_json"].read_text(encoding="utf-8") if paths["studio_json"].is_file() else None
    paths["studio_json"].write_text(
        json.dumps(
            {
                "theme_id": theme["id"],
                "tab_title_len": eff_len,
                "symbols": eff_syms,
                "appearance": clean_app,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    err = _install_module_transactional(paths["studio_lua"], lua_code, paths)
    if err is not None:
        if prev_studio is None:
            paths["studio_json"].unlink(missing_ok=True)
        else:
            paths["studio_json"].write_text(prev_studio, encoding="utf-8")
        return False, bkp_path, err

    if not paths["config_file"].is_file():
        init_wezterm_config(paths=paths)

    return True, bkp_path, None


def apply_appearance(
    updates: dict[str, Any],
    paths: dict[str, Path] | None = None,
) -> tuple[bool, str | None]:
    """Apply font, size, scheme, window, cursor, or tab bar appearance updates."""
    paths = paths or resolve_wezterm_paths()
    state = get_current_state(paths)
    theme = get_theme_by_id(state["theme_id"]) or THEMES[9]
    ok, _, err = apply_theme(
        theme,
        tab_title_len=state["tab_title_len"],
        symbols=state["symbols"],
        appearance_updates=updates,
        paths=paths,
    )
    return ok, err


def restore_backup(
    target_spec: str | int = 1,
    paths: dict[str, Path] | None = None,
) -> tuple[bool, str]:
    """Restore a backup by 1-based index, filename, or label substring."""
    paths = paths or resolve_wezterm_paths()
    backups = get_available_backups(paths=paths)
    if not backups:
        return False, f"Nenhum backup encontrado em {paths['backup_dir']}"

    selected = None
    spec_str = str(target_spec).strip()
    if spec_str.isdigit():
        idx = int(spec_str) - 1
        if 0 <= idx < len(backups):
            selected = backups[idx]
        else:
            return False, f"Índice de backup inválido: {spec_str}. Escolha entre 1 e {len(backups)}."
    else:
        for b in backups:
            if spec_str in b["filename"] or (b["label"] and spec_str.lower() in b["label"].lower()):
                selected = b
                break
        if not selected:
            return False, f"Nenhum backup correspondente a '{spec_str}'."

    loader = is_loader_mode(paths)
    target_lua = paths["tab_theme_lua"] if loader else paths["studio_lua"]
    target_json = paths["settings_json"] if loader else paths["studio_json"]

    try:
        content = sanitize_legacy_lua(selected["path"].read_text(encoding="utf-8"))
        backup_current_config(label="pre_restore", paths=paths)

        companion_json = selected["path"].with_suffix(".json")
        prev_json = target_json.read_text(encoding="utf-8") if target_json.is_file() else None
        if companion_json.is_file():
            shutil.copy2(companion_json, target_json)

        err = _install_module_transactional(target_lua, content, paths)
        if err:
            if prev_json is not None:
                target_json.write_text(prev_json, encoding="utf-8")
            return (
                False,
                f"O WezTerm rejeitou esse backup; a configuração atual foi mantida. "
                f"{err.strip().splitlines()[-1][:160]}",
            )

        if loader and not companion_json.is_file():
            tid, tlen, syms, _ = _parse_lua_metadata(target_lua)
            _sync_loader_settings(paths, theme_id=tid, tab_title_len=tlen, symbols=syms)

        idx_display = backups.index(selected) + 1
        return (
            True,
            f"Backup [{idx_display}] ({selected['theme_info']} - {selected['date_str']}) restaurado com sucesso!",
        )
    except Exception as exc:
        return False, f"Erro ao restaurar backup: {exc}"


def export_preset(dest_path: Path | None = None, paths: dict[str, Path] | None = None) -> tuple[str, Path | None]:
    """Export portable cross-platform preset JSON (shareable across Linux, macOS, Windows)."""
    state = get_current_state(paths=paths)
    preset = {
        "schema": "wezterm-theme-preset/v1",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "theme_id": state["theme_id"],
        "tab_title_len": state["tab_title_len"],
        "symbols": state["symbols"],
        "appearance": {k: v for k, v in state["appearance"].items() if v is not None},
    }
    raw = json.dumps(preset, indent=2, ensure_ascii=False) + "\n"
    if dest_path is not None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(raw, encoding="utf-8")
    return raw, dest_path


def import_preset(preset_data: dict[str, Any] | str | Path, paths: dict[str, Path] | None = None) -> tuple[bool, str]:
    """Import and apply a preset JSON dictionary, JSON string, or file path."""
    paths = paths or resolve_wezterm_paths()
    try:
        if isinstance(preset_data, Path):
            data = json.loads(preset_data.read_text(encoding="utf-8"))
        elif isinstance(preset_data, str):
            p = Path(preset_data).expanduser()
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
            else:
                data = json.loads(preset_data)
        elif isinstance(preset_data, dict):
            data = preset_data
        else:
            return False, "Formato de preset inválido."
    except Exception as exc:
        return False, f"Não foi possível ler o preset JSON: {exc}"

    tid = int(data.get("theme_id", 10))
    theme = get_theme_by_id(tid)
    if not theme:
        return False, f"Tema #{tid} não existe no catálogo (1–60)."

    tlen = int(data.get("tab_title_len", 15))
    tlen = max(4, min(60, tlen))
    syms = data.get("symbols")
    if syms is not None and not isinstance(syms, dict):
        syms = None
    app = data.get("appearance") if isinstance(data.get("appearance"), dict) else {}

    ok, _, err = apply_theme(
        theme,
        tab_title_len=tlen,
        symbols=syms,
        appearance_updates=app,
        paths=paths,
    )
    if not ok:
        return False, f"O WezTerm rejeitou o preset: {err}"
    return True, f"Preset aplicado com sucesso (Tema #{theme['id']:02d} {theme['name']})!"
