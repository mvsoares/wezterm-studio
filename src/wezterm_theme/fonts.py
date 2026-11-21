"""Cross-platform monospace & Nerd Font discovery and WezTerm color scheme catalog.

Works across Linux (FontConfig + WezTerm), macOS (CoreText + WezTerm),
and Windows (DirectWrite + WezTerm) with zero external Python dependencies.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .paths import find_wezterm_cmd, get_cache_dir, get_platform_name

# Curated coding & Nerd Font descriptions
_FONT_META: dict[str, tuple[str, str]] = {
    "0xProto Nerd Font": ("0xType", "Designed specifically for terminals and code. Strong x-height and zero ambiguity."),
    "BlexMono Nerd Font": ("IBM", "Engineered precision from IBM Design (IBM Plex Mono). Humanist mono with strong technical character."),
    "CaskaydiaCove Nerd Font": ("Microsoft", "Cascadia Code patched with Nerd Font glyphs. Cursive italics and rich ligature set."),
    "CommitMono Nerd Font": ("Eigil Nikolajsen", "Modern neutral coding font built for focus with smart spacing and optical balance."),
    "FiraCode Nerd Font": ("Mozilla", "Pioneered programming ligatures (!= -> =>) with clean strokes and universal clarity."),
    "GeistMono Nerd Font": ("Vercel", "Vercel's sleek modern monospace companion designed for clean developer interfaces."),
    "Hack Nerd Font": ("Source Foundry", "Workhorse coding font optimised for 8–14pt with open apertures and high contrast."),
    "Inconsolata Nerd Font": ("Raph Levien", "Humanist monospace inspired by Consolas with elegant curves at compact sizes."),
    "Iosevka Nerd Font": ("Belleve Invis", "Ultra-condensed, razor-sharp coding typeface that fits maximum columns per screen."),
    "IosevkaTerm Nerd Font": ("Belleve Invis", "Terminal-optimised Iosevka variant with strict cell widths and crisp alignment."),
    "JetBrainsMono Nerd Font": ("JetBrains", "Sharp geometric mono with 139 ligatures and tall x-height for long coding sessions."),
    "JetBrainsMonoNL Nerd Font": ("JetBrains", "JetBrains Mono without ligatures — distinct operators with identical clarity."),
    "MesloLGS Nerd Font": ("André Berg", "Tight line-height Apple Monaco derivative — classic choice for Powerlevel10k."),
    "MesloLGM Nerd Font": ("André Berg", "Medium line-height Apple Monaco derivative with balanced terminal spacing."),
    "MesloLGL Nerd Font": ("André Berg", "Generous line-height Meslo variant ideal for HiDPI displays."),
    "MesloLGSDZ Nerd Font": ("André Berg", "Meslo Small with dotted zero for unambiguous 0/O distinction."),
    "MesloLGMDZ Nerd Font": ("André Berg", "Meslo Medium with dotted zero variant."),
    "MesloLGLDZ Nerd Font": ("André Berg", "Meslo Large with dotted zero variant."),
    "MonaspiceNe Nerd Font": ("GitHub", "Monaspace Neon: neo-grotesque flagship coding font with texture healing."),
    "MonaspiceAr Nerd Font": ("GitHub", "Monaspace Argon: humanist fixed-width coding typeface."),
    "MonaspiceKr Nerd Font": ("GitHub", "Monaspace Krypton: mechanical slab-serif coding typeface."),
    "MonaspiceRn Nerd Font": ("GitHub", "Monaspace Radon: expressive handwriting-inspired coding typeface."),
    "MonaspiceXe Nerd Font": ("GitHub", "Monaspace Xenon: transitional serif coding typeface."),
    "RecMonoCasual Nerd Font": ("Arrow Type", "Recursive Casual — expressive strokes with relaxed baseline rhythm."),
    "RecMonoLinear Nerd Font": ("Arrow Type", "Recursive Linear — clean, distraction-free geometric monospace."),
    "RecMonoDuotone Nerd Font": ("Arrow Type", "Recursive Duotone — contrast-first design for rich syntax themes."),
    "RecMonoSmCasual Nerd Font": ("Arrow Type", "Recursive Semi-Casual — balanced personality with crisp readability."),
    "RobotoMono Nerd Font": ("Google", "Google's geometric monospace with friendly curves across all platforms."),
    "SauceCodePro Nerd Font": ("Adobe", "Adobe Source Code Pro patched with Nerd Font developer glyphs."),
    "SpaceMono Nerd Font": ("Colophon", "Distinctive editorial monospace with quirky retro-futuristic geometry."),
    "UbuntuMono Nerd Font": ("Canonical", "Canonical's warm humanist monospace with compact horizontal rhythm."),
    "VictorMono Nerd Font": ("Rune Bjørnerås", "Crisp semi-condensed monospace with signature cursive italics and ligatures."),
    "ZedMono Nerd Font": ("Zed Industries", "Clean, high-density coding font designed for Zed's minimal interface."),
    # Built-in & OS-native coding fonts (Linux / macOS / Windows)
    "JetBrains Mono": ("JetBrains · Built-in", "Bundled directly inside WezTerm on every OS — crisp geometric coding font with ligatures."),
    "Fira Code": ("Mozilla", "Classic ligature-enabled monospace font for modern code editors and terminals."),
    "Cascadia Code": ("Microsoft", "Windows Terminal's flagship monospace font with programming ligatures."),
    "Cascadia Mono": ("Microsoft", "Cascadia Code without ligatures — crisp DirectWrite/CoreText/FreeType rendering."),
    "Consolas": ("Microsoft", "Classic Windows ClearType humanist monospace engineered for source code."),
    "SF Mono": ("Apple", "Apple's clean neo-grotesque monospace built for Xcode and macOS Terminal."),
    "Menlo": ("Apple", "Refined macOS monospace typeface with open counters and clean punctuation."),
    "Monaco": ("Apple", "Iconic macOS terminal font with distinct characters and generous spacing."),
    "Source Code Pro": ("Adobe", "Adobe's open-source neutral monospace engineered for coding legibility."),
    "IBM Plex Mono": ("IBM", "Engineered precision from IBM Design with clear technical character."),
    "Ubuntu Mono": ("Canonical", "Warm humanist monospace with compact width and friendly curves."),
    "DejaVu Sans Mono": ("DejaVu", "Broad Unicode coverage workhorse monospace font."),
    "Liberation Mono": ("Red Hat", "Clean metric-compatible monospace font with crisp hints."),
}

_GENERIC_NERD = (
    "Nerd Font",
    "Patched with Nerd Font developer icons, powerline separators, and terminal glyphs.",
)
_GENERIC_MONO = (
    "Monospace",
    "Fixed-width monospace typeface suitable for terminals, code, and aligned tables.",
)

_NON_TEXT_FACES = {
    "symbols nerd font",
    "symbols nerd font mono",
    "noto color emoji",
    "apple color emoji",
    "segoe ui emoji",
    "segoe ui symbol",
    "twemoji mozilla",
    "emoji one",
    "lastresort",
    "wezterm symbol",
}

_MONO_KEYWORDS = (
    "mono",
    "nerd font",
    "code",
    "console",
    "consolas",
    "menlo",
    "monaco",
    "courier",
    "iosevka",
    "hack",
    "inconsolata",
    "cascadia",
    "caskaydia",
    "fira",
    "term",
    "fixed",
    "pragmata",
    "berkeley",
    "zed",
    "geist",
    "0xproto",
    "meslo",
    "jetbrains",
    "fantasque",
    "agave",
    "mononoki",
    "anonymice",
    "hurmit",
    "lekton",
    "lilex",
    "martian",
    "monoid",
    "profont",
    "proggy",
    "monaspice",
    "monaspace",
    "blex",
    "saucecode",
)

_WEIGHT_ORDER = [
    "Thin",
    "ExtraLight",
    "Light",
    "DemiLight",
    "Book",
    "Regular",
    "Medium",
    "DemiBold",
    "Bold",
    "ExtraBold",
    "Black",
    "ExtraBlack",
]

_FALLBACK_SCHEME_PALETTES: list[dict[str, Any]] = [
    {"name": "Builtin Dark", "bg": "#000000", "fg": "#bbbbbb", "cursor": "#bbbbbb", "ansi": ["#000000", "#bb0000", "#00bb00", "#bbbb00", "#0000bb", "#bb00bb", "#00bbbb", "#bbbbbb"], "is_dark": True},
    {"name": "Catppuccin Frappe", "bg": "#303446", "fg": "#c6d0f5", "cursor": "#f2d5cf", "ansi": ["#51576d", "#e78284", "#a6d189", "#e5c890", "#8caaee", "#f4b8e4", "#81c8be", "#b5bfe2"], "is_dark": True},
    {"name": "Catppuccin Latte", "bg": "#eff1f5", "fg": "#4c4f69", "cursor": "#dc8a78", "ansi": ["#5c5f77", "#d20f39", "#40a02b", "#df8e1d", "#1e66f5", "#ea76cb", "#179299", "#acb0be"], "is_dark": False},
    {"name": "Catppuccin Macchiato", "bg": "#24273a", "fg": "#cad3f5", "cursor": "#f4dbd6", "ansi": ["#494d64", "#ed8796", "#a6da95", "#eed49f", "#8aadf4", "#f5bde6", "#8bd5ca", "#b8c0e0"], "is_dark": True},
    {"name": "Catppuccin Mocha", "bg": "#1e1e2e", "fg": "#cdd6f4", "cursor": "#f5e0dc", "ansi": ["#45475a", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#bac2de"], "is_dark": True},
    {"name": "Cyberdyne", "bg": "#151144", "fg": "#00ff92", "cursor": "#00ff9c", "ansi": ["#080808", "#ff8373", "#00c172", "#d2a700", "#0071cf", "#ff90fe", "#6bffdd", "#f1f1f1"], "is_dark": True},
    {"name": "Dracula", "bg": "#282a36", "fg": "#f8f8f2", "cursor": "#f8f8f2", "ansi": ["#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9", "#ff79c6", "#8be9fd", "#f8f8f2"], "is_dark": True},
    {"name": "Dracula (Official)", "bg": "#282a36", "fg": "#f8f8f2", "cursor": "#f8f8f2", "ansi": ["#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9", "#ff79c6", "#8be9fd", "#f8f8f2"], "is_dark": True},
    {"name": "Everforest Dark (Gogh)", "bg": "#2d353b", "fg": "#d3c6aa", "cursor": "#d3c6aa", "ansi": ["#475258", "#e67e80", "#a7c080", "#dbbc7f", "#7fbbb3", "#d699b6", "#83c092", "#d3c6aa"], "is_dark": True},
    {"name": "GitHub Dark", "bg": "#101216", "fg": "#8b949e", "cursor": "#c9d1d9", "ansi": ["#000000", "#f78166", "#56d364", "#e3b341", "#6ca4f8", "#db61a2", "#2b7489", "#ffffff"], "is_dark": True},
    {"name": "Gruvbox Dark (Gogh)", "bg": "#282828", "fg": "#ebdbb2", "cursor": "#ebdbb2", "ansi": ["#282828", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286", "#689d6a", "#a89984"], "is_dark": True},
    {"name": "Gruvbox Light", "bg": "#fbf1c7", "fg": "#3c3836", "cursor": "#3c3836", "ansi": ["#fbf1c7", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286", "#689d6a", "#7c6f64"], "is_dark": False},
    {"name": "Gruvbox Material (Gogh)", "bg": "#282828", "fg": "#d4be98", "cursor": "#d4be98", "ansi": ["#3c3836", "#ea6962", "#a9b665", "#d8a657", "#7daea3", "#d3869b", "#89b482", "#d4be98"], "is_dark": True},
    {"name": "Kanagawa (Gogh)", "bg": "#1f1f28", "fg": "#dcd7ba", "cursor": "#c8c093", "ansi": ["#090618", "#c34043", "#76946a", "#c0a36e", "#7e9cd8", "#957fb8", "#6a9589", "#c8c093"], "is_dark": True},
    {"name": "Material Darker (base16)", "bg": "#212121", "fg": "#eeffff", "cursor": "#eeffff", "ansi": ["#212121", "#f07178", "#c3e88d", "#ffcb6b", "#82aaff", "#c792ea", "#89ddff", "#eeffff"], "is_dark": True},
    {"name": "Monokai Pro (Gogh)", "bg": "#2d2a2e", "fg": "#fcfcfa", "cursor": "#fcfcfa", "ansi": ["#403e41", "#ff6188", "#a9dc76", "#ffd866", "#fc9867", "#ab9df2", "#78dce8", "#fcfcfa"], "is_dark": True},
    {"name": "Monokai Remastered", "bg": "#0c0c0c", "fg": "#d9d9d9", "cursor": "#fc971f", "ansi": ["#1a1a1a", "#f4005f", "#98e024", "#fd971f", "#9d65ff", "#f4005f", "#58d1eb", "#c4c5b5"], "is_dark": True},
    {"name": "Monokai Vivid", "bg": "#121212", "fg": "#f9f9f9", "cursor": "#fb0007", "ansi": ["#121212", "#fa2934", "#98e123", "#fff30a", "#0443ff", "#f800f8", "#01b6ed", "#ffffff"], "is_dark": True},
    {"name": "Nightfox", "bg": "#192330", "fg": "#cdcecf", "cursor": "#cdcecf", "ansi": ["#393b44", "#c94f6d", "#81b29a", "#dbc074", "#719cd6", "#9d79d6", "#63cdcf", "#dfdfe0"], "is_dark": True},
    {"name": "Nord", "bg": "#2e3440", "fg": "#d8dee9", "cursor": "#d8dee9", "ansi": ["#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#88c0d0", "#e5e9f0"], "is_dark": True},
    {"name": "OneDark (base16)", "bg": "#282c34", "fg": "#abb2bf", "cursor": "#abb2bf", "ansi": ["#282c34", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#abb2bf"], "is_dark": True},
    {"name": "Outrun Dark (base16)", "bg": "#00002a", "fg": "#d0d0fa", "cursor": "#d0d0fa", "ansi": ["#00002a", "#ff4242", "#59f176", "#f3e877", "#66b0ff", "#f10596", "#0ef0f0", "#d0d0fa"], "is_dark": True},
    {"name": "Poimandres", "bg": "#1b1e28", "fg": "#a6accd", "cursor": "#a6accd", "ansi": ["#1b1e28", "#d0679d", "#5de4c7", "#fffac2", "#89ddff", "#fcc5e9", "#add7ff", "#ffffff"], "is_dark": True},
    {"name": "rose-pine", "bg": "#191724", "fg": "#e0def4", "cursor": "#e0def4", "ansi": ["#26233a", "#eb6f92", "#31748f", "#f6c177", "#9ccfd8", "#c4a7e7", "#ebbcba", "#e0def4"], "is_dark": True},
    {"name": "rose-pine-dawn", "bg": "#faf4ed", "fg": "#575279", "cursor": "#575279", "ansi": ["#f2e9e1", "#b4637a", "#286983", "#ea9d34", "#56949f", "#907aa9", "#d7827e", "#575279"], "is_dark": False},
    {"name": "rose-pine-moon", "bg": "#232136", "fg": "#e0def4", "cursor": "#e0def4", "ansi": ["#393552", "#eb6f92", "#3e8fb0", "#f6c177", "#9ccfd8", "#c4a7e7", "#ea9a97", "#e0def4"], "is_dark": True},
    {"name": "Solarized Dark (Gogh)", "bg": "#002b36", "fg": "#839496", "cursor": "#839496", "ansi": ["#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5"], "is_dark": True},
    {"name": "Solarized Light (Gogh)", "bg": "#fdf6e3", "fg": "#657b83", "cursor": "#657b83", "ansi": ["#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5"], "is_dark": False},
    {"name": "SynthWave 84", "bg": "#262335", "fg": "#ffffff", "cursor": "#ff7edb", "ansi": ["#262335", "#fe4450", "#72f1b8", "#fede5d", "#03edf9", "#ff7edb", "#03edf9", "#ffffff"], "is_dark": True},
    {"name": "Tokyo Night", "bg": "#1a1b26", "fg": "#c0caf5", "cursor": "#c0caf5", "ansi": ["#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#a9b1d6"], "is_dark": True},
    {"name": "Tokyo Night Day", "bg": "#e1e2e7", "fg": "#3760bf", "cursor": "#3760bf", "ansi": ["#e9e9ed", "#f52a65", "#587539", "#8c6c3e", "#2e7de9", "#9854f1", "#007197", "#6172b0"], "is_dark": False},
    {"name": "Tokyo Night Moon", "bg": "#222436", "fg": "#c8d3f5", "cursor": "#c8d3f5", "ansi": ["#1b1d2b", "#ff757f", "#c3e88d", "#ffc777", "#82aaff", "#c099ff", "#86e1fc", "#828bb8"], "is_dark": True},
    {"name": "Tokyo Night Storm", "bg": "#24283b", "fg": "#c0caf5", "cursor": "#c0caf5", "ansi": ["#1d202f", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#a9b1d6"], "is_dark": True},
]

_FONTS_CACHE: list[dict[str, Any]] | None = None
_SCHEMES_CACHE: list[str] | None = None
_SCHEME_PALETTES_CACHE: list[dict[str, Any]] | None = None

_WEZTERM_FONT_RE = re.compile(
    r'wezterm\.font\(\s*"([^"]+)"(?:\s*,\s*\{([^}]*)\})?\s*\)'
)
_WEIGHT_ATTR_RE = re.compile(r'weight\s*=\s*"([^"]+)"')


def _is_excluded_face(family: str) -> bool:
    low = family.strip().lower()
    if not low or low.startswith("."):
        return True
    if low in _NON_TEXT_FACES:
        return True
    if "emoji" in low or "dingbats" in low or "wingdings" in low or "webdings" in low:
        return True
    if low.startswith("font awesome") or low.startswith("material icons"):
        return True
    return False


def _looks_monospace(family: str) -> bool:
    if family in _FONT_META:
        return True
    low = family.lower()
    if any(bad in low for bad in ("sans", "serif", "roman", "display", "script", "hand")) and "mono" not in low and "code" not in low and "nerd font" not in low:
        return False
    return any(kw in low for kw in _MONO_KEYWORDS)


def _fc_list_mono_families() -> set[str] | None:
    """Return exact FontConfig monospace + Nerd Font families on Linux/BSD, or None if unavailable."""
    if not shutil.which("fc-list"):
        return None
    families: set[str] = set()
    try:
        out_mono = subprocess.check_output(
            ["fc-list", ":spacing=mono", "--format=%{family}\n"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=6,
        )
        for line in out_mono.splitlines():
            fam = line.split(",")[0].strip()
            if fam and not _is_excluded_face(fam):
                families.add(fam)

        out_all = subprocess.check_output(
            ["fc-list", "--format=%{family}\n"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=6,
        )
        for line in out_all.splitlines():
            fam = line.split(",")[0].strip()
            if fam and "Nerd Font" in fam and not _is_excluded_face(fam):
                families.add(fam)
    except Exception:
        return families or None
    return families


def _wezterm_list_fonts() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Run `wezterm ls-fonts --list-system` and return (builtin_fams, system_fams, weights_by_fam)."""
    builtin_fams: set[str] = set()
    system_fams: set[str] = set()
    weights: dict[str, set[str]] = {}

    wz = find_wezterm_cmd()
    if not wz:
        return builtin_fams, system_fams, weights

    try:
        res = subprocess.run(
            [*wz, "--skip-config", "ls-fonts", "--list-system"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        section = "builtin"
        for line in res.stdout.splitlines():
            if "system fonts found" in line:
                section = "system"
                continue
            m = _WEZTERM_FONT_RE.search(line)
            if not m:
                continue
            fam = m.group(1).strip()
            if _is_excluded_face(fam):
                continue
            attrs = m.group(2) or ""
            wm = _WEIGHT_ATTR_RE.search(attrs)
            w = wm.group(1) if wm else "Regular"
            weights.setdefault(fam, set()).add(w)
            if section == "builtin":
                builtin_fams.add(fam)
            else:
                system_fams.add(fam)
    except Exception:
        pass

    return builtin_fams, system_fams, weights


def _os_fallback_families() -> set[str]:
    """Fallback coding font defaults when neither `wezterm` nor `fc-list` is available."""
    plat = get_platform_name()
    defaults = {"JetBrains Mono"}
    if plat == "macos":
        defaults.update({"SF Mono", "Menlo", "Monaco", "Courier New"})
    elif plat == "windows":
        defaults.update({"Cascadia Code", "Cascadia Mono", "Consolas", "Courier New"})
    else:
        defaults.update({"DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono"})
    return defaults


def _sort_weights(wset: set[str]) -> list[str]:
    if not wset:
        return ["Regular", "Medium", "Bold"]
    rank = {name: idx for idx, name in enumerate(_WEIGHT_ORDER)}
    return sorted(wset, key=lambda w: (rank.get(w, 99), w))


def _meta_for_family(family: str) -> tuple[str, str]:
    if family in _FONT_META:
        return _FONT_META[family]
    base_no_mono = family.removesuffix(" Mono").removesuffix(" Propo")
    if base_no_mono in _FONT_META:
        return _FONT_META[base_no_mono]
    if "Nerd Font" in family:
        return _GENERIC_NERD
    return _GENERIC_MONO


def get_installed_fonts(refresh: bool = False) -> list[dict[str, Any]]:
    """Return installed monospace & Nerd Font families across Linux, macOS, and Windows."""
    global _FONTS_CACHE
    if _FONTS_CACHE is not None and not refresh:
        return _FONTS_CACHE

    builtin_fams, system_fams, weights_map = _wezterm_list_fonts()
    fc_fams = _fc_list_mono_families()

    candidates: set[str] = {"JetBrains Mono"}
    candidates.update(builtin_fams)

    if fc_fams is not None:
        # Authoritative monospace + Nerd Font set from FontConfig
        candidates.update(fc_fams)
        # Also include any built-in/custom font_dirs families discovered by WezTerm
        for fam in system_fams:
            if "Nerd Font" in fam:
                candidates.add(fam)
    elif system_fams:
        # macOS (CoreText) / Windows (DirectWrite): filter WezTerm system list to monospace/coding faces
        for fam in system_fams:
            if _looks_monospace(fam):
                candidates.add(fam)
    else:
        candidates.update(_os_fallback_families())

    # Deduplicate "... Nerd Font Mono" / "... Nerd Font Propo" ONLY when the base "... Nerd Font"
    # family is also present, so users who only installed the Mono variant still see their font!
    filtered_fams: list[str] = []
    for fam in candidates:
        if _is_excluded_face(fam):
            continue
        if fam.endswith(" Nerd Font Mono"):
            base = fam.removesuffix(" Mono")
            if base in candidates:
                # Merge weights into base entry
                if fam in weights_map:
                    weights_map.setdefault(base, set()).update(weights_map[fam])
                continue
        if fam.endswith(" Nerd Font Propo"):
            base = fam.removesuffix(" Propo")
            if base in candidates:
                continue
        filtered_fams.append(fam)

    fonts: list[dict[str, Any]] = []
    for fam in sorted(filtered_fams, key=str.casefold):
        author, text = _meta_for_family(fam)
        fonts.append({
            "name": fam,
            "family": fam,
            "desc": author,
            "text": text,
            "weights": _sort_weights(weights_map.get(fam, set())),
            "is_nerd": ("Nerd Font" in fam) or fam.endswith(" NF"),
        })

    _FONTS_CACHE = fonts
    return fonts


_SCHEME_PALETTE_LUA_TEMPLATE = """\
local wezterm = require 'wezterm'
local config = wezterm.config_builder()
local function norm(c, fb)
  local ok, col = pcall(wezterm.color.parse, c or fb)
  if not ok or not col then return fb end
  local r, g, b, _ = col:srgba_u8()
  return string.format('#%02x%02x%02x', r, g, b)
end
local out = {}
for name, s in pairs(wezterm.color.get_builtin_schemes()) do
  local bg = norm(s.background, '#000000')
  local fg = norm(s.foreground, '#ffffff')
  local cur = norm(s.cursor_bg or s.foreground, fg)
  local ansi = {}
  for i = 1, 8 do
    ansi[i] = norm(s.ansi and s.ansi[i], '#888888')
  end
  table.insert(out, {
    name = name,
    bg = bg,
    fg = fg,
    cursor = cur,
    ansi = ansi,
  })
end
table.sort(out, function(a, b) return a.name < b.name end)
local encode = (wezterm.serde and wezterm.serde.json_encode) or wezterm.json_encode
local f = io.open([==[__JSON_PATH__]==], 'w')
if f then
  f:write(encode(out))
  f:close()
end
return config
"""


def _hex_is_dark(hex_color: str) -> bool:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return True
    try:
        c = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        return lum < 0.35
    except ValueError:
        return True


def _wezterm_version() -> str | None:
    """Return `wezterm --version` output (e.g. 'wezterm 20240203-110809-5046fc22'), or None."""
    wz = find_wezterm_cmd()
    if not wz:
        return None
    try:
        res = subprocess.run([*wz, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return res.stdout.strip() or None


def get_color_scheme_palettes(refresh: bool = False) -> list[dict[str, Any]]:
    """Return WezTerm built-in color schemes with normalized hex swatches and dark/light flag.

    The disk cache is tagged with the WezTerm version, so upgrading WezTerm (which can add
    or rename built-in schemes) refreshes it automatically.
    """
    global _SCHEME_PALETTES_CACHE, _SCHEMES_CACHE
    if _SCHEME_PALETTES_CACHE is not None and not refresh:
        return _SCHEME_PALETTES_CACHE

    cache_file = get_cache_dir() / "color_scheme_palettes.json"
    current_version = _wezterm_version()
    if not refresh and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                cached_version, cached_list = cached.get("wezterm_version"), cached.get("palettes")
            else:
                # Caches written before version tagging are a bare list.
                cached_version, cached_list = None, cached
            usable = (
                isinstance(cached_list, list)
                and len(cached_list) > 50
                and isinstance(cached_list[0], dict)
                and "bg" in cached_list[0]
            )
            # Without a wezterm binary the cache cannot be rebuilt, so keep using it.
            if usable and (current_version is None or cached_version == current_version):
                _SCHEME_PALETTES_CACHE = cached_list
                _SCHEMES_CACHE = [item["name"] for item in cached_list]
                return cached_list
        except Exception:
            pass

    wz = find_wezterm_cmd()
    palettes: list[dict[str, Any]] = []
    if wz:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                lua_path = Path(tmp) / "dump_palettes.lua"
                json_path = Path(tmp) / "palettes.json"
                lua_path.write_text(
                    _SCHEME_PALETTE_LUA_TEMPLATE.replace("__JSON_PATH__", json_path.as_posix()),
                    encoding="utf-8",
                )
                subprocess.run(
                    [*wz, "--config-file", str(lua_path), "ls-fonts", "--text", "a"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
                if json_path.is_file():
                    raw_list = json.loads(json_path.read_text(encoding="utf-8"))
                    if isinstance(raw_list, list):
                        for entry in raw_list:
                            if isinstance(entry, dict) and entry.get("name"):
                                bg = str(entry.get("bg") or "#000000")
                                palettes.append({
                                    "name": str(entry["name"]),
                                    "bg": bg,
                                    "fg": str(entry.get("fg") or "#ffffff"),
                                    "cursor": str(entry.get("cursor") or "#ffffff"),
                                    "ansi": list(entry.get("ansi") or [])[:8],
                                    "is_dark": _hex_is_dark(bg),
                                })
        except Exception:
            palettes = []

    if not palettes:
        palettes = [dict(p) for p in _FALLBACK_SCHEME_PALETTES]

    if len(palettes) > 50:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"wezterm_version": current_version, "palettes": palettes}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    _SCHEME_PALETTES_CACHE = palettes
    _SCHEMES_CACHE = [item["name"] for item in palettes]
    return palettes


def get_color_schemes(refresh: bool = False) -> list[str]:
    """Return WezTerm's built-in color scheme names (memory + disk cached for instant startup)."""
    global _SCHEMES_CACHE
    if _SCHEMES_CACHE is not None and not refresh:
        return _SCHEMES_CACHE
    palettes = get_color_scheme_palettes(refresh=refresh)
    _SCHEMES_CACHE = [p["name"] for p in palettes]
    return _SCHEMES_CACHE
