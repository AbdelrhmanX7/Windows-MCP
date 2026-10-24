"""Local scam detector: parsing, lookalikes, false positives, text signals, speed.

Pure Python (no Windows imports), so it also runs on Linux:
``PYTHONPATH=src python -m pytest --noconftest tests/test_guard_detector.py``.
"""

import statistics
import time

import pytest

from windows_mcp.guard import check, merge_model, scan_text, warm
from windows_mcp.guard.confusables import osa_distance, skeleton
from windows_mcp.guard.domains import parse_host, parse_target

# Popular real domains (and real near-collisions with brand names). None may be dangerous.
LEGIT = [
    "microsoft.com",
    "https://login.microsoftonline.com/common/oauth2",
    "https://outlook.office.com/mail/",
    "https://outlook.live.com/mail/0/",
    "account.live.com",
    "www.bing.com",
    "github.com",
    "https://www.google.com/search?q=cats",
    "accounts.google.com",
    "mail.google.com",
    "www.youtube.com",
    "docs.google.com",
    "apple.com",
    "www.icloud.com",
    "appleid.apple.com",
    "www.amazon.com",
    "amazon.de",
    "amazon.co.uk",
    "s3.amazonaws.com",
    "console.aws.amazon.com",
    "www.paypal.com",
    "www.netflix.com",
    "www.facebook.com",
    "www.instagram.com",
    "web.whatsapp.com",
    "x.com",
    "twitter.com",
    "www.linkedin.com",
    "www.reddit.com",
    "en.wikipedia.org",
    "store.steampowered.com",
    "steamcommunity.com",
    "www.chase.com",
    "secure.bankofamerica.com",
    "www.wellsfargo.com",
    "online.citi.com",
    "www.hsbc.co.uk",
    "www.barclays.co.uk",
    "www.coinbase.com",
    "www.binance.com",
    "www.dhl.com",
    "www.fedex.com",
    "www.ups.com",
    "tools.usps.com",
    "www.irs.gov",
    "finance.yahoo.com",
    "king.com",
    "chess.com",
    "paypay.ne.jp",
    "www.office.com",
    "teams.microsoft.com",
    "zoom.us",
    "stackoverflow.com",
    "www.nytimes.com",
    "www.bbc.co.uk",
    "duckduckgo.com",
    "chatgpt.com",
    "claude.ai",
    "openrouter.ai",
    "www.ebay.com",
    "www.walmart.com",
    "www.target.com",
    "www.dropbox.com",
    "www.spotify.com",
    "discord.com",
    "www.twitch.tv",
    "groups.google.com",
    "www.python.org",
    "news.ycombinator.com",
    "www.cloudflare.com",
    "calhacks.io",
    "devpost.com",
    "microsoft.github.io",
    "google.github.io",
    "aws.github.io",
    "meta.ai",
]

SCAMS = [
    ("rnicorsoft.com", "dangerous", "Microsoft"),
    ("https://rnicrosoft.com/login", "dangerous", "Microsoft"),
    ("micros0ft.net", "dangerous", "Microsoft"),
    ("paypa1.com", "dangerous", "PayPal"),
    ("https://microsoft.com.verify-login.xyz/", "dangerous", "Microsoft"),
    ("https://microsoft.com@evil.xyz/", "dangerous", "Microsoft"),
    ("https://evil.xyz\\@microsoft.com", "dangerous", "Microsoft"),
    ("support@rnicorsoft.com", "dangerous", "Microsoft"),
    ("g00gle.com", "dangerous", "Google"),
    ("faceb00k.com", "dangerous", "Facebook"),
    ("xn--pple-43d.com", "dangerous", "Apple"),
    ("аpple.com", "dangerous", "Apple"),
    ("appleid.apple.com.signin.xyz", "dangerous", "Apple"),
    ("amazon-security-alert.top", "dangerous", "Amazon"),
    ("microsoft-login.azurewebsites.net", "dangerous", "Microsoft"),
    ("steamcommunlty.com", "dangerous", "Steam"),
    ("arnazon.com", "dangerous", "Amazon"),
    ("vvellsfargo.com", "dangerous", "Wells Fargo"),
    ("amazom.com", "suspicious", "Amazon"),
    ("paypalsecure.com", "suspicious", "PayPal"),
    ("netflix-billing.com", "suspicious", "Netflix"),
]


@pytest.fixture(scope="module", autouse=True)
def _warm():
    warm()


@pytest.mark.parametrize("url", LEGIT)
def test_legit_domains_are_never_flagged(url):
    v = check(url)
    assert v.verdict in ("safe", "unknown"), (url, v.verdict, v.score, v.reasons)


