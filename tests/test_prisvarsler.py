"""Enhetstester for prisparsing/-uthenting i gullsmed_prisvarsler.py.

Kjøres med:  pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gullsmed_prisvarsler as gp

# ==========================================================================
# parse_nok_number
# ==========================================================================

def test_parse_plain_integer():
    assert gp.parse_nok_number("11850") == 11850.0


def test_parse_with_thousand_separator_dot():
    assert gp.parse_nok_number("11.850") == 11850.0


def test_parse_with_thousand_separator_space():
    assert gp.parse_nok_number("11 850") == 11850.0


def test_parse_with_kr_suffix():
    assert gp.parse_nok_number("11 850 kr") == 11850.0


def test_parse_with_comma_decimal():
    assert gp.parse_nok_number("11.850,00") == 11850.0


def test_parse_with_comma_style_suffix():
    assert gp.parse_nok_number("11850,-") == 11850.0


def test_parse_none_returns_none():
    assert gp.parse_nok_number(None) is None


def test_parse_invalid_text_returns_none():
    assert gp.parse_nok_number("ikke en pris") is None


# ==========================================================================
# extract_price
# ==========================================================================

def test_extract_price_from_json_ld():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "Product", "offers": {"@type": "Offer", "price": "11850", "priceCurrency": "NOK"}}
    </script>
    </head><body>Frakt 199 kr. Andre priser: 500000 kr.</body></html>
    """
    assert gp.extract_price(html) == 11850.0


def test_extract_price_from_json_ld_list_offers():
    html = """
    <html><head>
    <script type="application/ld+json">
    [{"@type": "Product", "offers": [{"price": "11400,00"}]}]
    </script>
    </head><body></body></html>
    """
    assert gp.extract_price(html) == 11400.0


def test_extract_price_prefers_json_ld_over_regex_fallback():
    """Regresjonstest for tidligere bug: regex-fallbacket plukket opp feil
    tall (f.eks. et frakt- eller variantbeløp) når JSON-LD faktisk hadde
    riktig pris. JSON-LD skal alltid vinne når det finnes."""
    html = """
    <html><head>
    <script type="application/ld+json">
    {"offers": {"price": "11850"}}
    </script>
    </head><body>Frakt: 118500 kr</body></html>
    """
    assert gp.extract_price(html) == 11850.0


def test_extract_price_from_meta_tag():
    html = """
    <html><head>
    <meta property="product:price:amount" content="14999">
    </head><body></body></html>
    """
    assert gp.extract_price(html) == 14999.0


def test_extract_price_from_itemprop():
    html = '<html><body><span itemprop="price" content="11400">11 400 kr</span></body></html>'
    assert gp.extract_price(html) == 11400.0


def test_extract_price_with_css_selector_override():
    html = '<html><body><div class="final-price">11 850 kr</div><div>500 000 kr frakt</div></body></html>'
    assert gp.extract_price(html, selector=".final-price") == 11850.0


def test_extract_price_returns_none_when_nothing_found():
    html = "<html><body>Ingen priser her.</body></html>"
    assert gp.extract_price(html) is None


# ==========================================================================
# Sanity-sjekker (regresjon for P0-bugs: 10x/100x feilaktige priser)
# ==========================================================================

def test_is_sane_ring_price_rejects_out_of_range():
    assert gp.is_sane_ring_price(100.0, None) is False       # under RING_MIN_PRICE
    assert gp.is_sane_ring_price(600000.0, None) is False    # over RING_MAX_PRICE
    assert gp.is_sane_ring_price(11850.0, None) is True


def test_is_sane_ring_price_rejects_large_jump_from_previous():
    # 10x hopp fra forrige registrerte pris skal avvises.
    assert gp.is_sane_ring_price(118500.0, 11850.0) is False
    # Et lite, realistisk avvik skal godtas.
    assert gp.is_sane_ring_price(12100.0, 11850.0) is True


def test_is_sane_gold_price_rejects_out_of_range():
    assert gp.is_sane_gold_price(50.0, None) is False
    assert gp.is_sane_gold_price(900.0, None) is True


def test_is_sane_gold_price_rejects_large_jump_from_previous():
    assert gp.is_sane_gold_price(1200.0, 900.0) is False
    assert gp.is_sane_gold_price(920.0, 900.0) is True


# ==========================================================================
# NTFY placeholder fail-fast behaviour
# ==========================================================================

def test_send_ntfy_skips_when_topic_is_placeholder(monkeypatch, capsys):
    monkeypatch.setattr(gp, "NTFY_TOPIC", gp.NTFY_TOPIC_PLACEHOLDER)
    calls = []
    monkeypatch.setattr(gp.requests, "post", lambda *a, **k: calls.append((a, k)))
    gp.send_ntfy("Tittel", "Melding")
    assert calls == []
    assert "plassholderverdi" in capsys.readouterr().out
