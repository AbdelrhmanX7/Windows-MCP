"""Local scam detector: brand lookalikes, risky hosts and tech-support-scam text. No network.

``check(target)`` returns a :class:`Verdict` in well under a millisecond for a warm host
(per-host results are cached). Official brand domains are fast-pathed as ``safe``.

Scoring (0-100, capped): ``>= 70`` dangerous, ``>= 35`` suspicious, otherwise ``unknown``
(or ``safe`` for an official / known site). Edit distance alone never reaches ``dangerous``;
that needs a confusable-character hit or a second signal (risky TLD, password field,
brand in the subdomain, scam text, ...).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

from windows_mcp.guard.confusables import osa_distance, skeleton
from windows_mcp.guard.domains import HostInfo, Target, parse_host, parse_target

DANGEROUS = 70
SUSPICIOUS = 35
VERDICT_RANK = {"safe": 0, "unknown": 1, "suspicious": 2, "dangerous": 3}


@dataclass
class Verdict:
    """Detector output. ``ms`` is the wall time of the local check."""

    verdict: str
    score: int
    brand: str | None = None
    reasons: list[str] = field(default_factory=list)
    ms: float = 0.0
    domain: str | None = None
    host: str | None = None
    kind: str = "url"
    official: bool = False
    gray: bool = False  # Worth a model second opinion.
    model: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ms"] = round(self.ms, 3)
        if out["model"] is None:
            out.pop("model")
        return out


def verdict_for(score: int) -> str:
    if score >= DANGEROUS:
        return "dangerous"
    if score >= SUSPICIOUS:
        return "suspicious"
    return "unknown"


# --------------------------------------------------------------------------- brand data
@dataclass
class _BrandLabel:
    label: str
    brand: str
    fuzzy: bool
    skel: str


class _Brands:
    def __init__(self, data: dict) -> None:
        self.names: dict[str, str] = {}
        self.official: dict[str, str] = {}
        self.labels: list[_BrandLabel] = []
        self.by_len: dict[int, list[_BrandLabel]] = {}
        self.by_skel: dict[str, _BrandLabel] = {}
        self.tokens: dict[str, str] = {}
        for key, info in data["brands"].items():
            self.names[key] = info.get("name", key)
            for dom in info.get("domains", []):
                self.official.setdefault(dom.lower(), key)
            default_mode = info.get("match", "fuzzy")
            overrides = info.get("match_labels", {})
            for label in info.get("labels", [key]):
                mode = overrides.get(label, default_mode)
                bl = _BrandLabel(label, key, mode == "fuzzy" and len(label) >= 5, skeleton(label))
                self.labels.append(bl)
                self.by_skel.setdefault(bl.skel, bl)
                self.tokens.setdefault(label, key)
                if bl.fuzzy:
                    self.by_len.setdefault(len(label), []).append(bl)
        self.known_legit = frozenset(d.lower() for d in data.get("known_legit", []))
        self.freemail = frozenset(d.lower() for d in data.get("freemail", []))
        self.risky = frozenset(t.lower() for t in data.get("risky_tlds", []))
        self.official_by_brand: dict[str, set[str]] = {}
        for dom, key in self.official.items():
            self.official_by_brand.setdefault(key, set()).add(dom)
        for key, info in data["brands"].items():
            self.official_by_brand.setdefault(key, set()).update(
                d.lower() for d in info.get("domains", [])
            )

    def brand_of_domain(self, domain: str) -> str | None:
        return self.official.get(domain)


_BRANDS: _Brands | None = None
_BRANDS_LOCK = threading.Lock()


def brands() -> _Brands:
    global _BRANDS
    if _BRANDS is None:
        with _BRANDS_LOCK:
            if _BRANDS is None:
                raw = (
                    resources.files("windows_mcp.guard")
                    .joinpath("data/brands.json")
                    .read_text(encoding="utf-8")
                )
                _BRANDS = _Brands(json.loads(raw))
    return _BRANDS


def warm() -> None:
    """Load the suffix list and brand table (one-off ~10 ms) before the first real check."""
    from windows_mcp.guard import domains

    domains.warm()
    brands()
    check_host_info(parse_host("rnicorsoft.com"))
    check_host_info(parse_host("xn--pple-43d.com"))  # Imports the idna codec.


# --------------------------------------------------------------------------- host signals
@dataclass(frozen=True)
class _HostResult:
    score: int
    brand: str | None
    reasons: tuple[str, ...]
    official: bool
    known: bool
    brand_signal: bool


def _lookalike(word: str, b: _Brands) -> tuple[int, str | None, str | None]:
    """Best lookalike signal for one label-like word: (points, brand, reason)."""
    if len(word) < 3:
        return 0, None, None
    sk = skeleton(word)
    hit = b.by_skel.get(sk)
    if hit is not None:
        if word == hit.label:
            return 60, hit.brand, f"uses the brand name '{hit.label}' on a domain it does not own"
        return 75, hit.brand, f"'{word}' imitates '{hit.label}' with look-alike characters"
    if len(word) < 4:
        return 0, None, None
    best = (0, None, None)
    n = len(word)
    for length in range(n - 3, n + 4):
        for bl in b.by_len.get(length, ()):
            limit = 1 if len(bl.label) < 9 else 2
            if abs(len(sk) - len(bl.skel)) > limit:
                d_sk = limit + 1
            else:
                d_sk = osa_distance(sk, bl.skel, limit)
            if d_sk > limit:
                continue
            d_raw = osa_distance(word, bl.label, limit + 2)
            if d_sk < d_raw:
                cand = (70, bl.brand, f"'{word}' imitates '{bl.label}' (look-alike characters)")
            elif d_raw <= limit:
                cand = (45, bl.brand, f"'{word}' is one typo away from '{bl.label}'")
            else:
                continue
            if cand[0] > best[0]:
                best = cand
    return best


def _embedded_official(host: HostInfo, b: _Brands) -> str | None:
    subs = host.subdomains
    for i in range(len(subs) - 1):
        for j in range(i + 2, min(len(subs), i + 4) + 1):
            cand = ".".join(subs[i:j])
            if cand in b.official:
                return cand
    return None


@lru_cache(maxsize=4096)
def check_host_info(host: HostInfo | None) -> _HostResult:
    """Score a parsed host on its own (cached per host)."""
    if host is None:
        return _HostResult(0, None, ("no host",), False, False, False)
    b = brands()
    official = b.brand_of_domain(host.domain) or b.brand_of_domain(host.host)
    if official:
        return _HostResult(
            0, official, (f"official {b.names[official]} domain",), True, True, False
        )
    if host.domain in b.known_legit:
        return _HostResult(0, None, ("known site",), False, True, False)

    score = 0
    reasons: list[str] = []
    brand: str | None = None
    brand_points = 0

    def bump(points: int, reason: str, who: str | None = None) -> None:
        nonlocal score, brand, brand_points
        score += points
        reasons.append(reason)
        if who and points > brand_points:
            brand, brand_points = who, points

    if host.is_ip:
        bump(25, "raw IP address instead of a domain name")
    else:
        # 1. The registrable label (and its hyphen parts) imitating a brand.
        words = [host.label]
        if "-" in host.label:
            words += [w for w in host.label.split("-") if w]
        best = (0, None, None)
        for word in words:
            cand = _lookalike(word, b)
            if cand[0] > best[0]:
                best = cand
        if best[0] and "." in host.suffix and host.label in b.tokens and host.suffix in b.risky:
            # "microsoft.github.io": usually the brand's own account on shared hosting.
            bump(
                30,
                f"brand name '{host.label}' on shared hosting '.{host.suffix}' "
                "(the account may or may not belong to the brand)",
                best[1],
            )
        elif best[0]:
            bump(best[0], best[2], best[1])
        else:
            # 2. Brand name glued into a longer label: paypalsecure.com, microsoftsupport.net.
            for bl in b.labels:
                if bl.fuzzy and len(bl.label) >= 6 and bl.label in host.label:
                    bump(40, f"domain contains the brand name '{bl.label}'", bl.brand)
                    break

        # 3. Brand as a whole word in a subdomain: paypal.com.secure-login.xyz.
        sub_tokens = [t for s in host.subdomains for t in s.split("-") if t]
        for token in sub_tokens:
            who = b.tokens.get(token)
            if who is None and len(token) >= 5:
                hit = b.by_skel.get(skeleton(token))
                who = hit.brand if hit else None
            if who is not None and len(token) >= 3:
                bump(45, f"brand '{token}' appears in the subdomain of an unrelated domain", who)
                break
        embedded = _embedded_official(host, b)
        if embedded:
            bump(30, f"embeds the real domain '{embedded}' in front of an unrelated one")

        if host.idn:
            bump(10, "internationalized (punycode) domain name")
        tld = host.suffix.rsplit(".", 1)[-1]
        # "microsoft.github.io" / "google.github.io": a brand's own page on a hosting suffix.
        # Its name alone is not evidence of abuse, so the hosting suffix adds nothing.
        brand_page = "." in host.suffix and host.label in b.tokens
        if (host.suffix in b.risky or tld in b.risky) and not brand_page:
            bump(10, f"high-abuse domain ending '.{host.suffix}'")
        hyphens = host.label.count("-")
        if hyphens >= 3:
            bump(10, "many hyphens in the domain")
        elif hyphens == 2:
            bump(5, "several hyphens in the domain")
        if len(host.subdomains) >= 4:
            bump(5, "unusually deep subdomain chain")
    return _HostResult(min(score, 100), brand, tuple(reasons), False, False, brand is not None)


# --------------------------------------------------------------------------- page text
_TEXT_RULES = (
    (
        "infected",
        25,
        re.compile(
            r"(computer|pc|device|windows|system|mac|iphone)\s+(is|has been|may be|was)\s+"
            r"(infected|compromised|hacked|locked|blocked|disabled)"
            r"|virus(es)?\s+(detected|found|alert)|trojan|spyware|malware\s+detected"
            r"|pornographic\s+spyware|windows\s+defender\s+(alert|warning)|security\s+warning"
            r"|error\s+code\s*[:#]?\s*(0x|dw)[0-9a-z]+",
            re.I,
        ),
        "page claims the computer is infected or locked",
    ),
    (
        "call",
        25,
        re.compile(
            r"call\s+(microsoft|windows|apple|technical|tech|customer|toll[- ]free|the)?\s*"
            r"(support|helpline|help\s*desk|technician|number)"
            r"|contact\s+(microsoft|apple|windows)\s+(support|technician)"
            r"|(microsoft|windows|apple)\s+(certified\s+)?(support|technician)s?\s+(at|on)\b",
            re.I,
        ),
        "page tells you to call 'support'",
    ),
    (
        "phone",
        20,
        re.compile(
            r"(\+?1[\s.-]?)?\(?8(00|33|44|55|66|77|88)\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"
            r"|\+?\d{1,3}[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b(?=.{0,40}(toll|support|call))",
            re.I | re.S,
        ),
        "page shows a (toll-free) phone number",
    ),
    (
        "urgency",
        15,
        re.compile(
            r"do\s+not\s+(close|restart|shut\s*down|turn\s+off|ignore)"
            r"|within\s+\d+\s+(minutes|hours)|immediately|act\s+now|final\s+warning"
            r"|(account|data|files|license)\s+(will\s+be|has\s+been)\s+"
            r"(deleted|suspended|locked|disabled|terminated)"
            r"|access\s+(to\s+this\s+(pc|computer|device)\s+)?(has\s+been|is)\s+(blocked|denied)",
            re.I,
        ),
        "urgent / threatening language",
    ),
    (
        "credential",
        10,
        re.compile(
            r"verify\s+your\s+(account|identity|payment|information)|unusual\s+sign[- ]in"
            r"|confirm\s+your\s+(identity|account|password)|your\s+password\s+(has\s+)?expire"
            r"|update\s+your\s+(billing|payment)\s+(details|information)"
            r"|account\s+(is\s+)?on\s+hold|suspicious\s+activity",
            re.I,
        ),
        "asks you to verify account or payment details",
    ),
    (
        "payment",
        15,
        re.compile(
            r"gift\s*cards?|bitcoin\s+(atm|address)|pay\s+(with|using)\s+(gift|crypto|bitcoin)"
            r"|wire\s+transfer\s+immediately|refund\s+of\s+\$?\d",
            re.I,
        ),
        "asks for gift cards, crypto or an unexpected refund",
    ),
)
MAX_TEXT = 6000


def scan_text(text: str | None) -> tuple[int, list[str], set[str]]:
    """Score visible text for tech-support-scam / phishing language."""
    if not text:
        return 0, [], set()
    text = text[:MAX_TEXT]
    score, reasons, hits = 0, [], set()
    for name, points, rx, reason in _TEXT_RULES:
        if rx.search(text):
            score += points
            reasons.append(reason)
            hits.add(name)
    # The classic tech-support scam: infected + call + phone (or urgency) together.
    if {"infected", "call"} <= hits and ({"phone", "urgency"} & hits):
        score = max(score, 85)
        reasons.append("matches the fake-virus-warning / call-support scam pattern")
    elif len(hits) == 1:
        score = min(score, 20)  # One phrase alone is common on legitimate pages.
    return min(score, 100), reasons, hits


# --------------------------------------------------------------------------- public API
def check(
    target: str | Target,
    *,
    password_field: bool = False,
    text: str | None = None,
    title: str | None = None,
) -> Verdict:
    """Check a URL, bare domain or email address.

    Args:
        target: URL / domain / email (or an already parsed :class:`Target`).
        password_field: The focused field is a password box (strongest signal on a lookalike).
        text: Optional visible page text (clipped) for scam-language scoring.
        title: Optional page/window title (scanned with the text).
    """
    start = time.perf_counter()
    t = target if isinstance(target, Target) else parse_target(target)
    b = brands()
    hr = check_host_info(t.host)
    v = Verdict(
        verdict="safe" if (hr.official or hr.known) else "unknown",
        score=hr.score,
        brand=hr.brand,
        reasons=list(hr.reasons),
        domain=t.host.domain if t.host else None,
        host=t.host.host if t.host else None,
        kind=t.kind,
        official=hr.official,
    )
    if t.host is None:
        v.reasons = ["could not parse a host"]
        v.ms = (time.perf_counter() - start) * 1000
        return v

    extra = 0
    if t.userinfo:
        ui = t.userinfo.lower()
        named = next((d for d in b.official if d in ui), None) or next(
            (tok for tok in re.split(r"[^a-z0-9]+", ui) if tok in b.tokens and len(tok) >= 3),
            None,
        )
        if named:
            extra += 60
            v.reasons.append(
                f"URL starts with '{t.userinfo}@' to look like {named}; it really goes to {t.host.host}"
            )
            v.brand = v.brand or b.official.get(named) or b.tokens.get(named)
        else:
            extra += 20
            v.reasons.append("URL hides the real host behind 'user@'")
        v.official = False
    if t.kind == "email" and t.display_name:
        dn = t.display_name.lower()
        for tok in re.split(r"[^a-z0-9]+", dn):
            who = b.tokens.get(tok)
            if who and len(tok) >= 3 and t.host.domain not in b.official_by_brand.get(who, ()):
                extra += 40 if t.host.domain in b.freemail else 30
                v.reasons = [r for r in v.reasons if not r.startswith("official ")]
                v.reasons.append(
                    f"sender name says '{t.display_name}' but the address is @{t.host.domain}"
                )
                v.brand = who
                v.official = False
                break
    if t.kind == "url" and "@" in t.url:
        # https://evil.xyz\@microsoft.com or /@microsoft.com: the brand sits after the real host.
        tail = t.url.replace("\\", "/").split("@", 1)[1].lower() if not t.userinfo else ""
        named = next((d for d in b.official if tail.startswith(d)), None) if tail else None
        if named:
            extra += 60
            v.reasons.append(
                f"URL is crafted to look like {named}; it really goes to {t.host.host}"
            )
            v.brand = v.brand or b.official.get(named)

    if not v.official or extra:
        if t.scheme == "http" and (hr.brand_signal or password_field):
            extra += 10
            v.reasons.append("not encrypted (http)")
        if password_field:
            if hr.brand_signal or extra >= 40:
                extra += 30
                v.reasons.append("asks for a password on a look-alike site")
            elif not hr.known:
                extra += 10
                v.reasons.append("password field on an unrecognized site")
                v.gray = True
        tscore, treasons, _ = scan_text(" ".join(x for x in (title, text) if x))
        if tscore and not hr.official:
            extra += tscore
            v.reasons.extend(treasons)
            if tscore >= 20:
                v.gray = True

    v.score = min(100, hr.score + extra)
    if password_field and hr.brand_signal:
        v.score = max(v.score, DANGEROUS + 5)
    if v.official and not extra:
        v.verdict, v.score = "safe", 0
    elif hr.known and v.score < SUSPICIOUS:
        v.verdict = "safe"
    else:
        v.verdict = verdict_for(v.score)
    if v.verdict == "suspicious":
        v.gray = True
    if v.verdict in ("safe", "dangerous"):
        v.gray = False
    if v.brand:
        v.brand = b.names.get(v.brand, v.brand)
    v.ms = (time.perf_counter() - start) * 1000
    return v


def merge_model(v: Verdict, model: dict) -> Verdict:
    """Apply a model second opinion. It may only raise severity, never lower it.

    Page text reaches the model, so a page could try to talk it into "legit"; escalate-only
    keeps the local verdict as the floor.
    """
    probs = model.get("probabilities") or {}
    choice = model.get("choice")
    if not probs and choice:
        probs = {choice: float(model.get("confidence") or 0.0)}
    bad = max(float(probs.get("phishing", 0.0)), float(probs.get("tech_support_scam", 0.0)))
    out = Verdict(**{**asdict(v), "reasons": list(v.reasons)})
    out.model = model
    target = None
    if bad >= 0.7:
        target = "dangerous"
    elif bad >= 0.4:
        target = "suspicious"
    if target and VERDICT_RANK[target] > VERDICT_RANK[out.verdict]:
        label = (
            "tech-support scam"
            if probs.get("tech_support_scam", 0) >= probs.get("phishing", 0)
            else "phishing"
        )
        out.verdict = target
        out.score = max(out.score, DANGEROUS if target == "dangerous" else SUSPICIOUS)
        out.reasons.append(f"second opinion: {label} ({bad:.0%})")
    out.gray = False
    return out
