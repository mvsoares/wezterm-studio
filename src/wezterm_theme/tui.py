"""Interactive cross-platform ANSI TrueColor TUI for WezTerm Studio.

Includes:
- 60 Tab Bar Themes with live 4-tab previews & category navigation
- Symbol Customizer (`[s]`)
- Interactive Font & Size Picker (`[f]`)
- Built-in Color Scheme Picker (`[cs]`)
- Backup & Restore (`[b]` / `[u]`)
- 1-key Web Studio launcher (`[w]`)
"""

from __future__ import annotations

import random
import re
import shutil
import unicodedata
from typing import Any

from .core import (
    apply_appearance,
    apply_theme,
    backup_current_config,
    get_available_backups,
    get_current_state,
    init_wezterm_config,
    is_loader_mode,
    restore_backup,
    sanitize_legacy_lua,
)
from .fonts import get_color_schemes, get_installed_fonts
from .paths import resolve_wezterm_paths
from .server import run_studio_server
from .themes import (
    CATEGORIES,
    CATEGORY_SHORTCUTS,
    DEFAULT_SYMBOLS,
    SYMBOL_GROUPS,
    THEMES,
    apply_symbols,
    compose_symbols,
    filter_themes,
    find_theme,
    get_theme_by_id,
    project_slot,
    theme_uses_symbols,
)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
RED = "\033[31m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def ansi_fg(hex_str: str) -> str:
    r, g, b = hex_to_rgb(hex_str)
    return f"\033[38;2;{r};{g};{b}m"


def ansi_bg(hex_str: str) -> str:
    r, g, b = hex_to_rgb(hex_str)
    return f"\033[48;2;{r};{g};{b}m"


