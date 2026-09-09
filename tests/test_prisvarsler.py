"""Enhetstester for prisparsing/-uthenting i gullsmed_prisvarsler.py.

Kjøres med:  pytest tests/
"""

import sys
import json
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


def test_parse_gold_price_from_text_returns_none_for_out_of_bounds():
    assert gp.parse_gold_price_from_text("150 kr") is None
    assert gp.parse_gold_price_from_text("950 kr") == 950.0


def test_fetch_gullbanken_gold_nok_per_gram_24k_from_text_block(monkeypatch):
    class FakeResponse:
        text = "<html><body>Innleveringspris 24k gull 995 kr per gram</body></html>"

    monkeypatch.setattr(gp, "request_with_retries", lambda *a, **k: FakeResponse())
    assert gp.fetch_gullbanken_gold_nok_per_gram_24k() == 995.0


def test_merge_gold_source_history_versions_merges_by_date():
    current = """
    {
      "gullbanken": {
        "label": "Gullbanken",
        "entries": [{"date": "2026-09-08", "price_24k": 900.0}]
      }
    }
    """
    incoming = """
    {
      "gullbanken": {
        "label": "Gullbanken",
        "entries": [
          {"date": "2026-09-08", "price_24k": 910.0},
          {"date": "2026-09-09", "price_24k": 920.0}
        ]
      }
    }
    """
    assert gp.merge_gold_source_history_versions(current, incoming) == {
        "gullbanken": {
            "label": "Gullbanken",
            "entries": [
                {"date": "2026-09-08", "price_24k": 910.0},
                {"date": "2026-09-09", "price_24k": 920.0},
            ],
        }
    }


def test_check_gold_price_updates_source_history_and_prefers_gullbanken(monkeypatch, tmp_path):
    gold_history_file = tmp_path / "gullpris_historikk.json"
    source_history_file = tmp_path / "gullpris_kilder_historikk.json"
    gold_history_file.write_text("[]", encoding="utf-8")
    source_history_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(gp, "GOLD_HISTORY_FILE", gold_history_file)
    monkeypatch.setattr(gp, "GOLD_SOURCE_HISTORY_FILE", source_history_file)
    monkeypatch.setattr(gp, "send_ntfy", lambda *a, **k: None)
    monkeypatch.setattr(
        gp,
        "fetch_gold_prices_by_source",
        lambda: (
            {
                "gullbanken": {"label": "Gullbanken", "price_24k": 980.0},
                "internasjonal-spot": {"label": "Internasjonal", "price_24k": 970.0},
            },
            {},
        ),
    )

    gp.check_gold_price()

    gold_history = json.loads(gold_history_file.read_text(encoding="utf-8"))
    source_history = json.loads(source_history_file.read_text(encoding="utf-8"))

    assert gold_history[-1]["price_24k"] == 980.0
    assert source_history["gullbanken"]["entries"][-1]["price_24k"] == 980.0
    assert source_history["internasjonal-spot"]["entries"][-1]["price_24k"] == 970.0


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


def test_send_ntfy_sanitizes_unicode_headers(monkeypatch):
    monkeypatch.setattr(gp, "NTFY_TOPIC", "hemmelig-emne")

    class FakeResponse:
        def raise_for_status(self):
            return None

    captured = {}

    def fake_post(url, data, headers, timeout):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(gp.requests, "post", fake_post)

    gp.send_ntfy("✓ Prissjekk startet 📉", "Melding", priority="high", tags="💍")

    assert captured["headers"] == {
        "Title": "Prissjekk startet",
        "Priority": "high",
        "Tags": "moneybag",
    }


