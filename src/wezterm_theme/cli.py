"""Unified CLI entry point for `wezterm-theme` (and `wezterm-tab-theme` alias).

Supports both subcommands (`web`, `tui`, `init`, `uninstall`, `fonts`, `doctor`,
`export`, `import`) and direct one-liner flags (`-s`, `-t`, `--font`, `--size`,
`--tab-font`, `--scheme`, `--opacity`, `-w`, `-l`, `-r`, `-c`, `-b`, `-u`, etc.).
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .core import (
    PERF_PROFILES,
    VALIDATION_SKIPPED_MSG,
    apply_appearance,
    apply_theme,
    backup_current_config,
    clear_session_snapshot,
    detect_performance_warnings,
    detect_user_config_conflicts,
    export_preset,
    get_available_backups,
    get_current_state,
    get_perf_overview,
    get_session_snapshot_info,
    import_preset,
    init_wezterm_config,
    restore_backup,
    uninstall_wezterm_config,
    validate_config,
    validation_available,
)
from .fonts import get_color_scheme_palettes, get_color_schemes, get_installed_fonts
from .paths import init_terminal, resolve_wezterm_paths, system_summary
from .perf import BenchmarkError, benchmark_current, format_benchmark
from .server import DEFAULT_PORT, run_studio_server
from .themes import (
    BATTERY_MODES,
    BATTERY_STYLES,
    BATTERY_TIMES,
    CURSOR_EASINGS,
    THEMES,
    apply_symbols,
    filter_themes,
    find_theme,
    get_theme_by_id,
)
from .tui import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    failure_msg,
    fonts_menu,
    interactive_picker,
    interactive_restore_menu,
    print_backups_list,
    print_list,
    render_4_tab_bar,
    render_scheme_swatch,
    symbols_menu,
)

SUBCOMMANDS = {"init", "uninstall", "web", "studio", "tui", "fonts", "doctor", "session", "export", "import"}


def _cmd_init(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme init", description="Connect wezterm-theme to your wezterm.lua.")
    p.parse_args(argv)
    ok, msg = init_wezterm_config()
    if ok:
        print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
        return 0
    print(f"{RED}✖ {msg}{RESET}", file=sys.stderr)
    return 1


def _cmd_uninstall(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme uninstall", description="Remove the wezterm-theme hook from wezterm.lua.")
    p.add_argument("--purge", action="store_true", help="Also delete generated wezterm_theme.lua and wezterm_theme.json files")
    p.add_argument(
        "--keep-disabled-handlers",
        action="store_true",
        help="Leave the format-tab-title handlers that 'init' commented out disabled (default: re-enable them)",
    )
    ns = p.parse_args(argv)
    ok, msg = uninstall_wezterm_config(remove_files=ns.purge, restore_handlers=not ns.keep_disabled_handlers)
    if ok:
        print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
        return 0
    print(f"{RED}✖ {msg}{RESET}", file=sys.stderr)
    return 1


def _cmd_web(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme web", description="Launch interactive WezTerm Studio in your browser.")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("-p", "--port", type=int, default=None, help=f"HTTP port (default: auto starting at {DEFAULT_PORT})")
    p.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    ns = p.parse_args(argv)
    run_studio_server(host=ns.host, port=ns.port, open_browser=not ns.no_open)
    return 0


def _cmd_tui(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme tui", description="Open the interactive TrueColor picker in the terminal.")
    p.add_argument("-f", "--filter", default="", help="Initial theme filter")
    ns = p.parse_args(argv)
    interactive_picker(THEMES, initial_filter=ns.filter)
    return 0


def _cmd_fonts(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme fonts", description="List or select monospace and Nerd Fonts.")
    p.add_argument("-i", "--interactive", action="store_true", help="Open the interactive font picker menu in the terminal")
    p.add_argument("--nerd", action="store_true", help="Show Nerd Fonts only")
    p.add_argument("--refresh", action="store_true", help="Refresh cached system font list")
    ns = p.parse_args(argv)
    if ns.interactive:
        msg = fonts_menu()
        print(msg)
        return 0
    fonts = get_installed_fonts(refresh=ns.refresh)
    if ns.nerd:
        fonts = [f for f in fonts if f.get("is_nerd")]
    st = get_current_state()
    ed = st["appearance"].get("font_family") or ""
    tb = st["appearance"].get("tab_font_family") or ""
    print(f"\n{BOLD}{CYAN}=== MONOSPACE & NERD FONTS ({len(fonts)} found) ==={RESET}\n")
    for idx, f in enumerate(fonts, start=1):
        fam = f["family"]
        marks = []
        if ed and (ed == fam or ed.startswith(fam)):
            marks.append(f"{CYAN}{BOLD}← EDITOR{RESET}")
        if tb and (tb == fam or tb.startswith(fam)):
            marks.append(f"{MAGENTA}{BOLD}← TABS{RESET}")
        nf = f"{GREEN}[NF]{RESET} " if f.get("is_nerd") else "     "
        print(f"  {BOLD}[{idx:02d}]{RESET} {nf}{BOLD}{fam:<32}{RESET} {DIM}{f['desc']}{RESET} {' '.join(marks)}")
    print(f"\nUse: {BOLD}wezterm-theme --font \"<Family>\" --size 14{RESET} or {BOLD}wezterm-theme fonts -i{RESET}\n")
    return 0


def _cmd_doctor(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme doctor", description="Run cross-platform diagnostics and WezTerm config validation.")
    p.add_argument(
        "--perf",
        action="store_true",
        help="Also benchmark the generated tab-bar and status-bar Lua inside WezTerm",
    )
    ns = p.parse_args(argv)
    info = system_summary()
    state = get_current_state()
    paths = resolve_wezterm_paths()
    fonts = get_installed_fonts()

    print(f"\n{BOLD}{CYAN}=== WEZTERM STUDIO DOCTOR (v{__version__}) ==={RESET}\n")
    print(f"  {BOLD}Operating System:{RESET}   {info['os']}")
    print(f"  {BOLD}Python:{RESET}             {info['python']}")
    print(f"  {BOLD}WezTerm Binary:{RESET}     {info['wezterm_cmd']}")
    print(f"  {BOLD}Config Mode:{RESET}        {state['mode']} ({'connected' if state['initialized'] else 'awaiting init'})")
    print(f"  {BOLD}wezterm.lua Path:{RESET}   {info['config_file']}")
    print(f"  {BOLD}Theme Module:{RESET}       {state['active_module']}")
    print(f"  {BOLD}Cache Dir:{RESET}          {info['cache_dir']}")
    print(f"  {BOLD}Detected Fonts:{RESET}     {len(fonts)} monospace/Nerd Fonts")
    sess = get_session_snapshot_info(paths)
    if sess["exists"]:
        print(
            f"  {BOLD}Session Snapshot:{RESET}   {sess['total_tabs']} tab(s) across "
            f"{len(sess['workspaces'])} workspace(s) ({sess.get('saved_at') or 'saved'})"
        )
    else:
        print(f"  {BOLD}Session Snapshot:{RESET}   none saved yet (use Ctrl+Shift+P in WezTerm)")
    perf = get_perf_overview(paths, state.get("appearance"))
    renderer = perf["renderer"]
    source = {"studio": "set by Studio", "wezterm.lua": "set in wezterm.lua", "default": "WezTerm default"}[renderer["source"]]
    profile = PERF_PROFILES.get(perf["active_profile"], {}).get("label", "Custom")
    print(f"  {BOLD}Renderer:{RESET}           {renderer['front_end']} ({source})")
    print(f"  {BOLD}Perf Profile:{RESET}       {profile}")
    if ns.perf:
        try:
            for line in format_benchmark(benchmark_current(paths)):
                print(f"  {BOLD}Benchmark:{RESET}          {line}")
        except BenchmarkError as exc:
            print(f"  {BOLD}Benchmark:{RESET}          {YELLOW}skipped: {exc}{RESET}")

    cfg_path = paths["config_file"]
    if cfg_path.is_file():
        if not validation_available():
            print(f"\n  {YELLOW}{BOLD}⚠ Validation skipped:{RESET} {VALIDATION_SKIPPED_MSG}")
        else:
            err = validate_config(cfg_path)
            if err:
                print(f"\n  {RED}{BOLD}✖ WezTerm validation failed:{RESET}\n  {err}\n")
                return 1
            print(f"\n  {GREEN}{BOLD}✔ wezterm.lua validated cleanly!{RESET}")

        conflicts = detect_user_config_conflicts(paths)
        perf_warnings = detect_performance_warnings(paths, appearance=state.get("appearance"))
        all_warnings = [*conflicts, *perf_warnings]
        if all_warnings:
            print(f"\n  {YELLOW}{BOLD}⚠ Configuration & Performance Warning(s):{RESET}")
            for w in all_warnings:
                print(f"    • {w}")
            if conflicts:
                print(f"    {DIM}Hint: Run 'wezterm-theme init' to comment out conflicting 'format-tab-title' handlers automatically.{RESET}")
            print("")
        else:
            print("")
    else:
        print(f"\n  {YELLOW}ℹ {cfg_path} does not exist yet. Run 'wezterm-theme init' to create it.{RESET}\n")
    return 0


def _cmd_session(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(
        prog="wezterm-theme session",
        description="Inspect or clear saved WezTerm Studio workspace session snapshots.",
    )
    p.add_argument("action", nargs="?", default="status", choices=["status", "clear"], help="Action (default: status)")
    ns = p.parse_args(argv)
    if ns.action == "clear":
        ok, msg = clear_session_snapshot()
        if ok:
            print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
            return 0
        print(f"{YELLOW}ℹ {msg}{RESET}")
        return 0

    info = get_session_snapshot_info()
    print(f"\n{BOLD}{CYAN}=== WEZTERM STUDIO WORKSPACE SESSION SNAPSHOT ==={RESET}\n")
    print(f"  {BOLD}Snapshot File:{RESET}    {info['path']}")
    if not info["exists"]:
        print(f"  {BOLD}Status:{RESET}           {YELLOW}No snapshot saved yet{RESET}")
        print(f"\n  {DIM}Tip: Press Ctrl+Shift+P inside WezTerm → 'WezTerm Studio: Save Workspace Session Snapshot'{RESET}\n")
        return 0
    print(f"  {BOLD}Saved At:{RESET}         {info.get('saved_at') or 'unknown'}")
    print(f"  {BOLD}Active Workspace:{RESET} {info.get('active_workspace') or 'default'}")
    print(f"  {BOLD}Total Tabs:{RESET}       {info['total_tabs']} across {len(info['workspaces'])} workspace(s)")
    for ws in info["workspaces"]:
        print(f"    • {BOLD}{ws['name']}{RESET} ({ws['tab_count']} tab{'s' if ws['tab_count'] != 1 else ''})")
    print(f"\n  {DIM}Tip: Press Ctrl+Shift+P inside WezTerm → 'WezTerm Studio: Restore Saved Workspace Session'{RESET}\n")
    return 0


def _cmd_export(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme export", description="Export a portable JSON preset (Linux, macOS, Windows).")
    p.add_argument("output", nargs="?", help="Destination .json file (or stdout if omitted)")
    ns = p.parse_args(argv)
    dest = Path(ns.output).expanduser() if ns.output else None
    raw, written = export_preset(dest_path=dest)
    if written:
        print(f"{GREEN}{BOLD}✔ Preset exported to:{RESET} {written}")
    else:
        print(raw, end="")
    return 0


def _cmd_import(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme import", description="Import and apply a portable JSON preset.")
    p.add_argument("source", help="Path to the preset .json file")
    ns = p.parse_args(argv)
    ok, msg = import_preset(Path(ns.source).expanduser())
    if ok:
        print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
        return 0
    print(f"{RED}✖ {msg}{RESET}", file=sys.stderr)
    return 1


def _print_current_status() -> None:
    st = get_current_state()
    tid = st["theme_id"]
    tlen = st["tab_title_len"]
    syms = st["symbols"]
    app = st["appearance"]
    t = get_theme_by_id(tid)

    print(f"\n{BOLD}{CYAN}=== CURRENT WEZTERM STUDIO CONFIGURATION ==={RESET}\n")
    print(f"  {BOLD}Mode:{RESET}          {st['mode']} ({st['platform']})")
    print(f"  {BOLD}Config:{RESET}        {st['config_file']}")
    if t:
        print(f"  {BOLD}Tab Theme:{RESET}     {GREEN}#{tid:02d} — {t['name']}{RESET} {DIM}[{t['category']} · {tlen}c]{RESET}")
        if syms:
            print(f"  {BOLD}Symbols:{RESET}       {MAGENTA}Custom{RESET} {DIM}{syms}{RESET}")
        print(f"  {BOLD}Preview:{RESET}       {render_4_tab_bar(apply_symbols(t, syms), tab_title_len=tlen)}")
    lig_state = "Off" if app.get("font_ligatures") is False else "On"
    fb_state = " · Fallback: On" if app.get("font_fallback") else ""
    print(
        f"  {BOLD}Editor Font:{RESET}   {app.get('font_family') or 'WezTerm Default'} "
        f"({app.get('font_weight') or 'Regular'}, {app.get('font_size') or 13}pt · Ligatures: {lig_state}{fb_state})"
    )
    print(
        f"  {BOLD}Tab Font:{RESET}      {app.get('tab_font_family') or 'WezTerm Default'} "
        f"({app.get('tab_font_weight') or 'Medium'}, {app.get('tab_font_size') or 11}pt)"
    )
    light_info = f" (Light: {app['color_scheme_light']})" if app.get("color_scheme_light") else ""
    oled_info = " [OLED #000000]" if app.get("oled_black") else ""
    print(f"  {BOLD}Color Scheme:{RESET}  {app.get('color_scheme') or 'Default'}{light_info}{oled_info}")
    extras: list[str] = []
    if app.get("window_blur"):
        extras.append(f"Blur: {app['window_blur']}")
    if app.get("win32_backdrop"):
        extras.append(f"Win32: {app['win32_backdrop']}")
    if app.get("cursor_color"):
        extras.append(f"Cursor: {app['cursor_color']}")
    if app.get("inactive_pane_brightness") is not None:
        extras.append(f"Inactive dim: {app['inactive_pane_brightness']:g}")
    if app.get("background_gradient"):
        extras.append("Oklab gradient: On")
    if app.get("accent_border"):
        extras.append("1px accent border: On")
    if app.get("front_end"):
        extras.append(f"Renderer: {app['front_end']}")
    if app.get("max_fps"):
        extras.append(f"Max FPS: {app['max_fps']}")
    if app.get("animation_fps"):
        extras.append(f"Anim FPS: {app['animation_fps']}")
    if app.get("webgpu_power_preference"):
        extras.append(f"GPU: {app['webgpu_power_preference']}")
    if app.get("cursor_blink_ease"):
        extras.append(f"Cursor easing: {app['cursor_blink_ease']}")
    if app.get("status_update_interval"):
        extras.append(f"Status every {int(app['status_update_interval'])} ms")
    if app.get("enable_wayland") is not None:
        extras.append(f"Wayland: {'On' if app['enable_wayland'] else 'Off'}")
    if app.get("quick_select"):
        extras.append("QuickSelect: On")
    if app.get("status_bar") is not None:
        extras.append(f"Status bar: {'On' if app['status_bar'] else 'Off'}")
    if any(
        app.get(k) is not None
        for k in ("battery_mode", "battery_style", "battery_time", "battery_warn", "battery_crit", "battery_toast")
    ):
        b_mode = app.get("battery_mode") or "always"
        b_style = app.get("battery_style") or "nerd"
        b_time = app.get("battery_time") or "off"
        b_warn = app.get("battery_warn") or 30
        b_crit = app.get("battery_crit") or 15
        b_toast = " · toast" if app.get("battery_toast") else ""
        extras.append(
            "Battery: Off"
            if b_mode == "off"
            else f"Battery: {b_style}/{b_mode} (ETA:{b_time}, ≤{b_warn}%/≤{b_crit}%{b_toast})"
        )
    if app.get("tab_max_width") is not None:
        extras.append(f"Max tab width: {app['tab_max_width']}c")
    if app.get("use_fancy_tab_bar") is not None:
        extras.append(f"Tab bar: {'Fancy' if app['use_fancy_tab_bar'] else 'Retro'}")
    if app.get("open_in_editor") is not None or app.get("editor_cmd"):
        ed_lbl = app.get("editor_cmd") or "$EDITOR"
        extras.append(f"Click-to-editor: {'Off' if app.get('open_in_editor') is False else ed_lbl}")
    if app.get("agent_indicators") is not None:
        extras.append(f"AI agent radar: {'On' if app['agent_indicators'] else 'Off'}")
    if app.get("ssh_alert") is not None:
        extras.append(f"SSH/prod alert: {'On' if app['ssh_alert'] else 'Off'}")
    if app.get("command_palette") is not None:
        extras.append(f"Cmd palette: {'On' if app['command_palette'] else 'Off'}")
    if app.get("workspace_switcher") is not None:
        extras.append(f"WS switcher: {'On' if app['workspace_switcher'] else 'Off'}")
    if app.get("session_manager") is not None:
        extras.append(f"Session mgr: {'On' if app['session_manager'] else 'Off'}")
    if extras:
        print(f"  {BOLD}Window Extras:{RESET} {' · '.join(extras)}")
    if not st["initialized"]:
        print(f"\n  {YELLOW}⚡ Tip: run 'wezterm-theme init' to connect WezTerm Studio to your wezterm.lua.{RESET}")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    init_terminal()
    args_list = list(sys.argv[1:] if argv is None else argv)

    if args_list and not args_list[0].startswith("-") and args_list[0] in SUBCOMMANDS:
        sub = args_list[0]
        rest = args_list[1:]
        if sub == "init":
            return _cmd_init(rest)
        if sub == "uninstall":
            return _cmd_uninstall(rest)
        if sub in ("web", "studio"):
            return _cmd_web(rest)
        if sub == "tui":
            return _cmd_tui(rest)
        if sub == "fonts":
            return _cmd_fonts(rest)
        if sub == "doctor":
            return _cmd_doctor(rest)
        if sub == "session":
            return _cmd_session(rest)
        if sub == "export":
            return _cmd_export(rest)
        if sub == "import":
            return _cmd_import(rest)

    total_themes = len(THEMES)
    parser = argparse.ArgumentParser(
        prog="wezterm-theme",
        description=(
            f"WezTerm Studio — Cross-platform Tab Theme ({total_themes}), Symbol, Font & Appearance "
            "customizer for WezTerm.\n\n"
            "Subcommands:\n"
            "  init        Connect wezterm-theme to your wezterm.lua\n"
            "  web         Launch full WezTerm Studio in your browser (fonts + tabs + colors)\n"
            "  tui         Open the interactive TrueColor terminal picker (default with no args)\n"
            "  fonts       List or pick monospace and Nerd Fonts\n"
            "  session     Inspect or clear saved workspace session snapshots\n"
            "  doctor      Run WezTerm diagnostics on Linux, macOS, or Windows\n"
            "  export      Export a portable JSON preset\n"
            "  import      Import a portable JSON preset\n"
            "  uninstall   Remove the hook from wezterm.lua"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    # Tab theme flags
    parser.add_argument("-l", "--list", action="store_true", help=f"List all {total_themes} tab bar themes in the terminal")
    parser.add_argument("-s", "--set", metavar="THEME", help=f"Apply a tab theme by ID (1-{total_themes}), slug, or name")
    parser.add_argument("-t", "--tab-len", type=int, metavar="CHARS", help="Fixed tab title character width (4-60)")
    parser.add_argument("-r", "--random", action="store_true", help="Apply a random tab bar theme")
    parser.add_argument("-c", "--current", action="store_true", help="Show active tab theme, symbols, fonts, and colors")
    parser.add_argument("-f", "--filter", default="", help="Filter themes in --list, --random, or TUI mode")
    parser.add_argument("--symbols", action="store_true", help="Open the interactive tab symbol customizer")
    parser.add_argument("--reset-symbols", action="store_true", help="Reset tab symbols to the active theme's defaults")

    # Font & Appearance flags
    parser.add_argument("--font", metavar="FAMILY", help="Set the main terminal (Editor) font family")
    parser.add_argument("--weight", metavar="WEIGHT", help="Editor font weight (e.g. Regular, Medium, Bold)")
    parser.add_argument("--size", type=float, metavar="PT", help="Editor font size in points (e.g. 13.5)")
    parser.add_argument(
        "--ligatures",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable programming font ligatures (calt/clig/liga)",
    )
    parser.add_argument(
        "--font-fallback",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable automatic Symbols Nerd Font Mono + Emoji fallback chain",
    )
    parser.add_argument("--tab-font", metavar="FAMILY", help="Set the tab bar font family")
    parser.add_argument("--tab-weight", metavar="WEIGHT", help="Tab bar font weight (e.g. Medium, Bold)")
    parser.add_argument("--tab-size", type=float, metavar="PT", help="Tab bar font size in points (e.g. 11.5)")
    parser.add_argument("--tab-max-width", type=int, metavar="COLS", help="Max tab width in columns (16-120, or 0 for auto)")
    parser.add_argument(
        "--fancy-tabs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override theme tab bar style (Fancy pill frame vs Retro flat cells)",
    )
    parser.add_argument(
        "--editor",
        metavar="CMD",
        help="Set $EDITOR command for stack-trace & output openers (e.g. nvim, hx, code, or 'Default')",
    )
    parser.add_argument(
        "--open-in-editor",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable clicking file:line stack traces to open in $EDITOR (+ GitHub owner/repo#123 links)",
    )
    parser.add_argument("--scheme", metavar="NAME", help="Set WezTerm color scheme (e.g. 'Catppuccin Mocha')")
    parser.add_argument(
        "--light-scheme",
        metavar="NAME",
        help="Set companion color scheme for OS Light mode (use 'Default' to clear)",
    )
    parser.add_argument(
        "--oled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable OLED pitch-black (#000000) dark background override",
    )
    parser.add_argument(
        "--cursor-color",
        metavar="HEX",
        help="Override cursor color in #RRGGBB format (or 'Default' to clear)",
    )
    parser.add_argument("--opacity", type=float, metavar="0.1-1.0", help="Window background opacity (e.g. 0.92)")
    parser.add_argument("--blur", type=int, metavar="0-100", help="Cross-platform frosted-glass background blur (macOS, KDE, Win11; 0 disables)")
    parser.add_argument(
        "--win32-backdrop",
        choices=["Auto", "Acrylic", "Mica", "Tabbed", "Disable"],
        help="Windows 11 system backdrop material",
    )
    parser.add_argument("--padding", type=int, metavar="PX", help="Inner window padding in pixels (e.g. 10)")
    parser.add_argument(
        "--front-end",
        choices=["Auto", "WebGpu", "OpenGL", "Software"],
        help="WezTerm rendering backend (Auto, WebGpu, OpenGL, or Software)",
    )
    parser.add_argument("--max-fps", type=int, metavar="FPS", help="Maximum rendering FPS (15-255, or 0 for default)")
    parser.add_argument("--animation-fps", type=int, metavar="FPS", help="Animation & cursor blink FPS (1-255, or 0 for default)")
    parser.add_argument(
        "--perf-profile",
        choices=list(PERF_PROFILES),
        help="Apply a performance profile: " + "; ".join(
            f"{name} = {prof['label']}" for name, prof in PERF_PROFILES.items()
        ) + ". Explicit flags in the same command win over the profile.",
    )
    parser.add_argument(
        "--webgpu-power",
        choices=["Default", "LowPower", "HighPerformance"],
        help="GPU to use with front_end=WebGpu: LowPower (integrated) or HighPerformance (discrete)",
    )
    parser.add_argument(
        "--cursor-blink-ease",
        choices=["Default", *CURSOR_EASINGS],
        help="Cursor blink easing (Constant = plain on/off, cheapest with animation FPS 1)",
    )
    parser.add_argument(
        "--status-interval",
        type=int,
        metavar="MS",
        help="Milliseconds between status bar updates (0 = default; Studio uses 5000)",
    )
    parser.add_argument(
        "--wayland",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Explicitly enable or disable native Wayland support on Linux",
    )
    parser.add_argument(
        "--quick-select",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable Quick Select power patterns (Ctrl+Shift+Space: Git SHA, UUID, IP, K8s, file:line)",
    )
    parser.add_argument(
        "--dim-inactive",
        type=float,
        metavar="0.4-1.0",
        help="Brightness for inactive split panes (e.g. 0.78; 1.0 disables dimming)",
    )
    parser.add_argument(
        "--gradient",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable subtle Oklab radial window background gradient",
    )
    parser.add_argument(
        "--border",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable 1px theme-accented window frame border",
    )
    parser.add_argument(
        "--status-bar",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable Studio right-status bar (mode pill, workspace/mux, git, battery, clock)",
    )
    parser.add_argument(
        "--battery-mode",
        choices=["Default", *BATTERY_MODES],
        help="Status-bar battery visibility: always, hide_full (hide when 100%% or plugged at limit), low_only, or off",
    )
    parser.add_argument(
        "--battery-style",
        choices=["Default", *BATTERY_STYLES],
        help="Battery indicator style: nerd (10-step + bolt), emoji, bar ([████░]), icon_only, or percent",
    )
    parser.add_argument(
        "--battery-time",
        choices=["Default", *BATTERY_TIMES],
        help="Show remaining battery / time-to-full ETA: off, discharging, or always",
    )
    parser.add_argument(
        "--battery-warn",
        type=int,
        metavar="PCT",
        help="Warning battery percentage threshold for amber color & low_only mode (5-95, default 30; 0 resets)",
    )
    parser.add_argument(
        "--battery-crit",
        type=int,
        metavar="PCT",
        help="Critical battery percentage threshold for crimson alert pill & toast (1-90, default 15; 0 resets)",
    )
    parser.add_argument(
        "--battery-toast",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable 1-shot desktop notification when discharging reaches critical battery %%",
    )
    parser.add_argument(
        "--agent-radar",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable AI coding agent indicators & background completion toasts (Claude, Gemini, Codex, Aider, etc.)",
    )
    parser.add_argument(
        "--ssh-alert",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable SSH & production host (prod*, root@*) danger tinting on tabs and status bar",
    )
    parser.add_argument(
        "--command-palette",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable Studio & Zen Mode actions in WezTerm's Ctrl+Shift+P Command Palette",
    )
    parser.add_argument(
        "--workspace-switcher",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable zoxide & Git Project Workspace Switcher in Ctrl+Shift+P",
    )
    parser.add_argument(
        "--session-manager",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable Workspace Session Save & Restore in Ctrl+Shift+P",
    )
    parser.add_argument("--list-fonts", action="store_true", help="List all installed monospace and Nerd Fonts")
    parser.add_argument("--list-schemes", action="store_true", help="List built-in WezTerm color schemes with TrueColor swatches")

    # Web Studio flags
    parser.add_argument("-w", "--web", action="store_true", help="Launch interactive WezTerm Studio in your browser")
    parser.add_argument("--port", type=int, default=None, help=f"HTTP port for --web mode (default: {DEFAULT_PORT})")
    parser.add_argument("--no-open", action="store_true", help="Do not open browser automatically in --web mode")

    # Backups
    parser.add_argument("--backups", action="store_true", help="List available configuration backups")
    parser.add_argument("-b", "--backup", nargs="?", const="manual", metavar="LABEL", help="Create an instant backup")
    parser.add_argument("-u", "--restore", nargs="?", const="__INTERACTIVE__", metavar="ID_OR_LABEL", help="Restore a previous backup")

    args = parser.parse_args(args_list)

    if args.tab_len is not None and not (4 <= args.tab_len <= 60):
        print(f"{RED}Error: --tab-len must be between 4 and 60 characters.{RESET}", file=sys.stderr)
        return 1
    if args.opacity is not None and not (0.1 <= args.opacity <= 1.0):
        print(f"{RED}Error: --opacity must be between 0.1 and 1.0.{RESET}", file=sys.stderr)
        return 1
    if args.dim_inactive is not None and not (0.4 <= args.dim_inactive <= 1.0):
        print(f"{RED}Error: --dim-inactive must be between 0.4 and 1.0.{RESET}", file=sys.stderr)
        return 1
    if args.tab_max_width is not None and args.tab_max_width != 0 and not (16 <= args.tab_max_width <= 120):
        print(f"{RED}Error: --tab-max-width must be between 16 and 120 (or 0 to reset to auto).{RESET}", file=sys.stderr)
        return 1
    if args.blur is not None and not (0 <= args.blur <= 100):
        print(f"{RED}Error: --blur must be between 0 and 100.{RESET}", file=sys.stderr)
        return 1
    if args.max_fps is not None and args.max_fps != 0 and not (15 <= args.max_fps <= 255):
        print(f"{RED}Error: --max-fps must be between 15 and 255 (or 0 for default).{RESET}", file=sys.stderr)
        return 1
    if args.animation_fps is not None and args.animation_fps != 0 and not (1 <= args.animation_fps <= 255):
        print(f"{RED}Error: --animation-fps must be between 1 and 255 (or 0 for default).{RESET}", file=sys.stderr)
        return 1
    if args.battery_warn is not None and args.battery_warn != 0 and not (5 <= args.battery_warn <= 95):
        print(f"{RED}Error: --battery-warn must be between 5 and 95 (or 0 to reset).{RESET}", file=sys.stderr)
        return 1
    if args.battery_crit is not None and args.battery_crit != 0 and not (1 <= args.battery_crit <= 90):
        print(f"{RED}Error: --battery-crit must be between 1 and 90 (or 0 to reset).{RESET}", file=sys.stderr)
        return 1
    if args.cursor_color is not None and args.cursor_color not in ("Default", "off", "none", ""):
        import re as _re
        if not _re.fullmatch(r"#[0-9a-fA-F]{6}", args.cursor_color.strip()):
            print(f"{RED}Error: --cursor-color must be #RRGGBB (e.g. '#60a5fa') or 'Default'.{RESET}", file=sys.stderr)
            return 1

    if args.backups:
        print_backups_list(get_available_backups())
        return 0

    if args.backup is not None:
        dest = backup_current_config(label=args.backup)
        if dest:
            print(f"{GREEN}{BOLD}✔ Backup saved:{RESET} {dest}")
            return 0
        print(f"{YELLOW}No active configuration found to back up.{RESET}")
        return 1

    if args.restore is not None:
        if args.restore == "__INTERACTIVE__":
            print(interactive_restore_menu())
            return 0
        ok, msg = restore_backup(args.restore)
        if ok:
            print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
            return 0
        print(f"{RED}✖ {msg}{RESET}", file=sys.stderr)
        return 1

    if args.list_fonts:
        return _cmd_fonts([])

    if args.list_schemes:
        schemes = get_color_schemes()
        pal_map = {p["name"]: p for p in get_color_scheme_palettes()}
        q = args.filter.lower().strip()
        only_dark: bool | None = None
        if q == "dark" or q.startswith("dark "):
            only_dark = True
            q = q[4:].strip()
        elif q == "light" or q.startswith("light "):
            only_dark = False
            q = q[5:].strip()
        if q or only_dark is not None:
            schemes = [
                s
                for s in schemes
                if (not q or q in s.lower())
                and (only_dark is None or pal_map.get(s, {}).get("is_dark", True) is only_dark)
            ]
        print(f"\n{BOLD}{CYAN}=== WEZTERM COLOR SCHEMES ({len(schemes)}) ==={RESET}\n")
        for s in schemes:
            sw = render_scheme_swatch(pal_map.get(s))
            print(f"  {sw}  {s}")
        print()
        return 0

    if args.list:
        print_list(THEMES, filter_query=args.filter)
        return 0

    if args.current:
        _print_current_status()
        return 0

    if args.symbols:
        print(symbols_menu())
        return 0

    # Collect appearance updates if any CLI font/window/scheme flags were provided
    app_updates: dict[str, object] = {}
    if args.perf_profile:
        app_updates.update(PERF_PROFILES[args.perf_profile]["settings"])
    if args.font:
        app_updates["font_family"] = args.font
    if args.weight:
        app_updates["font_weight"] = args.weight
    if args.size is not None:
        app_updates["font_size"] = args.size
    if args.ligatures is not None:
        app_updates["font_ligatures"] = args.ligatures
    if args.font_fallback is not None:
        app_updates["font_fallback"] = args.font_fallback
    if args.tab_font:
        app_updates["tab_font_family"] = args.tab_font
    if args.tab_weight:
        app_updates["tab_font_weight"] = args.tab_weight
    if args.tab_size is not None:
        app_updates["tab_font_size"] = args.tab_size
    if args.tab_max_width is not None:
        app_updates["tab_max_width"] = args.tab_max_width
    if args.fancy_tabs is not None:
        app_updates["use_fancy_tab_bar"] = args.fancy_tabs
    if args.editor is not None:
        app_updates["editor_cmd"] = args.editor
    if args.open_in_editor is not None:
        app_updates["open_in_editor"] = args.open_in_editor
    if args.scheme is not None:
        app_updates["color_scheme"] = args.scheme
    if args.light_scheme is not None:
        app_updates["color_scheme_light"] = args.light_scheme
    if args.oled is not None:
        app_updates["oled_black"] = args.oled
    if args.cursor_color is not None:
        app_updates["cursor_color"] = args.cursor_color
    if args.opacity is not None:
        app_updates["window_opacity"] = args.opacity
    if args.blur is not None:
        app_updates["window_blur"] = args.blur
    if args.win32_backdrop is not None:
        app_updates["win32_backdrop"] = args.win32_backdrop
    if args.padding is not None:
        app_updates["window_padding"] = args.padding
    if args.front_end is not None:
        app_updates["front_end"] = args.front_end
    if args.max_fps is not None:
        app_updates["max_fps"] = args.max_fps
    if args.animation_fps is not None:
        app_updates["animation_fps"] = args.animation_fps
    if args.webgpu_power is not None:
        app_updates["webgpu_power_preference"] = args.webgpu_power
    if args.cursor_blink_ease is not None:
        app_updates["cursor_blink_ease"] = args.cursor_blink_ease
    if args.status_interval is not None:
        app_updates["status_update_interval"] = args.status_interval
    if args.wayland is not None:
        app_updates["enable_wayland"] = args.wayland
    if args.quick_select is not None:
        app_updates["quick_select"] = args.quick_select
    if args.dim_inactive is not None:
        app_updates["inactive_pane_brightness"] = args.dim_inactive
    if args.gradient is not None:
        app_updates["background_gradient"] = args.gradient
    if args.border is not None:
        app_updates["accent_border"] = args.border
    if args.status_bar is not None:
        app_updates["status_bar"] = args.status_bar
    if args.battery_mode is not None:
        app_updates["battery_mode"] = args.battery_mode
    if args.battery_style is not None:
        app_updates["battery_style"] = args.battery_style
    if args.battery_time is not None:
        app_updates["battery_time"] = args.battery_time
    if args.battery_warn is not None:
        app_updates["battery_warn"] = args.battery_warn
    if args.battery_crit is not None:
        app_updates["battery_crit"] = args.battery_crit
    if args.battery_toast is not None:
        app_updates["battery_toast"] = args.battery_toast
    if args.agent_radar is not None:
        app_updates["agent_indicators"] = args.agent_radar
    if args.ssh_alert is not None:
        app_updates["ssh_alert"] = args.ssh_alert
    if args.command_palette is not None:
        app_updates["command_palette"] = args.command_palette
    if args.workspace_switcher is not None:
        app_updates["workspace_switcher"] = args.workspace_switcher
    if args.session_manager is not None:
        app_updates["session_manager"] = args.session_manager

    mutated = False

    if args.reset_symbols:
        st = get_current_state()
        cur_t = get_theme_by_id(st["theme_id"]) or THEMES[0]
        ok, _, err = apply_theme(cur_t, tab_title_len=args.tab_len, symbols=None, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        print(f"{GREEN}{BOLD}✔ Reverted to default symbols for theme #{cur_t['id']:02d} ({cur_t['name']}).{RESET}")
        mutated = True
        app_updates = {}

    if args.random:
        pool = filter_themes(THEMES, args.filter) if args.filter else THEMES
        if not pool:
            print(f"{RED}No themes match filter '{args.filter}'.{RESET}", file=sys.stderr)
            return 1
        chosen = random.choice(pool)
        ok, _, err = apply_theme(chosen, tab_title_len=args.tab_len, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        print(f"{GREEN}{BOLD}🎲 Random theme #{chosen['id']:02d} ({chosen['name']}) applied!{RESET}")
        mutated = True
        app_updates = {}

    if args.set:
        chosen = find_theme(args.set)
        if not chosen:
            print(
                f"{RED}Error: Theme '{args.set}' not found. Use --list to browse all {total_themes} themes.{RESET}",
                file=sys.stderr,
            )
            return 1
        ok, _, err = apply_theme(chosen, tab_title_len=args.tab_len, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        st_after = get_current_state()
        init_hint = "" if st_after["initialized"] else f"\n{YELLOW}⚡ Tip: run 'wezterm-theme init' to connect WezTerm Studio to your wezterm.lua.{RESET}"
        print(f"{GREEN}{BOLD}✔ Theme #{chosen['id']:02d} ({chosen['name']}) applied!{RESET}{init_hint}")
        mutated = True
        app_updates = {}

    if args.tab_len is not None and not args.set and not args.random and not args.reset_symbols:
        st = get_current_state()
        cur_t = get_theme_by_id(st["theme_id"]) or THEMES[0]
        ok, _, err = apply_theme(cur_t, tab_title_len=args.tab_len, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        print(f"{GREEN}{BOLD}✔ Fixed tab width set to {args.tab_len} characters!{RESET}")
        mutated = True
        app_updates = {}

    if app_updates:
        ok, err = apply_appearance(app_updates)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        summary_parts = [f"{k}={v}" for k, v in app_updates.items()]
        profile_note = f" (performance profile: {PERF_PROFILES[args.perf_profile]['label']})" if args.perf_profile else ""
        print(f"{GREEN}{BOLD}✔ Configuration applied{profile_note}:{RESET} {', '.join(summary_parts)}")
        mutated = True

    if args.web:
        run_studio_server(port=args.port, open_browser=not args.no_open)
        return 0

    if mutated:
        return 0

    interactive_picker(THEMES, initial_filter=args.filter)
    return 0
