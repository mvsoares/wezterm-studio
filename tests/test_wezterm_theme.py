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
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wezterm_theme.cli import main as cli_main
from wezterm_theme.core import (
    apply_appearance,
    apply_theme,
    backup_current_config,
    export_preset,
    get_available_backups,
    get_current_state,
    import_preset,
    init_wezterm_config,
    restore_backup,
    uninstall_wezterm_config,
    validate_theme_lua,
)
from wezterm_theme.paths import find_wezterm_cmd, get_cache_dir, get_platform_name, resolve_wezterm_paths
from wezterm_theme.server import create_server
from wezterm_theme.themes import (
    MIN_TAB_CONTRAST,
    SYMBOL_GROUPS,
    THEMES,
    _hover_pair,
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
                "color_scheme": "Catppuccin Mocha",
                "color_scheme_light": "Catppuccin Latte",
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
        self.assertIn("Catppuccin Latte", lua)
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
                "wezterm.on = function(ev, fn)",
                "  if ev == 'format-tab-title' then last_cb = fn",
                "  elseif ev == 'update-status' then last_status_cb = fn",
                "  elseif ev == 'open-uri' then last_open_uri_cb = fn end",
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
                "}",
                "local mock_cfg = {",
                "  resolved_palette = {",
                "    background = '#1e1e2e',",
                "    ansi = { '#45475a', '#f38ba8', '#a6e3a1', '#f9e2af', '#89b4fa', '#f5c2e7', '#94e2d5', '#bac2de' },",
                "  },",
                "}",
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
                    f"dofile([==[{mod_file.as_posix()}]==]).apply({{}}, wezterm)",
                    f"assert(type(last_cb) == 'function', 'missing callback for {st}')",
                    f"assert(type(last_status_cb) == 'function', 'missing update-status callback for {st}')",
                    f"assert(type(last_open_uri_cb) == 'function', 'missing open-uri callback for {st}')",
                    "local out1 = last_cb(mock_tabs[1], mock_tabs, mock_tabs[1].panes, mock_cfg, false, 48)",
                    "local out2 = last_cb(mock_tabs[2], mock_tabs, mock_tabs[2].panes, mock_cfg, true, 48)",
                    f"assert(type(out1) == 'string' and out1:find('42%%') and out1:lower():find('deploy') and out1:find('²') and out1:find('🔍'), 'bad active output for {st}: ' .. tostring(out1))",
                    f"assert(type(out2) == 'string' and out2:find('~ ') and out2:lower():find('nvim|') and out2:find('³'), 'bad hover output for {st}: ' .. tostring(out2))",
                ])
            harness_lines.extend([
                "local captured_status = nil",
                "local spawned_action = nil",
                "local mock_win = {",
                "  leader_is_active = function() return true end,",
                "  active_key_table = function() return nil end,",
                "  active_workspace = function() return 'prod' end,",
                "  set_right_status = function(_, s) captured_status = s end,",
                "  perform_action = function(_, act_obj) spawned_action = act_obj end,",
                "}",
                "local mock_pane = {",
                "  get_dimensions = function() return { cols = 140 } end,",
                "  get_domain_name = function() return 'ssh-box' end,",
                "  get_metadata = function() return { since_last_response_ms = 24 } end,",
                f"  get_current_working_dir = function() return 'file://{SRC_DIR.parent.as_posix()}' end,",
                "}",
                "last_status_cb(mock_win, mock_pane)",
                "assert(type(captured_status) == 'string' and captured_status:find('LEADER') and captured_status:find('prod') and captured_status:find('ssh%-box 24ms'), 'bad status bar output: ' .. tostring(captured_status))",
                "local intercepted = last_open_uri_cb(mock_win, mock_pane, 'editfile://src/wezterm_theme/core.py:42')",
                "assert(intercepted == false and spawned_action ~= nil, 'open-uri failed to intercept editfile://')",
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
        })
        self.assertTrue(ok, err)

        saved_settings = json.loads((self.tmp_path / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_settings["keys"]["leader"]["key"], "a")
        self.assertEqual(saved_settings["tabs"]["theme_id"], 62)
        self.assertEqual(saved_settings["tabs"]["max_width"], 44)
        self.assertFalse(saved_settings["tabs"]["fancy"])
        self.assertEqual(saved_settings["font"]["size"], 15)
        self.assertFalse(saved_settings["font"]["ligatures"])
        self.assertEqual(saved_settings["colors"]["scheme"], "Monokai Vivid")
        self.assertEqual(saved_settings["colors"]["scheme_light"], "Builtin Light")
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
        try:
            html = urllib.request.urlopen(f"{base}/").read().decode("utf-8")
            self.assertIn("WezTerm Studio", html)
            import re
            html_ids = set(re.findall(r'id="([^"]+)"', html))
            js_ids = set(re.findall(r'getElementById\("([^"]+)"\)', html))
            self.assertEqual(js_ids - html_ids, set())

            boot = json.loads(urllib.request.urlopen(f"{base}/api/bootstrap").read().decode("utf-8"))
            self.assertTrue(boot["ok"])
            self.assertEqual(len(boot["themes"]), 62)

            req = urllib.request.Request(
                f"{base}/api/apply-tab-theme",
                data=json.dumps({"theme_id": 61, "tab_title_len": 16}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
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
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp_app = json.loads(urllib.request.urlopen(req_app).read().decode("utf-8"))
            self.assertTrue(resp_app["ok"], resp_app)
            self.assertFalse(resp_app["state"]["appearance"]["font_ligatures"])
            self.assertTrue(resp_app["state"]["appearance"]["background_gradient"])
            self.assertTrue(resp_app["state"]["appearance"]["accent_border"])
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
            ])
        self.assertEqual(rc, 0)
        st = get_current_state()
        self.assertEqual(st["theme_id"], 62)
        self.assertEqual(st["tab_title_len"], 14)
        self.assertEqual(st["appearance"]["font_family"], "JetBrains Mono")
        self.assertFalse(st["appearance"]["font_ligatures"])
        self.assertEqual(st["appearance"]["color_scheme_light"], "Catppuccin Latte")
        self.assertAlmostEqual(st["appearance"]["inactive_pane_brightness"], 0.78)
        self.assertTrue(st["appearance"]["background_gradient"])
        self.assertTrue(st["appearance"]["accent_border"])
        self.assertTrue(st["appearance"]["status_bar"])
        self.assertEqual(st["appearance"]["tab_max_width"], 42)
        self.assertFalse(st["appearance"]["use_fancy_tab_bar"])
        self.assertEqual(st["appearance"]["editor_cmd"], "nvim")
        self.assertTrue(st["appearance"]["open_in_editor"])

        with redirect_stdout(buf):
            rc = cli_main(["--current"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = cli_main(["doctor"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
