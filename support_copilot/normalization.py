"""Conservative text normalization; the source text is never overwritten."""
from dataclasses import dataclass
import unicodedata

# Provisional mapping from the challenge brief, NOT the not-yet-received data README.
KAZAKH_MAPPING_VERSION = "challenge-brief-v1-provisional"
KAZAKH_REPLACEMENTS = dict(zip("ќґѕєјўїЌҐЅЄЈЎЇ", "қғңәөұүҚҒҢӘӨҰҮ"))


@dataclass(frozen=True)
class NormalizedText:
    original: str
    text: str
    replacements: int
    mapping_version: str | None


def normalize_text(text: str, *, lang: str | None = None,
                   server_lang: str | None = None, repair_kazakh: bool = False) -> NormalizedText:
    normalized = unicodedata.normalize("NFC", text)
    count = 0
    enabled = repair_kazakh and "kk" in (lang, server_lang)
    if enabled:
        count = sum(character in KAZAKH_REPLACEMENTS for character in normalized)
        normalized = normalized.translate(str.maketrans(KAZAKH_REPLACEMENTS))
    return NormalizedText(text, normalized, count, KAZAKH_MAPPING_VERSION if enabled else None)