def char_cells(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(s: str) -> int:
    plain = _ANSI_RE.sub("", s)
    return sum(char_cells(ch) for ch in plain)


def format_tab_label(text: str, tab_len: int) -> str:
    tab_len = max(1, int(tab_len))
    w = visible_len(text)
    if w > tab_len:
        if tab_len == 1:
            return "…"
        target = tab_len - 1
        out = []
        used = 0
        for ch in text:
            cw = char_cells(ch)
            if used + cw > target:
                break
            out.append(ch)
            used += cw
        return "".join(out) + "…" + (" " * (target - used))
    return text + (" " * (tab_len - w))


def failure_msg(error: str | None) -> str:
    raw = (error or "erro desconhecido").strip()
    last = raw.splitlines()[-1] if raw else "erro desconhecido"
    return f"{RED}✖ O WezTerm rejeitou a configuração; nada foi alterado.{RESET} {DIM}{last[:160]}{RESET}"


def render_4_tab_bar(theme: dict[str, Any], tab_title_len: int = 15, max_cols: int | None = None) -> str:
    """Render a 4-tab bar in ANSI TrueColor showcasing the theme's 4 distinct palette colors."""
    pal = theme.get("palette", [])
    if len(pal) < 4:
        pal = [
            {"bg": "#4285F4", "fg": "#ffffff", "active_sym": "⚡ 🔵 ➜ ", "inactive_sym": " 🔘"},
            {"bg": "#EA4335", "fg": "#ffffff", "active_sym": "⚡ 🔴 ➜ ", "inactive_sym": " 🔘"},
            {"bg": "#34A853", "fg": "#ffffff", "active_sym": "⚡ 🟢 ➜ ", "inactive_sym": " 🔘"},
            {"bg": "#FBBC05", "fg": "#1a1a1a", "active_sym": "⚡ 🟠 ➜ ", "inactive_sym": " 🔘"},
        ]

    eff_len = int(tab_title_len)
    if max_cols is not None:
        eff_len = max(4, min(eff_len, max(4, (max_cols - 48) // 4)))

    l0 = format_tab_label("setup", eff_len)
    l1 = format_tab_label("mvsoares", eff_len)
    l2 = format_tab_label("careplus", eff_len)
    l3 = format_tab_label("cloud-run", eff_len)

    p0, p1, p2, p3 = pal[0], pal[1], pal[2], pal[3]
    style = theme.get("style", "pills")
    bg_bar = theme.get("bg_bar", "#15161c")
    gap = f"{ansi_bg(bg_bar)} " if theme.get("use_fancy", True) else ""

    if style == "powerline":
        sep = theme.get("sep_char", "")
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} {p0.get('active_sym', '⚡ ')}1 {l0} {ansi_bg(p1['bg'])}{ansi_fg(p0['bg'])}{sep}"
        t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} 2{p1.get('inactive_sym', '')} {l1} {ansi_bg(p2['bg'])}{ansi_fg(p1['bg'])}{sep}"
        t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(p2['fg'])} 3{p2.get('inactive_sym', '')} {l2} {ansi_bg(p3['bg'])}{ansi_fg(p2['bg'])}{sep}"
        t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} 4{p3.get('inactive_sym', '')} {l3} {ansi_bg(bg_bar)}{ansi_fg(p3['bg'])}{sep}{RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{t1}{t2}{t3} {RESET}"

    if style == "smart_badges":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} 󰊢 1 {l0} ✕ {RESET}"
        t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} 󰅩 2 {l1} ✕ {RESET}"
        t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(p2['fg'])} 󰒋 3 {l2} ✕ {RESET}"
        t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} 󰜎 4 {l3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "circle_numbers":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} ❶ {l0} ✕ {RESET}"
        t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} ❷ {l1} ✕ {RESET}"
        t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(p2['fg'])} ❸ {l2} ✕ {RESET}"
        t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} ❹ {l3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "pulse_dots":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} {p0.get('active_sym', '')}1 {l0} ✕ {RESET}"
        t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} {p1.get('inactive_sym', '')}2 {l1} ✕ {RESET}"
        t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(p2['fg'])} {p2.get('inactive_sym', '')}3 {l2} ✕ {RESET}"
        t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} {p3.get('inactive_sym', '')}4 {l3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "crt_amber":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} [* 01:{l0.upper()} *] {RESET}"
        t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} [- 02:{l1.upper()} -] {RESET}"
        t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(p2['fg'])} [- 03:{l2.upper()} -] {RESET}"
        t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} [- 04:{l3.upper()} -] {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "left_bar":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD}▌ 1 {l0} ✕ {RESET}"
        t1 = f"{ansi_bg('#1a1c23')}{ansi_fg(p1['bg'])}  2 {l1} ✕ {RESET}"
        t2 = f"{ansi_bg('#1a1c23')}{ansi_fg(p2['bg'])}  3 {l2} ✕ {RESET}"
        t3 = f"{ansi_bg('#1a1c23')}{ansi_fg(p3['bg'])}  4 {l3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "ghost":
        t0 = f"{ansi_bg('#252836')}{ansi_fg(p0['bg'])}{BOLD} 1 {l0} ✕ {RESET}"
        t1 = f"{ansi_bg(bg_bar)}{ansi_fg(p1['bg'])} 2 {l1} ✕ {RESET}"
        t2 = f"{ansi_bg(bg_bar)}{ansi_fg(p2['bg'])} 3 {l2} ✕ {RESET}"
        t3 = f"{ansi_bg(bg_bar)}{ansi_fg(p3['bg'])} 4 {l3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "top_neon":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} ▔ 1 {l0} ✕ {RESET}"
        t1 = f"{ansi_bg('#151720')}{ansi_fg(p1['bg'])} ▔ 2 {l1} ✕ {RESET}"
        t2 = f"{ansi_bg('#151720')}{ansi_fg(p2['bg'])} ▔ 3 {l2} ✕ {RESET}"
        t3 = f"{ansi_bg('#151720')}{ansi_fg(p3['bg'])} ▔ 4 {l3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "outline":
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} 【1 {l0}】 {RESET}"
        t1 = f"{ansi_bg('#161822')}{ansi_fg(p1['bg'])} 〔2 {l1}〕 {RESET}"
        t2 = f"{ansi_bg('#161822')}{ansi_fg(p2['bg'])} 〔3 {l2}〕 {RESET}"
        t3 = f"{ansi_bg('#161822')}{ansi_fg(p3['bg'])} 〔4 {l3}〕 {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "project_color":
        tabs = []
        for n, name in enumerate(["careplus", "linux-setup", "careplus", "infra"], start=1):
            c = theme["palette"][project_slot(name, len(theme["palette"]))]
            lbl = format_tab_label(name, eff_len)
            text = f"{c.get('active_sym', '')}{n}" if n == 1 else f"{n}{c.get('inactive_sym', '')}"
            tabs.append(f"{ansi_bg(c['bg'])}{ansi_fg(c['fg'])}{BOLD if n == 1 else ''} {text} {lbl} ✕ {RESET}")
        return f"{ansi_bg(bg_bar)} {gap.join(tabs)} {RESET}"

    if style == "activity":
        alert = theme.get("alert_color", "#fdd663")
        la0 = format_tab_label("setup", eff_len)
        la1 = format_tab_label("cargo|build", eff_len)
        la2 = format_tab_label("gemini", eff_len)
        la3 = format_tab_label("infra", eff_len)
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} {p0.get('active_sym', '')}1 {la0} ✕ {RESET}"
        t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} ⏳ 2{p1.get('inactive_sym', '')} {la1} ✕ {RESET}"
        t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(alert)} ● {ansi_fg(p2['fg'])}3{p2.get('inactive_sym', '')} {la2} ✕ {RESET}"
        t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} 4{p3.get('inactive_sym', '')} {la3} ✕ {RESET}"
        return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"

    if style == "mono_accent":
        muted_bg, muted_fg = theme.get("muted_bg", "#303134"), theme.get("muted_fg", "#9aa0a6")
        t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} {p0.get('active_sym', '')}1 {l0} ✕ {RESET}"
        rest = [
            f"{ansi_bg(muted_bg)}{ansi_fg(p['bg'])}{BOLD} {n}{RESET}{ansi_bg(muted_bg)}{ansi_fg(muted_fg)}"
            f"{p.get('inactive_sym', '')} {lbl} ✕ {RESET}"
            for n, p, lbl in ((2, p1, l1), (3, p2, l2), (4, p3, l3))
        ]
        return f"{ansi_bg(bg_bar)} {gap.join([t0, *rest])} {RESET}"

    t0 = f"{ansi_bg(p0['bg'])}{ansi_fg(p0['fg'])}{BOLD} {p0.get('active_sym', '⚡ ➜ ')}1 {l0} ✕ {RESET}"
    t1 = f"{ansi_bg(p1['bg'])}{ansi_fg(p1['fg'])} 2{p1.get('inactive_sym', ' 🔘')} {l1} ✕ {RESET}"
    t2 = f"{ansi_bg(p2['bg'])}{ansi_fg(p2['fg'])} 3{p2.get('inactive_sym', ' 🔘')} {l2} ✕ {RESET}"
    t3 = f"{ansi_bg(p3['bg'])}{ansi_fg(p3['fg'])} 4{p3.get('inactive_sym', ' 🔘')} {l3} ✕ {RESET}"
    return f"{ansi_bg(bg_bar)} {t0}{gap}{t1}{gap}{t2}{gap}{t3} {RESET}"


