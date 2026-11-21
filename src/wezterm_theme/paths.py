"""Cross-platform path resolution, terminal setup, and WezTerm binary discovery.

Supports Linux, macOS, and Windows out of the box using only Python's stdlib.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path

_FLATPAK_CACHE_LOCK = threading.Lock()
_FLATPAK_CACHE: tuple[str, float, bool] | None = None
_FLATPAK_CACHE_TTL = 60.0


def _current_umask() -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


_UMASK = _current_umask()


def atomic_write_text(path: Path, text: str, newline: str | None = None) -> None:
    """Write `text` atomically (temp file + rename) so WezTerm never reloads a half-written file.

    Symlinks (dotfile managers like stow/chezmoi) are followed so the link itself survives,
    and the existing file mode is preserved. `newline` is passed to `open()`: use "" to
    write `text` with its line endings untouched. Targets that exist but are not regular
    files (`/dev/stdout`, FIFOs, `>(...)` process substitution) are written directly.
    """
    if path.exists() and not path.is_file():
        with open(path, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        return
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            if target.exists():
                shutil.copymode(target, tmp)
            else:
                os.chmod(tmp, 0o666 & ~_UMASK)
        except OSError:
            pass
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                # Windows refuses to replace a file another process (WezTerm) has open.
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def init_terminal() -> None:
    """Configure UTF-8 stdio, safe SIGPIPE handling, and Windows VT100/TrueColor ANSI support."""
    if hasattr(signal, "SIGPIPE"):
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except Exception:
            pass

    if os.name == "nt":
        # Ensure UTF-8 output so Nerd Font glyphs, emojis, and box borders never fail on cp1252
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) on Windows consoles
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
                handle = kernel32.GetStdHandle(std_handle)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def get_platform_name() -> str:
    """Return normalized OS identifier: 'linux', 'macos', or 'windows'."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def resolve_wezterm_paths() -> dict[str, Path]:
    """Resolve WezTerm's active config directory, main wezterm.lua, and module paths.

    Follows upstream WezTerm config discovery across Linux, macOS, and Windows:
    1. $WEZTERM_CONFIG_FILE (if set)
    2. $XDG_CONFIG_HOME/wezterm/wezterm.lua (if $XDG_CONFIG_HOME is set)
    3. ~/.config/wezterm/wezterm.lua (preferred on Linux, macOS, and Windows)
    4. ~/.wezterm.lua (legacy home dotfile if present and ~/.config/wezterm/wezterm.lua is not)
    """
    home = Path.home()

    env_cfg = os.environ.get("WEZTERM_CONFIG_FILE", "").strip()
    if env_cfg:
        cfg_file = Path(env_cfg).expanduser()
        cfg_dir = cfg_file.parent if cfg_file.parent != home else (home / ".config" / "wezterm")
    else:
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base_cfg_dir = Path(xdg_cfg).expanduser() / "wezterm" if xdg_cfg else (home / ".config" / "wezterm")
        dir_lua = base_cfg_dir / "wezterm.lua"
        dot_lua = home / ".wezterm.lua"

        if dir_lua.exists():
            cfg_dir = base_cfg_dir
            cfg_file = dir_lua
        elif dot_lua.exists():
            cfg_dir = base_cfg_dir
            cfg_file = dot_lua
        else:
            cfg_dir = base_cfg_dir
            cfg_file = dir_lua

    return {
        "config_dir": cfg_dir,
        "config_file": cfg_file,
        "defaults_json": cfg_dir / "defaults.json",
        "settings_json": cfg_dir / "settings.json",
        "tab_theme_lua": cfg_dir / "tab_theme.lua",
        "studio_json": cfg_dir / "wezterm_theme.json",
        "studio_lua": cfg_dir / "wezterm_theme.lua",
        "session_json": cfg_dir / "wezterm_session.json",
        "backup_dir": cfg_dir / "backups",
    }


def get_cache_dir() -> Path:
    """Return OS-appropriate user cache directory for generated previews and caches."""
    plat = get_platform_name()
    home = Path.home()
    if plat == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return base / "wezterm-theme"
    if plat == "macos":
        return home / "Library" / "Caches" / "wezterm-theme"
    base = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
    return base / "wezterm-theme"


