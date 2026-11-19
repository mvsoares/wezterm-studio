"""Local cross-platform HTTP server for WezTerm Studio (zero external dependencies).

Binds to 127.0.0.1 by default, serves `assets/studio.html`, and exposes JSON endpoints
to live-apply tab themes, custom symbols, fonts, color schemes, window settings,
backups, and presets directly into WezTerm.
"""

from __future__ import annotations

import importlib.resources
import json
import socket
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .core import (
    _KEEP,
    apply_appearance,
    apply_theme,
    backup_current_config,
    export_preset,
    get_available_backups,
    get_current_state,
    import_preset,
    init_wezterm_config,
    restore_backup,
)
from .fonts import get_color_schemes, get_installed_fonts
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


def _load_studio_html() -> bytes:
    ref = importlib.resources.files("wezterm_theme").joinpath("assets/studio.html")
    return ref.read_bytes()


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
        return "Erro desconhecido"
    lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else err[:200]


class StudioRequestHandler(BaseHTTPRequestHandler):
    """REST + static HTML handler for WezTerm Studio."""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep terminal clean while serving UI requests
        pass

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            html_bytes = _load_studio_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if route == "/api/bootstrap":
            self._send_json({
                "ok": True,
                "version": __version__,
                "state": get_current_state(),
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

        if route == "/api/fonts":
            refresh = qs.get("refresh", ["0"])[0] == "1"
            self._send_json({"ok": True, "fonts": get_installed_fonts(refresh=refresh)})
            return

        if route == "/api/schemes":
            refresh = qs.get("refresh", ["0"])[0] == "1"
            self._send_json({"ok": True, "schemes": get_color_schemes(refresh=refresh)})
            return

        if route == "/api/backups":
            self._send_json({"ok": True, "backups": _serialize_backups()})
            return

        if route == "/api/export":
            raw, _ = export_preset()
            self._send_json({"ok": True, "preset": json.loads(raw)})
            return

        self._send_json({"ok": False, "msg": f"Rota não encontrada: {route}"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"ok": False, "msg": f"JSON inválido: {exc}"}, status=400)
            return

        if route == "/api/init":
            ok, msg = init_wezterm_config()
            self._send_json({"ok": ok, "msg": msg, "state": get_current_state()})
            return

        if route == "/api/apply-tab-theme":
            tid = body.get("theme_id")
            if tid is None:
                tid = get_current_state()["theme_id"]
            theme = get_theme_by_id(int(tid))
            if not theme:
                self._send_json({"ok": False, "msg": f"Tema #{tid} inválido."}, status=400)
                return

            tlen = body.get("tab_title_len")
            if tlen is not None:
                tlen = max(4, min(60, int(tlen)))

            sym_arg: Any = _KEEP
            if "symbols" in body:
                sym_val = body["symbols"]
                sym_arg = _KEEP if sym_val == "keep" else sym_val

            ok, _, err = apply_theme(theme, tab_title_len=tlen, symbols=sym_arg)
            if not ok:
                self._send_json({"ok": False, "msg": f"WezTerm rejeitou a configuração: {_short_err(err)}"})
                return
            self._send_json({
                "ok": True,
                "msg": f"Tema #{theme['id']:02d} ({theme['name']}) aplicado!",
                "state": get_current_state(),
            })
            return

        if route == "/api/apply-appearance":
            updates = body.get("updates")
            if not isinstance(updates, dict) or not updates:
                self._send_json({"ok": False, "msg": "Nenhuma alteração informada."}, status=400)
                return
            ok, err = apply_appearance(updates)
            if not ok:
                self._send_json({"ok": False, "msg": f"WezTerm rejeitou a alteração: {_short_err(err)}"})
                return
            label = body.get("label") or "Configuração atualizada!"
            self._send_json({"ok": True, "msg": label, "state": get_current_state()})
            return

        if route == "/api/backup":
            label = (body.get("label") or "").strip() or None
            dest = backup_current_config(label=label)
            if not dest:
                # Ensure a module exists first, then back it up
                st = get_current_state()
                t = get_theme_by_id(st["theme_id"]) or THEMES[9]
                apply_theme(t)
                dest = backup_current_config(label=label)
            msg = f"Backup salvo: {dest.name}" if dest else "Nenhum arquivo ativo para backup."
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
                self._send_json({"ok": False, "msg": "Campo 'preset' ausente."}, status=400)
                return
            ok, msg = import_preset(preset)
            self._send_json({"ok": ok, "msg": msg, "state": get_current_state()})
            return

        self._send_json({"ok": False, "msg": f"Rota POST não encontrada: {route}"}, status=404)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _find_free_port(host: str, preferred: int, max_tries: int = 15) -> int:
    for p in range(preferred, preferred + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise OSError(f"Nenhuma porta disponível entre {preferred} e {preferred + max_tries - 1}")


def create_server(host: str = "127.0.0.1", port: int | None = None) -> tuple[ThreadingHTTPServer, int]:
    """Instantiate the HTTP server on `port` (or auto-select starting from 7777)."""
    actual_port = port if port is not None else _find_free_port(host, DEFAULT_PORT)
    httpd = ReusableThreadingHTTPServer((host, actual_port), StudioRequestHandler)
    return httpd, actual_port


def run_studio_server(
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Start the interactive WezTerm Studio web server and open it in the browser."""
    httpd, actual_port = create_server(host=host, port=port)
    url = f"http://{host}:{actual_port}"

    print(f"\n  \033[1;36m✦ WezTerm Studio v{__version__}\033[0m rodando em: \033[1;32m{url}\033[0m")
    print("  \033[2mAlterações de temas de abas, símbolos, fontes e cores recarregam o WezTerm em tempo real.\033[0m")
    print("  \033[2mPressione Ctrl+C para encerrar o servidor.\033[0m\n")

    if open_browser and has_gui_display():
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Encerrando WezTerm Studio.")
    finally:
        httpd.server_close()
