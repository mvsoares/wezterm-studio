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
    "font_ligatures": None,
    "font_fallback": None,
    "tab_font_family": None,
    "tab_font_weight": "Medium",
    "tab_font_size": None,
    "color_scheme": None,
    "color_scheme_light": None,
    "oled_black": None,
    "cursor_color": None,
    "window_opacity": None,
    "window_blur": None,
    "win32_backdrop": None,
    "window_padding": None,
    "window_decorations": None,
    "inactive_pane_brightness": None,
    "background_gradient": None,
    "accent_border": None,
    "cursor_style": None,
    "cursor_blink_rate": None,
    "audible_bell": None,
    "scrollback_lines": None,
    "front_end": None,
    "max_fps": None,
    "animation_fps": None,
    "enable_wayland": None,
    "quick_select": None,
    "hide_tab_bar_if_only_one_tab": None,
    "tab_bar_at_bottom": None,
    "use_fancy_tab_bar": None,
    "tab_max_width": None,
    "status_bar": None,
    "open_in_editor": None,
    "editor_cmd": None,
    "agent_indicators": None,
    "ssh_alert": None,
    "command_palette": None,
    "workspace_switcher": None,
    "session_manager": None,
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


def _extract_loader_appearance(merged: dict[str, Any], settings_only: dict[str, Any] | None = None) -> dict[str, Any]:
    app = dict(DEFAULT_APPEARANCE)
    f = merged.get("font") if isinstance(merged.get("font"), dict) else {}
    c = merged.get("colors") if isinstance(merged.get("colors"), dict) else {}
    w = merged.get("window") if isinstance(merged.get("window"), dict) else {}
    tb = merged.get("tabs") if isinstance(merged.get("tabs"), dict) else {}
    cu = merged.get("cursor") if isinstance(merged.get("cursor"), dict) else {}
    be = merged.get("bell") if isinstance(merged.get("bell"), dict) else {}
    pf = merged.get("performance") if isinstance(merged.get("performance"), dict) else {}
    d = merged.get("display") if isinstance(merged.get("display"), dict) else {}
    st_sec = merged.get("status") if isinstance(merged.get("status"), dict) else {}
    hl = merged.get("hyperlinks") if isinstance(merged.get("hyperlinks"), dict) else {}
    user_tb = (settings_only or {}).get("tabs") if isinstance((settings_only or {}).get("tabs"), dict) else {}

    app["font_family"] = f.get("family")
    app["font_weight"] = f.get("weight") or "Regular"
    app["font_size"] = f.get("size")
    app["line_height"] = f.get("line_height")
    app["font_ligatures"] = f.get("ligatures") if isinstance(f.get("ligatures"), bool) else None
    app["font_fallback"] = f.get("fallback") if isinstance(f.get("fallback"), bool) else None

    ff = tb.get("frame_font") if isinstance(tb.get("frame_font"), dict) else {}
    app["tab_font_family"] = ff.get("family")
    app["tab_font_weight"] = ff.get("weight") or "Medium"
    app["tab_font_size"] = ff.get("size")

    app["color_scheme"] = c.get("scheme")
    app["color_scheme_light"] = c.get("scheme_light") or None
    if c.get("oled_black") is not None:
        app["oled_black"] = bool(c.get("oled_black"))
    app["cursor_color"] = c.get("cursor_color") or None
    app["window_opacity"] = w.get("opacity")
    app["window_blur"] = w.get("blur")
    app["win32_backdrop"] = w.get("win32_backdrop") or None

    pad = w.get("padding")
    if isinstance(pad, dict):
        vals = [pad.get(k) for k in ("left", "right", "top", "bottom") if isinstance(pad.get(k), (int, float))]
        app["window_padding"] = int(vals[0]) if vals and len(set(vals)) == 1 else pad
    elif isinstance(pad, (int, float)):
        app["window_padding"] = int(pad)

    app["window_decorations"] = w.get("decorations") or (
        "TITLE | RESIZE" if d.get("use_x11") else "INTEGRATED_BUTTONS | RESIZE"
    )
    hsb = w.get("inactive_pane_hsb")
    if isinstance(hsb, dict) and isinstance(hsb.get("brightness"), (int, float)):
        app["inactive_pane_brightness"] = float(hsb["brightness"])
    if w.get("background_gradient") is not None:
        app["background_gradient"] = bool(w.get("background_gradient"))
    if w.get("accent_border") is not None:
        app["accent_border"] = bool(w.get("accent_border"))
    app["cursor_style"] = cu.get("style")
    app["cursor_blink_rate"] = cu.get("blink_rate")
    app["audible_bell"] = be.get("audible")
    app["scrollback_lines"] = pf.get("scrollback_lines")
    app["front_end"] = pf.get("front_end") or None
    app["max_fps"] = pf.get("max_fps")
    app["animation_fps"] = pf.get("animation_fps")
    if d.get("enable_wayland") is not None:
        app["enable_wayland"] = bool(d.get("enable_wayland"))
    if hl.get("quick_select") is not None:
        app["quick_select"] = bool(hl.get("quick_select"))
    app["hide_tab_bar_if_only_one_tab"] = tb.get("hide_if_only_one_tab")
    app["tab_bar_at_bottom"] = tb.get("at_bottom")
    app["use_fancy_tab_bar"] = bool(user_tb["fancy"]) if isinstance(user_tb.get("fancy"), bool) else None
    app["tab_max_width"] = tb.get("max_width")
    if st_sec.get("enabled") is not None:
        app["status_bar"] = bool(st_sec.get("enabled"))
    if hl.get("open_in_editor") is not None:
        app["open_in_editor"] = bool(hl.get("open_in_editor"))
    app["editor_cmd"] = hl.get("editor") or None
    if tb.get("agent_indicators") is not None:
        app["agent_indicators"] = bool(tb.get("agent_indicators"))
    if tb.get("ssh_alert") is not None:
        app["ssh_alert"] = bool(tb.get("ssh_alert"))
    if tb.get("command_palette") is not None:
        app["command_palette"] = bool(tb.get("command_palette"))
    if tb.get("workspace_switcher") is not None:
        app["workspace_switcher"] = bool(tb.get("workspace_switcher"))
    if tb.get("session_manager") is not None:
        app["session_manager"] = bool(tb.get("session_manager"))
    return app