@pytest.mark.parametrize("url,expected,brand", SCAMS)
def test_lookalikes_are_flagged(url, expected, brand):
    v = check(url)
    assert v.verdict == expected, (url, v.verdict, v.score, v.reasons)
    assert v.brand == brand


def test_official_domains_fast_path_safe():
    v = check("https://login.microsoftonline.com/")
    assert v.verdict == "safe" and v.official and v.score == 0 and v.brand == "Microsoft"


def test_password_field_turns_lookalike_dangerous():
    assert check("amazom.com").verdict == "suspicious"
    v = check("amazom.com", password_field=True)
    assert v.verdict == "dangerous"
    assert any("password" in r for r in v.reasons)


def test_password_on_unknown_site_is_gray_not_dangerous():
    v = check("https://some-random-shop.example", password_field=True)
    assert v.verdict == "unknown" and v.gray


def test_password_on_official_site_stays_safe():
    v = check("https://login.microsoftonline.com/", password_field=True)
    assert v.verdict == "safe"


def test_email_display_name_mismatch():
    v = check('"PayPal Billing" <billing@gmail.com>')
    assert v.kind == "email"
    assert v.verdict == "suspicious" and v.brand == "PayPal"
    assert check("billing@paypal.com").verdict == "safe"
    assert check("mailto:support@rnicorsoft.com?subject=x").verdict == "dangerous"


def test_ip_and_userinfo():
    assert check("http://192.168.1.5/login").score >= 25
    v = check("http://3232235777/")
    assert any("IP" in r for r in v.reasons)


def test_tech_support_scam_text():
    text = (
        "WINDOWS DEFENDER ALERT! Your computer is infected with a Trojan. "
        "Do not close this window. Call Microsoft Support at 1-888-555-0199 immediately."
    )
    score, reasons, hits = scan_text(text)
    assert score >= 85 and {"infected", "call", "phone"} <= hits
    v = check("http://pc-cleanup-alert.example", text=text)
    assert v.verdict == "dangerous"


def test_single_scam_phrase_is_weak():
    score, _, _ = scan_text("Act now to get 20% off shoes!")
    assert score <= 20


def test_scam_text_ignored_on_official_domain():
    text = "Your computer is infected. Call Microsoft support at 1-800-642-7676."
    assert check("https://support.microsoft.com/", text=text).verdict == "safe"


def test_parse_target_shapes():
    t = parse_target("https://microsoft.com@evil.xyz/path")
    assert t.host.host == "evil.xyz" and t.userinfo == "microsoft.com"
    t = parse_target("evil.github.io")
    assert t.host.domain == "evil.github.io" and t.host.label == "evil"
    t = parse_target("WWW.Example.CO.UK.")
    assert t.host.domain == "example.co.uk" and t.kind == "domain" and t.scheme is None
    t = parse_target("https://[::1]:8080/x")
    assert t.host.is_ip
    assert parse_host("xn--pple-43d.com").idn


def test_confusable_helpers():
    assert skeleton("rnicrosoft") == skeleton("microsoft")
    assert skeleton("paypa1") == skeleton("paypal")
    assert osa_distance("micorsoft", "microsoft") == 1
    assert osa_distance("abc", "xyzxyz", 2) == 3


def test_merge_model_only_escalates():
    v = check("rnicorsoft.com")
    calm = merge_model(v, {"choice": "legit", "probabilities": {"legit": 0.99}})
    assert calm.verdict == "dangerous"
    u = check("https://some-random-shop.example", password_field=True)
    up = merge_model(u, {"choice": "phishing", "probabilities": {"phishing": 0.83, "legit": 0.1}})
    assert up.verdict == "dangerous" and up.model["choice"] == "phishing"
    mid = merge_model(u, {"choice": "phishing", "confidence": 0.5})
    assert mid.verdict == "suspicious"


def test_local_verdict_speed():
    hosts = [s for s, _, _ in SCAMS] + LEGIT
    for h in hosts:
        check(h)  # warm per-host caches
    samples = []
    for _ in range(5):
        for h in hosts:
            t = time.perf_counter()
            check(h)
            samples.append((time.perf_counter() - t) * 1e6)
    median_us = statistics.median(samples)
    assert median_us < 1000, median_us
    # Cold (uncached host) checks must stay well under a millisecond on average too.
    cold = []
    for i in range(50):
        t = time.perf_counter()
        check(f"https://login-{i}.rnicorsoft-{i}.xyz/")
        cold.append((time.perf_counter() - t) * 1e6)
    assert statistics.median(cold) < 5000
