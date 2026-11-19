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

_FALLBACK_SCHEMES = [
    "Builtin Dark",
    "Catppuccin Frappe",
    "Catppuccin Latte",
    "Catppuccin Macchiato",
    "Catppuccin Mocha",
    "Cyberdyne",
    "Dracula",
    "Dracula (Official)",
    "Everforest Dark (Gogh)",
    "GitHub Dark",
    "Gruvbox Dark (Gogh)",
    "Gruvbox Material (Gogh)",
    "Kanagawa (Gogh)",
    "Material Darker (base16)",
    "Monokai Pro (Gogh)",
    "Monokai Remastered",
    "Monokai Vivid",
    "Nightfox",
    "Nord",
    "OneDark (base16)",
    "Outrun Dark (base16)",
    "Poimandres",
    "rose-pine",
    "rose-pine-moon",
    "Solarized Dark (Gogh)",
    "SynthWave 84",
    "Tokyo Night",
    "Tokyo Night Moon",
    "Tokyo Night Storm",
]

_FONTS_CACHE: list[dict[str, Any]] | None = None
_SCHEMES_CACHE: list[str] | None = None

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


_SCHEME_LUA_TEMPLATE = """\
local wezterm = require 'wezterm'
local config = wezterm.config_builder()
local names = {}
for name, _ in pairs(wezterm.color.get_builtin_schemes()) do
  table.insert(names, name)
end
table.sort(names)
local encode = (wezterm.serde and wezterm.serde.json_encode) or wezterm.json_encode
local f = io.open([==[__JSON_PATH__]==], 'w')
if f then
  f:write(encode(names))
  f:close()
end
return config
"""


def get_color_schemes(refresh: bool = False) -> list[str]:
    """Return WezTerm's built-in color scheme names (memory + disk cached for instant startup)."""
    global _SCHEMES_CACHE
    if _SCHEMES_CACHE is not None and not refresh:
        return _SCHEMES_CACHE

    cache_file = get_cache_dir() / "color_schemes.json"
    if not refresh and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, list) and len(cached) > 50:
                _SCHEMES_CACHE = cached
                return cached
        except Exception:
            pass

    wz = find_wezterm_cmd()
    schemes: list[str] = []
    if wz:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                lua_path = Path(tmp) / "dump_schemes.lua"
                json_path = Path(tmp) / "schemes.json"
                lua_path.write_text(
                    _SCHEME_LUA_TEMPLATE.replace("__JSON_PATH__", json_path.as_posix()),
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
                    schemes = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            schemes = []

    if not schemes:
        schemes = list(_FALLBACK_SCHEMES)

    if len(schemes) > 50:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(schemes, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    _SCHEMES_CACHE = schemes
    return schemes
