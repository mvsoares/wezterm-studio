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
from wezterm_theme.paths import get_cache_dir, get_platform_name, resolve_wezterm_paths
from wezterm_theme.server import create_server
from wezterm_theme.themes import (
    MIN_TAB_CONTRAST,
    SYMBOL_GROUPS,
    THEMES,
    compose_symbols,
    contrast_ratio,
    find_theme,
    generate_lua_for_theme,
    get_theme_by_id,
)
from wezterm_theme.tui import symbol_cell, visible_len


class ThemeCatalogAndSymbolsTests(unittest.TestCase):
    def test_60_themes_and_wcag_contrast(self) -> None:
        self.assertEqual(len(THEMES), 60)
        ids = [t["id"] for t in THEMES]
        self.assertEqual(ids, list(range(1, 61)))
        for t in THEMES:
            for slot in t["palette"]:
                self.assertGreaterEqual(
                    contrast_ratio(slot["bg"], slot["fg"]),
                    MIN_TAB_CONTRAST,
                    f"Low contrast in theme #{t['id']} {t['name']}",
                )

    def test_find_theme_by_id_slug_and_substring(self) -> None:
        self.assertEqual(find_theme("8")["id"], 8)
        self.assertEqual(find_theme("#08")["id"], 8)
        self.assertEqual(find_theme("catppuccin-mocha")["id"], 22)
        self.assertEqual(find_theme("edgerunners")["id"], 31)

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
                appearance={"font_family": "JetBrains Mono", "font_size": 13, "window_opacity": 0.95},
            )
            err = validate_theme_lua(lua)
            self.assertIsNone(err, f"Style {st} (theme #{t['id']}) failed validation: {err}")


class CrossPlatformPathsTests(unittest.TestCase):
    def test_platform_and_cache_dirs(self) -> None:
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(get_platform_name(), "macos")
            self.assertIn("Library/Caches/wezterm-theme", get_cache_dir().as_posix())

        with mock.patch("sys.platform", "win32"):
            self.assertEqual(get_platform_name(), "windows")
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
            "tab_font_family": "JetBrains Mono",
            "tab_font_size": 11.5,
            "color_scheme": "Dracula",
            "window_opacity": 0.92,
            "window_padding": 12,
        })
        self.assertTrue(ok, err)

        st = get_current_state()
        self.assertEqual(st["theme_id"], 31)
        self.assertEqual(st["tab_title_len"], 18)
        self.assertEqual(st["appearance"]["color_scheme"], "Dracula")

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

        ok, _, err = apply_theme(get_theme_by_id(12), tab_title_len=14)
        self.assertTrue(ok, err)
        ok, err = apply_appearance({"font_size": 15, "color_scheme": "Monokai Vivid"})
        self.assertTrue(ok, err)

        saved_settings = json.loads((self.tmp_path / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_settings["keys"]["leader"]["key"], "a")
        self.assertEqual(saved_settings["tabs"]["theme_id"], 12)
        self.assertEqual(saved_settings["font"]["size"], 15)
        self.assertEqual(saved_settings["colors"]["scheme"], "Monokai Vivid")


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

            boot = json.loads(urllib.request.urlopen(f"{base}/api/bootstrap").read().decode("utf-8"))
            self.assertTrue(boot["ok"])
            self.assertEqual(len(boot["themes"]), 60)

            req = urllib.request.Request(
                f"{base}/api/apply-tab-theme",
                data=json.dumps({"theme_id": 25, "tab_title_len": 16}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["state"]["theme_id"], 25)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_cli_commands(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["init"])
        self.assertEqual(rc, 0)

        with redirect_stdout(buf):
            rc = cli_main(["-s", "42", "-t", "14", "--font", "JetBrains Mono", "--size", "13"])
        self.assertEqual(rc, 0)
        st = get_current_state()
        self.assertEqual(st["theme_id"], 42)
        self.assertEqual(st["tab_title_len"], 14)
        self.assertEqual(st["appearance"]["font_family"], "JetBrains Mono")

        with redirect_stdout(buf):
            rc = cli_main(["doctor"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
