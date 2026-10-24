"""Confusable skeletons and a bounded Damerau-Levenshtein (optimal string alignment) distance.

``skeleton()`` folds characters that look alike into one canonical form, so ``rnicrosoft``,
``rnicr0soft`` and ``microsoft`` (with a Cyrillic ``о``) share a skeleton. It is applied to
both the observed label and the brand label, so the mapping only needs to be consistent.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

# Single code points that render like a Latin letter (Cyrillic, Greek, IPA, fullwidth handled
# by NFKC upstream). Not exhaustive: the common homograph-attack set.
_CHAR_MAP = {
    # Cyrillic
    "а": "a",
    "в": "b",
    "с": "c",
    "ԁ": "d",
    "е": "e",
    "ё": "e",
    "һ": "h",
    "і": "i",
    "ї": "i",
    "ј": "j",
    "к": "k",
    "ӏ": "l",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "ԛ": "q",
    "г": "r",
    "ѕ": "s",
    "т": "t",
    "ц": "u",
    "џ": "u",
    "ν": "v",
    "ѵ": "v",
    "ш": "w",
    "ԝ": "w",
    "х": "x",
    "у": "y",
    "ү": "y",
    "з": "3",
    "ь": "b",
    "п": "n",
    # Greek
    "α": "a",
    "β": "b",
    "ϲ": "c",
    "ε": "e",
    "η": "n",
    "ι": "i",
    "κ": "k",
    "μ": "u",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "χ": "x",
    "γ": "y",
    "ω": "w",
    # Latin look-alikes / IPA
    "ı": "i",
    "ɩ": "i",
    "ɡ": "g",
    "ɑ": "a",
    "ʟ": "l",
    "ɴ": "n",
    "ʀ": "r",
    "ꜱ": "s",
    "ʏ": "y",
    "ł": "l",
    "ø": "o",
    "đ": "d",
    "ħ": "h",
    "ŀ": "l",
    "ƚ": "l",
    # Digits and symbols used as letters
    "0": "o",
    "1": "l",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
    "9": "g",
    "@": "a",
    "$": "s",
    "|": "l",
    "!": "l",
    "_": "",
    "-": "",
    # Letters that are visually ambiguous with each other at small sizes
    "i": "l",
}
# Multi-character sequences that read as one letter (applied after the char map).
_SEQ = (("rn", "m"), ("vv", "w"), ("cl", "d"))


@lru_cache(maxsize=8192)
def skeleton(text: str) -> str:
    """Fold *text* to its confusable skeleton (lowercase ASCII-ish)."""
    s = unicodedata.normalize("NFKD", text.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = "".join(_CHAR_MAP.get(ch, ch) for ch in s)
    for seq, rep in _SEQ:
        s = s.replace(seq, rep)
    return s


def osa_distance(a: str, b: str, limit: int = 3) -> int:
    """Optimal-string-alignment Damerau-Levenshtein distance, capped at ``limit + 1``.

    Pure Python with an early exit when a whole row exceeds ``limit``; callers prefilter by
    length so it only runs on plausible pairs.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    if _rf_osa is not None:
        d = _rf_osa(a, b, score_cutoff=limit)
        return d if d <= limit else limit + 1
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= limit else limit + 1


try:  # rapidfuzz ships with python-levenshtein (a runtime dependency); optional for speed.
    from rapidfuzz.distance.OSA import distance as _rf_osa
except Exception:  # pragma: no cover - exercised on hosts without rapidfuzz
    _rf_osa = None
