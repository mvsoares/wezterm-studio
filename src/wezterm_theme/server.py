"""Local cross-platform HTTP server for WezTerm Studio (zero external dependencies).

Binds to 127.0.0.1 by default, serves `assets/studio.html`, and exposes JSON endpoints
to live-apply tab themes, custom symbols, fonts, color schemes, window settings,
backups, and presets directly into WezTerm.

Because these endpoints rewrite the user's WezTerm config, any web page open in the
browser must not be able to call them. Requests are therefore checked for:
- a loopback `Host` header (blocks DNS rebinding),
- `Content-Type: application/json` on POST (forces a CORS preflight, which is refused),
- a same-origin `Origin` header when present,
- a per-process session token, embedded in the served HTML and sent back as
  `X-Studio-Token`.
"""

from __future__ import annotations

import importlib.resources
import ipaddress
import json
import secrets
import socket
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .core import (
    _KEEP,
    VALIDATION_SKIPPED_MSG,
    apply_appearance,
    apply_theme,
    backup_current_config,
    clear_session_snapshot,
    detect_performance_warnings,
    export_preset,
    get_available_backups,
    get_current_state,
    get_session_snapshot_info,
    import_preset,
    init_wezterm_config,
    normalize_appearance_updates,
    normalize_symbols,
    restore_backup,
    validation_available,
)
from .fonts import get_color_scheme_palettes, get_color_schemes, get_installed_fonts
from .paths import has_gui_display, system_summary
from .themes import (
    CATEGORIES,
    DEFAULT_SYMBOLS,
    NO_SYMBOL_STYLES,
    SYMBOL_GROUPS,
    THEMES,
    get_theme_by_id,
)

DEFAULT_PORT = 7777
MAX_BODY_BYTES = 5 * 1024 * 1024
TOKEN_HEADER = "X-Studio-Token"
_LOOPBACK_HOST_NAMES = {"127.0.0.1", "localhost", "[::1]"}


class RequestError(Exception):
    """A client error that maps directly to an HTTP status and JSON message."""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg


def _load_studio_html() -> bytes:
    ref = importlib.resources.files("wezterm_theme").joinpath("assets/studio.html")
    return ref.read_bytes()


def _render_studio_html(token: str) -> bytes:
    """Serve the Studio page with this server's session token embedded for the JS API calls."""
    meta = f'<head>\n  <meta name="studio-token" content="{token}">'.encode("utf-8")
    return _load_studio_html().replace(b"<head>", meta, 1)


def _is_loopback_host(host: str) -> bool:
    name = host.strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


def _studio_warnings(appearance: dict[str, Any] | None) -> list[str]:
    warnings = [] if validation_available() else [VALIDATION_SKIPPED_MSG]
    return warnings + detect_performance_warnings(appearance=appearance)


def _serialize_backups() -> list[dict[str, Any]]:
    out = []
    for idx, b in enumerate(get_available_backups(), start=1):
        out.append({
            "index": idx,
            "filename": b["filename"],
            "date_str": b["date_str"],
            "label": b["label"],
            "theme_info": b["theme_info"],
            "theme_id": b["theme_id"],
            "path": str(b["path"]),
        })
    return out


def _serialize_symbol_groups() -> list[dict[str, Any]]:
    groups = []
    for key, code, title, options in SYMBOL_GROUPS:
        clean_opts = []
        for opt in options:
            clean_opts.append({
                "key": opt["key"],
                "name": opt["name"],
                "chars": opt["chars"],
                "has_match": "match" in opt,
            })
        groups.append({"key": key, "code": code, "title": title, "options": clean_opts})
    return groups


def _short_err(err: str | None) -> str:
    if not err:
        return "Unknown error"
    lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else err[:200]