def detect_user_config_conflicts(paths: dict[str, Path] | None = None) -> list[str]:
    """Inspect user's wezterm.lua for conflicting handlers that override Studio themes."""
    paths = paths or resolve_wezterm_paths()
    cfg_file = paths["config_file"]
    if not cfg_file.is_file() or is_loader_mode(paths):
        return []
    try:
        content = cfg_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    # Strip out the wezterm-theme hook block so we don't flag our own generated hook
    clean = re.sub(
        r"\n?" + re.escape(HOOK_START) + r".*?" + re.escape(HOOK_END) + r"\n?",
        "",
        content,
        flags=re.DOTALL,
    )
    # Strip single-line Lua comments
    clean_lines = [re.sub(r"--.*$", "", line) for line in clean.splitlines()]
    clean_code = "\n".join(clean_lines)

    conflicts: list[str] = []
    if re.search(r"""(?:wezterm\s*\.\s*on\s*\(\s*['"]format-tab-title['"])""", clean_code):
        conflicts.append(
            "format-tab-title listener detected in wezterm.lua. "
            "It overrides WezTerm Studio tab bar themes."
        )
    return conflicts


def detect_performance_warnings(
    paths: dict[str, Path] | None = None,
    appearance: dict[str, Any] | None = None,
) -> list[str]:
    """Inspect user's wezterm.lua and Studio appearance for CPU-heavy rendering combinations."""
    paths = paths or resolve_wezterm_paths()
    app = appearance if appearance is not None else get_current_state(paths).get("appearance", {})

    fe = app.get("front_end")
    anim_fps = app.get("animation_fps")
    grad = bool(app.get("background_gradient"))
    blur = int(app.get("window_blur") or 0)

    cfg_file = paths["config_file"]
    if cfg_file.is_file():
        try:
            raw = cfg_file.read_text(encoding="utf-8", errors="ignore")
            clean = "\n".join(re.sub(r"--.*$", "", ln) for ln in raw.splitlines())
            if not fe:
                m_fe = re.search(r"""front_end\s*=\s*['"](WebGpu|OpenGL|Software)['"]""", clean)
                if m_fe:
                    fe = m_fe.group(1)
            if anim_fps is None:
                m_afps = re.search(r"""animation_fps\s*=\s*(\d+)""", clean)
                if m_afps:
                    anim_fps = int(m_afps.group(1))
        except Exception:
            pass

    warnings: list[str] = []
    if fe == "Software":
        eff_anim = 10 if anim_fps is None else int(anim_fps)
        if eff_anim > 5:
            warnings.append(
                f"front_end='Software' with animation_fps={eff_anim} causes high CPU drain; "
                "set animation_fps=1 or switch front_end to OpenGL/WebGpu."
            )
        if grad or blur > 0:
            warnings.append(
                "front_end='Software' combined with Oklab gradient or window blur renders on CPU; "
                "prefer OpenGL/WebGpu when using backdrop effects."
            )
    return warnings


