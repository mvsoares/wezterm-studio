"""Measure what the generated tab-bar and status-bar Lua costs inside a real WezTerm.

WezTerm calls `format-tab-title` twice per tab on every tab-bar update, on the GUI
thread, and reading a pane's `foreground_process_name` or `current_working_dir` makes
it query the operating system. The benchmark loads generated modules into
`wezterm ls-fonts` (no window needed) with simulated tabs and reports, per module:

- `tab_bar_ms`: Lua time for one tab-bar update (both calls for every tab);
- `lazy_reads`: process/cwd fields read during one update (each one is an OS query);
- `status_ms` / `status_queries`: the same for one update-status tick, when present.

Each simulated update gets its own clock value, so caches never carry over between
updates and the numbers describe a cold update, like a real tab switch.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .paths import find_wezterm_cmd, resolve_wezterm_paths
from .themes import _lua_long_str

BENCH_TABS = 8
BENCH_UPDATES = 100

_HARNESS = r"""
local real_wz = require 'wezterm'
local MODULES = { __MODULES__ }
local OUT = __OUT__
local TABS, UPDATES = __TABS__, __UPDATES__

local fake_now = 1000.0
local lazy_reads, proc_queries = 0, 0
local progs = { 'cargo build --release', 'nvim src/main.rs', 'claude --continue', 'ssh root@prod-db-01',
  'htop', 'npm run dev', 'git log', 'python3 app.py' }
local fgs = { '/bin/zsh', '/usr/bin/nvim', '/usr/local/bin/claude', '/usr/bin/ssh', '/usr/bin/htop',
  '/usr/bin/node', '/usr/bin/git', '/usr/bin/python3' }