def print_single_card(
    theme: dict[str, Any],
    cur_id: int,
    symbols: dict[str, str] | None = None,
    tab_title_len: int = 15,
    term_cols: int = 100,
) -> None:
    theme = apply_symbols(theme, symbols)
    tid = theme["id"]
    name = theme["name"]
    cat = theme["category"]
    desc = theme.get("description", "")
    pal = theme.get("palette", [])
    is_active = tid == cur_id

    num_str = f"{BOLD}{CYAN}#{tid:02d}{RESET}"
    name_str = f"{BOLD}{name}{RESET}"
    cat_str = f"{DIM}[{cat}]{RESET}"
    active_badge = f"  {GREEN}{BOLD}★ TEMA ATUALMENTE ATIVO{RESET}" if is_active else ""

    swatches = [f"{ansi_fg(p['bg'])}●{RESET} {DIM}{p['bg']}{RESET}" for p in pal]
    swatch_line = "  ".join(swatches)

    bar_preview = render_4_tab_bar(theme, tab_title_len=tab_title_len, max_cols=term_cols)
    rule_w = max(56, min(86, term_cols - 6))

    border_color = GREEN if is_active else CYAN
    print(f"  {border_color}┌─{RESET} {num_str} {name_str} {cat_str}{active_badge}")
    print(f"  {border_color}│{RESET}  {swatch_line}")
    print(f"  {border_color}│{RESET}  {bar_preview}")
    print(f"  {border_color}│{RESET}  {DIM}{desc}{RESET}")
    print(f"  {border_color}└{'─' * rule_w}{RESET}")


def print_list(themes: list[dict[str, Any]], filter_query: str = "") -> None:
    cur_id = get_current_state()["theme_id"]
    items = filter_themes(themes, filter_query)
    hdr_extra = f" — FILTRO: '{filter_query}' ({len(items)} encontrados)" if filter_query else " (60 TEMAS)"
    print(f"\n{BOLD}{CYAN}=== CATÁLOGO DE TEMAS DE ABA WEZTERM{hdr_extra} ==={RESET}")
    if not items:
        print(f"\n  {YELLOW}Nenhum tema corresponde ao filtro '{filter_query}'.{RESET}\n")
        return

    current_cat = None
    for t in items:
        if t["category"] != current_cat:
            current_cat = t["category"]
            print(f"\n  {BOLD}{BLUE}▸ {current_cat}{RESET}")
        tid = t["id"]
        pal = t.get("palette", [])
        dots = "".join(f"{ansi_fg(p['bg'])}● {RESET}" for p in pal)
        active_str = f" {GREEN}{BOLD}← ATIVO{RESET}" if tid == cur_id else ""
        print(f"    {BOLD}#{tid:02d}{RESET}  {dots}  {BOLD}{t['name']:<28}{RESET}  {DIM}({t.get('style', 'pills')}){RESET}{active_str}")
    print(f"\nUse: {BOLD}wezterm-theme -s <NUMERO_OU_SLUG>{RESET} para aplicar diretamente.")


def print_backups_list(backups: list[dict[str, Any]]) -> None:
    paths = resolve_wezterm_paths()
    active_lua = paths["tab_theme_lua"] if is_loader_mode(paths) else paths["studio_lua"]
    print(f"\n{BOLD}{CYAN}=== BACKUPS DE CONFIGURAÇÃO WEZTERM ==={RESET}\n")
    if not backups:
        print(f"  {YELLOW}Nenhum backup encontrado em {paths['backup_dir']}.{RESET}\n")
        return

    cur_norm = ""
    if active_lua.is_file():
        try:
            cur_norm = sanitize_legacy_lua(active_lua.read_text(encoding="utf-8")).strip()
        except Exception:
            pass

    marked_current = False
    for idx, b in enumerate(backups, start=1):
        is_cur = False
        if not marked_current and cur_norm and b.get("content_norm") == cur_norm:
            is_cur = True
            marked_current = True
        cur_tag = f" {GREEN}{BOLD}← IDÊNTICO AO ATIVO{RESET}" if is_cur else ""
        label_str = f" [{YELLOW}{BOLD}{b['label']}{RESET}]" if b["label"] else ""
        print(f"  {BOLD}[{idx:02d}]{RESET}  {CYAN}{b['date_str']}{RESET}  {BOLD}{b['theme_info']:<52}{RESET}{label_str}{cur_tag}")
        print(f"        {DIM}Arquivo: {b['path']}{RESET}")
    print(f"\nPara restaurar: {BOLD}wezterm-theme --restore <NUMERO>{RESET}\n")


