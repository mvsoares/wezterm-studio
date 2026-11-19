# ✦ WezTerm Studio (`wezterm-theme`)

**WezTerm Studio** (`wezterm-theme`) é um pacote multiplataforma unificado (**Linux, macOS e Windows**) para personalizar o **WezTerm** em tempo real — sem precisar reiniciar o terminal e com **zero dependências externas** (100% biblioteca padrão do Python `>= 3.10`).

Reúne em uma única ferramenta (CLI + TUI TrueColor + Estúdio Web Local):
- 🎨 **60 Temas de Barra de Abas** (Google/Material, Powerline, Paletas Dev, Cyberpunk/Neon, Natureza, Minimalistas/CRT) com contraste WCAG verificado e largura uniforme configurável (`TAB_TITLE_LEN`).
- ✦ **Construtor de Símbolos das Abas** (prefixos, marcadores adaptativos por cor da aba via CIE LCh, setas Powerline e indicadores de aba inativa).
- 🔤 **Seletor Ao Vivo de Fontes & Nerd Fonts** (detecta automaticamente fontes monoespaçadas e Nerd Fonts via `wezterm ls-fonts`, FontConfig, CoreText e DirectWrite, separando fonte/peso/tamanho do **Editor** e das **Abas**).
- 🖥 **1.100+ Color Schemes, Janela & Cursor** (opacidade, padding, decorações, cursor, line-height e scrollback).
- 💾 **Backups Instantâneos & Presets JSON Portáteis** (compartilháveis entre Linux, macOS e Windows, com validação prévia no `wezterm` e rollback automático).

---

## 📦 Instalação

### Direto do GitHub (`pipx`, `uv` ou `pip`)
```bash
# Via pipx (recomendado)
pipx install git+https://github.com/mvsoares/wezterm-studio.git

# Ou via uv
uv tool install git+https://github.com/mvsoares/wezterm-studio.git

# Ou via pip
pip install --user git+https://github.com/mvsoares/wezterm-studio.git
```

### Instalação Local (Clone / Desenvolvimento)
```bash
git clone https://github.com/mvsoares/wezterm-studio.git
cd wezterm-studio
pipx install -e . --force
# ou: pip install --user -e .
```

Os comandos `wezterm-theme` (principal) e `wezterm-tab-theme` (alias de compatibilidade) ficam disponíveis no `PATH`.

---

## 🚀 Como Usar

### 1. Integrar ao seu `wezterm.lua` (apenas 1 vez)
Funciona em qualquer configuração padrão do WezTerm no Linux, macOS ou Windows (`~/.config/wezterm/wezterm.lua`, `~/.wezterm.lua` ou `$WEZTERM_CONFIG_FILE`):

```bash
wezterm-theme init
```

> **Nota:** Se você já utiliza o loader modular (`defaults.json` + `settings.json`), o `wezterm-theme` detecta automaticamente o modo **Loader** e sincroniza tudo nativamente com `settings.json` e `tab_theme.lua`.

### 2. Abrir o WezTerm Studio no Navegador (Fontes + Abas + Cores ao vivo)
```bash
wezterm-theme web
# ou: wezterm-theme -w
```
Abre `http://127.0.0.1:7777` com pré-visualização real das fontes instaladas, ligaduras (`!= === >= =>`), ícones Nerd Font, 60 temas de abas, construtor de símbolos e aplicação em 1 clique direto no WezTerm.

### 3. Usar o Seletor Interativo TrueColor no Terminal (TUI)
```bash
wezterm-theme
```
Atalhos dentro da TUI:
- `[1-60]` — Aplicar tema de abas imediatamente
- `[s]` — Construtor interativo de símbolos das abas
- `[f]` — Seletor interativo de fontes e tamanhos (Editor + Abas)
- `[cs]` — Seletor de Color Scheme do terminal
- `[w]` — Iniciar o Web Studio no navegador
- `[c1]..[c6]` / `[/termo]` — Filtrar categorias ou buscar
- `[len 15]` — Ajustar largura fixa dos títulos das abas
- `[b]` / `[u]` — Criar ou restaurar backup

### 4. Comandos Diretos via CLI
```bash
# Aplicar tema por ID ou slug + largura fixa das abas
wezterm-theme -s 8 -t 15
wezterm-theme -s catppuccin-mocha

# Trocar fonte e tamanho do Editor e da Barra de Abas
wezterm-theme --font "JetBrainsMono Nerd Font" --size 13.5 --tab-font "JetBrainsMono Nerd Font" --tab-size 11.5

# Trocar Color Scheme e opacidade da janela
wezterm-theme --scheme "Catppuccin Mocha" --opacity 0.95 --padding 10

# Listar fontes, temas ou status atual
wezterm-theme fonts --nerd
wezterm-theme --list
wezterm-theme --current
wezterm-theme doctor

# Exportar / importar preset portátil entre máquinas (Linux / macOS / Windows)
wezterm-theme export meu-setup.json
wezterm-theme import meu-setup.json

# Remover hook do wezterm.lua se desejar desinstalar
wezterm-theme uninstall --purge
```

---

## 🌍 Compatibilidade Multiplataforma

| Plataforma | Descoberta de Config | Descoberta de Fontes | TUI TrueColor & UTF-8 |
| :--- | :--- | :--- | :--- |
| **Linux / BSD** | `$WEZTERM_CONFIG_FILE`, `$XDG_CONFIG_HOME/wezterm`, `~/.config/wezterm`, `~/.wezterm.lua` | `wezterm ls-fonts` + `fc-list` (binário nativo ou Flatpak) | Nativo |
| **macOS** | `$WEZTERM_CONFIG_FILE`, `~/.config/wezterm`, `~/.wezterm.lua` | `wezterm ls-fonts` (`/Applications/WezTerm.app` / Homebrew + CoreText) | Nativo |
| **Windows** | `%WEZTERM_CONFIG_FILE%`, `%USERPROFILE%\.config\wezterm`, `%USERPROFILE%\.wezterm.lua` | `wezterm.exe ls-fonts` (Program Files / Scoop / LocalAppData + DirectWrite) | UTF-8 + Console Virtual Terminal ANSI habilitados automaticamente |

---

## 🧪 Testes Automatizados

```bash
python3 -m unittest discover -s tests -v
```

## 📄 Licença

MIT License.