def find_wezterm_cmd() -> list[str] | None:
    """Locate the `wezterm` CLI executable across Linux, macOS, and Windows."""
    env_bin = os.environ.get("WEZTERM_BIN", "").strip()
    if env_bin and Path(env_bin).exists():
        return [env_bin]

    direct = shutil.which("wezterm")
    if direct:
        return [direct]

    home = Path.home()
    plat = get_platform_name()

    candidates: list[Path] = []
    if plat == "macos":
        candidates.extend([
            Path("/Applications/WezTerm.app/Contents/MacOS/wezterm"),
            home / "Applications" / "WezTerm.app" / "Contents" / "MacOS" / "wezterm",
            Path("/opt/homebrew/bin/wezterm"),
            Path("/usr/local/bin/wezterm"),
        ])
    elif plat == "windows":
        prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        prog86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        local_app = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates.extend([
            prog / "WezTerm" / "wezterm.exe",
            prog86 / "WezTerm" / "wezterm.exe",
            local_app / "Programs" / "WezTerm" / "wezterm.exe",
            home / "scoop" / "apps" / "wezterm" / "current" / "wezterm.exe",
        ])
    else:
        candidates.extend([
            home / ".local" / "bin" / "wezterm",
            Path("/usr/local/bin/wezterm"),
            Path("/usr/bin/wezterm"),
        ])

    for cand in candidates:
        if cand.is_file():
            return [str(cand)]

    # Linux Flatpak fallback (cached so repeated status checks don't spawn flatpak)
    if plat == "linux":
        fp = shutil.which("flatpak")
        if fp and _flatpak_has_wezterm(fp):
            return ["flatpak", "run", "org.wezfurlong.wezterm"]

    return None


def _flatpak_has_wezterm(flatpak_bin: str) -> bool:
    global _FLATPAK_CACHE
    now = time.monotonic()
    with _FLATPAK_CACHE_LOCK:
        if _FLATPAK_CACHE is not None:
            cached_bin, ts, available = _FLATPAK_CACHE
            if cached_bin == flatpak_bin and (now - ts) < _FLATPAK_CACHE_TTL:
                return available
    available = False
    try:
        res = subprocess.run(
            [flatpak_bin, "info", "org.wezfurlong.wezterm"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        available = res.returncode == 0
    except Exception:
        available = False
    with _FLATPAK_CACHE_LOCK:
        _FLATPAK_CACHE = (flatpak_bin, now, available)
    return available


def find_studio_cli() -> str | None:
    """Locate the installed `wezterm-theme` executable for GUI/Command Palette callbacks.

    Paths are made absolute but symlinks are NOT resolved: a venv's `bin/python` links to
    the system interpreter, and Homebrew/Nix shims point into versioned directories that
    disappear on upgrade, while the shim path itself stays valid.
    """
    direct = shutil.which("wezterm-theme")
    if direct:
        return str(Path(direct).absolute())
    exe_names = ("wezterm-theme.exe", "wezterm-theme") if os.name == "nt" else ("wezterm-theme",)
    if sys.argv and sys.argv[0]:
        try:
            argv_p = Path(sys.argv[0]).expanduser()
            if argv_p.name.lower() in exe_names and argv_p.is_file():
                return str(argv_p.absolute())
        except OSError:
            pass
    bin_dirs: list[Path] = []
    if sys.executable:
        bin_dirs.append(Path(sys.executable).parent)
    for scheme in (None, "user"):
        try:
            scripts = sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")) if scheme else sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        if scripts:
            bin_dirs.append(Path(scripts))
    for bin_dir in bin_dirs:
        for name in exe_names:
            cand = bin_dir / name
            try:
                if cand.is_file():
                    return str(cand.absolute())
            except OSError:
                continue
    return None


def has_gui_display() -> bool:
    """Return True if opening a browser window is supported in the current session."""
    plat = get_platform_name()
    if plat == "windows":
        return True
    if plat == "macos":
        # Avoid hanging over headless SSH unless display/local Aqua session exists
        if os.environ.get("SSH_CONNECTION") and not os.environ.get("DISPLAY"):
            return False
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def system_summary() -> dict[str, str]:
    """Diagnostic dictionary used by `wezterm-theme doctor` and `/status`."""
    paths = resolve_wezterm_paths()
    wz = find_wezterm_cmd()
    return {
        "os": f"{platform.system()} {platform.release()} ({get_platform_name()})",
        "python": platform.python_version(),
        "wezterm_cmd": " ".join(wz) if wz else "not found in PATH",
        "config_dir": str(paths["config_dir"]),
        "config_file": str(paths["config_file"]),
        "cache_dir": str(get_cache_dir()),
    }
