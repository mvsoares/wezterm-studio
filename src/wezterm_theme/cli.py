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
    validate_config,
)
from .fonts import get_color_schemes, get_installed_fonts
from .paths import init_terminal, resolve_wezterm_paths, system_summary
from .server import DEFAULT_PORT, run_studio_server
from .themes import THEMES, filter_themes, find_theme, get_theme_by_id
from .tui import (
    BLUE,
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
    symbols_menu,
)

SUBCOMMANDS = {"init", "uninstall", "web", "studio", "tui", "fonts", "doctor", "export", "import"}


def _cmd_init(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme init", description="Integra o wezterm-theme ao seu wezterm.lua.")
    p.parse_args(argv)
    ok, msg = init_wezterm_config()
    if ok:
        print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
        return 0
    print(f"{RED}✖ {msg}{RESET}", file=sys.stderr)
    return 1


def _cmd_uninstall(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme uninstall", description="Remove o hook do wezterm-theme do wezterm.lua.")
    p.add_argument("--purge", action="store_true", help="Remove também os arquivos gerados wezterm_theme.lua e wezterm_theme.json")
    ns = p.parse_args(argv)
    ok, msg = uninstall_wezterm_config(remove_files=ns.purge)
    if ok:
        print(f"{GREEN}{BOLD}✔ {msg}{RESET}")
        return 0
    print(f"{RED}✖ {msg}{RESET}", file=sys.stderr)
    return 1


def _cmd_web(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme web", description="Inicia o WezTerm Studio interativo no navegador.")
    p.add_argument("--host", default="127.0.0.1", help="Endereço local (padrão: 127.0.0.1)")
    p.add_argument("-p", "--port", type=int, default=None, help=f"Porta HTTP (padrão: auto a partir de {DEFAULT_PORT})")
    p.add_argument("--no-open", action="store_true", help="Não abrir o navegador automaticamente")
    ns = p.parse_args(argv)
    run_studio_server(host=ns.host, port=ns.port, open_browser=not ns.no_open)
    return 0


def _cmd_tui(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme tui", description="Abre o seletor interativo TrueColor no terminal.")
    p.add_argument("-f", "--filter", default="", help="Filtro inicial de temas")
    ns = p.parse_args(argv)
    interactive_picker(THEMES, initial_filter=ns.filter)
    return 0


def _cmd_fonts(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme fonts", description="Lista ou seleciona fontes monoespaçadas e Nerd Fonts.")
    p.add_argument("-i", "--interactive", action="store_true", help="Abre o menu interativo de fontes no terminal")
    p.add_argument("--nerd", action="store_true", help="Exibe apenas Nerd Fonts")
    p.add_argument("--refresh", action="store_true", help="Recarrega cache de fontes do sistema")
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
    print(f"\n{BOLD}{CYAN}=== FONTES MONOESPAÇADAS & NERD FONTS ({len(fonts)} encontradas) ==={RESET}\n")
    for idx, f in enumerate(fonts, start=1):
        fam = f["family"]
        marks = []
        if ed and (ed == fam or ed.startswith(fam)):
            marks.append(f"{CYAN}{BOLD}← EDITOR{RESET}")
        if tb and (tb == fam or tb.startswith(fam)):
            marks.append(f"{MAGENTA}{BOLD}← ABAS{RESET}")
        nf = f"{GREEN}[NF]{RESET} " if f.get("is_nerd") else "     "
        print(f"  {BOLD}[{idx:02d}]{RESET} {nf}{BOLD}{fam:<32}{RESET} {DIM}{f['desc']}{RESET} {' '.join(marks)}")
    print(f"\nUse: {BOLD}wezterm-theme --font \"<Familia>\" --size 14{RESET} ou {BOLD}wezterm-theme fonts -i{RESET}\n")
    return 0


def _cmd_doctor(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme doctor", description="Verifica diagnóstico multiplataforma e saúde da configuração WezTerm.")
    p.parse_args(argv)
    info = system_summary()
    state = get_current_state()
    paths = resolve_wezterm_paths()
    fonts = get_installed_fonts()

    print(f"\n{BOLD}{CYAN}=== WEZTERM STUDIO DOCTOR (v{__version__}) ==={RESET}\n")
    print(f"  {BOLD}Sistema Operacional:{RESET}  {info['os']}")
    print(f"  {BOLD}Python:{RESET}               {info['python']}")
    print(f"  {BOLD}Binário WezTerm:{RESET}      {info['wezterm_cmd']}")
    print(f"  {BOLD}Modo de Config:{RESET}       {state['mode']} ({'integrado' if state['initialized'] else 'aguardando init'})")
    print(f"  {BOLD}Arquivo wezterm.lua:{RESET}  {info['config_file']}")
    print(f"  {BOLD}Módulo de Tema:{RESET}       {state['active_module']}")
    print(f"  {BOLD}Cache Dir:{RESET}            {info['cache_dir']}")
    print(f"  {BOLD}Fontes Detectadas:{RESET}    {len(fonts)} monoespaçadas/Nerd Fonts")

    cfg_path = paths["config_file"]
    if cfg_path.is_file():
        err = validate_config(cfg_path)
        if err:
            print(f"\n  {RED}{BOLD}✖ Validação WezTerm falhou:{RESET}\n  {err}\n")
            return 1
        print(f"\n  {GREEN}{BOLD}✔ wezterm.lua validado sem erros!{RESET}\n")
    else:
        print(f"\n  {YELLOW}ℹ {cfg_path} ainda não existe. Execute 'wezterm-theme init' para criar.{RESET}\n")
    return 0


def _cmd_export(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme export", description="Exporta preset JSON portátil (Linux, macOS, Windows).")
    p.add_argument("output", nargs="?", help="Arquivo .json de destino (ou stdout se omitido)")
    ns = p.parse_args(argv)
    dest = Path(ns.output).expanduser() if ns.output else None
    raw, written = export_preset(dest_path=dest)
    if written:
        print(f"{GREEN}{BOLD}✔ Preset exportado para:{RESET} {written}")
    else:
        print(raw, end="")
    return 0


def _cmd_import(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="wezterm-theme import", description="Importa e aplica um preset JSON portátil.")
    p.add_argument("source", help="Caminho para o arquivo .json de preset")
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

    print(f"\n{BOLD}{CYAN}=== CONFIGURAÇÃO ATUAL DO WEZTERM STUDIO ==={RESET}\n")
    print(f"  {BOLD}Modo:{RESET}          {st['mode']} ({st['platform']})")
    print(f"  {BOLD}Config:{RESET}        {st['config_file']}")
    if t:
        print(f"  {BOLD}Tema de Abas:{RESET}  {GREEN}#{tid:02d} — {t['name']}{RESET} {DIM}[{t['category']} · {tlen}c]{RESET}")
        if syms:
            print(f"  {BOLD}Símbolos:{RESET}      {MAGENTA}Personalizados{RESET} {DIM}{syms}{RESET}")
        from .themes import apply_symbols as _as
        print(f"  {BOLD}Prévia:{RESET}        {render_4_tab_bar(_as(t, syms), tab_title_len=tlen)}")
    print(
        f"  {BOLD}Fonte Editor:{RESET}  {app.get('font_family') or 'Padrão WezTerm'} "
        f"({app.get('font_weight') or 'Regular'}, {app.get('font_size') or 13}pt)"
    )
    print(
        f"  {BOLD}Fonte Abas:{RESET}    {app.get('tab_font_family') or 'Padrão WezTerm'} "
        f"({app.get('tab_font_weight') or 'Medium'}, {app.get('tab_font_size') or 11}pt)"
    )
    print(f"  {BOLD}Color Scheme:{RESET}  {app.get('color_scheme') or 'Padrão'}")
    if not st["initialized"]:
        print(f"\n  {YELLOW}⚡ Dica: execute 'wezterm-theme init' para conectar ao seu wezterm.lua.{RESET}")
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
        if sub == "export":
            return _cmd_export(rest)
        if sub == "import":
            return _cmd_import(rest)

    parser = argparse.ArgumentParser(
        prog="wezterm-theme",
        description=(
            "WezTerm Studio — Seletor multiplataforma de Temas de Abas (60), Símbolos, "
            "Fontes & Aparência para WezTerm.\n\n"
            "Subcomandos disponíveis:\n"
            "  init        Conecta o wezterm-theme ao seu wezterm.lua\n"
            "  web         Abre o WezTerm Studio completo no navegador (fontes + abas + cores)\n"
            "  tui         Abre o seletor interativo TrueColor no terminal (padrão sem argumentos)\n"
            "  fonts       Lista ou escolhe fontes monoespaçadas e Nerd Fonts\n"
            "  doctor      Verifica diagnóstico do WezTerm no Linux, macOS ou Windows\n"
            "  export      Exporta preset JSON portátil\n"
            "  import      Importa preset JSON portátil\n"
            "  uninstall   Remove a integração do wezterm.lua"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    # Tab theme flags
    parser.add_argument("-l", "--list", action="store_true", help="Lista os 60 temas de abas no terminal")
    parser.add_argument("-s", "--set", metavar="TEMA", help="Aplica um tema por ID (1-60), slug ou nome")
    parser.add_argument("-t", "--tab-len", type=int, metavar="CHARS", help="Largura fixa do título das abas (4-60)")
    parser.add_argument("-r", "--random", action="store_true", help="Aplica um tema de abas aleatório")
    parser.add_argument("-c", "--current", action="store_true", help="Mostra tema, símbolos, fontes e cores ativos")
    parser.add_argument("-f", "--filter", default="", help="Filtra temas no modo lista (-l) ou TUI")
    parser.add_argument("--symbols", action="store_true", help="Abre a tela interativa de símbolos das abas")
    parser.add_argument("--reset-symbols", action="store_true", help="Restaura os símbolos nativos do tema ativo")

    # Font & Appearance flags
    parser.add_argument("--font", metavar="FAMILIA", help="Define a fonte principal do terminal (Editor)")
    parser.add_argument("--weight", metavar="PESO", help="Peso da fonte do Editor (ex: Regular, Medium, Bold)")
    parser.add_argument("--size", type=float, metavar="PT", help="Tamanho da fonte do Editor em pontos (ex: 13.5)")
    parser.add_argument("--tab-font", metavar="FAMILIA", help="Define a fonte da barra de abas")
    parser.add_argument("--tab-weight", metavar="PESO", help="Peso da fonte da barra de abas (ex: Medium, Bold)")
    parser.add_argument("--tab-size", type=float, metavar="PT", help="Tamanho da fonte da barra de abas (ex: 11.5)")
    parser.add_argument("--scheme", metavar="NOME", help="Define o Color Scheme do WezTerm (ex: 'Catppuccin Mocha')")
    parser.add_argument("--opacity", type=float, metavar="0.2-1.0", help="Opacidade da janela (ex: 0.92)")
    parser.add_argument("--padding", type=int, metavar="PX", help="Padding interno da janela em pixels (ex: 10)")
    parser.add_argument("--list-fonts", action="store_true", help="Lista todas as fontes monoespaçadas e Nerd Fonts instaladas")
    parser.add_argument("--list-schemes", action="store_true", help="Lista os esquemas de cores integrados do WezTerm")

    # Web Studio flags
    parser.add_argument("-w", "--web", action="store_true", help="Inicia o WezTerm Studio interativo no navegador")
    parser.add_argument("--port", type=int, default=None, help=f"Porta para o modo --web (padrão: {DEFAULT_PORT})")
    parser.add_argument("--no-open", action="store_true", help="Não abrir navegador automaticamente no modo --web")

    # Backups
    parser.add_argument("--backups", action="store_true", help="Lista os backups disponíveis")
    parser.add_argument("-b", "--backup", nargs="?", const="manual", metavar="NOME", help="Cria um backup instantâneo")
    parser.add_argument("-u", "--restore", nargs="?", const="__INTERACTIVE__", metavar="ID_OU_NOME", help="Restaura um backup anterior")

    args = parser.parse_args(args_list)

    if args.tab_len is not None and not (4 <= args.tab_len <= 60):
        print(f"{RED}Erro: --tab-len deve estar entre 4 e 60 caracteres.{RESET}", file=sys.stderr)
        return 1
    if args.opacity is not None and not (0.1 <= args.opacity <= 1.0):
        print(f"{RED}Erro: --opacity deve estar entre 0.1 e 1.0.{RESET}", file=sys.stderr)
        return 1

    if args.backups:
        print_backups_list(get_available_backups())
        return 0

    if args.backup is not None:
        dest = backup_current_config(label=args.backup)
        if dest:
            print(f"{GREEN}{BOLD}✔ Backup salvo com sucesso:{RESET} {dest}")
            return 0
        print(f"{YELLOW}Nenhuma configuração ativa encontrada para fazer backup.{RESET}")
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
        q = args.filter.lower().strip()
        if q:
            schemes = [s for s in schemes if q in s.lower()]
        print(f"\n{BOLD}{CYAN}=== WEZTERM COLOR SCHEMES ({len(schemes)}) ==={RESET}\n")
        for s in schemes:
            print(f"  • {s}")
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
    if args.font:
        app_updates["font_family"] = args.font
    if args.weight:
        app_updates["font_weight"] = args.weight
    if args.size is not None:
        app_updates["font_size"] = args.size
    if args.tab_font:
        app_updates["tab_font_family"] = args.tab_font
    if args.tab_weight:
        app_updates["tab_font_weight"] = args.tab_weight
    if args.tab_size is not None:
        app_updates["tab_font_size"] = args.tab_size
    if args.scheme is not None:
        app_updates["color_scheme"] = args.scheme
    if args.opacity is not None:
        app_updates["window_opacity"] = args.opacity
    if args.padding is not None:
        app_updates["window_padding"] = args.padding

    mutated = False

    if args.reset_symbols:
        st = get_current_state()
        cur_t = get_theme_by_id(st["theme_id"]) or THEMES[0]
        ok, _, err = apply_theme(cur_t, tab_title_len=args.tab_len, symbols=None, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        print(f"{GREEN}{BOLD}✔ Abas de volta aos símbolos nativos do tema #{cur_t['id']:02d} ({cur_t['name']}).{RESET}")
        mutated = True
        app_updates = {}

    if args.random:
        pool = filter_themes(THEMES, args.filter) if args.filter else THEMES
        if not pool:
            print(f"{RED}Nenhum tema corresponde ao filtro '{args.filter}'.{RESET}", file=sys.stderr)
            return 1
        chosen = random.choice(pool)
        ok, _, err = apply_theme(chosen, tab_title_len=args.tab_len, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        print(f"{GREEN}{BOLD}🎲 Tema aleatório #{chosen['id']:02d} ({chosen['name']}) aplicado!{RESET}")
        mutated = True
        app_updates = {}

    if args.set:
        chosen = find_theme(args.set)
        if not chosen:
            print(f"{RED}Erro: Tema '{args.set}' não encontrado. Use --list para ver os 60 temas.{RESET}", file=sys.stderr)
            return 1
        ok, _, err = apply_theme(chosen, tab_title_len=args.tab_len, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        st_after = get_current_state()
        init_hint = "" if st_after["initialized"] else f"\n{YELLOW}⚡ Dica: execute 'wezterm-theme init' para conectar ao seu wezterm.lua.{RESET}"
        print(f"{GREEN}{BOLD}✔ Tema #{chosen['id']:02d} ({chosen['name']}) aplicado!{RESET}{init_hint}")
        mutated = True
        app_updates = {}

    if args.tab_len is not None and not args.set and not args.random and not args.reset_symbols:
        st = get_current_state()
        cur_t = get_theme_by_id(st["theme_id"]) or THEMES[0]
        ok, _, err = apply_theme(cur_t, tab_title_len=args.tab_len, appearance_updates=app_updates or None)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        print(f"{GREEN}{BOLD}✔ Largura fixa das abas ajustada para {args.tab_len} caracteres!{RESET}")
        mutated = True
        app_updates = {}

    if app_updates:
        ok, err = apply_appearance(app_updates)
        if not ok:
            print(failure_msg(err), file=sys.stderr)
            return 1
        summary_parts = [f"{k}={v}" for k, v in app_updates.items()]
        print(f"{GREEN}{BOLD}✔ Configuração aplicada:{RESET} {', '.join(summary_parts)}")
        mutated = True

    if args.web:
        run_studio_server(port=args.port, open_browser=not args.no_open)
        return 0

    if mutated:
        return 0

    interactive_picker(THEMES, initial_filter=args.filter)
    return 0