def get_session_snapshot_info(paths: dict[str, Path] | None = None) -> dict[str, Any]:
    """Return metadata for the saved workspace session snapshot (`wezterm_session.json`), if any."""
    paths = paths or resolve_wezterm_paths()
    snap = paths.get("session_json") or (paths["config_dir"] / "wezterm_session.json")
    if not snap.is_file():
        return {"exists": False, "path": str(snap), "saved_at": None, "active_workspace": None, "workspaces": [], "total_tabs": 0}
    data = _read_json_safe(snap)
    ws_raw = data.get("workspaces") if isinstance(data.get("workspaces"), list) else []
    ws_summary: list[dict[str, Any]] = []
    total_tabs = 0
    for item in ws_raw:
        if isinstance(item, dict):
            wname = str(item.get("name") or "default")
            tabs = item.get("tabs") if isinstance(item.get("tabs"), list) else []
            total_tabs += len(tabs)
            ws_summary.append({"name": wname, "tab_count": len(tabs)})
    return {
        "exists": True,
        "path": str(snap),
        "saved_at": data.get("saved_at"),
        "active_workspace": data.get("active_workspace") or "default",
        "workspaces": ws_summary,
        "total_tabs": total_tabs,
    }


def clear_session_snapshot(paths: dict[str, Path] | None = None) -> tuple[bool, str]:
    """Delete the saved workspace session snapshot file."""
    paths = paths or resolve_wezterm_paths()
    snap = paths.get("session_json") or (paths["config_dir"] / "wezterm_session.json")
    if not snap.is_file():
        return False, "No saved workspace session snapshot found."
    snap.unlink(missing_ok=True)
    return True, f"Cleared workspace session snapshot ({snap.name})."