class StudioRequestHandler(BaseHTTPRequestHandler):
    """REST + static HTML handler for WezTerm Studio."""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep terminal clean while serving UI requests
        pass

    server: ReusableThreadingHTTPServer

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            raise RequestError(400, "Invalid Content-Length header.") from None
        if length > MAX_BODY_BYTES:
            raise RequestError(413, f"Request body too large (limit {MAX_BODY_BYTES // (1024 * 1024)} MB).")
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
        except ValueError as exc:
            raise RequestError(400, f"Invalid JSON payload: {exc}") from None
        if not isinstance(data, dict):
            raise RequestError(400, "JSON payload must be an object.")
        return data

    def _check_host(self) -> None:
        """Reject DNS-rebinding requests: a loopback-bound server only answers loopback Host names."""
        if not self.server.loopback_only:
            return
        host = (self.headers.get("Host") or "").strip().lower()
        name, sep, port = host.rpartition(":")
        if not sep or port != str(self.server.server_address[1]) or name not in self.server.allowed_host_names:
            raise RequestError(403, f"Host '{host}' is not allowed.")

    def _check_post(self) -> None:
        """Reject cross-site requests that try to change the configuration."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise RequestError(415, "Content-Type must be application/json.")
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").strip().lower()
            if urllib.parse.urlsplit(origin.strip()).netloc.lower() != host:
                raise RequestError(403, "Cross-origin requests are not allowed.")
        token = (self.headers.get(TOKEN_HEADER) or "").encode("utf-8", "replace")
        if not secrets.compare_digest(token, self.server.studio_token.encode("utf-8")):
            raise RequestError(403, "Missing or invalid Studio session token; reload the page.")

    def _dispatch(self, handler: Any, *args: Any) -> None:
        try:
            handler(*args)
        except (BrokenPipeError, ConnectionResetError):
            return
        except RequestError as exc:
            self._send_json({"ok": False, "msg": exc.msg}, status=exc.status)
        except (ValueError, TypeError) as exc:
            self._send_json({"ok": False, "msg": f"Invalid request: {exc}"}, status=400)
        except Exception as exc:
            self._send_json({"ok": False, "msg": f"Internal error: {exc}"}, status=500)

    def do_GET(self) -> None:
        self._dispatch(self._handle_get)

    def do_POST(self) -> None:
        self._dispatch(self._handle_post)

    def _handle_get(self) -> None:
        self._check_host()
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            html_bytes = _render_studio_html(self.server.studio_token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if route == "/api/bootstrap":
            st = get_current_state()
            self._send_json({
                "ok": True,
                "version": __version__,
                "state": st,
                "warnings": _studio_warnings(st.get("appearance")),
                "session": get_session_snapshot_info(),
                "system": system_summary(),
                "themes": THEMES,
                "categories": [
                    {"code": c, "name": n, "lo": lo, "hi": hi} for c, n, lo, hi in CATEGORIES
                ],
                "symbol_groups": _serialize_symbol_groups(),
                "default_symbols": DEFAULT_SYMBOLS,
                "no_symbol_styles": sorted(NO_SYMBOL_STYLES),
            })
            return

        if route == "/api/session":
            self._send_json({"ok": True, "session": get_session_snapshot_info()})
            return

        if route == "/api/fonts":
            refresh = qs.get("refresh", ["0"])[0] == "1"
            self._send_json({"ok": True, "fonts": get_installed_fonts(refresh=refresh)})
            return

        if route == "/api/schemes":
            refresh = qs.get("refresh", ["0"])[0] == "1"
            palettes = get_color_scheme_palettes(refresh=refresh)
            self._send_json({
                "ok": True,
                "schemes": [p["name"] for p in palettes],
                "palettes": palettes,
            })
            return

        if route == "/api/backups":
            self._send_json({"ok": True, "backups": _serialize_backups()})
            return

        if route == "/api/export":
            raw, _ = export_preset()
            self._send_json({"ok": True, "preset": json.loads(raw)})
            return

        self._send_json({"ok": False, "msg": f"Route not found: {route}"}, status=404)

    def _handle_post(self) -> None:
        self._check_host()
        self._check_post()
        route = urllib.parse.urlparse(self.path).path
        body = self._read_json_body()

        # `init` also resolves conflicts, so the UI's "Fix Automatically" button shares it.
        if route in ("/api/init", "/api/fix-conflicts"):
            ok, msg = init_wezterm_config(disable_conflicts=True)
            self._send_json({"ok": ok, "msg": msg, "state": get_current_state()})
            return

        if route == "/api/clear-session":
            ok, msg = clear_session_snapshot()
            self._send_json({"ok": ok, "msg": msg, "session": get_session_snapshot_info()})
            return

        if route == "/api/apply-tab-theme":
            tid = body.get("theme_id")
            if tid is None:
                tid = get_current_state()["theme_id"]
            theme = get_theme_by_id(int(tid))
            if not theme:
                self._send_json({"ok": False, "msg": f"Invalid theme #{tid}."}, status=400)
                return

            tlen = body.get("tab_title_len")
            if tlen is not None:
                tlen = max(4, min(60, int(tlen)))

            sym_arg: Any = _KEEP
            if "symbols" in body:
                sym_val = body["symbols"]
                sym_arg = _KEEP if sym_val == "keep" else normalize_symbols(sym_val)

            ok, _, err = apply_theme(theme, tab_title_len=tlen, symbols=sym_arg)
            if not ok:
                self._send_json({"ok": False, "msg": f"WezTerm rejected configuration: {_short_err(err)}"})
                return
            self._send_json({
                "ok": True,
                "msg": f"Theme #{theme['id']:02d} ({theme['name']}) applied!",
                "state": get_current_state(),
            })
            return

        if route == "/api/apply-appearance":
            updates = body.get("updates")
            if not isinstance(updates, dict) or not updates:
                self._send_json({"ok": False, "msg": "No appearance updates provided."}, status=400)
                return
            # Validate up front so bad input is a 400, not a "WezTerm rejected" message.
            updates = normalize_appearance_updates(updates)
            if not updates:
                self._send_json({"ok": False, "msg": "No known appearance settings in update."}, status=400)
                return
            ok, err = apply_appearance(updates)
            if not ok:
                self._send_json({"ok": False, "msg": f"WezTerm rejected update: {_short_err(err)}"})
                return
            label = body.get("label") or "Settings updated!"
            new_st = get_current_state()
            self._send_json({
                "ok": True,
                "msg": label,
                "state": new_st,
                "warnings": _studio_warnings(new_st.get("appearance")),
            })
            return

        if route == "/api/backup":
            label = str(body.get("label") or "").strip() or None
            dest = backup_current_config(label=label)
            msg = f"Backup saved: {dest.name}" if dest else "Nothing to back up yet: apply a tab theme first."
            self._send_json({"ok": bool(dest), "msg": msg, "backups": _serialize_backups()})
            return

        if route == "/api/restore":
            target = body.get("target", 1)
            ok, msg = restore_backup(target)
            self._send_json({
                "ok": ok,
                "msg": msg,
                "state": get_current_state(),
                "backups": _serialize_backups(),
            })
            return

        if route == "/api/import":
            preset = body.get("preset")
            if preset is None:
                self._send_json({"ok": False, "msg": "Missing 'preset' field."}, status=400)
                return
            # Parse here: core.import_preset would treat a non-JSON string as a local file path.
            if isinstance(preset, str):
                try:
                    preset = json.loads(preset)
                except json.JSONDecodeError as exc:
                    raise RequestError(400, f"Preset is not valid JSON: {exc}") from None
            if not isinstance(preset, dict):
                raise RequestError(400, "'preset' must be a JSON object.")
            ok, msg = import_preset(preset)
            self._send_json({"ok": ok, "msg": msg, "state": get_current_state()})
            return

        self._send_json({"ok": False, "msg": f"POST route not found: {route}"}, status=404)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    studio_token: str = ""
    loopback_only: bool = True
    allowed_host_names: set[str] = _LOOPBACK_HOST_NAMES


def _find_free_port(host: str, preferred: int, max_tries: int = 15) -> int:
    for p in range(preferred, preferred + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise OSError(f"No available TCP port between {preferred} and {preferred + max_tries - 1}")


def create_server(host: str = "127.0.0.1", port: int | None = None) -> tuple[ThreadingHTTPServer, int]:
    """Instantiate the HTTP server on `port` (or auto-select starting from 7777)."""
    actual_port = port if port is not None else _find_free_port(host, DEFAULT_PORT)
    httpd = ReusableThreadingHTTPServer((host, actual_port), StudioRequestHandler)
    httpd.studio_token = secrets.token_urlsafe(32)
    httpd.loopback_only = _is_loopback_host(host)
    bound_name = f"[{host}]" if ":" in host else host
    httpd.allowed_host_names = _LOOPBACK_HOST_NAMES | {bound_name.lower()}
    return httpd, actual_port


def run_studio_server(
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Start the interactive WezTerm Studio web server and open it in the browser."""
    httpd, actual_port = create_server(host=host, port=port)
    url = f"http://{host}:{actual_port}"

    print(f"\n  \033[1;36m✦ WezTerm Studio v{__version__}\033[0m running at: \033[1;32m{url}\033[0m")
    print("  \033[2mChanges to tab themes, symbols, fonts, and colors hot-reload WezTerm in real time.\033[0m")
    print("  \033[2mPress Ctrl+C to stop the server.\033[0m\n")
    if not httpd.loopback_only:
        print(
            f"  \033[1;33m⚠ Listening on {host}: anyone who can reach this address can load the page"
            " and change your WezTerm configuration. Prefer the default 127.0.0.1.\033[0m\n"
        )

    if open_browser and has_gui_display():
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping WezTerm Studio.")
    finally:
        httpd.server_close()