def interactive_restore_menu() -> str:
    backups = get_available_backups()
    if not backups:
        return f"{YELLOW}Nenhum backup disponível para restauração.{RESET}"

    term_cols = shutil.get_terminal_size((80, 24)).columns
    box_w = max(68, min(88, term_cols - 4))
    title = "  ↺ RESTAURAR BACKUP DO WEZTERM STUDIO"
    title_pad = title + " " * max(0, box_w - visible_len(title))

    print("\033[2J\033[H", end="")
    print(f"{BOLD}{BLUE}╔{'═' * box_w}╗{RESET}")
    print(f"{BOLD}{BLUE}║{title_pad}║{RESET}")
    print(f"{BOLD}{BLUE}╚{'═' * box_w}╝{RESET}")

    print_backups_list(backups)
    print(f"  {DIM}Digite o número do backup (1-{len(backups)}) para restaurar, ou [q] para voltar.{RESET}\n")

    try:
        ans = input(f"  {BOLD}{CYAN}❯ Número do backup (1-{len(backups)}):{RESET} ").strip()
    except (KeyboardInterrupt, EOFError):
        return "Restauração cancelada."

    if not ans or ans.lower() in ("q", "quit", "cancel", "voltar"):
        return "Restauração cancelada."

    success, msg = restore_backup(ans)
    return f"{GREEN}{BOLD}✔ {msg}{RESET}" if success else f"{RED}✖ {msg}{RESET}"


def symbol_cell(code: str, option: dict[str, Any], selected: bool, width: int) -> str:
    if option["chars"] is None:
        glyphs = f"{DIM}={RESET}"
    else:
        sample = (option["chars"] or [])[:4]
        glyphs = "".join(sample) or f"{DIM}—{RESET}"
    mark = f"{GREEN}{BOLD}●{RESET}" if selected else " "
    text = f"{mark} {BOLD}{code:<3}{RESET} {glyphs}  {DIM}{option['name']}{RESET}"
    return text + " " * max(width - visible_len(text), 1)


