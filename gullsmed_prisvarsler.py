#!/usr/bin/env python3
"""
gullsmed_prisvarsler.py (v3)

Kjører HELE automatikken som nettsiden ikke får lov til å gjøre selv
(artefakt-sandkassen til Claude tillater ikke at siden henter data fra
andre nettsteder eller sender push på egen hånd). Dette scriptet har
vanlig internett-tilgang og kan derfor:

  1. Hente dagens gullpris (USD/oz) + USD/NOK-kurs, regne om til kr/gram
     for valgt karat, og varsle når prisen faller mer enn valgt terskel.
  2. Sjekke prisen på ringene dine (konfigurert i rings.json), og varsle
     når én av dem er BÅDE billigst av de du følger OG lavere enn sitt
     eget historiske snitt (ikke bare "alltid billigst").
  3. Sende ekte push via ntfy.sh for begge deler.

--------------------------------------------------------------------------
OPPSETT
--------------------------------------------------------------------------
1. pip install -r requirements.txt
2. Fyll inn NTFY_TOPIC (miljøvariabel/GitHub-secret) og juster ringene i
   rings.json samt GOLD_KARAT / GOLD_DROP_THRESHOLD_PCT nedenfor.
3. Test manuelt:  python3 gullsmed_prisvarsler.py --debug
4. Legg i cron for daglig automatisk kjøring, f.eks. kl 08:00:
   crontab -e
   0 8 * * * /usr/bin/python3 /full/path/gullsmed_prisvarsler.py >> /full/path/log.txt 2>&1

Historikk lagres i to filer ved siden av scriptet:
  gullpris_historikk.json   (gullpris over tid)
  ring_historikk.json       (pris per ring over tid)

Ringene som følges konfigureres i rings.json (samme fil som nettsiden
leser), slik at Python og JavaScript aldri kommer ut av synk.
--------------------------------------------------------------------------
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ==========================================================================
# KONFIGURASJON — tilpass dette
# ==========================================================================

# NTFY_TOPIC kan settes her direkte, ELLER som miljøvariabel/GitHub-secret
# (miljøvariabelen vinner hvis den finnes) — praktisk når scriptet kjører
# via GitHub Actions og du ikke vil ha emnenavnet liggende i selve koden.
NTFY_TOPIC_PLACEHOLDER = "sett-ditt-ntfy-emne-her"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", NTFY_TOPIC_PLACEHOLDER)
NTFY_SERVER = "https://ntfy.sh"

GOLD_KARAT = 0.75              # 18K = 0.75, 14K = 0.585, 9K = 0.375, 22K = 0.916
GOLD_DROP_THRESHOLD_PCT = 1.0  # varsle når gullprisen faller mer enn dette siden forrige sjekk
RING_BELOW_AVG_PCT = 3.0       # varsle når en ring er > 3% under sitt eget snitt
GOLD_PREFERRED_SOURCE_KEYS = ["gullbanken", "spot"]
GOLD_SOURCES = [
    {
        "key": "gullbanken",
        "name": "Gullbanken",
        "url": "https://www.gullbanken.no/gullpriser/",
    },
    {
        "key": "spot",
        "name": "Spot (USD/oz → NOK/g)",
        "url": None,
    },
]

# Sanity-grenser brukt til å avvise åpenbart feilaktige skrapte priser før
# de lagres i historikken (se P0-bugs: regex-fallback har tidligere plukket
# opp fraktpriser/varianter og lagret priser 10-100x for høye).
GOLD_MIN_NOK_PER_GRAM_24K = 200.0
GOLD_MAX_NOK_PER_GRAM_24K = 5000.0
GOLD_MAX_CHANGE_PCT = 15.0     # avvis endring fra forrige dag > 15% (sannsynlig feil)

RING_MIN_PRICE = 500.0
RING_MAX_PRICE = 500000.0
RING_MAX_CHANGE_PCT = 50.0     # avvis endring fra forrige registrering > 50% (sannsynlig feil)

RINGS_CONFIG_FILE = Path(__file__).parent / "rings.json"
GOLD_HISTORY_FILE = Path(__file__).parent / "gullpris_historikk.json"
RING_HISTORY_FILE = Path(__file__).parent / "ring_historikk.json"
GOLD_HISTORY_LIMIT_DAYS = 730
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PrisvarslerBot/1.0; personlig prissjekk)"}
DEBUG = "--debug" in sys.argv

HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_SECONDS = 2
RESOLVE_HISTORY_CONFLICTS_FLAG = "--resolve-history-conflicts"


def load_rings():
    """Leser ringkonfigurasjonen fra rings.json (delt med nettsiden)."""
    if not RINGS_CONFIG_FILE.exists():
        print(f"  ADVARSEL: fant ikke {RINGS_CONFIG_FILE}, ingen ringer å sjekke.")
        return []
    try:
        rings = json.loads(RINGS_CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ADVARSEL: klarte ikke lese {RINGS_CONFIG_FILE}: {e}")
        return []
    if not isinstance(rings, list):
        print(f"  ADVARSEL: {RINGS_CONFIG_FILE} skal inneholde en liste.")
        return []
    return [r for r in rings if isinstance(r, dict) and r.get("url") and r.get("name")]


RINGS = load_rings()


# ==========================================================================
# HJELPEFUNKSJONER
# ==========================================================================

def log(*args):
    if DEBUG:
        print(*args)


def sanitize_http_header_value(value, fallback="Varsel"):
    value = str(value).strip()
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        normalized = unicodedata.normalize("NFKD", value)
        cleaned = normalized.encode("latin-1", "ignore").decode("latin-1").strip()
        return cleaned or fallback


def request_with_retries(method, url, **kwargs):
    """Enkel retry/backoff-wrapper rundt requests, for å tåle forbigående
    nettverksfeil i stedet for å hoppe over hele dagens datapunkt."""
    last_exc = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            last_exc = e
            if attempt < HTTP_MAX_RETRIES:
                wait = HTTP_BACKOFF_SECONDS * attempt
                log(f"    Forsøk {attempt} feilet ({e}), prøver igjen om {wait}s …")
                time.sleep(wait)
    raise last_exc


def send_ntfy(title, message, priority="default", tags="moneybag"):
    if not NTFY_TOPIC or NTFY_TOPIC == NTFY_TOPIC_PLACEHOLDER:
        print(
            "  ADVARSEL: NTFY_TOPIC er ikke satt (fortsatt plassholderverdi). "
            "Hopper over varsel i stedet for å sende det til et offentlig, "
            "ubrukt emne. Sett miljøvariabelen/secreten NTFY_TOPIC for å "
            "aktivere push-varsler."
        )
        return
    try:
        response = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": sanitize_http_header_value(title),
                "Priority": sanitize_http_header_value(priority, fallback="default"),
                "Tags": sanitize_http_header_value(tags, fallback="moneybag"),
            },
            timeout=10,
        )
        response.raise_for_status()
        print(f"  → ntfy sendt: {title}")
    except requests.RequestException as e:
        print(f"  Klarte ikke sende ntfy-varsel: {e}")


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def split_conflicted_text(text):
    """Returnerer (current, incoming) fra en tekst med git-konfliktmarkører."""
    current = []
    incoming = []
    mode = "both"

    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< "):
            mode = "ours"
            continue
        if line.startswith("=======") and mode == "ours":
            mode = "theirs"
            continue
        if line.startswith(">>>>>>> ") and mode == "theirs":
            mode = "both"
            continue

        if mode in {"both", "ours"}:
            current.append(line)
        if mode in {"both", "theirs"}:
            incoming.append(line)

    return "".join(current), "".join(incoming)


def merge_history_entries_by_date(entries_list, price_key):
    merged = {}
    for entries in entries_list:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            date = entry.get("date")
            price = entry.get(price_key)
            if not date or price is None:
                continue
            merged[date] = {"date": date, price_key: price}
    return [merged[date] for date in sorted(merged)]


def extract_gold_prices_24k_map(entry):
    if not isinstance(entry, dict):
        return {}
    prices = {}
    raw_map = entry.get("prices_24k")
    if isinstance(raw_map, dict):
        for source_key, value in raw_map.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric > 0:
                prices[str(source_key)] = numeric
    if not prices:
        legacy_price = entry.get("price_24k")
        try:
            legacy_numeric = float(legacy_price)
        except (TypeError, ValueError):
            legacy_numeric = None
        if legacy_numeric and legacy_numeric > 0:
            source = str(entry.get("source") or "spot")
            prices[source] = legacy_numeric
    return prices


def preferred_gold_price_24k(prices_24k):
    if not isinstance(prices_24k, dict):
        return None
    for source_key in GOLD_PREFERRED_SOURCE_KEYS:
        value = prices_24k.get(source_key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    for source_key in sorted(prices_24k):
        value = prices_24k.get(source_key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def merge_gold_history_versions(*versions):
    histories = []
    for version in versions:
        if not version:
            continue
        data = json.loads(version)
        if isinstance(data, list):
            histories.append(data)
    merged_by_date = {}
    for entries in histories:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            date = entry.get("date")
            if not date:
                continue
            current = merged_by_date.setdefault(date, {"date": date, "prices_24k": {}})
            current["prices_24k"].update(extract_gold_prices_24k_map(entry))

    merged = []
    for date in sorted(merged_by_date):
        prices_24k = merged_by_date[date]["prices_24k"]
        if not prices_24k:
            continue
        item = {"date": date, "prices_24k": prices_24k}
        preferred = preferred_gold_price_24k(prices_24k)
        if preferred:
            item["price_24k"] = preferred
        merged.append(item)
    return merged[-GOLD_HISTORY_LIMIT_DAYS:]


def merge_ring_history_versions(*versions):
    merged = {}
    for version in versions:
        if not version:
            continue
        data = json.loads(version)
        if not isinstance(data, dict):
            continue
        for url, ring_data in data.items():
            if not isinstance(ring_data, dict):
                continue
            current = merged.setdefault(url, {"name": "", "shop": "", "entries": []})
            if ring_data.get("name"):
                current["name"] = ring_data["name"]
            if ring_data.get("shop"):
                current["shop"] = ring_data["shop"]
            current["entries"] = merge_history_entries_by_date(
                [current["entries"], ring_data.get("entries", [])],
                "price",
            )[-300:]
    return merged


def resolve_history_conflicts(paths):
    for raw_path in paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8")
        current, incoming = split_conflicted_text(text)
        if path.name == GOLD_HISTORY_FILE.name:
            merged = merge_gold_history_versions(current, incoming)
        elif path.name == RING_HISTORY_FILE.name:
            merged = merge_ring_history_versions(current, incoming)
        else:
            raise ValueError(f"Ukjent historikkfil for konfliktoppløsning: {path}")
        save_json(path, merged)
        print(f"  Løste konflikt i {path.name}")


# ==========================================================================
# GULLPRIS
# ==========================================================================

def fetch_spot_gold_nok_per_gram_24k():
    """Henter gullpris i USD/oz og USD/NOK-kurs, returnerer kr/gram for rent (24K) gull."""
    gold_res = request_with_retries("GET", "https://data-asg.goldprice.org/dbXRates/USD", headers=HEADERS, timeout=15)
    usd_per_oz = gold_res.json()["items"][0]["xauPrice"]

    fx_res = request_with_retries("GET", "https://api.frankfurter.app/latest?from=USD&to=NOK", headers=HEADERS, timeout=15)
    nok_rate = fx_res.json()["rates"]["NOK"]

    return (usd_per_oz * nok_rate) / 31.1034768


def extract_gold_price_24k_from_html(html):
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.find_all("tr"):
        row_text = " ".join(row.stripped_strings)
        if not re.search(r"(24\s*k|999(?:[,.]\d+)?)", row_text, flags=re.IGNORECASE):
            continue
        for match in re.findall(r"\d{1,3}(?:[ .]\d{3})*(?:,\d{1,2})?\s?(?:kr|NOK)?", row_text, flags=re.IGNORECASE):
            price = parse_nok_number(match)
            if price and GOLD_MIN_NOK_PER_GRAM_24K <= price <= GOLD_MAX_NOK_PER_GRAM_24K:
                return price

    text = soup.get_text(" ", strip=True)
    patterns = [
        r"(?:24\s*k|999(?:[,.]\d+)?)[^\d]{0,20}(\d{1,3}(?:[ .]\d{3})*(?:,\d{1,2})?)\s?(?:kr|NOK)",
        r"(\d{1,3}(?:[ .]\d{3})*(?:,\d{1,2})?)\s?(?:kr|NOK)?[^\d]{0,20}(?:24\s*k|999(?:[,.]\d+)?)",
    ]
    for pattern in patterns:
        for value in re.findall(pattern, text, flags=re.IGNORECASE):
            price = parse_nok_number(value)
            if price and GOLD_MIN_NOK_PER_GRAM_24K <= price <= GOLD_MAX_NOK_PER_GRAM_24K:
                return price
    return None


def fetch_gullbanken_nok_per_gram_24k():
    response = request_with_retries(
        "GET",
        "https://www.gullbanken.no/gullpriser/",
        headers=HEADERS,
        timeout=20,
    )
    price = extract_gold_price_24k_from_html(response.text)
    if price is None:
        raise ValueError("Fant ikke 24K-pris på gullbanken.no")
    return price


def fetch_gold_source_nok_per_gram_24k(source):
    if source["key"] == "spot":
        return fetch_spot_gold_nok_per_gram_24k()
    if source["key"] == "gullbanken":
        return fetch_gullbanken_nok_per_gram_24k()
    raise ValueError(f"Ukjent gullkilde: {source['key']}")


def is_sane_gold_price(price_24k, previous_price_24k):
    """Avviser gullpriser som er urealistiske eller hopper for mye siden sist,
    slik at en API-feil ikke stille forurenser historikken."""
    if not (GOLD_MIN_NOK_PER_GRAM_24K <= price_24k <= GOLD_MAX_NOK_PER_GRAM_24K):
        return False
    if previous_price_24k:
        change_pct = abs(price_24k - previous_price_24k) / previous_price_24k * 100
        if change_pct > GOLD_MAX_CHANGE_PCT:
            return False
    return True


def check_gold_price():
    print("Sjekker gullpris …")
    history = load_json(GOLD_HISTORY_FILE, [])
    today = datetime.now(timezone.utc).date().isoformat()
    new_prices_24k = {}

    for source in GOLD_SOURCES:
        try:
            price_24k = fetch_gold_source_nok_per_gram_24k(source)
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
            print(f"  Feil ved henting fra {source['name']}: {e}")
            continue

        prev_source_price = None
        for historical_entry in reversed(history):
            source_prices = extract_gold_prices_24k_map(historical_entry)
            if source["key"] in source_prices:
                prev_source_price = source_prices[source["key"]]
                break

        if not is_sane_gold_price(price_24k, prev_source_price):
            print(
                f"  ADVARSEL: skrapet gullpris fra {source['name']} ({price_24k:.1f} kr/g for 24K) "
                "ser urealistisk ut og lagres ikke."
            )
            send_ntfy(
                "⚠️ Mistenkelig gullpris",
                (
                    f"{source['name']}: hentet {price_24k:.0f} kr/g (24K), "
                    f"forrige var {prev_source_price or '–'}. Lagres ikke automatisk."
                ),
                priority="high",
                tags="warning",
            )
            continue

        new_prices_24k[source["key"]] = price_24k
        print(f"  {source['name']}: {price_24k:.1f} kr/g (24K)")

    if not new_prices_24k:
        print("  Klarte ikke hente en gyldig gullpris fra noen kilde.")
        return

    price_24k = preferred_gold_price_24k(new_prices_24k)
    price_karat = price_24k * GOLD_KARAT
    today = datetime.now(timezone.utc).date().isoformat()
    print(f"  Pris nå ({GOLD_KARAT * 24:.0f}K): {price_karat:.1f} kr/g")

    prev = None
    if history:
        prev = preferred_gold_price_24k(extract_gold_prices_24k_map(history[-1]))
    if prev:
        diff_pct = (price_24k - prev) / prev * 100
        if diff_pct <= -GOLD_DROP_THRESHOLD_PCT:
            send_ntfy(
                "Gullprisen har gått ned 📉",
                f"Ned {abs(diff_pct):.2f}% — nå {price_karat:.0f} kr/g ({GOLD_KARAT*24:.0f}K)",
                priority="high",
            )
        else:
            log(f"  Endring: {diff_pct:+.2f}% (under terskel på {GOLD_DROP_THRESHOLD_PCT}%)")

    if history and history[-1].get("date") == today:
        merged_today = extract_gold_prices_24k_map(history[-1])
        merged_today.update(new_prices_24k)
        history[-1]["prices_24k"] = merged_today
        history[-1]["price_24k"] = preferred_gold_price_24k(merged_today)
    else:
        history.append({
            "date": today,
            "prices_24k": new_prices_24k,
            "price_24k": price_24k,
        })
    save_json(GOLD_HISTORY_FILE, history[-GOLD_HISTORY_LIMIT_DAYS:])


# ==========================================================================
# RINGPRISER
# ==========================================================================

def parse_nok_number(text):
    if text is None:
        return None
    text = str(text).strip()
    text = re.sub(r"(kr|NOK|,-)", "", text, flags=re.IGNORECASE)
    text = text.replace("\xa0", "").replace(" ", "")
    if re.search(r",\d{2}$", text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "").replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def extract_price(html, selector=None):
    """Prøver strukturerte kilder først (JSON-LD, meta, itemprop), som er
    langt mer pålitelige enn en fri-tekst-regex over hele siden. Regex-
    fallbacket er bevisst begrenset og bør helst unngås — bruk heller en
    per-nettsted CSS-selector-overstyring (`selector`) i rings.json hvis en
    butikk mangler strukturert data."""
    soup = BeautifulSoup(html, "html.parser")

    if selector:
        tag = soup.select_one(selector)
        if tag:
            price = parse_nok_number(tag.get("content") or tag.get_text())
            if price:
                log(f"  [funnet via CSS-selector {selector!r}]")
                return price

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            for o in (offers if isinstance(offers, list) else [offers] if offers else []):
                if isinstance(o, dict) and "price" in o:
                    price = parse_nok_number(o["price"])
                    if price:
                        log("  [funnet via JSON-LD]")
                        return price

    for prop in ["product:price:amount", "og:price:amount"]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            price = parse_nok_number(tag["content"])
            if price:
                log(f"  [funnet via meta {prop}]")
                return price

    tag = soup.find(attrs={"itemprop": "price"})
    if tag:
        price = parse_nok_number(tag.get("content") or tag.get_text())
        if price:
            log("  [funnet via itemprop=price]")
            return price

    # Siste utvei: se etter et pris-lignende mønster i selve teksten. Dette
    # er upålitelig (kan plukke opp frakt/andre priser på siden), så bruk
    # kun det første beløpet som ser ut som en "vanlig" pris i kr-format,
    # og la sanity-sjekken i check_ring_prices() luke ut åpenbare feil.
    text = soup.get_text(" ", strip=True)
    matches = re.findall(r"\d{1,3}(?:[ .]\d{3})*(?:,\d{2})?\s?(?:,-|kr)", text, flags=re.IGNORECASE)
    prices = [p for p in (parse_nok_number(m) for m in matches) if p and 500 < p < 500000]
    if prices:
        log(f"  [funnet via regex-fallback: {prices[:5]}]")
        return prices[0]

    return None


def fetch_ring_price(url, selector=None):
    resp = request_with_retries("GET", url, headers=HEADERS, timeout=15)
    return extract_price(resp.text, selector=selector)


def is_sane_ring_price(price, previous_price):
    if not (RING_MIN_PRICE <= price <= RING_MAX_PRICE):
        return False
    if previous_price:
        change_pct = abs(price - previous_price) / previous_price * 100
        if change_pct > RING_MAX_CHANGE_PCT:
            return False
    return True


def check_ring_prices():
    print("Sjekker ringpriser …")
    history = load_json(RING_HISTORY_FILE, {})
    today = datetime.now(timezone.utc).date().isoformat()
    current_prices = {}

    for ring in RINGS:
        name, url = ring["name"], ring["url"]
        selector = ring.get("selector")
        print(f"  {ring.get('shop', '?')} – {name}")
        try:
            price = fetch_ring_price(url, selector=selector)
        except requests.RequestException as e:
            print(f"    Feil ved henting: {e}")
            continue
        if price is None:
            print("    Fant ikke pris automatisk (kjør med --debug for detaljer).")
            continue

        previous_entries = history.get(url, {}).get("entries", [])
        previous_price = previous_entries[-1]["price"] if previous_entries else None
        if not is_sane_ring_price(price, previous_price):
            print(
                f"    ADVARSEL: skrapet pris ({price:.0f} kr) ser urealistisk ut "
                f"(forrige: {previous_price or '–'} kr) og lagres ikke."
            )
            send_ntfy(
                "⚠️ Mistenkelig ringpris",
                f"{ring.get('shop', '?')} – {name}: hentet {price:.0f} kr, forrige {previous_price or '–'} kr. Lagres ikke.",
                priority="high",
                tags="warning",
            )
            continue

        print(f"    Pris nå: {price:.0f} kr")
        current_prices[url] = price

        entries = history.setdefault(url, {"name": name, "shop": ring.get("shop", ""), "entries": []})["entries"]
        if entries and entries[-1]["date"] == today:
            entries[-1]["price"] = price
        else:
            entries.append({"date": today, "price": price})
        history[url]["entries"] = entries[-300:]

    save_json(RING_HISTORY_FILE, history)

    if len(current_prices) < 2:
        return  # trenger minst to priser for å si hva som er "billigst"

    cheapest_url = min(
        (u for u in history if u in current_prices),
        key=lambda u: current_prices[u],
        default=None,
    )
    if not cheapest_url:
        return

    cheapest = history[cheapest_url]
    entries = cheapest["entries"]
    if len(entries) < 3:
        return  # trenger litt historikk for å vite hva som er "vanlig"

    prev_prices = [e["price"] for e in entries[:-1]]
    avg = sum(prev_prices) / len(prev_prices)
    current = entries[-1]["price"]

    if current < avg * (1 - RING_BELOW_AVG_PCT / 100):
        send_ntfy(
            f"{cheapest['shop']} er nå billigst 💍",
            f"{cheapest['name']}: {current:.0f} kr — lavere enn snittet ({avg:.0f} kr)",
            priority="high",
        )
    else:
        log(f"  Billigst er {cheapest['shop']} ({current:.0f} kr), snitt er {avg:.0f} kr — ikke lavt nok ennå.")


# ==========================================================================
# HOVEDPROGRAM
# ==========================================================================

def main():
    if RESOLVE_HISTORY_CONFLICTS_FLAG in sys.argv:
        flag_index = sys.argv.index(RESOLVE_HISTORY_CONFLICTS_FLAG)
        conflict_paths = sys.argv[flag_index + 1:] or [str(GOLD_HISTORY_FILE), str(RING_HISTORY_FILE)]
        resolve_history_conflicts(conflict_paths)
        return
    send_ntfy("✓ Prissjekk startet", "Gullpris-varsler kjører nå...", priority="default", tags="heart")
    check_gold_price()
    print()
    check_ring_prices()
    print("\nFerdig.")


if __name__ == "__main__":
    main()
