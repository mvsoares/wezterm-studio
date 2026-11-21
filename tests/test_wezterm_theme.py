"""Comprehensive unit & integration tests for `wezterm_theme`.

All tests isolate WezTerm configuration via `WEZTERM_CONFIG_FILE` in temporary
directories so the host user's real `~/.config/wezterm` is never touched.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
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
    _atomic_write_text,
    _backup_wezterm_lua,
    _find_tab_title_handlers,
    apply_appearance,
    apply_theme,
    backup_current_config,
    disable_conflicting_tab_handlers,
    export_preset,
    get_available_backups,
    get_current_state,
    import_preset,
    init_wezterm_config,
    restore_backup,
    restore_disabled_tab_handlers,
    uninstall_wezterm_config,
    validate_theme_lua,
)
from wezterm_theme.fonts import get_color_scheme_palettes
from wezterm_theme.paths import find_wezterm_cmd, get_cache_dir, get_platform_name, resolve_wezterm_paths
from wezterm_theme.server import create_server
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
                "last_status_cb(mock_win, mock_pane)",
                "assert(type(captured_status) == 'string' and captured_status:find('LEADER') and captured_status:find('PROD:root@prod%-api%-01') and captured_status:find('ssh%-box 24ms'), 'bad status bar output: ' .. tostring(captured_status))",
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

        _atomic_write_text(link, "new\n")
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
        for _ in range(MAX_PRE_RESTORE_BACKUPS + 3):
            backup_current_config(label="pre_restore")
        for _ in range(3):
            backup_current_config(label="manual")
        names = [p.name for p in (self.tmp_path / "backups").glob("tab_theme.lua.*.bak")]
        self.assertEqual(sum("_pre_restore" in n for n in names), MAX_PRE_RESTORE_BACKUPS)
        self.assertEqual(sum("_manual" in n for n in names), 3)

        paths = resolve_wezterm_paths()
        for _ in range(MAX_WEZTERM_LUA_BACKUPS + 2):
            _backup_wezterm_lua(paths, "conflict_fix")
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


if __name__ == "__main__":
    unittest.main()
