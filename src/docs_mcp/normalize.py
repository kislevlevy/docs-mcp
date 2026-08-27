"""Deterministic text and identifier normalization."""

from __future__ import annotations

import re
import unicodedata

_SPACE_RE = re.compile(r"[ \t\f\v]+")
_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)

# A conservative subset of the Adobe Symbol encoding. It is applied only when
# the originating font explicitly identifies itself as Symbol; ordinary Latin
# text is never guessed into Greek.
_SYMBOL_ASCII = str.maketrans(
    {
        "A": "Α", "B": "Β", "G": "Γ", "D": "Δ", "E": "Ε", "Z": "Ζ",
        "H": "Η", "Q": "Θ", "I": "Ι", "K": "Κ", "L": "Λ", "M": "Μ",
        "N": "Ν", "X": "Ξ", "O": "Ο", "P": "Π", "R": "Ρ", "S": "Σ",
        "T": "Τ", "U": "Υ", "F": "Φ", "C": "Χ", "Y": "Ψ", "W": "Ω",
        "a": "α", "b": "β", "g": "γ", "d": "δ", "e": "ε", "z": "ζ",
        "h": "η", "q": "θ", "i": "ι", "k": "κ", "l": "λ", "m": "μ",
        "n": "ν", "x": "ξ", "o": "ο", "p": "π", "r": "ρ", "s": "σ",
        "t": "τ", "u": "υ", "f": "φ", "c": "χ", "y": "ψ", "w": "ω",
    }
)


def normalize_text(text: str, *, preserve_layout: bool = False) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    if preserve_layout:
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def slugify(value: str, fallback: str = "untitled-section") -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = _SLUG_RE.sub("-", value).strip("-_")
    return value[:80].rstrip("-_") or fallback


def repair_symbol_text(text: str, font_name: str | None) -> str:
    if not font_name or "symbol" not in font_name.casefold():
        return normalize_text(text, preserve_layout=True)
    return normalize_text(text.translate(_SYMBOL_ASCII), preserve_layout=True)
