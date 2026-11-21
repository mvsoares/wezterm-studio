"""Comprehensive unit & integration tests for `wezterm_theme`.

All tests isolate WezTerm configuration via `WEZTERM_CONFIG_FILE` in temporary
directories so the host user's real `~/.config/wezterm` is never touched.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wezterm_theme.cli import main as cli_main
import wezterm_theme.fonts as fonts_mod
from wezterm_theme.core import (
    MAX_PRE_RESTORE_BACKUPS,
    MAX_WEZTERM_LUA_BACKUPS,
    PERF_PROFILES,
    PERF_TUNING_KEYS,
    _backup_wezterm_lua,
    _find_tab_title_handlers,
    apply_appearance,
    apply_perf_profile,
    apply_theme,
    backup_current_config,
    detect_perf_profile,
    detect_performance_warnings,
    disable_conflicting_tab_handlers,
    export_preset,
    get_available_backups,
    get_current_state,
    get_renderer_info,
    import_preset,
    init_wezterm_config,
    normalize_appearance_updates,
    restore_backup,
    restore_disabled_tab_handlers,
    uninstall_wezterm_config,
    validate_theme_lua,
)
from wezterm_theme.fonts import get_color_scheme_palettes
from wezterm_theme.paths import (
    atomic_write_text,
    find_studio_cli,
    find_wezterm_cmd,
    get_cache_dir,
    get_platform_name,
    resolve_wezterm_paths,
)
from wezterm_theme.perf import benchmark_modules
from wezterm_theme.server import RequestError, _address_family_for_host, _parse_strict_int, create_server
from wezterm_theme.themes import (
    AI_AGENTS,
    MIN_TAB_CONTRAST,
    SYMBOL_GROUPS,
    THEMES,
    _hover_pair,
    _lua_str,
    compose_symbols,
    contrast_ratio,
    find_theme,
    generate_lua_for_theme,
    get_theme_by_id,
)
from wezterm_theme.tui import render_4_tab_bar, symbol_cell, visible_len


class ThemeCatalogAndSymbolsTests(unittest.TestCase):
    def test_62_themes_and_wcag_contrast(self) -> None:
        self.assertEqual(len(THEMES), 62)
        ids = [t["id"] for t in THEMES]
        self.assertEqual(ids, list(range(1, 63)))
        for t in THEMES:
            bar = render_4_tab_bar(t, tab_title_len=14)
            self.assertGreater(visible_len(bar), 20)
            for slot in t["palette"]:
                self.assertGreaterEqual(
                    contrast_ratio(slot["bg"], slot["fg"]),
                    MIN_TAB_CONTRAST,
                    f"Low contrast in theme #{t['id']} {t['name']}",
                )
                h_bg, h_fg = _hover_pair(slot["bg"], slot["fg"])
                self.assertGreaterEqual(
                    contrast_ratio(h_bg, h_fg),
                    MIN_TAB_CONTRAST,
                    f"Low hover contrast in theme #{t['id']} {t['name']}",
                )

    def test_find_theme_by_id_slug_and_substring(self) -> None:
        self.assertEqual(find_theme("8")["id"], 8)
        self.assertEqual(find_theme("#08")["id"], 8)
        self.assertEqual(find_theme("catppuccin-mocha")["id"], 22)
        self.assertEqual(find_theme("edgerunners")["id"], 31)
        self.assertEqual(find_theme("scheme-adaptive-powerline")["id"], 61)
        self.assertEqual(find_theme("workspace-chameleon")["id"], 62)

    def test_symbol_cells_uniform_width(self) -> None:
        cell_w = 40
        for _, letter, _, opts in SYMBOL_GROUPS:
            for idx, opt in enumerate(opts, start=1):
                for sel in (False, True):
                    cell = symbol_cell(f"{letter}{idx}", opt, sel, cell_w)
                    self.assertEqual(visible_len(cell), cell_w)

    def test_hue_matched_symbols(self) -> None:
        act_red, _ = compose_symbols({"marker": "balls"}, 0, "#ea4335")
        act_blue, _ = compose_symbols({"marker": "balls"}, 0, "#4285f4")
        self.assertIn("🔴", act_red)
        self.assertIn("🔵", act_blue)

    def test_title_progress_zoom_hover_and_appearance_lua_generation(self) -> None:
        lua = generate_lua_for_theme(
            get_theme_by_id(61),
            tab_title_len=16,
            appearance={
                "font_family": "JetBrains Mono",
                "font_size": 13,
                "font_ligatures": False,
                "font_fallback": True,
                "tab_font_family": "Fira Code",
                "color_scheme": "Catppuccin Mocha",
                "color_scheme_light": "Catppuccin Latte",
                "oled_black": True,
                "cursor_color": "#60a5fa",
                "window_blur": 24,
                "win32_backdrop": "Mica",
                "front_end": "WebGpu",
                "max_fps": 120,
                "animation_fps": 30,
                "enable_wayland": True,
                "quick_select": True,
                "inactive_pane_brightness": 0.78,
                "background_gradient": True,
                "accent_border": True,
                "status_bar": True,
            },
        )
        self.assertIn("tab.tab_title", lua)
        self.assertIn("WEZTERM_PROG", lua)
        self.assertIn("pane.progress", lua)
        self.assertIn("pane.is_zoomed", lua)
        self.assertIn("harfbuzz_features", lua)
        self.assertIn("wezterm.font_with_fallback", lua)
        self.assertIn("Symbols Nerd Font Mono", lua)
        self.assertIn("Catppuccin Latte", lua)
        self.assertIn("config.colors.background = '#000000'", lua)
        self.assertIn("config.colors.cursor_bg = '#60a5fa'", lua)
        self.assertIn("macos_window_background_blur = 24", lua)
        self.assertIn("kde_window_background_blur = true", lua)
        self.assertIn("win32_system_backdrop = 'Mica'", lua)
        self.assertIn("front_end = 'WebGpu'", lua)
        self.assertIn("max_fps = 120", lua)
        self.assertIn("animation_fps = 30", lua)
        self.assertIn("enable_wayland = true", lua)
        self.assertIn("quick_select_patterns", lua)
        self.assertIn("inactive_pane_hsb", lua)
        self.assertIn("window_background_gradient", lua)
        self.assertIn("border_left_width = '1px'", lua)
        self.assertIn("wezterm.on('update-status'", lua)
        self.assertIn("wezterm.GLOBAL._wt_git_cache", lua)
        self.assertIsNone(validate_theme_lua(lua))

    def test_representative_lua_styles_validate_in_wezterm(self) -> None:
        # Validate every distinct style family in WezTerm
        seen_styles: set[str] = set()
        for t in THEMES:
            st = t.get("style", "pills")
            if st in seen_styles:
                continue
            seen_styles.add(st)
            lua = generate_lua_for_theme(
                t,
                tab_title_len=16,
                symbols={"prefix": "rocket", "marker": "balls", "arrow": "powerline", "inactive": "diamond"},
                appearance={
                    "font_family": "JetBrains Mono",
                    "font_size": 13,
                    "font_ligatures": False,
                    "color_scheme": "Dracula",
                    "color_scheme_light": "Builtin Light",
                    "window_opacity": 0.95,
                    "inactive_pane_brightness": 0.8,
                    "background_gradient": True,
                    "accent_border": True,
                },
            )
            err = validate_theme_lua(lua)
            self.assertIsNone(err, f"Style {st} (theme #{t['id']}) failed validation: {err}")

    def test_format_tab_title_runtime_execution_in_wezterm(self) -> None:
        wz = find_wezterm_cmd()
        if not wz:
            self.skipTest("wezterm binary not installed")
        import subprocess

        seen_styles: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            harness_lines = [
                "local wezterm = require 'wezterm'",
                "local orig_on = wezterm.on",
                "local last_cb = nil",
                "local last_status_cb = nil",
                "local last_open_uri_cb = nil",
                "local last_palette_cb = nil",
                "wezterm.on = function(ev, fn)",
                "  if ev == 'format-tab-title' then last_cb = fn",
                "  elseif ev == 'update-status' then last_status_cb = fn",
                "  elseif ev == 'open-uri' then last_open_uri_cb = fn",
                "  elseif ev == 'augment-command-palette' then last_palette_cb = fn end",
                "end",
                "local mock_tabs = {",
                "  {",
                "    tab_index = 0, is_active = true, tab_title = 'deploy-prod',",
                "    panes = { {}, {} },",
                "    active_pane = {",
                "      current_working_dir = 'file:///home/dev/wezterm-studio',",
                "      user_vars = { WEZTERM_PROG = 'cargo build --release' },",
                "      foreground_process_name = '/bin/zsh',",
                "      is_zoomed = true,",
                "      has_unseen_output = false,",
                "      progress = { Percentage = 42 },",
                "    },",
                "  },",
                "  {",
                "    tab_index = 1, is_active = false, tab_title = '',",
                "    panes = { {}, {}, {} },",
                "    active_pane = {",
                "      current_working_dir = 'file:///home/dev/backend',",
                "      user_vars = { WEZTERM_PROG = '/usr/bin/nvim src/main.rs' },",
                "      foreground_process_name = '/bin/bash',",
                "      is_zoomed = false,",
                "      has_unseen_output = true,",
                "      progress = 'Indeterminate',",
                "    },",
                "  },",
                "  {",
                "    tab_index = 2, is_active = false, tab_title = '',",
                "    panes = { {} },",
                "    active_pane = {",
                "      current_working_dir = 'file:///home/dev/ai-agent',",
                "      user_vars = { WEZTERM_PROG = 'claude --continue' },",
                "      foreground_process_name = '/usr/local/bin/claude',",
                "      is_zoomed = false,",
                "      has_unseen_output = true,",
                "    },",
                "  },",
                "  {",
                "    tab_index = 3, is_active = true, tab_title = '',",
                "    panes = { {} },",
                "    active_pane = {",
                "      current_working_dir = 'file:///home/dev/infra',",
                "      user_vars = { WEZTERM_PROG = 'ssh root@prod-db-01' },",
                "      foreground_process_name = '/usr/bin/ssh',",
                "      is_zoomed = false,",
                "      has_unseen_output = false,",
                "    },",
                "  },",
                "}",
                "local mock_cfg = {",
                "  resolved_palette = {",
                "    background = '#1e1e2e',",
                "    ansi = { '#45475a', '#f38ba8', '#a6e3a1', '#f9e2af', '#89b4fa', '#f5c2e7', '#94e2d5', '#bac2de' },",
                "  },",
                "}",
                "local last_mod = nil",
            ]
            for t in THEMES:
                st = t.get("style", "pills")
                if st in seen_styles:
                    continue
                seen_styles.add(st)
                mod_file = Path(tmp) / f"theme_{t['id']}.lua"
                mod_file.write_text(
                    generate_lua_for_theme(
                        t,
                        tab_title_len=16,
                        appearance={"status_bar": True, "open_in_editor": True, "editor_cmd": "hx"},
                    ),
                    encoding="utf-8",
                )
                harness_lines.extend([
                    "last_cb = nil",
                    "last_status_cb = nil",
                    "last_open_uri_cb = nil",
                    "last_palette_cb = nil",
                    f"last_mod = dofile([==[{mod_file.as_posix()}]==])",
                    "last_mod.apply({}, wezterm)",
                    f"assert(type(last_cb) == 'function', 'missing callback for {st}')",
                    f"assert(type(last_status_cb) == 'function', 'missing update-status callback for {st}')",
                    f"assert(type(last_open_uri_cb) == 'function', 'missing open-uri callback for {st}')",
                    f"assert(type(last_palette_cb) == 'function', 'missing augment-command-palette callback for {st}')",
                    "local out1 = last_cb(mock_tabs[1], mock_tabs, mock_tabs[1].panes, mock_cfg, false, 48)",
                    "local out2 = last_cb(mock_tabs[2], mock_tabs, mock_tabs[2].panes, mock_cfg, true, 48)",
                    "local out_agent = last_cb(mock_tabs[3], mock_tabs, mock_tabs[3].panes, mock_cfg, false, 48)",
                    "local out_prod = last_cb(mock_tabs[4], mock_tabs, mock_tabs[4].panes, mock_cfg, false, 48)",
                    f"assert(type(out1) == 'string' and out1:find('42%%') and out1:lower():find('deploy') and out1:find('²') and out1:find('🔍'), 'bad active output for {st}: ' .. tostring(out1))",
                    f"assert(type(out2) == 'string' and out2:find('~ ') and out2:lower():find('nvim|') and out2:find('³'), 'bad hover output for {st}: ' .. tostring(out2))",
                    f"assert(type(out_agent) == 'string' and out_agent:find('🤖●'), 'missing AI agent radar for {st}: ' .. tostring(out_agent))",
                    f"assert(type(out_prod) == 'string' and out_prod:find('220:38:38'), 'missing SSH prod danger tint for {st}: ' .. tostring(out_prod))",
                ])
            harness_lines.extend([
                "local captured_status = nil",
                "local spawned_action = nil",
                "local captured_toast = nil",
                "local win_overrides = {}",
                "local mock_win = {",
                "  leader_is_active = function() return true end,",
                "  active_key_table = function() return nil end,",
                "  active_workspace = function() return 'prod' end,",
                "  set_right_status = function(_, s) captured_status = s end,",
                "  perform_action = function(_, act_obj) spawned_action = act_obj end,",
                "  toast_notification = function(_, title, body) captured_toast = title .. ':' .. body end,",
                "  get_config_overrides = function() return win_overrides end,",
                "  set_config_overrides = function(_, ov) win_overrides = ov end,",
                "  effective_config = function() return { font_size = 13.0 } end,",
                "  mux_window = function() return {",
                "    tabs_with_info = function() return {",
                "      { is_active = false, tab = { tab_id = function() return 99 end, active_pane = function() return {",
                "        get_user_vars = function() return { WEZTERM_PROG = 'claude' } end,",
                "        get_foreground_process_name = function() return '/usr/bin/claude' end,",
                "        has_unseen_output = function() return true end,",
                "      } end } },",
                "    } end,",
                "  } end,",
                "}",
                "local mock_pane = {",
                "  get_dimensions = function() return { cols = 150 } end,",
                "  get_domain_name = function() return 'ssh-box' end,",
                "  get_metadata = function() return { since_last_response_ms = 24 } end,",
                "  get_user_vars = function() return { WEZTERM_PROG = 'ssh root@prod-api-01' } end,",
                "  get_foreground_process_name = function() return '/usr/bin/ssh' end,",
                f"  get_current_working_dir = function() return 'file://{SRC_DIR.parent.as_posix()}' end,",
                "}",
                "local orig_bat = wezterm.battery_info",
                "wezterm.battery_info = function() return { { state_of_charge = 0.12, state = 'Discharging' }, { state_of_charge = 0.80, state = 'Charging' } } end",
                "last_status_cb(mock_win, mock_pane)",
                "wezterm.battery_info = orig_bat",
                "assert(type(captured_status) == 'string' and captured_status:find('LEADER') and captured_status:find('PROD:root@prod%-api%-01') and captured_status:find('ssh%-box 24ms') and captured_status:find('12%%') and captured_status:find('80%%'), 'bad status bar output: ' .. tostring(captured_status))",
                "assert(type(captured_toast) == 'string' and captured_toast:find('claude'), 'missing AI agent toast: ' .. tostring(captured_toast))",
                "local intercepted = last_open_uri_cb(mock_win, mock_pane, 'editfile://src/wezterm_theme/core.py:42')",
                "assert(intercepted == false and spawned_action ~= nil, 'open-uri failed to intercept editfile://')",
                "local orig_ac = wezterm.action_callback",
                "local captured_cbs = {}",
                "wezterm.action_callback = function(fn) local u = orig_ac(fn); captured_cbs[tostring(u)] = fn; return u end",
                f"wezterm.config_dir = [==[{Path(tmp).as_posix()}]==]",
                f"wezterm.home_dir = [==[{Path(tmp).as_posix()}]==]",
                "local spawned_ws = 0",
                "local spawned_tabs = 0",
                "wezterm.mux = {",
                "  get_workspace_names = function() return { 'default' } end,",
                "  all_windows = function() return {",
                "    { get_workspace = function() return 'studio-ws' end, tabs = function() return {",
                f"      {{ get_title = function() return 'api' end, active_pane = function() return {{ get_current_working_dir = function() return 'file://{Path(tmp).as_posix()}' end }} end }},",
                f"      {{ get_title = function() return 'ui' end, active_pane = function() return {{ get_current_working_dir = function() return 'file://{Path(tmp).as_posix()}' end }} end }},",
                "    } end },",
                "  } end,",
                "  spawn_window = function()",
                "    spawned_ws = spawned_ws + 1",
                "    spawned_tabs = spawned_tabs + 1",
                "    return { set_title = function() end }, {}, { spawn_tab = function() spawned_tabs = spawned_tabs + 1; return { set_title = function() end } end }",
                "  end,",
                "}",
                "local pal_entries = last_palette_cb(mock_win, mock_pane)",
                "assert(type(pal_entries) == 'table' and #pal_entries == 8, 'expected 8 command palette entries, got ' .. tostring(#pal_entries))",
                "captured_cbs[tostring(pal_entries[2].action)](mock_win, mock_pane)",
                "captured_cbs[tostring(pal_entries[3].action)](mock_win, mock_pane)",
                "assert(captured_toast and captured_toast:find('Saved 2 tab'), 'session save failed: ' .. tostring(captured_toast))",
                "-- Simulate restart: only an empty 'default' workspace is open now",
                "wezterm.mux.all_windows = function() return { { get_workspace = function() return 'default' end, tabs = function() return {} end } } end",
                "captured_cbs[tostring(pal_entries[4].action)](mock_win, mock_pane)",
                "assert(spawned_ws == 1 and spawned_tabs == 2, 'session restore failed: ws=' .. tostring(spawned_ws) .. ' tabs=' .. tostring(spawned_tabs))",
                "wezterm.action_callback = orig_ac",
            ])
            harness_lines.append("wezterm.on = orig_on")
            harness_lines.append("return wezterm.config_builder()")
            cfg = Path(tmp) / "wezterm.lua"
            cfg.write_text("\n".join(harness_lines) + "\n", encoding="utf-8")
            res = subprocess.run([*wz, "--config-file", str(cfg), "ls-fonts", "--text", "a"], capture_output=True, text=True, timeout=20)
            errs = [ln for ln in res.stderr.splitlines() if " ERROR " in ln]
            self.assertEqual(errs, [], "\n".join(errs))


class CrossPlatformPathsTests(unittest.TestCase):
    def test_platform_and_cache_dirs(self) -> None:
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(get_platform_name(), "macos")
            self.assertIn("Library/Caches/wezterm-theme", get_cache_dir().as_posix())

        with mock.patch("sys.platform", "win32"):
            self.assertEqual(get_platform_name(), "windows")
            self.assertIn("wezterm-theme", get_cache_dir().as_posix())

        with mock.patch("sys.platform", "linux"):
            self.assertEqual(get_platform_name(), "linux")
            self.assertIn("wezterm-theme", get_cache_dir().as_posix())

    def test_legacy_dotfile_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            dot_lua = fake_home / ".wezterm.lua"
            dot_lua.write_text("return {}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"WEZTERM_CONFIG_FILE": "", "XDG_CONFIG_HOME": ""}, clear=False):
                with mock.patch("pathlib.Path.home", return_value=fake_home):
                    paths = resolve_wezterm_paths()
                    self.assertEqual(paths["config_file"], dot_lua)
                    self.assertEqual(paths["config_dir"], fake_home / ".config" / "wezterm")


class StandaloneAndLoaderEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def test_standalone_init_apply_backup_preset_uninstall(self) -> None:
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            "config.initial_cols = 120\n"
            "return config\n",
            encoding="utf-8",
        )
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertTrue(get_current_state()["initialized"])

        ok, _, err = apply_theme(get_theme_by_id(31), tab_title_len=18)
        self.assertTrue(ok, err)

        ok, err = apply_appearance({
            "font_family": "JetBrains Mono",
            "font_size": 13.5,
            "font_ligatures": False,
            "tab_font_family": "JetBrains Mono",
            "tab_font_size": 11.5,
            "color_scheme": "Dracula",
            "color_scheme_light": "Catppuccin Latte",
            "window_opacity": 0.92,
            "window_padding": 12,
            "inactive_pane_brightness": 0.78,
            "background_gradient": True,
            "accent_border": True,
        })
        self.assertTrue(ok, err)

        st = get_current_state()
        self.assertEqual(st["theme_id"], 31)
        self.assertEqual(st["tab_title_len"], 18)
        self.assertEqual(st["appearance"]["color_scheme"], "Dracula")
        self.assertEqual(st["appearance"]["color_scheme_light"], "Catppuccin Latte")
        self.assertFalse(st["appearance"]["font_ligatures"])
        self.assertAlmostEqual(st["appearance"]["inactive_pane_brightness"], 0.78)
        self.assertTrue(st["appearance"]["background_gradient"])
        self.assertTrue(st["appearance"]["accent_border"])

        bkp = backup_current_config(label="checkpoint")
        self.assertIsNotNone(bkp)
        apply_theme(get_theme_by_id(5), tab_title_len=12)
        self.assertEqual(get_current_state()["theme_id"], 5)

        ok, msg = restore_backup("checkpoint")
        self.assertTrue(ok, msg)
        self.assertEqual(get_current_state()["theme_id"], 31)

        preset_raw, _ = export_preset()
        apply_theme(get_theme_by_id(1))
        ok, msg = import_preset(preset_raw)
        self.assertTrue(ok, msg)
        self.assertEqual(get_current_state()["theme_id"], 31)

        ok, msg = uninstall_wezterm_config(remove_files=True)
        self.assertTrue(ok, msg)
        self.assertNotIn("wezterm-theme", self.cfg_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(get_available_backups()), 5)

    def test_table_return_wezterm_lua_init(self) -> None:
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "return {\n"
            "  scrollback_lines = 4000,\n"
            "}\n",
            encoding="utf-8",
        )
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertTrue(get_current_state()["initialized"])

    def test_loader_mode_preserves_custom_settings(self) -> None:
        (self.tmp_path / "defaults.json").write_text(
            json.dumps({
                "font": {"family": "JetBrains Mono", "weight": "Regular", "size": 13},
                "tabs": {"theme_file": "tab_theme.lua", "frame_font": {"family": "JetBrains Mono", "weight": "Medium", "size": 11}},
                "colors": {"scheme": "Builtin Dark"},
            }),
            encoding="utf-8",
        )
        (self.tmp_path / "settings.json").write_text(
            json.dumps({"keys": {"leader": {"key": "a", "mods": "CTRL"}}}),
            encoding="utf-8",
        )
        self.cfg_path.write_text(
            "-- WezTerm loader\n"
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            f"local t = dofile([==[{(self.tmp_path / 'tab_theme.lua').as_posix()}]==])\n"
            "t.apply(config, wezterm)\n"
            "return config\n",
            encoding="utf-8",
        )
        st = get_current_state()
        self.assertEqual(st["mode"], "loader")

        ok, _, err = apply_theme(get_theme_by_id(62), tab_title_len=14)
        self.assertTrue(ok, err)
        ok, err = apply_appearance({
            "font_size": 15,
            "font_ligatures": False,
            "color_scheme": "Monokai Vivid",
            "color_scheme_light": "Builtin Light",
            "inactive_pane_brightness": 0.8,
            "background_gradient": True,
            "accent_border": True,
            "status_bar": False,
            "open_in_editor": True,
            "editor_cmd": "hx",
            "tab_max_width": 44,
            "use_fancy_tab_bar": False,
            "agent_indicators": False,
            "ssh_alert": False,
            "command_palette": False,
            "font_fallback": True,
            "oled_black": True,
            "cursor_color": "#ff7edb",
            "window_blur": 20,
            "win32_backdrop": "Acrylic",
            "front_end": "OpenGL",
            "max_fps": 144,
            "animation_fps": 15,
            "enable_wayland": False,
            "quick_select": True,
            "workspace_switcher": False,
            "session_manager": False,
        })
        self.assertTrue(ok, err)

        saved_settings = json.loads((self.tmp_path / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_settings["keys"]["leader"]["key"], "a")
        self.assertEqual(saved_settings["tabs"]["theme_id"], 62)
        self.assertEqual(saved_settings["tabs"]["max_width"], 44)
        self.assertFalse(saved_settings["tabs"]["fancy"])
        self.assertFalse(saved_settings["tabs"]["agent_indicators"])
        self.assertFalse(saved_settings["tabs"]["ssh_alert"])
        self.assertFalse(saved_settings["tabs"]["command_palette"])
        self.assertFalse(saved_settings["tabs"]["workspace_switcher"])
        self.assertFalse(saved_settings["tabs"]["session_manager"])
        self.assertEqual(saved_settings["font"]["size"], 15)
        self.assertFalse(saved_settings["font"]["ligatures"])
        self.assertTrue(saved_settings["font"]["fallback"])
        self.assertEqual(saved_settings["colors"]["scheme"], "Monokai Vivid")
        self.assertEqual(saved_settings["colors"]["scheme_light"], "Builtin Light")
        self.assertTrue(saved_settings["colors"]["oled_black"])
        self.assertEqual(saved_settings["colors"]["cursor_color"], "#ff7edb")
        self.assertEqual(saved_settings["window"]["blur"], 20)
        self.assertEqual(saved_settings["window"]["win32_backdrop"], "Acrylic")
        self.assertEqual(saved_settings["performance"]["front_end"], "OpenGL")
        self.assertEqual(saved_settings["performance"]["max_fps"], 144)
        self.assertEqual(saved_settings["performance"]["animation_fps"], 15)
        self.assertFalse(saved_settings["display"]["enable_wayland"])
        self.assertTrue(saved_settings["hyperlinks"]["quick_select"])
        self.assertEqual(saved_settings["window"]["inactive_pane_hsb"]["brightness"], 0.8)
        self.assertTrue(saved_settings["window"]["background_gradient"])
        self.assertTrue(saved_settings["window"]["accent_border"])
        self.assertFalse(saved_settings["status"]["enabled"])
        self.assertTrue(saved_settings["hyperlinks"]["open_in_editor"])
        self.assertEqual(saved_settings["hyperlinks"]["editor"], "hx")

        st_after = get_current_state()
        self.assertEqual(st_after["theme_id"], 62)
        self.assertFalse(st_after["appearance"]["font_ligatures"])
        self.assertEqual(st_after["appearance"]["color_scheme_light"], "Builtin Light")
        self.assertEqual(st_after["appearance"]["inactive_pane_brightness"], 0.8)
        self.assertTrue(st_after["appearance"]["background_gradient"])
        self.assertTrue(st_after["appearance"]["accent_border"])
        self.assertFalse(st_after["appearance"]["status_bar"])
        self.assertTrue(st_after["appearance"]["open_in_editor"])
        self.assertEqual(st_after["appearance"]["editor_cmd"], "hx")
        self.assertEqual(st_after["appearance"]["tab_max_width"], 44)
        self.assertFalse(st_after["appearance"]["use_fancy_tab_bar"])
        self.assertFalse(st_after["appearance"]["agent_indicators"])
        self.assertFalse(st_after["appearance"]["ssh_alert"])
        self.assertFalse(st_after["appearance"]["command_palette"])


class StudioHTTPServerAndCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def test_http_endpoints(self) -> None:
        httpd, port = create_server(host="127.0.0.1", port=0)
        actual_port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{actual_port}"
        json_headers = {"Content-Type": "application/json", "X-Studio-Token": httpd.studio_token}
        try:
            html = urllib.request.urlopen(f"{base}/").read().decode("utf-8")
            self.assertIn("WezTerm Studio", html)
            self.assertIn(f'<meta name="studio-token" content="{httpd.studio_token}">', html)
            import re
            html_ids = set(re.findall(r'id="([^"]+)"', html))
            js_ids = set(re.findall(r'getElementById\("([^"]+)"\)', html))
            self.assertEqual(js_ids - html_ids, set())

            boot = json.loads(urllib.request.urlopen(f"{base}/api/bootstrap").read().decode("utf-8"))
            self.assertTrue(boot["ok"])
            self.assertEqual(len(boot["themes"]), 62)

            schemes_resp = json.loads(urllib.request.urlopen(f"{base}/api/schemes").read().decode("utf-8"))
            self.assertTrue(schemes_resp["ok"])
            self.assertGreaterEqual(len(schemes_resp["palettes"]), 20)
            sample_pal = schemes_resp["palettes"][0]
            self.assertTrue(sample_pal["bg"].startswith("#"))
            self.assertEqual(len(sample_pal["ansi"]), 8)
            self.assertIn("is_dark", sample_pal)

            req = urllib.request.Request(
                f"{base}/api/apply-tab-theme",
                data=json.dumps({"theme_id": 61, "tab_title_len": 16}).encode("utf-8"),
                headers=json_headers,
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["state"]["theme_id"], 61)

            req_app = urllib.request.Request(
                f"{base}/api/apply-appearance",
                data=json.dumps({
                    "updates": {
                        "font_ligatures": False,
                        "inactive_pane_brightness": 0.76,
                        "background_gradient": True,
                        "accent_border": True,
                    }
                }).encode("utf-8"),
                headers=json_headers,
                method="POST",
            )
            resp_app = json.loads(urllib.request.urlopen(req_app).read().decode("utf-8"))
            self.assertTrue(resp_app["ok"], resp_app)
            self.assertFalse(resp_app["state"]["appearance"]["font_ligatures"])
            self.assertTrue(resp_app["state"]["appearance"]["background_gradient"])
            self.assertTrue(resp_app["state"]["appearance"]["accent_border"])

            sess_resp = json.loads(urllib.request.urlopen(f"{base}/api/session").read().decode("utf-8"))
            self.assertTrue(sess_resp["ok"])
            self.assertFalse(sess_resp["session"]["exists"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_cli_commands(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["init"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = cli_main([
                "-s", "62",
                "-t", "14",
                "--font", "JetBrains Mono",
                "--size", "13",
                "--no-ligatures",
                "--light-scheme", "Catppuccin Latte",
                "--dim-inactive", "0.78",
                "--gradient",
                "--border",
                "--status-bar",
                "--tab-max-width", "42",
                "--no-fancy-tabs",
                "--editor", "nvim",
                "--open-in-editor",
                "--no-agent-radar",
                "--no-ssh-alert",
                "--no-command-palette",
                "--font-fallback",
                "--oled",
                "--cursor-color", "#60a5fa",
                "--blur", "22",
                "--win32-backdrop", "Acrylic",
                "--front-end", "Software",
                "--max-fps", "120",
                "--animation-fps", "30",
                "--no-wayland",
                "--quick-select",
                "--no-workspace-switcher",
                "--no-session-manager",
            ])
        self.assertEqual(rc, 0)
        st = get_current_state()
        self.assertEqual(st["theme_id"], 62)
        self.assertEqual(st["tab_title_len"], 14)
        self.assertEqual(st["appearance"]["font_family"], "JetBrains Mono")
        self.assertFalse(st["appearance"]["font_ligatures"])
        self.assertTrue(st["appearance"]["font_fallback"])
        self.assertEqual(st["appearance"]["color_scheme_light"], "Catppuccin Latte")
        self.assertTrue(st["appearance"]["oled_black"])
        self.assertEqual(st["appearance"]["cursor_color"], "#60a5fa")
        self.assertEqual(st["appearance"]["window_blur"], 22)
        self.assertEqual(st["appearance"]["win32_backdrop"], "Acrylic")
        self.assertEqual(st["appearance"]["front_end"], "Software")
        self.assertEqual(st["appearance"]["max_fps"], 120)
        self.assertEqual(st["appearance"]["animation_fps"], 30)
        self.assertFalse(st["appearance"]["enable_wayland"])
        self.assertTrue(st["appearance"]["quick_select"])
        self.assertFalse(st["appearance"]["workspace_switcher"])
        self.assertFalse(st["appearance"]["session_manager"])
        self.assertAlmostEqual(st["appearance"]["inactive_pane_brightness"], 0.78)
        self.assertTrue(st["appearance"]["background_gradient"])
        self.assertTrue(st["appearance"]["accent_border"])
        self.assertTrue(st["appearance"]["status_bar"])
        self.assertEqual(st["appearance"]["tab_max_width"], 42)
        self.assertFalse(st["appearance"]["use_fancy_tab_bar"])
        self.assertEqual(st["appearance"]["editor_cmd"], "nvim")
        self.assertTrue(st["appearance"]["open_in_editor"])
        self.assertFalse(st["appearance"]["agent_indicators"])
        self.assertFalse(st["appearance"]["ssh_alert"])
        self.assertFalse(st["appearance"]["command_palette"])

        pals = get_color_scheme_palettes()
        self.assertGreaterEqual(len(pals), 20)

        with redirect_stdout(buf):
            rc = cli_main(["--list-schemes", "-f", "dark catppuccin"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = cli_main(["--current"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = cli_main(["doctor"])
        self.assertEqual(rc, 0)
        self.assertIn("front_end='Software'", buf.getvalue())

        (self.tmp_path / "wezterm_session.json").write_text(
            json.dumps({
                "schema": "wezterm-studio-session/v1",
                "saved_at": "2026-11-20T19:30:00",
                "active_workspace": "wezterm-studio",
                "workspaces": [{"name": "wezterm-studio", "tabs": [{"title": "dev", "cwd": "/tmp"}]}],
            }),
            encoding="utf-8",
        )
        sess_buf = io.StringIO()
        with redirect_stdout(sess_buf):
            rc = cli_main(["session", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("wezterm-studio", sess_buf.getvalue())

        with redirect_stdout(sess_buf):
            rc = cli_main(["session", "clear"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.tmp_path / "wezterm_session.json").exists())

    def test_detect_user_config_conflicts(self) -> None:
        from wezterm_theme.core import detect_user_config_conflicts
        cfg = self.tmp_path / "wezterm.lua"

        # No file -> no conflicts
        self.assertEqual(detect_user_config_conflicts({"config_file": cfg, "defaults_json": self.tmp_path / "none"}), [])

        # Clean config -> no conflicts
        cfg.write_text("local wezterm = require 'wezterm'\nlocal config = {}\nreturn config\n", encoding="utf-8")
        self.assertEqual(detect_user_config_conflicts({"config_file": cfg, "defaults_json": self.tmp_path / "none"}), [])

        # Commented format-tab-title -> no conflicts
        cfg.write_text("-- wezterm.on('format-tab-title', function(tab) return 'x' end)\nreturn config\n", encoding="utf-8")
        self.assertEqual(detect_user_config_conflicts({"config_file": cfg, "defaults_json": self.tmp_path / "none"}), [])

        # Active format-tab-title -> conflict detected
        cfg.write_text("local wezterm = require 'wezterm'\nlocal config = {}\nwezterm.on('format-tab-title', function(tab) return 'x' end)\nreturn config\n", encoding="utf-8")
        paths = {"config_file": cfg, "defaults_json": self.tmp_path / "none", "backup_dir": self.tmp_path / "backups", "config_dir": self.tmp_path, "studio_lua": self.tmp_path / "wezterm_theme.lua", "studio_json": self.tmp_path / "wezterm_theme.json", "tab_theme_lua": self.tmp_path / "tab_theme.lua"}
        conflicts = detect_user_config_conflicts(paths)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("format-tab-title listener detected", conflicts[0])

        # Test automatic resolution during init
        from wezterm_theme.core import init_wezterm_config
        ok, msg = init_wezterm_config(paths=paths, disable_conflicts=True)
        self.assertTrue(ok)
        self.assertEqual(detect_user_config_conflicts(paths), [])
        self.assertIn("[disabled by wezterm-theme", cfg.read_text(encoding="utf-8"))


class ConflictHandlerScannerTests(unittest.TestCase):
    """`format-tab-title` disabling must touch only the conflicting call, exactly once."""

    def test_named_handler_only_disables_that_call(self) -> None:
        src = (
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            "local function fmt(tab) return tab.active_pane.title end\n"
            "wezterm.on('format-tab-title', fmt)\n"
            "config.font_size = 14\n"
            "wezterm.on('update-status', function(window)\n"
            "  window:set_right_status('hi')\n"
            "end)\n"
            "return config\n"
        )
        out, changed = disable_conflicting_tab_handlers(src)
        self.assertTrue(changed)
        self.assertIn("-- wezterm.on('format-tab-title', fmt)\n", out)
        self.assertIn("\nconfig.font_size = 14\n", out)
        self.assertIn("\nwezterm.on('update-status', function(window)\n  window:set_right_status('hi')\nend)\n", out)

    def test_handler_with_nested_closure_is_disabled_completely(self) -> None:
        src = (
            "wezterm.on('format-tab-title', function(tab)\n"
            "  local ok, t = pcall(function() return tab.active_pane.title end)\n"
            "  return t .. ')'\n"
            "end)\n"
            "return config\n"
        )
        out, changed = disable_conflicting_tab_handlers(src)
        self.assertTrue(changed)
        code_lines = [ln for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]
        self.assertEqual(code_lines, ["return config"])

    def test_disabling_is_idempotent(self) -> None:
        src = "wezterm.on('format-tab-title', function(tab)\n  return tab.active_pane.title\nend)\nreturn config\n"
        once, changed_once = disable_conflicting_tab_handlers(src)
        twice, changed_twice = disable_conflicting_tab_handlers(once)
        self.assertTrue(changed_once)
        self.assertFalse(changed_twice)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("[disabled by wezterm-theme"), 1)

    def test_inline_call_and_string_literals(self) -> None:
        src = (
            "local x = 1; wezterm.on('format-tab-title', fmt) local y = ')'\n"
            "local s = \"wezterm.on('format-tab-title', f)\"\n"
        )
        out, changed = disable_conflicting_tab_handlers(src)
        self.assertTrue(changed)
        self.assertTrue(out.startswith(
            "local x = 1; --[[ [disabled by wezterm-theme] wezterm.on('format-tab-title', fmt) ]] local y = ')'\n"
        ))
        self.assertIn("local s = \"wezterm.on('format-tab-title', f)\"\n", out)

    def test_block_comments_are_not_conflicts(self) -> None:
        src = "--[[\nwezterm.on('format-tab-title', function() end)\n]]\nreturn config\n"
        self.assertEqual(_find_tab_title_handlers(src), [])

    def test_lua_str_escapes_control_characters(self) -> None:
        self.assertEqual(_lua_str("a\nb'c\\"), "'a\\010b\\'c\\\\'")

    def test_restore_reverses_disable_exactly(self) -> None:
        sources = [
            "local function fmt(tab) return 'x' end\nwezterm.on('format-tab-title', fmt)\nconfig.font_size = 14\n",
            "if true then\n  wezterm.on('format-tab-title', function(tab)\n    return pcall(function() end)\n  end)\nend\n",
            "local x = 1; wezterm.on('format-tab-title', fmt) local y = ')'\n",
        ]
        for src in sources:
            disabled, changed = disable_conflicting_tab_handlers(src)
            self.assertTrue(changed, src)
            restored, count = restore_disabled_tab_handlers(disabled)
            self.assertEqual(count, 1, src)
            self.assertEqual(restored, src)

    def test_restore_handles_blocks_from_older_versions(self) -> None:
        # Exactly what wezterm-theme <= 1.4.0 wrote for a named handler (it over-captured
        # everything up to the next `end)`); restoring must re-enable all of it.
        legacy = (
            "local function fmt(tab) return tab.active_pane.title end\n"
            "-- [disabled by wezterm-theme: conflicts with dynamic tab bar themes]\n"
            "-- wezterm.on('format-tab-title', fmt)\n"
            "-- config.font_size = 14\n"
            "-- wezterm.on('update-status', function(window)\n"
            "--   window:set_right_status('hi')\n"
            "-- end)\n"
            "-- a comment the user wrote\n"
            "return config\n"
        )
        restored, count = restore_disabled_tab_handlers(legacy)
        self.assertEqual(count, 1)
        self.assertEqual(
            restored,
            "local function fmt(tab) return tab.active_pane.title end\n"
            "wezterm.on('format-tab-title', fmt)\n"
            "config.font_size = 14\n"
            "wezterm.on('update-status', function(window)\n"
            "  window:set_right_status('hi')\n"
            "end)\n"
            "-- a comment the user wrote\n"
            "return config\n",
        )

    def test_agent_list_is_shared_by_tab_radar_and_status_toast(self) -> None:
        lua = generate_lua_for_theme(get_theme_by_id(1), appearance={"status_bar": True})
        agent_list = "{ " + ", ".join(f"'{name}'" for name in AI_AGENTS) + " }"
        self.assertEqual(lua.count(agent_list), 2)
        self.assertNotIn("probe:match(", lua)


class SecurityAndRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def _write_basic_config(self, extra: str = "") -> None:
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            f"{extra}"
            "return config\n",
            encoding="utf-8",
        )

    def test_init_with_nested_closure_handler_then_rerun_is_noop(self) -> None:
        self._write_basic_config(
            "wezterm.on('format-tab-title', function(tab)\n"
            "  local ok, t = pcall(function() return tab.active_pane.title end)\n"
            "  return t\n"
            "end)\n"
        )
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        after_first = self.cfg_path.read_text(encoding="utf-8")
        self.assertEqual(_find_tab_title_handlers(after_first), [])

        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertIn("already integrated", msg)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), after_first)

    def test_init_upgrades_legacy_hook_with_absolute_module_path(self) -> None:
        legacy_hook = (
            "-- >>> wezterm-theme >>>\n"
            "do\n"
            "  local _wt_wz = require 'wezterm'\n"
            "  local _wt_p = (_wt_wz.config_dir or '') .. '/wezterm_theme.lua'\n"
            "  local _wt_f = io.open(_wt_p, 'r')\n"
            "  if _wt_f then\n"
            "    _wt_f:close()\n"
            "    local _wt_ok, _wt_m = pcall(dofile, _wt_p)\n"
            "    if _wt_ok and type(_wt_m) == 'table' and type(_wt_m.apply) == 'function' then\n"
            "      _wt_m.apply(cfg, _wt_wz)\n"
            "    end\n"
            "  end\n"
            "end\n"
            "-- <<< wezterm-theme <<<\n"
        )
        self.cfg_path.write_text(
            f"local wezterm = require 'wezterm'\nlocal cfg = wezterm.config_builder()\n{legacy_hook}return cfg\n",
            encoding="utf-8",
        )
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertIn("Updated the wezterm-theme hook", msg)
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn((self.tmp_path / "wezterm_theme.lua").resolve().as_posix(), text)
        self.assertIn("_wt_m.apply(cfg, _wt_wz)", text)
        self.assertIn("log_error", text)

        ok, msg = init_wezterm_config()
        self.assertIn("already integrated", msg)

    def test_invalid_loader_settings_json_is_never_overwritten(self) -> None:
        (self.tmp_path / "defaults.json").write_text("{}", encoding="utf-8")
        broken = '{\n  "keys": [ { "key": "k" } ],\n  "launch_menu": [ "htop" ],\n}\n'
        settings = self.tmp_path / "settings.json"
        settings.write_text(broken, encoding="utf-8")
        self.cfg_path.write_text("-- WezTerm loader\nreturn {}\n", encoding="utf-8")

        ok, _, err = apply_theme(get_theme_by_id(5))
        self.assertFalse(ok)
        self.assertIn("not valid JSON", err)
        self.assertEqual(settings.read_text(encoding="utf-8"), broken)
        self.assertFalse((self.tmp_path / "tab_theme.lua").exists())

    def test_invalid_appearance_values_are_rejected_without_changes(self) -> None:
        self._write_basic_config()
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        before = (self.tmp_path / "wezterm_theme.json").read_text(encoding="utf-8")

        for bad in (
            {"font_size": "abc"},
            {"font_family": "Evil\nFont"},
            {"window_opacity": 3},
            {"cursor_color": "red"},
            {"status_bar": "maybe"},
            {"window_padding": {"left": "x"}},
        ):
            ok, err = apply_appearance(bad)
            self.assertFalse(ok, bad)
            self.assertTrue(err)
        self.assertEqual((self.tmp_path / "wezterm_theme.json").read_text(encoding="utf-8"), before)

        ok, err = apply_appearance({"cursor_color": "Default", "font_size": "13.5", "status_bar": True})
        self.assertTrue(ok, err)
        self.assertEqual(get_current_state()["appearance"]["font_size"], 13.5)

    def test_import_preset_from_long_json_string(self) -> None:
        self._write_basic_config()
        preset = {
            "schema": "wezterm-theme-preset/v1",
            "theme_id": 12,
            "tab_title_len": 18,
            "symbols": {"arrow": "chevron", "inactive": "radio", "marker": "balls", "prefix": "bolt"},
            "appearance": {
                "font_family": "JetBrains Mono",
                "font_size": 13.5,
                "color_scheme": "Catppuccin Mocha",
                "window_opacity": 0.92,
                "window_padding": 10,
                "status_bar": True,
                "open_in_editor": True,
                "editor_cmd": "nvim",
            },
        }
        raw = json.dumps(preset, indent=2)
        self.assertGreater(max(len(part) for part in raw.split("/")), 255)
        ok, msg = import_preset(raw)
        self.assertTrue(ok, msg)
        self.assertEqual(get_current_state()["theme_id"], 12)

        ok, msg = import_preset({"theme_id": "abc"})
        self.assertFalse(ok)
        self.assertIn("theme_id", msg)

    def test_concurrent_applies_are_serialized(self) -> None:
        self._write_basic_config()
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)

        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def fake_validate(_path: Path) -> None:
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with counter_lock:
                active -= 1
            return None

        with mock.patch("wezterm_theme.core.validate_config", side_effect=fake_validate):
            threads = [
                threading.Thread(target=apply_theme, args=(get_theme_by_id(tid),)) for tid in (3, 7, 21, 40)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(max_active, 1)
        st = get_current_state()
        saved = json.loads((self.tmp_path / "wezterm_theme.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["theme_id"], st["theme_id"])

    @unittest.skipIf(os.name == "nt", "symlinks need extra privileges on Windows")
    def test_atomic_write_keeps_symlink_and_mode(self) -> None:
        real = self.tmp_path / "dotfiles" / "wezterm.lua"
        real.parent.mkdir()
        real.write_text("old\n", encoding="utf-8")
        os.chmod(real, 0o640)
        link = self.tmp_path / "link.lua"
        link.symlink_to(real)

        atomic_write_text(link, "new\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in real.parent.iterdir()], ["wezterm.lua"])

    def test_missing_wezterm_binary_is_reported(self) -> None:
        self._write_basic_config()
        with mock.patch("wezterm_theme.core.find_wezterm_cmd", return_value=None), redirect_stderr(io.StringIO()):
            ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertIn("Not validated", msg)

    def test_http_rejects_cross_site_and_bad_input(self) -> None:
        httpd, _ = create_server(host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        good = {"Content-Type": "application/json", "X-Studio-Token": httpd.studio_token}

        def call(path: str, payload: object | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
            data = None if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))
            req = urllib.request.Request(f"{base}{path}", data=data, headers=headers or {}, method="POST" if data is not None else "GET")
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read().decode("utf-8"))

        self._write_basic_config()
        try:
            # Backing up before any theme exists must not silently apply one.
            status, body = call("/api/backup", {"label": "first"}, good)
            self.assertEqual(status, 200)
            self.assertFalse(body["ok"])
            self.assertFalse((self.tmp_path / "wezterm_theme.lua").exists())

            update = {"updates": {"accent_border": True}}
            self.assertEqual(call("/api/bootstrap", headers={"Host": f"evil.example:{port}"})[0], 403)
            self.assertEqual(call("/api/apply-appearance", update, {"Content-Type": "application/json"})[0], 403)
            self.assertEqual(call("/api/apply-appearance", update, {**good, "Content-Type": "text/plain"})[0], 415)
            self.assertEqual(call("/api/apply-appearance", update, {**good, "Origin": "https://evil.example"})[0], 403)
            self.assertEqual(call("/api/apply-appearance", update, {**good, "Host": f"evil.example:{port}"})[0], 403)

            status, body = call("/api/apply-appearance", update, {**good, "Origin": base})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"], body)

            status, body = call("/api/apply-tab-theme", {"theme_id": "abc"}, good)
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])
            status, body = call("/api/apply-appearance", {"updates": {"font_size": "big"}}, good)
            self.assertEqual(status, 400)
            self.assertIn("font_size", body["msg"])
            self.assertEqual(call("/api/import", {"preset": "/etc/passwd"}, good)[0], 400)
            self.assertEqual(call("/api/import", b"[1, 2]", good)[0], 400)

            preset_raw, _ = export_preset()
            status, body = call("/api/import", {"preset": preset_raw}, good)
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"], body)

            with urllib.request.urlopen(f"{base}/") as resp:
                self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        finally:
            httpd.shutdown()
            httpd.server_close()


class BackupAndMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            "wezterm.on('format-tab-title', function(tab)\n"
            "  return tab.active_pane.title\n"
            "end)\n"
            "return config\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def test_uninstall_reenables_handlers_disabled_by_init(self) -> None:
        original = self.cfg_path.read_text(encoding="utf-8")
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertEqual(_find_tab_title_handlers(self.cfg_path.read_text(encoding="utf-8")), [])

        ok, msg = uninstall_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertIn("re-enabled 1 format-tab-title handler", msg)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), original)

        init_wezterm_config()
        ok, msg = uninstall_wezterm_config(restore_handlers=False)
        self.assertTrue(ok, msg)
        self.assertEqual(_find_tab_title_handlers(self.cfg_path.read_text(encoding="utf-8")), [])

    def test_restore_by_exact_filename_and_across_modes(self) -> None:
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        apply_theme(get_theme_by_id(31), tab_title_len=18)
        apply_theme(get_theme_by_id(5))
        b31 = next(b for b in get_available_backups() if b["theme_id"] == 31)

        ok, msg = restore_backup(b31["filename"])
        self.assertTrue(ok, msg)
        self.assertEqual(get_current_state()["theme_id"], 31)

        # Switch to loader mode: the standalone companion JSON must not replace settings.json.
        (self.tmp_path / "defaults.json").write_text("{}", encoding="utf-8")
        settings = self.tmp_path / "settings.json"
        settings.write_text(json.dumps({"keys": {"leader": {"key": "a"}}}), encoding="utf-8")
        self.cfg_path.write_text("-- WezTerm loader\nreturn {}\n", encoding="utf-8")
        apply_theme(get_theme_by_id(5))

        ok, msg = restore_backup(b31["filename"])
        self.assertTrue(ok, msg)
        self.assertIn("standalone mode", msg)
        saved = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(saved["keys"]["leader"]["key"], "a")
        self.assertEqual(saved["tabs"]["theme_id"], 31)
        self.assertEqual(saved["tabs"]["tab_title_len"], 18)
        self.assertNotIn("appearance", saved)

    def test_automatic_backups_are_pruned_but_user_labels_are_kept(self) -> None:
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        # Many backups land in the same second here; the newest must never be the one pruned.
        for _ in range(MAX_PRE_RESTORE_BACKUPS + 3):
            newest = backup_current_config(label="pre_restore")
            self.assertTrue(newest.exists(), newest)
        for _ in range(3):
            backup_current_config(label="manual")
        names = [p.name for p in (self.tmp_path / "backups").glob("tab_theme.lua.*.bak")]
        self.assertEqual(sum("_pre_restore" in n for n in names), MAX_PRE_RESTORE_BACKUPS)
        self.assertEqual(sum("_manual" in n for n in names), 3)

        paths = resolve_wezterm_paths()
        for _ in range(MAX_WEZTERM_LUA_BACKUPS + 2):
            newest = _backup_wezterm_lua(paths, "conflict_fix")
            self.assertTrue(newest.exists(), newest)
        lua_backups = [p.name for p in (self.tmp_path / "backups").glob("wezterm.lua.*.bak")]
        self.assertEqual(len(lua_backups), MAX_WEZTERM_LUA_BACKUPS + 1)
        self.assertEqual(sum(n.endswith("_pre_init.bak") for n in lua_backups), 1)

    def test_scheme_cache_is_refreshed_when_wezterm_changes(self) -> None:
        fake = [
            {"name": f"Fake {i}", "bg": "#000000", "fg": "#ffffff", "cursor": "#ffffff", "ansi": ["#000000"] * 8, "is_dark": True}
            for i in range(60)
        ]
        cache_dir = self.tmp_path / "cache"
        cache_dir.mkdir()
        cache_file = cache_dir / "color_scheme_palettes.json"
        with (
            mock.patch.object(fonts_mod, "get_cache_dir", return_value=cache_dir),
            mock.patch.object(fonts_mod, "_SCHEME_PALETTES_CACHE", None),
            mock.patch.object(fonts_mod, "_SCHEMES_CACHE", None),
        ):
            cache_file.write_text(json.dumps({"wezterm_version": "wezterm 1", "palettes": fake}), encoding="utf-8")
            with mock.patch.object(fonts_mod, "_wezterm_version", return_value="wezterm 1"):
                self.assertEqual(fonts_mod.get_color_scheme_palettes()[0]["name"], "Fake 0")

            fonts_mod._SCHEME_PALETTES_CACHE = None
            with (
                mock.patch.object(fonts_mod, "_wezterm_version", return_value="wezterm 2"),
                mock.patch.object(fonts_mod, "find_wezterm_cmd", return_value=None),
            ):
                names = [p["name"] for p in fonts_mod.get_color_scheme_palettes()]
            self.assertNotIn("Fake 0", names)

            # Pre-versioning caches (bare list) stay usable when wezterm cannot rebuild them.
            fonts_mod._SCHEME_PALETTES_CACHE = None
            cache_file.write_text(json.dumps(fake), encoding="utf-8")
            with mock.patch.object(fonts_mod, "_wezterm_version", return_value=None):
                self.assertEqual(fonts_mod.get_color_scheme_palettes()[0]["name"], "Fake 0")


class Bugfix142Tests(unittest.TestCase):
    """Regression tests for v1.4.2 high & medium priority bug fixes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def _write_basic_config(self) -> None:
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            "return config\n",
            encoding="utf-8",
        )

    def test_open_in_editor_default_on_and_explicit_off(self) -> None:
        lua_default = generate_lua_for_theme(get_theme_by_id(10), appearance=None)
        self.assertIn("wezterm.on('open-uri'", lua_default)

        lua_cmd_only = generate_lua_for_theme(get_theme_by_id(10), appearance={"editor_cmd": "hx"})
        self.assertIn("wezterm.on('open-uri'", lua_cmd_only)
        self.assertIn("'hx'", lua_cmd_only)

        lua_off = generate_lua_for_theme(get_theme_by_id(10), appearance={"open_in_editor": False})
        self.assertNotIn("wezterm.on('open-uri'", lua_off)

    def test_doctor_reports_validation_skipped_when_wezterm_missing(self) -> None:
        self._write_basic_config()
        buf = io.StringIO()
        with (
            mock.patch("wezterm_theme.core.find_wezterm_cmd", return_value=None),
            mock.patch("wezterm_theme.paths.find_wezterm_cmd", return_value=None),
            redirect_stdout(buf),
            redirect_stderr(io.StringIO()),
        ):
            rc = cli_main(["doctor"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Validation skipped", out)
        self.assertNotIn("validated cleanly", out)

    def test_backup_listing_uses_filename_stamp_and_seq_not_mtime(self) -> None:
        bkp_dir = self.tmp_path / "backups"
        bkp_dir.mkdir(parents=True)
        files = [
            ("tab_theme.lua.20261121-100000.bak", 1000.0, 1),
            ("tab_theme.lua.20261121-100000-2.bak", 900.0, 2),
            ("tab_theme.lua.20261121-100000-10.bak", 800.0, 3),
            ("tab_theme.lua.20261121-100001.bak", 700.0, 4),
        ]
        for fname, mtime, tid in files:
            p = bkp_dir / fname
            p.write_text(f"-- Theme #{tid:02d}: T{tid} (Test)\nreturn {{}}\n", encoding="utf-8")
            os.utime(p, (mtime, mtime))

        listed = [b["filename"] for b in get_available_backups()]
        self.assertEqual(
            listed,
            [
                "tab_theme.lua.20261121-100001.bak",
                "tab_theme.lua.20261121-100000-10.bak",
                "tab_theme.lua.20261121-100000-2.bak",
                "tab_theme.lua.20261121-100000.bak",
            ],
        )

    def test_enum_validation_fps_cap_font_reset_and_block_comments(self) -> None:
        self._write_basic_config()
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)

        for bad in (
            {"front_end": "Vulkan"},
            {"win32_backdrop": "Glass"},
            {"cursor_style": "Beam"},
            {"audible_bell": "Loud"},
            {"window_decorations": "INVALID_FLAG"},
            {"max_fps": 500},
            {"animation_fps": 500},
        ):
            ok, err = apply_appearance(bad)
            self.assertFalse(ok, f"Should reject {bad}")
            self.assertTrue(err)

        # Case-insensitive auto and resetting fonts to Default in Standalone mode
        ok, err = apply_appearance({
            "font_family": "JetBrains Mono",
            "tab_font_family": "Fira Code",
            "use_fancy_tab_bar": "AUTO",
            "enable_wayland": "Auto",
            "window_decorations": "TITLE | RESIZE",
        })
        self.assertTrue(ok, err)
        self.assertEqual(get_current_state()["appearance"]["font_family"], "JetBrains Mono")
        self.assertEqual(get_current_state()["appearance"]["tab_font_family"], "Fira Code")

        ok, err = apply_appearance({"font_family": "Default", "tab_font_family": "Default"})
        self.assertTrue(ok, err)
        self.assertIsNone(get_current_state()["appearance"]["font_family"])
        self.assertIsNone(get_current_state()["appearance"]["tab_font_family"])

        # Block comments must not trigger Software renderer performance warnings
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "local config = wezterm.config_builder()\n"
            "--[[ config.front_end = 'Software'\nconfig.animation_fps = 60 ]]\n"
            "return config\n",
            encoding="utf-8",
        )
        self.assertEqual(detect_performance_warnings(appearance={}), [])

        # Loader mode font reset and reset sentinels
        (self.tmp_path / "defaults.json").write_text("{}", encoding="utf-8")
        (self.tmp_path / "settings.json").write_text("{}", encoding="utf-8")
        self.cfg_path.write_text("-- WezTerm loader\nreturn {}\n", encoding="utf-8")
        ok, err = apply_appearance({"font_family": "Hack", "tab_font_family": "Hack", "color_scheme": "Dracula"})
        self.assertTrue(ok, err)
        ok, err = apply_appearance({"font_family": "Default", "tab_font_family": "none", "color_scheme": "Auto"})
        self.assertTrue(ok, err)
        saved_settings = json.loads((self.tmp_path / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("family", saved_settings.get("font", {}))
        self.assertNotIn("family", saved_settings.get("tabs", {}).get("frame_font", {}))
        self.assertNotIn("scheme", saved_settings.get("colors", {}))

    def test_init_ignores_commented_return_and_handles_return_table(self) -> None:
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "return {\n"
            "  font_size = 12,\n"
            "}\n"
            "-- return fake_config\n"
            "--[[ return other_config ]]\n",
            encoding="utf-8",
        )
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        txt = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("local _wt_config = {", txt)
        self.assertIn("return _wt_config", txt)
        self.assertLess(txt.index("return _wt_config"), txt.index("-- return fake_config"))

    def test_failed_validation_does_not_create_backup(self) -> None:
        self._write_basic_config()
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        before_bkps = list((self.tmp_path / "backups").glob("tab_theme.lua.*.bak"))
        with mock.patch("wezterm_theme.core.validate_theme_lua", return_value="syntax error"):
            ok, bkp, err = apply_theme(get_theme_by_id(5))
        self.assertFalse(ok)
        self.assertIsNone(bkp)
        self.assertIn("syntax error", err or "")
        after_bkps = list((self.tmp_path / "backups").glob("tab_theme.lua.*.bak"))
        self.assertEqual(len(after_bkps), len(before_bkps))

    def test_fonts_is_nerd_detects_nfm_and_nfp(self) -> None:
        with (
            mock.patch.object(fonts_mod, "_FONTS_CACHE", None),
            mock.patch.object(fonts_mod, "_wezterm_list_fonts", return_value=(set(), set(), {})),
            mock.patch.object(
                fonts_mod,
                "_fc_list_mono_families",
                return_value={"VictorMono NFM", "CascadiaCode NFP", "Hack NF", "DejaVu Sans Mono"},
            ),
        ):
            by_name = {f["name"]: f["is_nerd"] for f in fonts_mod.get_installed_fonts(refresh=True)}
        self.assertTrue(by_name["VictorMono NFM"])
        self.assertTrue(by_name["CascadiaCode NFP"])
        self.assertTrue(by_name["Hack NF"])
        self.assertFalse(by_name["DejaVu Sans Mono"])

    def test_ipv6_loopback_server_and_strict_int_validation(self) -> None:
        self._write_basic_config()
        try:
            httpd, _ = create_server(host="::1", port=0)
        except OSError:
            self.skipTest("IPv6 loopback ::1 not available in this environment")
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://[::1]:{port}"
        good = {"Content-Type": "application/json", "X-Studio-Token": httpd.studio_token}
        try:
            with urllib.request.urlopen(f"{base}/api/bootstrap") as resp:
                self.assertEqual(resp.status, 200)
                boot = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(boot["ok"])

            # Reject JSON booleans as theme_id / tab_title_len
            for bad_payload in ({"theme_id": True}, {"theme_id": 10, "tab_title_len": False}):
                req = urllib.request.Request(
                    f"{base}/api/apply-tab-theme",
                    data=json.dumps(bad_payload).encode("utf-8"),
                    headers=good,
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req)
                self.assertEqual(ctx.exception.code, 400)
                ctx.exception.close()

            # /api/apply-tab-theme must include updated warnings
            req = urllib.request.Request(
                f"{base}/api/apply-tab-theme",
                data=json.dumps({"theme_id": 10}).encode("utf-8"),
                headers=good,
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data["ok"])
                self.assertIn("warnings", data)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_lua_runtime_session_restore_default_oled_ligatures_git_and_powerline(self) -> None:
        wz = find_wezterm_cmd()
        if not wz:
            self.skipTest("wezterm binary not installed")

        snap_file = self.tmp_path / "custom_session.json"
        snap_file.write_text(
            json.dumps({
                "schema": "wezterm-studio-session/v1",
                "saved_at": "2026-11-21T14:00:00",
                "active_workspace": "default",
                "workspaces": [
                    {
                        "name": "default",
                        "tabs": [
                            {"title": "home", "cwd": "/home/u"},
                            {"title": "projA", "cwd": "/proj/a"},
                            {"title": "projB", "cwd": "/proj/b"},
                        ],
                    },
                    {
                        "name": "work",
                        "tabs": [
                            {"title": "api", "cwd": "/work/api"},
                            {"title": "web", "cwd": "/work/web"},
                        ],
                    },
                ],
            }),
            encoding="utf-8",
        )

        # Fake git repo & non-git dir to verify zero subprocess in normal repos and non-git dirs
        fake_repo = self.tmp_path / "myrepo"
        (fake_repo / ".git").mkdir(parents=True)
        (fake_repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/v142\n", encoding="utf-8")
        non_repo = self.tmp_path / "plain_dir"
        non_repo.mkdir()

        mod_oled_plain = self.tmp_path / "mod_oled_plain.lua"
        mod_oled_plain.write_text(
            generate_lua_for_theme(
                get_theme_by_id(11),  # Powerline style
                appearance={
                    "oled_black": True,
                    "font_ligatures": False,
                    "status_bar": True,
                    "ssh_alert": True,
                    "command_palette": True,
                    "session_manager": True,
                },
                session_path=snap_file.as_posix(),
            ),
            encoding="utf-8",
        )
        mod_oled_light = self.tmp_path / "mod_oled_light.lua"
        mod_oled_light.write_text(
            generate_lua_for_theme(
                get_theme_by_id(62),  # workspace_color style
                appearance={
                    "color_scheme": "Catppuccin Mocha",
                    "color_scheme_light": "Catppuccin Latte",
                    "oled_black": True,
                },
            ),
            encoding="utf-8",
        )

        harness = self.tmp_path / "harness_142.lua"
        harness.write_text(
            "\n".join([
                "local real_wz = require 'wezterm'",
                "local captured_cbs = {}",
                "local format_tab_cb, update_status_cb",
                "local child_calls = 0",
                "local mock_wz = setmetatable({",
                "  GLOBAL = {},",
                "  gui = { get_appearance = function() return 'Light' end },",
                "  on = function(ev, cb)",
                "    if ev == 'format-tab-title' then format_tab_cb = cb end",
                "    if ev == 'update-status' then update_status_cb = cb end",
                "  end,",
                "  action_callback = function(fn)",
                "    local u = real_wz.action_callback(fn)",
                "    captured_cbs[tostring(u)] = fn",
                "    return u",
                "  end,",
                "  run_child_process = function(argv)",
                "    child_calls = child_calls + 1",
                "    return true, 'fallback-branch\\n', ''",
                "  end,",
                "  battery_info = function() return {} end,",
                "  config_dir = " + _lua_str(self.tmp_path.as_posix()) + ",",
                "}, { __index = real_wz })",
                "",
                "-- 1) OLED without light scheme MUST apply #000000 even in OS Light mode",
                "local cfg1 = {}",
                f"local m1 = dofile({_lua_str(mod_oled_plain.as_posix())})",
                "m1.apply(cfg1, mock_wz)",
                "local powerline_fmt_cb = format_tab_cb",
                "assert(cfg1.colors and cfg1.colors.background == '#000000', 'OLED failed in Light OS without light scheme')",
                "",
                "-- 2) OLED with light scheme MUST NOT force #000000 in OS Light mode",
                "local cfg2 = {}",
                f"local m2 = dofile({_lua_str(mod_oled_light.as_posix())})",
                "m2.apply(cfg2, mock_wz)",
                "local ws_fmt_cb = format_tab_cb",
                "assert(not (cfg2.colors and cfg2.colors.background == '#000000'), 'OLED wrongly forced black when light scheme is active')",
                "",
                "-- 3) Toggle Programming Ligatures respects initial config.harfbuzz_features on 1st press",
                "local entries = m1.command_palette_entries(cfg1, mock_wz)",
                "local toggle_lig_fn, restore_fn",
                "for _, e in ipairs(entries) do",
                "  if e.brief:find('Toggle Programming Ligatures') then toggle_lig_fn = captured_cbs[tostring(e.action)] end",
                "  if e.brief:find('Restore Saved Workspace Session') then restore_fn = captured_cbs[tostring(e.action)] end",
                "end",
                "assert(toggle_lig_fn and restore_fn, 'Missing command palette callbacks')",
                "local cur_ov = {}",
                "local fake_win = {",
                "  get_config_overrides = function() return cur_ov end,",
                "  set_config_overrides = function(_, o) cur_ov = o end,",
                "  perform_action = function() end,",
                "  toast_notification = function() end,",
                "  active_workspace = function() return 'default' end,",
                "  active_key_table = function() return nil end,",
                "  set_right_status = function() end,",
                "}",
                "toggle_lig_fn(fake_win, {})",
                "assert(type(cur_ov.harfbuzz_features) == 'table' and #cur_ov.harfbuzz_features == 0, '1st toggle should enable ligatures')",
                "toggle_lig_fn(fake_win, {})",
                "assert(#cur_ov.harfbuzz_features == 3, '2nd toggle should disable ligatures')",
                "",
                "-- 4) Session Restore restores missing tabs in existing 'default' workspace AND is idempotent",
                "local default_tabs = { { cwd = '/home/u', title = '' } }",
                "local function make_tab_obj(t)",
                "  return {",
                "    active_pane = function() return { get_current_working_dir = function() return { file_path = t.cwd } end } end,",
                "    set_title = function(_, s) t.title = s end,",
                "  }",
                "end",
                "local all_wins = {}",
                "local function make_win(ws, init_tabs)",
                "  local w = { ws = ws, _tabs = init_tabs }",
                "  function w:get_workspace() return self.ws end",
                "  function w:tabs()",
                "    local out = {}",
                "    for _, t in ipairs(self._tabs) do table.insert(out, make_tab_obj(t)) end",
                "    return out",
                "  end",
                "  function w:spawn_tab(opts)",
                "    local t = { cwd = (opts and opts.cwd) or '', title = '' }",
                "    table.insert(self._tabs, t)",
                "    return make_tab_obj(t)",
                "  end",
                "  return w",
                "end",
                "table.insert(all_wins, make_win('default', default_tabs))",
                "mock_wz.mux = {",
                "  all_windows = function() return all_wins end,",
                "  spawn_window = function(opts)",
                "    local t1 = { cwd = (opts and opts.cwd) or '', title = '' }",
                "    local nw = make_win(opts.workspace or 'default', { t1 })",
                "    table.insert(all_wins, nw)",
                "    return make_tab_obj(t1), nil, nw",
                "  end,",
                "}",
                "restore_fn(fake_win, {})",
                "assert(#all_wins == 2, 'Expected default + work windows, got ' .. tostring(#all_wins))",
                "assert(#all_wins[1]._tabs == 3, 'Expected 3 tabs in default workspace, got ' .. tostring(#all_wins[1]._tabs))",
                "assert(all_wins[1]._tabs[2].cwd == '/proj/a' and all_wins[1]._tabs[3].cwd == '/proj/b', 'Wrong restored default tabs')",
                "assert(#all_wins[2]._tabs == 2 and all_wins[2].ws == 'work', 'Wrong restored work workspace')",
                "-- Running restore a second time must not duplicate any tabs",
                "restore_fn(fake_win, {})",
                "assert(#all_wins == 2 and #all_wins[1]._tabs == 3 and #all_wins[2]._tabs == 2, 'Restore was not idempotent')",
                "",
                "-- 5) Pure-Lua git branch check skips subprocess in non-git dir and normal git repo",
                "local right_status_val = ''",
                "fake_win.set_right_status = function(_, v) right_status_val = v end",
                "local function fake_pane_for(p)",
                "  return {",
                "    get_current_working_dir = function() return { file_path = p } end,",
                "    get_domain_name = function() return 'local' end,",
                "    get_user_vars = function() return {} end,",
                "    get_foreground_process_name = function() return '/bin/bash' end,",
                "    get_title = function() return '' end,",
                "    pane_id = function() return 1 end,",
                "  }",
                "end",
                f"update_status_cb(fake_win, fake_pane_for({_lua_str(non_repo.as_posix())}))",
                "assert(child_calls == 0, 'Non-git dir should not spawn git subprocess')",
                f"update_status_cb(fake_win, fake_pane_for({_lua_str(fake_repo.as_posix())}))",
                "assert(child_calls == 0, 'Normal git repo should read .git/HEAD without subprocess')",
                "assert(right_status_val:find('feat/v142', 1, true), 'Git branch feat/v142 not rendered in status bar')",
                "",
                "-- 6) Powerline separator uses next tab SSH/PROD danger tint",
                "local t_local = { tab_index = 0, is_active = true, tab_title = '', panes = { {} }, active_pane = { current_working_dir = '/tmp', foreground_process_name = '/bin/bash', title = 'bash', user_vars = {} } }",
                "local t_prod = { tab_index = 1, is_active = false, tab_title = '', panes = { {} }, active_pane = { current_working_dir = '/tmp', foreground_process_name = '/usr/bin/ssh', title = 'ssh', user_vars = { WEZTERM_PROG = 'ssh root@prod-db-01' } } }",
                "local fmt_out = powerline_fmt_cb(t_local, { t_local, t_prod }, {}, cfg1, false, 40)",
                "assert(tostring(fmt_out):find('#dc2626', 1, true) or tostring(fmt_out):match('220[:;]38[:;]38'), 'Powerline separator did not use prod crimson bg: ' .. tostring(fmt_out))",
                "",
                "-- 7) workspace_color truncates multi-byte UTF-8 workspace names cleanly",
                "mock_wz.mux.get_active_workspace = function() return 'ação-rápida-dev' end",
                "local ws_out = ws_fmt_cb(t_local, { t_local }, {}, cfg2, false, 40)",
                "assert(tostring(ws_out):find('[ação-rá…]', 1, true), 'UTF-8 workspace name corrupted: ' .. tostring(ws_out))",
                "",
                "return real_wz.config_builder()",
            ]),
            encoding="utf-8",
        )
        res = subprocess.run(
            [*wz, "--config-file", str(harness), "ls-fonts", "--text", "a"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        errors = [ln for ln in res.stderr.splitlines() if " ERROR " in ln]
        self.assertEqual(errors, [], f"Lua harness errors:\n{res.stderr}")
        self.assertEqual(res.returncode, 0, res.stderr)


class ReviewFollowup142Tests(unittest.TestCase):
    """Regression tests for the v1.4.2 code-review follow-up fixes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def _no_validation(self) -> mock._patch:
        return mock.patch("wezterm_theme.core.validate_config", return_value=None)

    def test_init_hooks_the_top_level_return_not_a_helper_function(self) -> None:
        self.cfg_path.write_text(
            "local wezterm = require 'wezterm'\n"
            "local function pick(x)\n"
            "  return x\n"
            "end\n"
            "return {\n"
            "  font_size = 12,\n"
            "}\n",
            encoding="utf-8",
        )
        ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("local function pick(x)\n  return x\nend\n", text)
        self.assertNotIn("_wt_m.apply(x,", text)
        self.assertIn("_wt_m.apply(_wt_config,", text)
        self.assertTrue(text.rstrip().endswith("return _wt_config"), text)

    def test_restoring_the_oldest_pre_restore_backup_keeps_its_settings(self) -> None:
        theme = get_theme_by_id(3)
        with self._no_validation():
            self.assertTrue(apply_theme(theme, appearance_updates={"font_size": 11})[0])
            oldest = backup_current_config(label="pre_restore")
            for size in range(12, 12 + MAX_PRE_RESTORE_BACKUPS - 1):
                apply_theme(theme, appearance_updates={"font_size": size})
                backup_current_config(label="pre_restore")
            apply_theme(theme, appearance_updates={"font_size": 30})
            ok, msg = restore_backup(oldest.name)
        self.assertTrue(ok, msg)
        self.assertEqual(get_current_state()["appearance"]["font_size"], 11)
        self.assertTrue(oldest.is_file(), "the restored backup was pruned while restoring it")

    def test_init_rollback_and_uninstall_keep_crlf_line_endings(self) -> None:
        original = b"local wezterm = require 'wezterm'\r\nlocal config = {}\r\nreturn config\r\n"
        self.cfg_path.write_bytes(original)

        def broken_hook(path: Path) -> str | None:
            text = Path(path).read_text(encoding="utf-8")
            return "12:00:00.000  ERROR  lua: boom" if "wezterm-theme >>>" in text else None

        with mock.patch("wezterm_theme.core.validate_config", side_effect=broken_hook):
            ok, _ = init_wezterm_config()
        self.assertFalse(ok)
        self.assertEqual(self.cfg_path.read_bytes(), original, "rollback changed the line endings")

        with self._no_validation():
            self.assertTrue(init_wezterm_config()[0])
            data = self.cfg_path.read_bytes()
            self.assertEqual(data.count(b"\r\n"), data.count(b"\n"), "init mixed LF into a CRLF file")
            self.assertTrue(uninstall_wezterm_config()[0])
        self.assertEqual(self.cfg_path.read_bytes(), original)

    def test_init_never_replaces_invalid_state_json_and_ignores_bad_saved_values(self) -> None:
        state_json = self.tmp_path / "wezterm_theme.json"
        state_json.write_text('{"theme_id": 5,}\n', encoding="utf-8")
        with self._no_validation():
            ok, msg = init_wezterm_config()
        self.assertFalse(ok)
        self.assertIn("not valid JSON", msg)
        self.assertEqual(state_json.read_text(encoding="utf-8"), '{"theme_id": 5,}\n')

        saved = '{"theme_id": 5, "appearance": {"window_blur": "abc", "font_size": 14}}\n'
        state_json.write_text(saved, encoding="utf-8")
        with self._no_validation():
            ok, msg = init_wezterm_config()
        self.assertTrue(ok, msg)
        self.assertEqual(state_json.read_text(encoding="utf-8"), saved, "a valid state file was rewritten")
        app = get_current_state()["appearance"]
        self.assertEqual(app["font_size"], 14)
        self.assertIsNone(app["window_blur"])

    def test_errors_already_in_the_user_config_do_not_block_changes(self) -> None:
        with self._no_validation():
            self.assertTrue(apply_theme(get_theme_by_id(3))[0])
        studio_lua = self.tmp_path / "wezterm_theme.lua"
        old_error = "12:00:00.000  ERROR  lua: user typo in wezterm.lua"

        def fake_validate(path: Path) -> str | None:
            if Path(path) != self.cfg_path:
                return None  # the generated module validated on its own is fine
            errors = [old_error]
            if "-- Theme #05:" in studio_lua.read_text(encoding="utf-8"):
                errors.append("12:00:01.000  ERROR  lua: broken by the change")
            return "\n".join(errors)

        with mock.patch("wezterm_theme.core.validate_config", side_effect=fake_validate):
            ok, _, err = apply_theme(get_theme_by_id(4))
            self.assertTrue(ok, err)
            ok, _, err = apply_theme(get_theme_by_id(5))
        self.assertFalse(ok)
        self.assertIn("broken by the change", err or "")
        self.assertIn("-- Theme #04:", studio_lua.read_text(encoding="utf-8"))

    def test_backups_stay_ordered_when_the_clock_goes_back(self) -> None:
        with self._no_validation():
            self.assertTrue(apply_theme(get_theme_by_id(3))[0])

        class FakeDateTime(datetime):
            current = datetime(2026, 11, 1, 1, 30, 0)

            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return cls.current

        with mock.patch("wezterm_theme.core.datetime", FakeDateTime):
            backup_current_config()
            FakeDateTime.current = datetime(2026, 11, 1, 1, 0, 0)  # DST fall-back
            after = backup_current_config()
            self.assertEqual(get_available_backups()[0]["filename"], after.name)
            for _ in range(25):
                newest = backup_current_config()
                self.assertTrue(newest.is_file(), "the newest backup was pruned as the oldest")

    def test_python_m_propagates_the_exit_code(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(SRC_DIR), "WEZTERM_CONFIG_FILE": str(self.cfg_path)}
        res = subprocess.run(
            [sys.executable, "-m", "wezterm_theme", "-s", "no-such-theme-xyz"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_superscript_digits_are_rejected_instead_of_crashing(self) -> None:
        self.assertIsNone(find_theme("²"))
        with self._no_validation():
            self.assertTrue(apply_theme(get_theme_by_id(3))[0])
            backup_current_config(label="manual")
            ok, _ = restore_backup("³")
        self.assertFalse(ok)

    def test_import_preset_resets_missing_settings_and_skips_invalid_ones(self) -> None:
        with self._no_validation():
            self.assertTrue(
                apply_theme(
                    get_theme_by_id(3),
                    appearance_updates={"window_blur": 40, "color_scheme_light": "Catppuccin Latte", "font_size": 13},
                )[0]
            )
            ok, msg = import_preset({
                "theme_id": 7,
                "tab_title_len": 20,
                "appearance": {"font_size": 15, "max_fps": 250, "cursor_style": "Bogus"},
            })
        self.assertTrue(ok, msg)
        self.assertIn("cursor_style", msg)
        state = get_current_state()
        self.assertEqual(state["theme_id"], 7)
        self.assertEqual(state["appearance"]["font_size"], 15)
        self.assertEqual(state["appearance"]["max_fps"], 250)
        self.assertIsNone(state["appearance"]["window_blur"])
        self.assertIsNone(state["appearance"]["color_scheme_light"])

    def test_reset_values_become_none_in_both_modes(self) -> None:
        resets = {
            "cursor_color": "Auto",
            "color_scheme": "off",
            "cursor_style": "",
            "audible_bell": "Default",
            "window_decorations": "",
            "win32_backdrop": "Auto",
        }
        self.assertEqual(normalize_appearance_updates(resets), dict.fromkeys(resets))

        (self.tmp_path / "defaults.json").write_text("{}\n", encoding="utf-8")
        settings = self.tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"colors": {"cursor_color": "#ffffff", "scheme": "Dracula"}, "cursor": {"style": "SteadyBar"}}),
            encoding="utf-8",
        )
        with self._no_validation():
            ok, err = apply_appearance({"cursor_color": "Auto", "color_scheme": "none", "cursor_style": "Default"})
        self.assertTrue(ok, err)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("cursor_color", data["colors"])
        self.assertNotIn("scheme", data["colors"])
        self.assertNotIn("style", data["cursor"])

    def test_module_update_does_not_touch_wezterm_lua_again(self) -> None:
        with self._no_validation():
            self.assertTrue(apply_theme(get_theme_by_id(3))[0])
            os.utime(self.cfg_path, (1_000_000_000, 1_000_000_000))
            self.assertTrue(apply_theme(get_theme_by_id(4))[0])
        self.assertEqual(self.cfg_path.stat().st_mtime, 1_000_000_000)

    def test_software_front_end_in_wezterm_lua_is_detected(self) -> None:
        self.cfg_path.write_text(
            "local config = {}\nconfig.front_end = 'Software'\nconfig.animation_fps = 60\nreturn config\n",
            encoding="utf-8",
        )
        warnings = detect_performance_warnings(appearance={})
        self.assertTrue(any("front_end='Software'" in w for w in warnings), warnings)

        self.cfg_path.write_text(
            "local config = {}\n"
            "-- config.front_end = 'Software'\n"
            "if config.front_end == 'Software' then end\n"
            "return config\n",
            encoding="utf-8",
        )
        self.assertEqual(detect_performance_warnings(appearance={}), [])

    @unittest.skipUnless(Path("/dev/null").exists(), "needs /dev/null")
    def test_export_preset_to_a_non_regular_file(self) -> None:
        raw, dest = export_preset(Path("/dev/null"))
        self.assertEqual(dest, Path("/dev/null"))
        self.assertIn('"schema"', raw)

    def test_session_module_keeps_a_portable_fallback_path(self) -> None:
        lua = generate_lua_for_theme(
            get_theme_by_id(3),
            appearance={"command_palette": True, "session_manager": True},
            session_path="/home/alice/.config/wezterm/wezterm_session.json",
        )
        fn = lua[lua.index("local function _wt_session_paths()") :]
        fn = fn[: fn.index("\n  end\n")]
        self.assertIn("/home/alice/.config/wezterm/wezterm_session.json", fn)
        self.assertIn("wezterm.config_dir", fn)

    @unittest.skipIf(os.name == "nt", "symlinks need extra privileges on Windows")
    def test_find_studio_cli_keeps_shim_and_venv_paths(self) -> None:
        real = self.tmp_path / "Cellar" / "1.0" / "bin" / "wezterm-theme"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        shim = self.tmp_path / "bin" / "wezterm-theme"
        shim.parent.mkdir()
        shim.symlink_to(real)
        with mock.patch("wezterm_theme.paths.shutil.which", return_value=str(shim)):
            self.assertEqual(find_studio_cli(), str(shim))

        venv_bin = self.tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        (venv_bin / "wezterm-theme").write_text("#!/bin/sh\n", encoding="utf-8")
        with (
            mock.patch("wezterm_theme.paths.shutil.which", return_value=None),
            mock.patch.object(sys, "argv", ["not-the-cli"]),
            mock.patch.object(sys, "executable", str(venv_bin / "python")),
        ):
            self.assertEqual(find_studio_cli(), str(venv_bin / "wezterm-theme"))

    def test_server_prefers_ipv4_and_rejects_non_integer_ids(self) -> None:
        infos = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]
        with mock.patch("wezterm_theme.server.socket.getaddrinfo", return_value=infos):
            self.assertEqual(_address_family_for_host("localhost"), socket.AF_INET)
        self.assertEqual(_address_family_for_host("::1"), socket.AF_INET6)

        for bad in (float("inf"), 10.9, "1e3", True, None, [1]):
            with self.assertRaises(RequestError, msg=repr(bad)) as ctx:
                _parse_strict_int("theme_id", bad)
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(_parse_strict_int("theme_id", 10.0), 10)
        self.assertEqual(_parse_strict_int("theme_id", " 12 "), 12)

    def test_lua_runtime_git_fallbacks_editor_args_ligatures_and_session_fallback(self) -> None:
        wz = find_wezterm_cmd()
        if not wz:
            self.skipTest("wezterm binary not installed")

        deep_repo = self.tmp_path / "deeprepo"
        (deep_repo / ".git").mkdir(parents=True)
        (deep_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        deep_dir = deep_repo.joinpath(*["d"] * 21)
        deep_dir.mkdir(parents=True)
        reftable_repo = self.tmp_path / "reftable"
        (reftable_repo / ".git").mkdir(parents=True)
        (reftable_repo / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n", encoding="utf-8")

        mod = self.tmp_path / "mod_review.lua"
        mod.write_text(
            generate_lua_for_theme(
                get_theme_by_id(3),
                appearance={
                    "status_bar": True,
                    "open_in_editor": True,
                    "editor_cmd": "code --wait",
                    "command_palette": True,
                    "session_manager": True,
                },
                session_path="/nonexistent-wezterm-theme-dir/wezterm_session.json",
            ),
            encoding="utf-8",
        )
        harness = self.tmp_path / "harness_review.lua"
        harness.write_text(
            "\n".join([
                "local real_wz = require 'wezterm'",
                "local captured_cbs = {}",
                "local update_status_cb, open_uri_cb",
                "local child_calls = 0",
                "local mock_wz = setmetatable({",
                "  GLOBAL = {},",
                "  on = function(ev, cb)",
                "    if ev == 'update-status' then update_status_cb = cb end",
                "    if ev == 'open-uri' then open_uri_cb = cb end",
                "  end,",
                "  action = setmetatable({",
                "    SpawnCommandInNewTab = function(t) return { spawn = t } end,",
                "  }, { __index = real_wz.action }),",
                "  action_callback = function(fn)",
                "    local u = real_wz.action_callback(fn)",
                "    captured_cbs[tostring(u)] = fn",
                "    return u",
                "  end,",
                "  run_child_process = function(argv)",
                "    child_calls = child_calls + 1",
                "    return true, 'cli-branch\\n', ''",
                "  end,",
                "  battery_info = function() return {} end,",
                "  config_dir = " + _lua_str(self.tmp_path.as_posix()) + ",",
                "}, { __index = real_wz })",
                "local cfg = { harfbuzz_features = { 'zero', 'ss01' } }",
                f"local m = dofile({_lua_str(mod.as_posix())})",
                "m.apply(cfg, mock_wz)",
                "local status = ''",
                "local performed = nil",
                "local cur_ov = {}",
                "local win = {",
                "  get_config_overrides = function() return cur_ov end,",
                "  set_config_overrides = function(_, o) cur_ov = o end,",
                "  perform_action = function(_, a) performed = a end,",
                "  toast_notification = function() end,",
                "  active_workspace = function() return 'default' end,",
                "  active_key_table = function() return nil end,",
                "  leader_is_active = function() return false end,",
                "  set_right_status = function(_, v) status = v end,",
                "}",
                "local function pane_for(p)",
                "  return {",
                "    get_current_working_dir = function() return { file_path = p } end,",
                "    get_domain_name = function() return 'local' end,",
                "    get_user_vars = function() return {} end,",
                "    get_foreground_process_name = function() return '/bin/bash' end,",
                "    get_title = function() return '' end,",
                "    pane_id = function() return 1 end,",
                "  }",
                "end",
                "",
                "-- Git: a walk that runs out of levels, and a reftable stub HEAD, ask git itself",
                f"update_status_cb(win, pane_for({_lua_str(deep_dir.as_posix())}))",
                "assert(child_calls == 1, 'deep cwd should fall back to git, calls=' .. child_calls)",
                f"update_status_cb(win, pane_for({_lua_str(reftable_repo.as_posix())}))",
                "assert(child_calls == 2, 'reftable HEAD should fall back to git, calls=' .. child_calls)",
                "assert(status:find('cli-branch', 1, true) and not status:find('.invalid', 1, true), 'bad branch: ' .. status)",
                "",
                "-- Editor commands with arguments are split into argv",
                "open_uri_cb(win, pane_for('/tmp'), 'editfile:///srv/app/main.py:12')",
                "local args = performed and performed.spawn and performed.spawn.args",
                "assert(args and table.concat(args, ' ') == 'code --wait -g /srv/app/main.py:12', 'bad editor argv: ' .. (args and table.concat(args, ' ') or 'nil'))",
                "",
                "-- Ligature toggle keeps unrelated harfbuzz features",
                "local toggle_fn, save_fn",
                "for _, e in ipairs(m.command_palette_entries(cfg, mock_wz)) do",
                "  if e.brief:find('Toggle Programming Ligatures') then toggle_fn = captured_cbs[tostring(e.action)] end",
                "  if e.brief:find('Save Workspace Session') then save_fn = captured_cbs[tostring(e.action)] end",
                "end",
                "toggle_fn(win, {})",
                "assert(table.concat(cur_ov.harfbuzz_features, ',') == 'zero,ss01,calt=0,clig=0,liga=0', '1st toggle: ' .. table.concat(cur_ov.harfbuzz_features, ','))",
                "toggle_fn(win, {})",
                "assert(table.concat(cur_ov.harfbuzz_features, ',') == 'zero,ss01', '2nd toggle: ' .. table.concat(cur_ov.harfbuzz_features, ','))",
                "",
                "-- Session snapshot falls back to config_dir when the baked-in path is unusable",
                "mock_wz.mux = { all_windows = function() return {",
                "  { get_workspace = function() return 'default' end, tabs = function() return {",
                "    { get_title = function() return 'a' end, active_pane = function() return { get_current_working_dir = function() return { file_path = '/tmp' } end } end },",
                "  } end },",
                "} end }",
                "save_fn(win, {})",
                f"local f = io.open({_lua_str((self.tmp_path / 'wezterm_session.json').as_posix())}, 'r')",
                "assert(f, 'session snapshot was not written to the config_dir fallback')",
                "f:close()",
                "return real_wz.config_builder()",
            ]),
            encoding="utf-8",
        )
        res = subprocess.run(
            [*wz, "--config-file", str(harness), "ls-fonts", "--text", "a"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        errors = [ln for ln in res.stderr.splitlines() if " ERROR " in ln]
        self.assertEqual(errors, [], f"Lua harness errors:\n{res.stderr}")


class Performance143Tests(unittest.TestCase):
    """v1.4.3: lighter generated Lua, performance profiles and rendering diagnostics."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._old_env = os.environ.get("WEZTERM_CONFIG_FILE")
        self.cfg_path = self.tmp_path / "wezterm.lua"
        os.environ["WEZTERM_CONFIG_FILE"] = str(self.cfg_path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("WEZTERM_CONFIG_FILE", None)
        else:
            os.environ["WEZTERM_CONFIG_FILE"] = self._old_env
        self._tmp.cleanup()

    def _no_validation(self) -> mock._patch:
        return mock.patch("wezterm_theme.core.validate_config", return_value=None)

    def test_each_profile_applies_and_is_detected_as_itself(self) -> None:
        for name, prof in PERF_PROFILES.items():
            self.assertLessEqual(set(PERF_TUNING_KEYS), set(prof["settings"]), f"{name} must set every tuning key")
        with self._no_validation():
            self.assertTrue(apply_theme(get_theme_by_id(3), appearance_updates={"window_blur": 30})[0])
            # Remote then Battery: Battery must leave the Software renderer, not keep it.
            for name in ("remote", "battery", "smooth", "default"):
                ok, msg = apply_perf_profile(name)
                self.assertTrue(ok, msg)
                self.assertEqual(detect_perf_profile(get_current_state()["appearance"]), name)
                self.assertIn("already active", apply_perf_profile(name)[1])
            self.assertFalse(apply_perf_profile("turbo")[0])
        app = get_current_state()["appearance"]
        self.assertIsNone(app["window_blur"], "battery/remote should have turned blur off")
        for key in ("front_end", "max_fps", "animation_fps", "cursor_blink_ease", "status_update_interval"):
            self.assertIsNone(app[key], key)

    def test_new_settings_are_validated_and_emitted(self) -> None:
        self.assertEqual(
            normalize_appearance_updates({"webgpu_power_preference": "Default", "cursor_blink_ease": "", "status_update_interval": 0}),
            {"webgpu_power_preference": None, "cursor_blink_ease": None, "status_update_interval": 0},
        )
        for bad in ({"cursor_blink_ease": "Bounce"}, {"webgpu_power_preference": "Turbo"}, {"status_update_interval": -1}):
            with self.assertRaises(ValueError):
                normalize_appearance_updates(bad)
        app = {"webgpu_power_preference": "HighPerformance", "cursor_blink_ease": "Constant", "status_update_interval": 8000, "status_bar": True}
        lua = generate_lua_for_theme(get_theme_by_id(3), appearance=app)
        self.assertIn("config.webgpu_power_preference = 'HighPerformance'", lua)
        self.assertIn("config.cursor_blink_ease_in = 'Constant'", lua)
        self.assertIn("config.cursor_blink_ease_out = 'Constant'", lua)
        self.assertLess(lua.index("config.status_update_interval = 8000"), lua.index("config.status_update_interval or 5000"))
        if find_wezterm_cmd():
            self.assertIsNone(validate_theme_lua(lua))

    def test_loader_mode_stores_and_applies_new_settings(self) -> None:
        (self.tmp_path / "defaults.json").write_text("{}\n", encoding="utf-8")
        settings = self.tmp_path / "settings.json"
        settings.write_text('{"font": {"size": 12}}\n', encoding="utf-8")
        with self._no_validation():
            ok, err = apply_appearance({"cursor_blink_ease": "Constant", "webgpu_power_preference": "LowPower", "status_update_interval": 8000})
        self.assertTrue(ok, err)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["cursor"]["blink_ease"], "Constant")
        self.assertEqual(data["performance"]["webgpu_power_preference"], "LowPower")
        self.assertEqual(data["status"]["update_interval"], 8000)
        self.assertEqual(data["font"]["size"], 12)
        app = get_current_state()["appearance"]
        self.assertEqual((app["cursor_blink_ease"], app["webgpu_power_preference"], app["status_update_interval"]), ("Constant", "LowPower", 8000))
        module = (self.tmp_path / "tab_theme.lua").read_text(encoding="utf-8")
        self.assertIn("config.cursor_blink_ease_in = 'Constant'", module)
        self.assertIn("config.status_update_interval = 8000", module)

        with self._no_validation():
            self.assertTrue(apply_appearance({"cursor_blink_ease": "Default", "webgpu_power_preference": "Default", "status_update_interval": 0})[0])
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("blink_ease", data["cursor"])
        self.assertNotIn("webgpu_power_preference", data["performance"])
        self.assertNotIn("update_interval", data["status"])
        self.assertNotIn("cursor_blink_ease_in", (self.tmp_path / "tab_theme.lua").read_text(encoding="utf-8"))

    def test_renderer_info_and_new_warnings(self) -> None:
        self.assertEqual(get_renderer_info(appearance={}), {"front_end": "OpenGL", "source": "default"})
        self.cfg_path.write_text("local config = {}\nconfig.front_end = 'WebGpu'\nreturn config\n", encoding="utf-8")
        self.assertEqual(get_renderer_info(appearance={}), {"front_end": "WebGpu", "source": "wezterm.lua"})
        self.assertEqual(get_renderer_info(appearance={"front_end": "Software"})["source"], "studio")

        env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
        with mock.patch.dict(os.environ, env, clear=True):
            def warns(app: dict) -> str:
                return " | ".join(detect_performance_warnings(appearance=app))

            self.assertIn("scrollback_lines=200,000", warns({"scrollback_lines": 200_000}))
            self.assertIn("status_update_interval=500", warns({"status_update_interval": 500}))
            self.assertEqual(warns({"webgpu_power_preference": "HighPerformance"}), "")  # wezterm.lua sets WebGpu
            self.assertIn("only takes effect", warns({"webgpu_power_preference": "HighPerformance", "front_end": "OpenGL"}))
            self.assertIn("keep the GPU compositing", warns({"animation_fps": 1, "window_blur": 20}))
            self.assertEqual(warns({"max_fps": 120}), "")
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
            self.assertIn("ignored when WezTerm runs natively on Wayland", warns({"max_fps": 120}))
            self.assertEqual(warns({"max_fps": 120, "enable_wayland": False}), "")

    def test_cli_profile_flag_doctor_and_status(self) -> None:
        with self._no_validation(), redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["-s", "3"]), 0)
            self.assertEqual(cli_main(["--perf-profile", "battery", "--max-fps", "45"]), 0)
        app = get_current_state()["appearance"]
        self.assertEqual(app["max_fps"], 45, "an explicit flag must win over the profile")
        self.assertEqual(app["animation_fps"], 1)

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli_main(["doctor", "--perf"]), 0)
        out = buf.getvalue()
        self.assertIn("Renderer:", out)
        self.assertIn("Perf Profile:", out)
        self.assertIn("Benchmark:", out)
        if find_wezterm_cmd():
            self.assertIn("process/cwd lookups", out)

    def test_http_performance_routes(self) -> None:
        with self._no_validation():
            self.assertTrue(apply_theme(get_theme_by_id(3))[0])
        httpd, port = create_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            html = urllib.request.urlopen(base + "/").read().decode()
            token = html.split('name="studio-token" content="', 1)[1].split('"', 1)[0]

            def post(route: str, body: dict, with_token: bool = True) -> tuple[int, dict]:
                headers = {"Content-Type": "application/json", **({"X-Studio-Token": token} if with_token else {})}
                req = urllib.request.Request(base + route, data=json.dumps(body).encode(), headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req) as res:
                        return res.status, json.loads(res.read())
                except urllib.error.HTTPError as exc:
                    with exc:
                        return exc.code, json.loads(exc.read())

            boot = json.loads(urllib.request.urlopen(base + "/api/bootstrap").read())
            self.assertEqual([p["id"] for p in boot["perf"]["profiles"]], list(PERF_PROFILES))
            with self._no_validation():
                status, body = post("/api/perf-profile", {"profile": "remote"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["perf"]["active_profile"], "remote")
            self.assertEqual(body["perf"]["renderer"], {"front_end": "Software", "source": "studio"})
            self.assertEqual(post("/api/perf-profile", {"profile": "turbo"})[0], 400)
            self.assertEqual(post("/api/perf-profile", {"profile": "default"}, with_token=False)[0], 403)
            self.assertEqual(post("/api/perf-benchmark", {}, with_token=False)[0], 403)
            status, body = post("/api/perf-benchmark", {})
            self.assertEqual(status, 200)
            if find_wezterm_cmd():
                self.assertTrue(body["ok"], body)
                self.assertIn("process/cwd lookups", body["lines"][0])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_benchmark_regression_limits_for_every_style(self) -> None:
        if not find_wezterm_cmd():
            self.skipTest("wezterm binary not installed")
        modules = []
        for theme in THEMES:
            style = theme.get("style", "pills")
            mod = self.tmp_path / f"bench_{style}.lua"
            if mod.exists():
                continue
            mod.write_text(
                generate_lua_for_theme(theme, appearance={"status_bar": True, "agent_indicators": True, "ssh_alert": True}),
                encoding="utf-8",
            )
            modules.append(mod)
        results = benchmark_modules(modules, updates=20)
        self.assertEqual(len(results), len(modules))
        for res in results:
            name = Path(res["module"]).stem
            self.assertNotIn("error", res, name)
            # One process and one cwd lookup per tab per update, never per call.
            self.assertLessEqual(res["lazy_reads"], 2 * res["tabs"], name)
            self.assertLess(res["tab_bar_ms"], 10.0, name)
            self.assertLessEqual(res["status_queries"], res["tabs"], name)

    def test_pane_cache_rereads_when_the_title_changes(self) -> None:
        wz = find_wezterm_cmd()
        if not wz:
            self.skipTest("wezterm binary not installed")
        mod = self.tmp_path / "mod_cache.lua"
        mod.write_text(generate_lua_for_theme(get_theme_by_id(3)), encoding="utf-8")
        harness = self.tmp_path / "harness_cache.lua"
        harness.write_text(
            "\n".join([
                "local real_wz = require 'wezterm'",
                "local fmt_cb",
                "local reads = 0",
                "local mock = setmetatable({",
                "  GLOBAL = {},",
                "  on = function(ev, cb) if ev == 'format-tab-title' then fmt_cb = cb end end,",
                "  time = { now = function() return { format = function() return '1000.000' end } end },",
                "}, { __index = real_wz })",
                f"dofile({_lua_str(mod.as_posix())}).apply({{}}, mock)",
                "local fields = { pane_id = 7, title = 'zsh', current_working_dir = 'file:///home/dev/alpha', foreground_process_name = '/bin/zsh' }",
                "local pane = setmetatable({}, { __index = function(_, k)",
                "  if k == 'current_working_dir' or k == 'foreground_process_name' then reads = reads + 1 end",
                "  return fields[k]",
                "end })",
                "local tab = { tab_index = 0, is_active = true, tab_title = '', panes = { {} }, active_pane = pane }",
                "local out1 = fmt_cb(tab, { tab }, tab.panes, {}, false, 40)",
                "local out2 = fmt_cb(tab, { tab }, tab.panes, {}, false, 40)",
                "assert(reads == 2, 'second call in the same update must reuse the lookups, reads=' .. reads)",
                "assert(tostring(out1):find('alpha', 1, true) and tostring(out2):find('alpha', 1, true), 'missing dir')",
                "-- Same instant, but the shell changed the title after a cd: the cache must not be used",
                "fields.title = 'zsh: beta'",
                "fields.current_working_dir = 'file:///home/dev/beta'",
                "local out3 = fmt_cb(tab, { tab }, tab.panes, {}, false, 40)",
                "assert(reads == 4 and tostring(out3):find('beta', 1, true), 'stale cwd after a title change: ' .. tostring(out3))",
                "return real_wz.config_builder()",
            ]),
            encoding="utf-8",
        )
        res = subprocess.run([*wz, "--config-file", str(harness), "ls-fonts", "--text", "a"], capture_output=True, text=True, timeout=30)
        errors = [ln for ln in res.stderr.splitlines() if " ERROR " in ln]
        self.assertEqual(errors, [], res.stderr)


if __name__ == "__main__":
    unittest.main()