def disable_conflicting_tab_handlers(content: str) -> tuple[str, bool]:
    """Comment out conflicting `wezterm.on('format-tab-title', ...)` blocks in user Lua code."""
    hook_m = re.search(
        r"(" + re.escape(HOOK_START) + r".*?" + re.escape(HOOK_END) + r")",
        content,
        flags=re.DOTALL,
    )
    if hook_m:
        before = content[:hook_m.start()]
        hook_block = hook_m.group(1)
        after = content[hook_m.end():]
    else:
        before = content
        hook_block = ""
        after = ""

    pattern = re.compile(
        r"""([ \t]*wezterm\s*\.\s*on\s*\(\s*['"]format-tab-title['"].*?end\s*\))""",
        re.DOTALL,
    )

    changed = False

    def repl(m: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        block = m.group(1)
        lines = ["-- [disabled by wezterm-theme: conflicts with dynamic tab bar themes]"]
        for line in block.splitlines():
            lines.append(f"-- {line}")
        return "\n".join(lines)

    new_before = pattern.sub(repl, before)
    new_after = pattern.sub(repl, after)

    if hook_m:
        return new_before + hook_block + new_after, changed
    return new_before, changed


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
            "appearance": _extract_loader_appearance(merged, settings_only=settings),
            "conflicts": [],
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

    conflicts = detect_user_config_conflicts(paths)

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
        "conflicts": conflicts,
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
        return "WezTerm timed out while validating the configuration"
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


def init_wezterm_config(
    paths: dict[str, Path] | None = None,
    disable_conflicts: bool = True,
) -> tuple[bool, str]:
    """Integrate `wezterm-theme` non-destructively into the user's `wezterm.lua`.

    If `disable_conflicts` is True (default), conflicting `format-tab-title` handlers
    in `wezterm.lua` are commented out with an automatic timestamped backup.
    """
    paths = paths or resolve_wezterm_paths()
    paths["config_dir"].mkdir(parents=True, exist_ok=True)

    if is_loader_mode(paths):
        # Ensure theme module exists
        if not paths["tab_theme_lua"].is_file():
            state = get_current_state(paths)
            theme = get_theme_by_id(state["theme_id"]) or THEMES[9]
            apply_theme(theme, tab_title_len=state["tab_title_len"], symbols=state["symbols"], paths=paths)
        return True, f"WezTerm JSON Loader mode detected and ready at {paths['config_file']}"

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
            return False, f"Failed to validate newly created wezterm.lua: {err}"
        return True, f"Created {cfg_file} integrated with wezterm-theme!"

    original = cfg_file.read_text(encoding="utf-8")

    # If hook already installed, check if user has conflicting handlers to resolve
    if HOOK_START in original:
        if disable_conflicts:
            cleaned, changed = disable_conflicting_tab_handlers(original)
            if changed:
                bkp_dir = paths["backup_dir"]
                bkp_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(cfg_file, bkp_dir / f"wezterm.lua.{stamp}_conflict_fix.bak")
                cfg_file.write_text(cleaned, encoding="utf-8")
                err = validate_config(cfg_file)
                if err:
                    cfg_file.write_text(original, encoding="utf-8")
                    return False, f"Failed to resolve conflicts in {cfg_file} (rolled back): {err}"
                return True, f"Resolved conflicting format-tab-title handlers in {cfg_file}!"
        return True, f"wezterm-theme is already integrated into {cfg_file}"

    # Match `return <identifier>` near EOF
    working_text = original
    if disable_conflicts:
        working_text, _ = disable_conflicting_tab_handlers(working_text)

    m_ret = re.search(r"^([ \t]*)return[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:--.*)?$", working_text, re.MULTILINE)
    if m_ret:
        # Use the last top-level return identifier in the file
        matches = list(
            re.finditer(r"^([ \t]*)return[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:--.*)?$", working_text, re.MULTILINE)
        )
        last = matches[-1]
        cfg_var = last.group(2)
        hook = _build_hook_block(cfg_var)
        updated = working_text[: last.start()] + hook + "\n\n" + working_text[last.start() :]
    else:
        # Fallback for `return { ... }` style configs
        m_tbl = list(re.finditer(r"^([ \t]*)return[ \t]*\{", working_text, re.MULTILINE))
        if not m_tbl:
            return (
                False,
                f"Could not find 'return config' in {cfg_file}. "
                f"Add the following block manually right before your return statement:\n{_build_hook_block('config')}",
            )
        last = m_tbl[-1]
        hook = _build_hook_block("_wt_config")
        updated = (
            working_text[: last.start()]
            + f"{last.group(1)}local _wt_config = {{"
            + working_text[last.end() :]
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
        return False, f"WezTerm rejected the change in {cfg_file} (automatically rolled back): {err}"

    conflicts = detect_user_config_conflicts(paths)
    if conflicts:
        conflict_msg = " Note: " + "; ".join(conflicts) + " You may want to comment out or remove custom format-tab-title handlers to allow Studio themes to take effect."
        return True, f"Successfully integrated wezterm-theme into {cfg_file}.{conflict_msg}"

    return True, f"Successfully integrated wezterm-theme into {cfg_file}"


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
                return False, f"Failed to remove hook block from {cfg_file} (rolled back): {err}"
            removed_hook = True

    if remove_files:
        paths["studio_lua"].unlink(missing_ok=True)
        paths["studio_json"].unlink(missing_ok=True)

    if removed_hook:
        return True, f"Removed wezterm-theme hook from {cfg_file}."
    return True, "No standalone hook found to remove."


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
        if (
            "font_family" in appearance_updates
            or "font_weight" in appearance_updates
            or "font_size" in appearance_updates
            or "line_height" in appearance_updates
            or "font_ligatures" in appearance_updates
            or "font_fallback" in appearance_updates
        ):
            f = data.setdefault("font", {})
            if appearance_updates.get("font_family"):
                f["family"] = str(appearance_updates["font_family"])
            if appearance_updates.get("font_weight"):
                f["weight"] = str(appearance_updates["font_weight"])
            if appearance_updates.get("font_size") is not None:
                f["size"] = float(appearance_updates["font_size"]) if float(appearance_updates["font_size"]) % 1 else int(float(appearance_updates["font_size"]))
            if appearance_updates.get("line_height") is not None:
                f["line_height"] = round(float(appearance_updates["line_height"]), 2)
            if "font_ligatures" in appearance_updates and appearance_updates["font_ligatures"] is not None:
                f["ligatures"] = bool(appearance_updates["font_ligatures"])
            if "font_fallback" in appearance_updates and appearance_updates["font_fallback"] is not None:
                f["fallback"] = bool(appearance_updates["font_fallback"])

        if "tab_font_family" in appearance_updates or "tab_font_weight" in appearance_updates or "tab_font_size" in appearance_updates:
            ff = tabs.setdefault("frame_font", {})
            if appearance_updates.get("tab_font_family"):
                ff["family"] = str(appearance_updates["tab_font_family"])
            if appearance_updates.get("tab_font_weight"):
                ff["weight"] = str(appearance_updates["tab_font_weight"])
            if appearance_updates.get("tab_font_size") is not None:
                val = float(appearance_updates["tab_font_size"])
                ff["size"] = val if val % 1 else int(val)

        if (
            "color_scheme" in appearance_updates
            or "color_scheme_light" in appearance_updates
            or "oled_black" in appearance_updates
            or "cursor_color" in appearance_updates
        ):
            c = data.setdefault("colors", {})
            if "color_scheme" in appearance_updates:
                scheme = appearance_updates["color_scheme"]
                if not scheme or scheme == "Default":
                    c.pop("scheme", None)
                else:
                    c["scheme"] = str(scheme)
            if "color_scheme_light" in appearance_updates:
                scheme_light = appearance_updates["color_scheme_light"]
                if not scheme_light or scheme_light == "Default":
                    c.pop("scheme_light", None)
                else:
                    c["scheme_light"] = str(scheme_light)
            if "oled_black" in appearance_updates and appearance_updates["oled_black"] is not None:
                c["oled_black"] = bool(appearance_updates["oled_black"])
            if "cursor_color" in appearance_updates:
                cc = appearance_updates["cursor_color"]
                if not cc or str(cc) in ("Default", "off", "none"):
                    c.pop("cursor_color", None)
                else:
                    c["cursor_color"] = str(cc).strip().lower()

        if (
            "window_opacity" in appearance_updates
            or "window_blur" in appearance_updates
            or "win32_backdrop" in appearance_updates
            or "window_padding" in appearance_updates
            or "window_decorations" in appearance_updates
            or "inactive_pane_brightness" in appearance_updates
            or "background_gradient" in appearance_updates
            or "accent_border" in appearance_updates
        ):
            w = data.setdefault("window", {})
            if appearance_updates.get("window_opacity") is not None:
                w["opacity"] = round(float(appearance_updates["window_opacity"]), 2)
            if "window_blur" in appearance_updates:
                bval = appearance_updates["window_blur"]
                if bval is None or int(bval or 0) <= 0:
                    w.pop("blur", None)
                else:
                    w["blur"] = max(0, min(100, int(bval)))
            if "win32_backdrop" in appearance_updates:
                wbd = appearance_updates["win32_backdrop"]
                if not wbd or str(wbd) in ("Auto", "Default"):
                    w.pop("win32_backdrop", None)
                else:
                    w["win32_backdrop"] = str(wbd)
            if appearance_updates.get("window_padding") is not None:
                pad = appearance_updates["window_padding"]
                if isinstance(pad, dict):
                    w["padding"] = {k: int(pad.get(k, 10)) for k in ("left", "right", "top", "bottom")}
                else:
                    p = int(float(pad))
                    w["padding"] = {"left": p, "right": p, "top": p, "bottom": p}
            if appearance_updates.get("window_decorations"):
                w["decorations"] = str(appearance_updates["window_decorations"])
            if "inactive_pane_brightness" in appearance_updates:
                br = appearance_updates["inactive_pane_brightness"]
                if br is None:
                    w.pop("inactive_pane_hsb", None)
                else:
                    br_f = round(float(br), 2)
                    w["inactive_pane_hsb"] = {
                        "saturation": 1.0 if br_f >= 0.99 else 0.9,
                        "brightness": br_f,
                    }
            if "background_gradient" in appearance_updates and appearance_updates["background_gradient"] is not None:
                w["background_gradient"] = bool(appearance_updates["background_gradient"])
            if "accent_border" in appearance_updates and appearance_updates["accent_border"] is not None:
                w["accent_border"] = bool(appearance_updates["accent_border"])

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

        if (
            "scrollback_lines" in appearance_updates
            or "front_end" in appearance_updates
            or "max_fps" in appearance_updates
            or "animation_fps" in appearance_updates
        ):
            pf = data.setdefault("performance", {})
            if "scrollback_lines" in appearance_updates and appearance_updates.get("scrollback_lines") is not None:
                pf["scrollback_lines"] = int(appearance_updates["scrollback_lines"])
            if "front_end" in appearance_updates:
                fe_v = appearance_updates["front_end"]
                if not fe_v or str(fe_v) in ("Auto", "Default"):
                    pf.pop("front_end", None)
                else:
                    pf["front_end"] = str(fe_v)
            if "max_fps" in appearance_updates:
                mfps = appearance_updates["max_fps"]
                if mfps is None or int(mfps or 0) <= 0:
                    pf.pop("max_fps", None)
                else:
                    pf["max_fps"] = int(mfps)
            if "animation_fps" in appearance_updates:
                afps = appearance_updates["animation_fps"]
                if afps is None or int(afps or 0) <= 0:
                    pf.pop("animation_fps", None)
                else:
                    pf["animation_fps"] = int(afps)

        if "enable_wayland" in appearance_updates:
            d_sec = data.setdefault("display", {})
            wl_v = appearance_updates["enable_wayland"]
            if wl_v is None or wl_v == "auto":
                d_sec.pop("enable_wayland", None)
            else:
                d_sec["enable_wayland"] = bool(wl_v)

        if "hide_tab_bar_if_only_one_tab" in appearance_updates and appearance_updates["hide_tab_bar_if_only_one_tab"] is not None:
            tabs["hide_if_only_one_tab"] = bool(appearance_updates["hide_tab_bar_if_only_one_tab"])
        if "tab_bar_at_bottom" in appearance_updates and appearance_updates["tab_bar_at_bottom"] is not None:
            tabs["at_bottom"] = bool(appearance_updates["tab_bar_at_bottom"])
        if "use_fancy_tab_bar" in appearance_updates:
            fval = appearance_updates["use_fancy_tab_bar"]
            if fval is None or fval == "auto":
                tabs.pop("fancy", None)
            else:
                tabs["fancy"] = bool(fval)
        if "tab_max_width" in appearance_updates:
            mval = appearance_updates["tab_max_width"]
            if mval is None or int(mval or 0) <= 0:
                tabs.pop("max_width", None)
            else:
                tabs["max_width"] = int(mval)
        if "status_bar" in appearance_updates and appearance_updates["status_bar"] is not None:
            st_sec = data.setdefault("status", {})
            st_sec["enabled"] = bool(appearance_updates["status_bar"])
        if "agent_indicators" in appearance_updates and appearance_updates["agent_indicators"] is not None:
            tabs["agent_indicators"] = bool(appearance_updates["agent_indicators"])
            st_sec = data.setdefault("status", {})
            st_sec["agent_toast"] = bool(appearance_updates["agent_indicators"])
        if "ssh_alert" in appearance_updates and appearance_updates["ssh_alert"] is not None:
            tabs["ssh_alert"] = bool(appearance_updates["ssh_alert"])
            st_sec = data.setdefault("status", {})
            st_sec["ssh_host"] = bool(appearance_updates["ssh_alert"])
        if "command_palette" in appearance_updates and appearance_updates["command_palette"] is not None:
            tabs["command_palette"] = bool(appearance_updates["command_palette"])
        if "workspace_switcher" in appearance_updates and appearance_updates["workspace_switcher"] is not None:
            tabs["workspace_switcher"] = bool(appearance_updates["workspace_switcher"])
        if "session_manager" in appearance_updates and appearance_updates["session_manager"] is not None:
            tabs["session_manager"] = bool(appearance_updates["session_manager"])
        if "open_in_editor" in appearance_updates or "editor_cmd" in appearance_updates or "quick_select" in appearance_updates:
            hl = data.setdefault("hyperlinks", {})
            if "open_in_editor" in appearance_updates and appearance_updates["open_in_editor"] is not None:
                hl["open_in_editor"] = bool(appearance_updates["open_in_editor"])
            if "quick_select" in appearance_updates and appearance_updates["quick_select"] is not None:
                hl["quick_select"] = bool(appearance_updates["quick_select"])
            if "editor_cmd" in appearance_updates:
                ed = appearance_updates["editor_cmd"]
                if not ed or ed == "Default":
                    hl.pop("editor", None)
                else:
                    hl["editor"] = str(ed)

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
                date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                date_str = raw_stamp
        else:
            date_str = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        theme_info = "Custom Configuration"
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
                theme_info = f"Theme #{theme_id:02d}: {tm.group(2).strip()}{len_suffix}"
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
        _sync_loader_settings(
            paths,
            theme_id=theme["id"],
            tab_title_len=eff_len,
            symbols=eff_syms,
            appearance_updates=appearance_updates,
        )
        new_settings = _read_json_safe(paths["settings_json"])
        merged_after = _deep_merge(_read_json_safe(paths["defaults_json"]), new_settings)
        merged_tabs = merged_after.get("tabs") if isinstance(merged_after.get("tabs"), dict) else {}
        user_fancy = (new_settings.get("tabs") or {}).get("fancy") if isinstance(new_settings.get("tabs"), dict) else None
        loader_app: dict[str, Any] = {
            "_loader_only": True,
            "agent_indicators": merged_tabs.get("agent_indicators"),
            "ssh_alert": merged_tabs.get("ssh_alert"),
            "command_palette": merged_tabs.get("command_palette"),
            "workspace_switcher": merged_tabs.get("workspace_switcher"),
            "session_manager": merged_tabs.get("session_manager"),
        }
        if isinstance(user_fancy, bool):
            loader_app["use_fancy_tab_bar"] = user_fancy
        lua_code = generate_lua_for_theme(theme, tab_title_len=eff_len, symbols=eff_syms, appearance=loader_app)
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
                if k in ("color_scheme", "color_scheme_light", "editor_cmd", "cursor_color", "front_end", "win32_backdrop") and (
                    not v or str(v) in ("Default", "Auto", "off", "none")
                ):
                    merged_app[k] = None
                elif k in ("use_fancy_tab_bar", "enable_wayland") and v == "auto":
                    merged_app[k] = None
                elif k in ("tab_max_width", "window_blur", "max_fps", "animation_fps") and (v is None or int(v or 0) <= 0):
                    merged_app[k] = None
                else:
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
        return False, f"No backups found in {paths['backup_dir']}"

    selected = None
    spec_str = str(target_spec).strip()
    if spec_str.isdigit():
        idx = int(spec_str) - 1
        if 0 <= idx < len(backups):
            selected = backups[idx]
        else:
            return False, f"Invalid backup index: {spec_str}. Choose between 1 and {len(backups)}."
    else:
        for b in backups:
            if spec_str in b["filename"] or (b["label"] and spec_str.lower() in b["label"].lower()):
                selected = b
                break
        if not selected:
            return False, f"No backup matching '{spec_str}'."

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
                f"WezTerm rejected this backup; current configuration was kept. "
                f"{err.strip().splitlines()[-1][:160]}",
            )

        if loader and not companion_json.is_file():
            tid, tlen, syms, _ = _parse_lua_metadata(target_lua)
            _sync_loader_settings(paths, theme_id=tid, tab_title_len=tlen, symbols=syms)

        idx_display = backups.index(selected) + 1
        return (
            True,
            f"Backup [{idx_display}] ({selected['theme_info']} - {selected['date_str']}) restored successfully!",
        )
    except Exception as exc:
        return False, f"Error restoring backup: {exc}"


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
            return False, "Invalid preset format."
    except Exception as exc:
        return False, f"Failed to parse preset JSON: {exc}"

    tid = int(data.get("theme_id", 10))
    theme = get_theme_by_id(tid)
    if not theme:
        return False, f"Theme #{tid} does not exist in catalog (1–{len(THEMES)})."

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
        return False, f"WezTerm rejected the preset: {err}"
    return True, f"Preset applied successfully (Theme #{theme['id']:02d} {theme['name']})!"