local function pick(list, i) return list[(i - 1) % #list + 1] end

local function pane_info(i)
  local fields = {
    pane_id = i, title = pick(progs, i), domain_name = 'local',
    user_vars = { WEZTERM_PROG = pick(progs, i) }, is_zoomed = false, has_unseen_output = (i % 2 == 0),
    current_working_dir = 'file:///home/dev/project-' .. i, foreground_process_name = pick(fgs, i),
  }
  return setmetatable({}, { __index = function(_, k)
    if k == 'foreground_process_name' or k == 'current_working_dir' then lazy_reads = lazy_reads + 1 end
    return fields[k]
  end })
end
local tabs = {}
for i = 1, TABS do
  tabs[i] = { tab_id = i, tab_index = i - 1, window_id = 1, is_active = (i == 1), tab_title = '',
    panes = { {} }, active_pane = pane_info(i) }
end
local cfg = { resolved_palette = { background = '#1e1e2e', ansi = { '#45475a', '#f38ba8', '#a6e3a1',
  '#f9e2af', '#89b4fa', '#f5c2e7', '#94e2d5', '#bac2de' } } }

local function mux_pane(i)
  return {
    get_current_working_dir = function() return { file_path = '/nonexistent/project-' .. i } end,
    get_domain_name = function() return 'local' end,
    get_user_vars = function() return { WEZTERM_PROG = pick(progs, i) } end,
    get_foreground_process_name = function() proc_queries = proc_queries + 1; return pick(fgs, i) end,
    get_metadata = function() return {} end,
    get_dimensions = function() return { cols = 160 } end,
    has_unseen_output = function() return i % 2 == 0 end,
    pane_id = function() return i end,
    get_title = function() return pick(progs, i) end,
  }
end
local win = {
  leader_is_active = function() return false end,
  active_key_table = function() return nil end,
  active_workspace = function() return 'default' end,
  set_right_status = function() end,
  toast_notification = function() end,
  mux_window = function() return { tabs_with_info = function()
    local out = {}
    for i = 1, TABS do
      out[i] = { is_active = i == 1, tab = { tab_id = function() return i end, active_pane = function() return mux_pane(i) end } }
    end
    return out
  end } end,
}

local function update_tab_bar(fmt_cb)
  fake_now = fake_now + 1
  for i = 1, TABS do fmt_cb(tabs[i], tabs, tabs[i].panes, cfg, false, 32) end
  for i = 1, TABS do fmt_cb(tabs[i], tabs, tabs[i].panes, cfg, i == 2, 24) end
end

local results = {}
for _, path in ipairs(MODULES) do
  local fmt_cb, status_cb
  local mock = setmetatable({
    GLOBAL = {},
    on = function(ev, cb)
      if ev == 'format-tab-title' then fmt_cb = cb elseif ev == 'update-status' then status_cb = cb end
    end,
    time = { now = function() return { format = function() return string.format('%.3f', fake_now) end } end },
    mux = {
      get_window = function() return { get_workspace = function() return 'default' end } end,
      get_active_workspace = function() return 'default' end,
    },
    battery_info = function() return {} end,
    run_child_process = function() return true, 'main\n', '' end,
  }, { __index = real_wz })
  local r = { module = path }
  local ok, err = pcall(function()
    dofile(path).apply({}, mock)
    if fmt_cb then
      lazy_reads = 0
      update_tab_bar(fmt_cb)
      r.lazy_reads = lazy_reads
      local t0 = os.clock()
      for _ = 1, UPDATES do update_tab_bar(fmt_cb) end
      r.tab_bar_ms = (os.clock() - t0) / UPDATES * 1000
    end
    if status_cb then
      proc_queries = 0
      local t0 = os.clock()
      for _ = 1, UPDATES do
        fake_now = fake_now + 10
        status_cb(win, mux_pane(1))
      end
      r.status_ms = (os.clock() - t0) / UPDATES * 1000
      r.status_queries = proc_queries / UPDATES
    end
  end)
  if not ok then r.error = tostring(err) end
  table.insert(results, r)
end
local encode = (real_wz.serde and real_wz.serde.json_encode) or real_wz.json_encode
local f = assert(io.open(OUT, 'w'))
f:write(encode(results))
f:close()
return real_wz.config_builder()
"""


class BenchmarkError(RuntimeError):
    """The benchmark could not run (no wezterm binary, or the harness failed)."""


def benchmark_modules(
    modules: list[Path],
    tabs: int = BENCH_TABS,
    updates: int = BENCH_UPDATES,
) -> list[dict[str, Any]]:
    """Benchmark generated Lua modules in one `wezterm` run; one result dict per module."""
    wz = find_wezterm_cmd()
    if not wz:
        raise BenchmarkError("WezTerm binary not found (set WEZTERM_BIN or add wezterm to PATH).")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "bench.json"
        harness = Path(tmp) / "bench.lua"
        harness.write_text(
            _HARNESS.replace("__MODULES__", ", ".join(_lua_long_str(Path(m).as_posix()) for m in modules))
            .replace("__OUT__", _lua_long_str(out.as_posix()))
            .replace("__TABS__", str(int(tabs)))
            .replace("__UPDATES__", str(int(updates))),
            encoding="utf-8",
        )
        try:
            res = subprocess.run(
                [*wz, "--config-file", str(harness), "ls-fonts", "--text", "a"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise BenchmarkError("WezTerm timed out while running the benchmark.") from None
        if not out.is_file():
            errors = [ln for ln in res.stderr.splitlines() if " ERROR " in ln]
            raise BenchmarkError(errors[-1] if errors else f"wezterm exited with status {res.returncode}")
        data = json.loads(out.read_text(encoding="utf-8"))
    return [{**item, "tabs": int(tabs)} for item in (data if isinstance(data, list) else [])]


def benchmark_current(paths: dict[str, Path] | None = None) -> dict[str, Any]:
    """Benchmark the module wezterm-theme currently installs (standalone or loader mode)."""
    from .core import is_loader_mode

    paths = paths or resolve_wezterm_paths()
    module = paths["tab_theme_lua"] if is_loader_mode(paths) else paths["studio_lua"]
    if not module.is_file():
        raise BenchmarkError(f"{module.name} does not exist yet: apply a theme first.")
    result = benchmark_modules([module])[0]
    if result.get("error"):
        raise BenchmarkError(f"{module.name} failed in the benchmark: {result['error']}")
    return result


def format_benchmark(result: dict[str, Any]) -> list[str]:
    """Human-readable summary lines for the CLI and TUI."""
    lines = []
    if result.get("tab_bar_ms") is not None:
        lines.append(
            f"Tab bar update ({result['tabs']} tabs): {result['tab_bar_ms']:.2f} ms of Lua, "
            f"{int(result.get('lazy_reads') or 0)} process/cwd lookups"
        )
    if result.get("status_ms") is not None:
        lines.append(
            f"Status bar tick: {result['status_ms']:.2f} ms of Lua, "
            f"{result.get('status_queries', 0):g} process lookups"
        )
    return lines or ["The module has no tab-bar or status-bar handlers to measure."]