def symbols_menu() -> str:
    state = get_current_state()
    saved = state["symbols"]
    draft = dict(saved) if saved else None
    status_msg = ""
    cell_w = 40

    while True:
        st = get_current_state()
        cur_id = st["theme_id"]
        cur_len = st["tab_title_len"]
        theme = get_theme_by_id(cur_id) or THEMES[0]
        term_cols = shutil.get_terminal_size((80, 24)).columns
        cols = max(1, (term_cols - 4) // cell_w)
        box_w = max(68, min(88, term_cols - 4))
        hdr_title = "  ✦ SÍMBOLOS DAS ABAS (prefixo + marcador + seta na ativa, símbolo na inativa)"
        hdr_pad = hdr_title[:box_w] + " " * max(0, box_w - visible_len(hdr_title[:box_w]))

        print("\033[2J\033[H", end="")
        print(f"{BOLD}{BLUE}╔{'═' * box_w}╗{RESET}")
        print(f"{BOLD}{BLUE}║{hdr_pad}║{RESET}")
        print(f"{BOLD}{BLUE}╚{'═' * box_w}╝{RESET}")
        print(f"\n  {GREEN}Tema ativo:{RESET} {BOLD}#{theme['id']:02d} {theme['name']}{RESET} {DIM}[{theme.get('style', 'pills')} · {cur_len}c]{RESET}")
        if not theme_uses_symbols(theme):
            print(f"  {YELLOW}Este estilo não exibe símbolos; a escolha valerá quando aplicar um tema compatível.{RESET}")

        sym_state = f"{MAGENTA}{BOLD}Personalizado{RESET}" if draft else f"{DIM}Símbolos do próprio tema{RESET}"
        changed = f"  {YELLOW}(não aplicado — digite ok){RESET}" if draft != saved else ""
        print(f"\n  {BOLD}Prévia:{RESET} {sym_state}{changed}")
        print(f"    {render_4_tab_bar(apply_symbols(theme, draft), tab_title_len=cur_len, max_cols=term_cols)}")
        if draft:
            pal = theme["palette"]
            active, _ = compose_symbols(draft, 0, pal[0]["bg"])
            _, inactive = compose_symbols(draft, 1, pal[1]["bg"])
            print(f"    {DIM}aba ativa:{RESET} ' {active}1 setup '   {DIM}aba inativa:{RESET} ' 2{inactive} mvsoares '")

        for key, letter, title, options in SYMBOL_GROUPS:
            current = (draft or {}).get(key)
            print(f"\n  {BOLD}{CYAN}{title}{RESET} {DIM}({letter}1-{letter}{len(options)}){RESET}")
            cells = [
                symbol_cell(f"{letter}{n}", o, o["key"] == current, cell_w)
                for n, o in enumerate(options, start=1)
            ]
            for row in range(0, len(cells), cols):
                print("  " + "".join(cells[row : row + cols]).rstrip())

        if status_msg:
            print(f"\n  {status_msg}")
            status_msg = ""

        print(
            f"\n  {DIM}{BOLD}[p<N>] [m<N>] [a<N>] [i<N>]{RESET}{DIM} escolher (ex: m3)  |  {BOLD}[ok]{RESET}{DIM} aplicar  |  "
            f"{BOLD}[x]{RESET}{DIM} símbolos do tema  |  {BOLD}[q]{RESET}{DIM} voltar{RESET}"
        )
        try:
            cmd = input(f"  {BOLD}{CYAN}❯ Código do símbolo ou comando:{RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            cmd = "q"

        m = re.fullmatch(r"([pmai])\s*(\d+)", cmd)
        if m:
            group = next(g for g in SYMBOL_GROUPS if g[1] == m.group(1))
            key, _, title, options = group
            idx = int(m.group(2)) - 1
            if 0 <= idx < len(options):
                draft = {**DEFAULT_SYMBOLS, **(draft or {}), key: options[idx]["key"]}
            else:
                status_msg = f"{RED}{title}: escolha entre {m.group(1)}1 e {m.group(1)}{len(options)}.{RESET}"
        elif cmd in ("ok", "s", "salvar", "aplicar", "apply"):
            ok, _, err = apply_theme(theme, symbols=draft)
            if not ok:
                status_msg = failure_msg(err)
                continue
            if draft:
                return f"{GREEN}{BOLD}✔ Símbolos personalizados aplicados às abas!{RESET}"
            return f"{GREEN}{BOLD}✔ Abas de volta aos símbolos do tema #{theme['id']:02d}.{RESET}"
        elif cmd in ("x", "reset", "padrao", "padrão"):
            draft = None
            status_msg = f"{CYAN}Prévia com os símbolos do tema. Digite ok para aplicar.{RESET}"
        elif cmd in ("q", "quit", "voltar", "sair"):
            return "Símbolos não alterados." if draft == saved else f"{YELLOW}Alterações de símbolos descartadas.{RESET}"
        elif cmd:
            status_msg = f"{RED}Comando '{cmd}' não reconhecido. Use p/m/a/i + número (ex: m3), ok, x ou q.{RESET}"


def fonts_menu() -> str:
    """Interactive terminal menu to browse and apply Editor & Tab fonts and sizes."""
    fonts = get_installed_fonts()
    query = ""
    page = 0
    page_size = 14
    status_msg = ""

    while True:
        state = get_current_state()
        app = state["appearance"]
        ed_font = app.get("font_family") or "Padrão"
        ed_size = app.get("font_size") or 13
        tb_font = app.get("tab_font_family") or "Padrão"
        tb_size = app.get("tab_font_size") or 11

        filtered = [
            (idx + 1, f)
            for idx, f in enumerate(fonts)
            if not query or query.lower() in f["name"].lower() or query.lower() in f["desc"].lower()
        ]
        total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))

        term_cols = shutil.get_terminal_size((90, 28)).columns
        box_w = max(68, min(88, term_cols - 4))
        hdr = "  🔤 SELETOR DE FONTES & TAMANHOS (Editor + Barra de Abas)"
        hdr_pad = hdr + " " * max(0, box_w - visible_len(hdr))

        print("\033[2J\033[H", end="")
        print(f"{BOLD}{BLUE}╔{'═' * box_w}╗{RESET}")
        print(f"{BOLD}{BLUE}║{hdr_pad}║{RESET}")
        print(f"{BOLD}{BLUE}╚{'═' * box_w}╝{RESET}")
        print(f"\n  {CYAN}Editor:{RESET} {BOLD}{ed_font}{RESET} ({ed_size}pt)    {MAGENTA}Abas:{RESET} {BOLD}{tb_font}{RESET} ({tb_size}pt)")
        if query:
            print(f"  {YELLOW}Filtro ativo:{RESET} '{query}' ({len(filtered)} de {len(fonts)} fontes) — digite [c] para limpar")
        print(f"  {DIM}Página {page + 1}/{total_pages} ({len(filtered)} fontes encontradas){RESET}\n")

        chunk = filtered[page * page_size : (page + 1) * page_size]
        for num, f in chunk:
            fam = f["family"]
            is_ed = ed_font == fam or ed_font.startswith(fam)
            is_tb = tb_font == fam or tb_font.startswith(fam)
            tags = []
            if is_ed:
                tags.append(f"{CYAN}{BOLD}★ EDITOR{RESET}")
            if is_tb:
                tags.append(f"{MAGENTA}{BOLD}★ ABAS{RESET}")
            if f.get("is_nerd"):
                tags.append(f"{GREEN}NF{RESET}")
            tag_str = " ".join(tags)
            print(f"    {BOLD}[{num:02d}]{RESET} {fam:<32} {DIM}{f['desc']:<18}{RESET} {tag_str}")

        if status_msg:
            print(f"\n  {status_msg}")
            status_msg = ""

        print(
            f"\n  {DIM}{BOLD}[1-{len(fonts)}]{RESET}{DIM} Fonte Editor  |  {BOLD}[t<N>]{RESET}{DIM} Fonte Abas (ex: t12)  |  "
            f"{BOLD}[size <PT>]{RESET}{DIM} Tam Editor  |  {BOLD}[tsize <PT>]{RESET}{DIM} Tam Abas{RESET}"
        )
        print(
            f"  {DIM}{BOLD}[n/p]{RESET}{DIM} Página  |  {BOLD}[/texto]{RESET}{DIM} Filtrar  |  "
            f"{BOLD}[c]{RESET}{DIM} Limpar filtro  |  {BOLD}[q]{RESET}{DIM} Voltar aos Temas{RESET}"
        )
        try:
            ans = input(f"  {BOLD}{CYAN}❯ Escolha da fonte ou comando:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            return "Menu de fontes encerrado."

        low = ans.lower()
        if low in ("q", "quit", "voltar", ""):
            if low == "":
                page = (page + 1) % total_pages
                continue
            return "Retornando ao catálogo de temas."
        if low in ("n", "next"):
            page = (page + 1) % total_pages
        elif low in ("p", "prev"):
            page = (page - 1) % total_pages
        elif low in ("c", "clear"):
            query = ""
            page = 0
        elif ans.startswith("/"):
            query = ans[1:].strip()
            page = 0
        elif low.isdigit():
            idx = int(low) - 1
            if 0 <= idx < len(fonts):
                fam = fonts[idx]["family"]
                ok, err = apply_appearance({"font_family": fam})
                status_msg = f"{GREEN}{BOLD}✔ Fonte do Editor alterada para {fam}!{RESET}" if ok else failure_msg(err)
            else:
                status_msg = f"{RED}Escolha um número entre 1 e {len(fonts)}.{RESET}"
        elif re.fullmatch(r"t\s*(\d+)", low):
            idx = int(re.sub(r"^t\s*", "", low)) - 1
            if 0 <= idx < len(fonts):
                fam = fonts[idx]["family"]
                ok, err = apply_appearance({"tab_font_family": fam})
                status_msg = f"{GREEN}{BOLD}✔ Fonte das Abas alterada para {fam}!{RESET}" if ok else failure_msg(err)
            else:
                status_msg = f"{RED}Escolha entre t1 e t{len(fonts)}.{RESET}"
        elif low.startswith("size "):
            try:
                sz = float(ans.split(None, 1)[1])
                ok, err = apply_appearance({"font_size": sz})
                status_msg = f"{GREEN}{BOLD}✔ Tamanho do Editor ajustado para {sz:g}pt!{RESET}" if ok else failure_msg(err)
            except ValueError:
                status_msg = f"{RED}Uso: size 13.5{RESET}"
        elif low.startswith("tsize "):
            try:
                sz = float(ans.split(None, 1)[1])
                ok, err = apply_appearance({"tab_font_size": sz})
                status_msg = f"{GREEN}{BOLD}✔ Tamanho das Abas ajustado para {sz:g}pt!{RESET}" if ok else failure_msg(err)
            except ValueError:
                status_msg = f"{RED}Uso: tsize 11.5{RESET}"
        else:
            # Filter by typed text
            query = ans
            page = 0


def color_scheme_menu() -> str:
    """Interactive quick Color Scheme picker."""
    schemes = get_color_schemes()
    favorites = [
        "Monokai Vivid",
        "Catppuccin Mocha",
        "Catppuccin Macchiato",
        "Tokyo Night",
        "Tokyo Night Storm",
        "Dracula",
        "Gruvbox Dark (Gogh)",
        "Kanagawa (Gogh)",
        "Nord",
        "Everforest Dark (Gogh)",
        "rose-pine",
        "SynthWave 84",
    ]
    status_msg = ""
    matches: list[str] = []

    while True:
        cur_scheme = get_current_state()["appearance"].get("color_scheme") or "Padrão"
        print("\033[2J\033[H", end="")
        print(f"{BOLD}{BLUE}╔══════════════════════════════════════════════════════════════════════════╗{RESET}")
        print(f"{BOLD}{BLUE}║  🎨 ESQUEMAS DE CORES DO TERMINAL ({len(schemes)} esquemas integrados no WezTerm)  ║{RESET}")
        print(f"{BOLD}{BLUE}╚══════════════════════════════════════════════════════════════════════════╝{RESET}")
        print(f"\n  {GREEN}Esquema ativo:{RESET} {BOLD}{cur_scheme}{RESET}\n")
        print(f"  {BOLD}Favoritos Rápidos:{RESET}")
        for i, fav in enumerate(favorites, start=1):
            mark = f" {GREEN}★{RESET}" if fav == cur_scheme else ""
            print(f"    {BOLD}[{i:02d}]{RESET} {fav}{mark}")

        if matches:
            print(f"\n  {BOLD}{CYAN}Resultados da busca ({len(matches)}):{RESET}")
            for s in matches[:15]:
                print(f"    • {s}")

        if status_msg:
            print(f"\n  {status_msg}")
            status_msg = ""

        print(f"\n  {DIM}[1-{len(favorites)}] Aplicar favorito  |  [/termo] Buscar em {len(schemes)} esquemas  |  [nome exato] Aplicar  |  [q] Voltar{RESET}")
        try:
            ans = input(f"  {BOLD}{CYAN}❯ Esquema ou busca:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            return "Menu de cores encerrado."

        if not ans or ans.lower() in ("q", "quit", "voltar"):
            return "Retornando ao catálogo."
        if ans.isdigit():
            idx = int(ans) - 1
            if 0 <= idx < len(favorites):
                chosen = favorites[idx]
                ok, err = apply_appearance({"color_scheme": chosen})
                status_msg = f"{GREEN}{BOLD}✔ Esquema '{chosen}' aplicado!{RESET}" if ok else failure_msg(err)
        elif ans.startswith("/"):
            q = ans[1:].strip().lower()
            matches = [s for s in schemes if q in s.lower()]
            if not matches:
                status_msg = f"{YELLOW}Nenhum esquema encontrado para '{q}'.{RESET}"
        else:
            exact = next((s for s in schemes if s.lower() == ans.lower()), None)
            if exact:
                ok, err = apply_appearance({"color_scheme": exact})
                status_msg = f"{GREEN}{BOLD}✔ Esquema '{exact}' aplicado!{RESET}" if ok else failure_msg(err)
            else:
                matches = [s for s in schemes if ans.lower() in s.lower()]
                if len(matches) == 1:
                    ok, err = apply_appearance({"color_scheme": matches[0]})
                    status_msg = f"{GREEN}{BOLD}✔ Esquema '{matches[0]}' aplicado!{RESET}" if ok else failure_msg(err)
                elif matches:
                    status_msg = f"{CYAN}{len(matches)} esquemas encontrados — digite o nome para aplicar.{RESET}"
                else:
                    status_msg = f"{RED}Esquema '{ans}' não encontrado.{RESET}"


def interactive_picker(themes: list[dict[str, Any]] = THEMES, initial_filter: str = "") -> None:
    """Main interactive WezTerm Studio TUI."""
    filter_query = initial_filter
    page: int | None = None if not initial_filter else 0
    status_msg = ""

    while True:
        state = get_current_state()
        cur_id = state["theme_id"]
        cur_theme = get_theme_by_id(cur_id)
        cur_name = cur_theme["name"] if cur_theme else f"#{cur_id}"
        cur_cat = cur_theme["category"] if cur_theme else ""
        cur_len = state["tab_title_len"]
        cur_syms = state["symbols"]
        ed_font = state["appearance"].get("font_family") or "Padrão"
        ed_size = state["appearance"].get("font_size") or 13

        term_size = shutil.get_terminal_size((100, 36))
        term_cols, term_lines = term_size.columns, term_size.lines
        overhead = 23 if filter_query else 20
        page_size = max(2, min(8, (term_lines - overhead) // 6))

        filtered = filter_themes(themes, filter_query)
        total_pages = max((len(filtered) + page_size - 1) // page_size, 1)

        if page is None:
            active_idx = next((i for i, t in enumerate(filtered) if t["id"] == cur_id), 0)
            page = active_idx // page_size

        page = max(0, min(page, total_pages - 1))

        print("\033[2J\033[H", end="")

        box_w = max(68, min(92, term_cols - 4))
        line1_plain = "  ✦ WEZTERM STUDIO — Temas de Abas (60), Símbolos, Fontes & Cores"
        line1_pad = line1_plain + " " * max(0, box_w - visible_len(line1_plain))

        active_info = f"#{cur_id:02d} {cur_name}"
        right_info = f"Fonte: {ed_font[:18]} ({ed_size}pt) · {cur_len}c"
        line2_vis = f"  ✔ Aba: {active_info}  {right_info}"
        gap_w = max(1, box_w - visible_len(line2_vis) - 1)
        line2_ansi = (
            f"  {GREEN}✔ Aba:{RESET} {BOLD}{active_info}{RESET}"
            f"{' ' * gap_w}  {CYAN}{right_info}{RESET} "
        )

        print(f"{BOLD}{BLUE}╔{'═' * box_w}╗{RESET}")
        print(f"{BOLD}{BLUE}║{RESET}{BOLD}{line1_pad}{RESET}{BOLD}{BLUE}║{RESET}")
        print(f"{BOLD}{BLUE}║{RESET}{line2_ansi}{BOLD}{BLUE}║{RESET}")
        print(f"{BOLD}{BLUE}╚{'═' * box_w}╝{RESET}")

        if not state["initialized"]:
            print(
                f"  {YELLOW}⚡ Seu wezterm.lua ainda não está conectado — digite {BOLD}[init]{RESET}{YELLOW} para integrar automaticamente!{RESET}"
            )

        print(f"\n  {BOLD}Categorias de Abas:{RESET}")
        for row_i in range(0, len(CATEGORIES), 2):
            left_code, left_name, l_lo, l_hi = CATEGORIES[row_i]
            left_col = f"{BOLD}[{left_code}]{RESET} {left_name} ({l_lo:02d}-{l_hi:02d})"
            left_pad = left_col + " " * max(1, 36 - visible_len(left_col))
            if row_i + 1 < len(CATEGORIES):
                right_code, right_name, r_lo, r_hi = CATEGORIES[row_i + 1]
                right_col = f"{BOLD}[{right_code}]{RESET} {right_name} ({r_lo:02d}-{r_hi:02d})"
            else:
                right_col = ""
            print(f"    {left_pad}{right_col}")

        if filter_query:
            print(
                f"\n  {YELLOW}Filtro ativo:{RESET} {BOLD}'{filter_query}'{RESET} "
                f"({len(filtered)} temas) — digite {BOLD}[c]{RESET} para mostrar todos os 60"
            )

        print(f"\n  {BOLD}{MAGENTA}─── Página {page + 1} de {total_pages} ({len(filtered)} temas) ───{RESET}\n")

        if not filtered:
            print(f"  {RED}Nenhum tema encontrado para '{filter_query}'. Digite [c] para limpar.{RESET}\n")
        else:
            start = page * page_size
            for t in filtered[start : start + page_size]:
                print_single_card(t, cur_id, cur_syms, tab_title_len=cur_len, term_cols=term_cols)

        if status_msg:
            print(f"  {status_msg}\n")
            status_msg = ""

        print(
            f"  {DIM}Comandos: {BOLD}[1-60]{RESET}{DIM} Aplicar Tema  |  {BOLD}[s]{RESET}{DIM} Símbolos  |  "
            f"{BOLD}[f]{RESET}{DIM} Fontes & Tamanho  |  {BOLD}[cs]{RESET}{DIM} Color Scheme  |  {BOLD}[w]{RESET}{DIM} Web Studio{RESET}"
        )
        print(
            f"  {DIM}Navegar:  {BOLD}[n/p]{RESET}{DIM} Pág  |  {BOLD}[/busca]{RESET}{DIM} Filtrar  |  "
            f"{BOLD}[len <N>]{RESET}{DIM} Largura  |  {BOLD}[r]{RESET}{DIM} Aleatório  |  {BOLD}[b/u]{RESET}{DIM} Backup/Restore  |  {BOLD}[q]{RESET}{DIM} Sair{RESET}"
        )

        try:
            ans = input(f"\n  {BOLD}{CYAN}❯ Escolha ou comando:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {DIM}Saindo do WezTerm Studio.{RESET}")
            break

        if not ans:
            page = (page + 1) % total_pages
            continue

        low = ans.lower()

        if low in ("q", "quit", "exit", "sair"):
            print(f"  {DIM}Saindo do WezTerm Studio.{RESET}")
            break
        elif low == "init":
            ok, msg = init_wezterm_config()
            status_msg = f"{GREEN}{BOLD}✔ {msg}{RESET}" if ok else f"{RED}✖ {msg}{RESET}"
        elif low in ("n", "next", ">"):
            page = (page + 1) % total_pages
        elif low in ("p", "prev", "<"):
            page = (page - 1) % total_pages
        elif low in ("c", "clear", "all", "todos"):
            filter_query = ""
            page = 0
            status_msg = f"{CYAN}Exibindo todos os 60 temas.{RESET}"
        elif low in ("s", "sym", "simbolos", "símbolos", "symbols"):
            status_msg = symbols_menu()
        elif low in ("f", "font", "fonts", "fonte", "fontes"):
            status_msg = fonts_menu()
        elif low in ("cs", "scheme", "colors", "cores"):
            status_msg = color_scheme_menu()
        elif low in ("w", "web", "studio", "html", "galeria"):
            run_studio_server(open_browser=True)
            status_msg = f"{GREEN}Servidor Web Studio encerrado.{RESET}"
        elif low in ("b", "backup", "salvar"):
            try:
                lbl = input(f"  {BOLD}{CYAN}❯ Rótulo opcional (Enter p/ automático):{RESET} ").strip()
            except (KeyboardInterrupt, EOFError):
                lbl = ""
            bkp = backup_current_config(label=lbl if lbl else "manual")
            status_msg = (
                f"{GREEN}{BOLD}✔ Backup salvo:{RESET} {DIM}{bkp.name}{RESET}"
                if bkp
                else f"{YELLOW}Nenhuma configuração ativa para backup.{RESET}"
            )
        elif low in ("u", "undo", "restore", "restaurar", "backups"):
            status_msg = interactive_restore_menu()
        elif low in ("r", "random", "aleatorio", "aleatório"):
            pool = filtered if filtered else themes
            chosen = random.choice(pool)
            ok, _, err = apply_theme(chosen)
            status_msg = (
                f"{GREEN}{BOLD}🎲 Tema #{chosen['id']:02d} ({chosen['name']}) aplicado!{RESET}"
                if ok
                else failure_msg(err)
            )
        elif low in CATEGORY_SHORTCUTS:
            filter_query = CATEGORY_SHORTCUTS[low]
            page = 0
            status_msg = f"{CYAN}Filtrando categoria: {filter_query}{RESET}"
        elif low == "len" or low.startswith("len "):
            parts = low.split()
            if len(parts) == 2 and parts[1].isdigit():
                new_len = int(parts[1])
                if 4 <= new_len <= 60:
                    active_t = get_theme_by_id(cur_id) or themes[0]
                    ok, _, err = apply_theme(active_t, tab_title_len=new_len)
                    status_msg = (
                        f"{GREEN}{BOLD}✔ Largura das abas ajustada para {new_len} caracteres!{RESET}"
                        if ok
                        else failure_msg(err)
                    )
                else:
                    status_msg = f"{RED}Informe um tamanho entre 4 e 60 (ex: len 15).{RESET}"
            else:
                status_msg = f"{YELLOW}Uso: digite 'len 12' ou 'len 15' (entre 4 e 60).{RESET}"
        elif ans.startswith("/"):
            filter_query = ans[1:].strip()
            page = 0
            status_msg = f"{CYAN}Buscando por: '{filter_query}'{RESET}"
        elif low.isdigit():
            tid = int(low)
            chosen = get_theme_by_id(tid)
            if chosen:
                ok, bkp, err = apply_theme(chosen)
                if not ok:
                    status_msg = failure_msg(err)
                else:
                    bkp_note = f" {DIM}(Backup: {bkp.name}){RESET}" if bkp else ""
                    status_msg = f"{GREEN}{BOLD}✔ Tema #{tid:02d} ({chosen['name']}) aplicado!{RESET}{bkp_note}"
            else:
                status_msg = f"{RED}Número inválido ({tid}). Digite de 1 a 60.{RESET}"
        else:
            chosen = find_theme(ans, min_substr_len=3)
            if chosen:
                ok, bkp, err = apply_theme(chosen)
                if not ok:
                    status_msg = failure_msg(err)
                else:
                    bkp_note = f" {DIM}(Backup: {bkp.name}){RESET}" if bkp else ""
                    status_msg = f"{GREEN}{BOLD}✔ Tema #{chosen['id']:02d} ({chosen['name']}) aplicado!{RESET}{bkp_note}"
            else:
                filter_query = ans
                page = 0
                status_msg = f"{CYAN}Filtrando temas por: '{filter_query}'{RESET}"