def test_check_ring_prices_uses_url_not_name_for_cheapest_duplicate_names(monkeypatch, tmp_path):
    history_file = tmp_path / "ring_historikk.json"
    history_file.write_text(
        """
        {
          "https://butikk-a.no/ring": {
            "name": "Samme ring",
            "shop": "Butikk A",
            "entries": [
              {"date": "2026-09-01", "price": 10000.0},
              {"date": "2026-09-02", "price": 9900.0}
            ]
          },
          "https://butikk-b.no/ring": {
            "name": "Samme ring",
            "shop": "Butikk B",
            "entries": [
              {"date": "2026-09-01", "price": 9000.0},
              {"date": "2026-09-02", "price": 8900.0}
            ]
          }
        }
        """.strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(gp, "RING_HISTORY_FILE", history_file)
    monkeypatch.setattr(
        gp,
        "RINGS",
        [
            {"name": "Samme ring", "shop": "Butikk A", "url": "https://butikk-a.no/ring"},
            {"name": "Samme ring", "shop": "Butikk B", "url": "https://butikk-b.no/ring"},
        ],
    )
    monkeypatch.setattr(
        gp,
        "fetch_ring_price",
        lambda url, selector=None: {
            "https://butikk-a.no/ring": 10050.0,
            "https://butikk-b.no/ring": 8500.0,
        }[url],
    )

    calls = []
    monkeypatch.setattr(gp, "send_ntfy", lambda title, message, priority="default", tags="moneybag": calls.append((title, message)))

    gp.check_ring_prices()

    assert calls == [
        (
            "Butikk B er nå billigst 💍",
            "Samme ring: 8500 kr — lavere enn snittet (8950 kr)",
        )
    ]


def test_merge_gold_history_versions_prefers_ours_for_same_date():
    theirs = """
    [
      {"date": "2026-09-08", "price_24k": 900.0},
      {"date": "2026-09-09", "price_24k": 910.0}
    ]
    """
    ours = """
    [
      {"date": "2026-09-09", "price_24k": 920.0},
      {"date": "2026-09-10", "price_24k": 930.0}
    ]
    """

    assert gp.merge_gold_history_versions(theirs, ours) == [
        {"date": "2026-09-08", "price_24k": 900.0},
        {"date": "2026-09-09", "price_24k": 920.0},
        {"date": "2026-09-10", "price_24k": 930.0},
    ]


def test_resolve_history_conflicts_merges_ring_history_file(tmp_path):
    history_file = tmp_path / "ring_historikk.json"
    history_file.write_text(
        """<<<<<<< HEAD
{
  "https://butikk-a.no/ring": {
    "name": "Ring A",
    "shop": "Butikk A",
    "entries": [
      {"date": "2026-09-08", "price": 10000.0}
    ]
  }
}
=======
{
  "https://butikk-a.no/ring": {
    "name": "Ring A",
    "shop": "Butikk A",
    "entries": [
      {"date": "2026-09-08", "price": 9900.0},
      {"date": "2026-09-09", "price": 9800.0}
    ]
  },
  "https://butikk-b.no/ring": {
    "name": "Ring B",
    "shop": "Butikk B",
    "entries": [
      {"date": "2026-09-09", "price": 12000.0}
    ]
  }
}
>>>>>>> upstream
""",
        encoding="utf-8",
    )

    gp.resolve_history_conflicts([history_file])

    assert gp.load_json(history_file, {}) == {
        "https://butikk-a.no/ring": {
            "name": "Ring A",
            "shop": "Butikk A",
            "entries": [
                {"date": "2026-09-08", "price": 9900.0},
                {"date": "2026-09-09", "price": 9800.0},
            ],
        },
        "https://butikk-b.no/ring": {
            "name": "Ring B",
            "shop": "Butikk B",
            "entries": [
                {"date": "2026-09-09", "price": 12000.0},
            ],
        },
    }


def test_resolve_history_conflicts_merges_gold_source_history_file(tmp_path):
    history_file = tmp_path / "gullpris_kilder_historikk.json"
    history_file.write_text(
        """<<<<<<< HEAD
{
  "gullbanken": {
    "label": "Gullbanken",
    "entries": [
      {"date": "2026-09-08", "price_24k": 900.0}
    ]
  }
}
=======
{
  "gullbanken": {
    "label": "Gullbanken",
    "entries": [
      {"date": "2026-09-08", "price_24k": 910.0},
      {"date": "2026-09-09", "price_24k": 920.0}
    ]
  }
}
>>>>>>> upstream
""",
        encoding="utf-8",
    )

    old_file = gp.GOLD_SOURCE_HISTORY_FILE
    try:
        gp.GOLD_SOURCE_HISTORY_FILE = history_file
        gp.resolve_history_conflicts([history_file])
    finally:
        gp.GOLD_SOURCE_HISTORY_FILE = old_file

    assert gp.load_json(history_file, {}) == {
        "gullbanken": {
            "label": "Gullbanken",
            "entries": [
                {"date": "2026-09-08", "price_24k": 910.0},
                {"date": "2026-09-09", "price_24k": 920.0},
            ],
        }
    }
