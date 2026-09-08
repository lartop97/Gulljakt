#!/usr/bin/env python3
"""
gullsmed_prisvarsler.py (v2)

Kjører HELE automatikken som nettsiden ikke får lov til å gjøre selv
(artefakt-sandkassen til Claude tillater ikke at siden henter data fra
andre nettsteder eller sender push på egen hånd). Dette scriptet har
vanlig internett-tilgang og kan derfor:

  1. Hente dagens gullpris (USD/oz) + USD/NOK-kurs, regne om til kr/gram
     for valgt karat, og varsle når prisen faller mer enn valgt terskel.
  2. Sjekke prisen på ringene dine hos Thune, David-Andersen og Gullfunn,
     og varsle når én av dem er BÅDE billigst av de du følger OG lavere
     enn sitt eget historiske snitt (ikke bare "alltid billigst").
  3. Sende ekte push via ntfy.sh for begge deler.

--------------------------------------------------------------------------
OPPSETT
--------------------------------------------------------------------------
1. pip install requests beautifulsoup4
2. Fyll inn NTFY_TOPIC og juster RINGS / GOLD_KARAT / GOLD_DROP_THRESHOLD_PCT
   nedenfor.
3. Test manuelt:  python3 gullsmed_prisvarsler.py --debug
4. Legg i cron for daglig automatisk kjøring, f.eks. kl 08:00:
   crontab -e
   0 8 * * * /usr/bin/python3 /full/path/gullsmed_prisvarsler.py >> /full/path/log.txt 2>&1

Historikk lagres i to filer ved siden av scriptet:
  gullpris_historikk.json   (gullpris over tid)
  ring_historikk.json       (pris per ring over tid)
--------------------------------------------------------------------------
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ==========================================================================
# KONFIGURASJON — tilpass dette
# ==========================================================================

# NTFY_TOPIC kan settes her direkte, ELLER som miljøvariabel/GitHub-secret
# (miljøvariabelen vinner hvis den finnes) — praktisk når scriptet kjører
# via GitHub Actions og du ikke vil ha emnenavnet liggende i selve koden.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "sett-ditt-ntfy-emne-her")
NTFY_SERVER = "https://ntfy.sh"

GOLD_KARAT = 0.75              # 18K = 0.75, 14K = 0.585, 9K = 0.375, 22K = 0.916
GOLD_DROP_THRESHOLD_PCT = 1.0  # varsle når gullprisen faller mer enn dette siden forrige sjekk
RING_BELOW_AVG_PCT = 3.0       # varsle når en ring er > 3% under sitt eget snitt

RINGS = [
    {
        "name": "Giftering Promise 4 mm oval",
        "shop": "Thune",
        "url": "https://www.thune.no/giftering-promise-4-mm-gult-gull-oval",
    },
    {
        "name": "Giftering, D-A Solid",
        "shop": "David-Andersen",
        "url": "https://david-andersen.no/giftering-d-a-solid/?productId=solid",
    },
    {
        "name": "Giftering 585 gult gull",
        "shop": "Gullfunn",
        "url": "https://www.gullfunn.no/produkter/1001206/giftering-i-585-gult-gull--4-mm-bredde",
    },
]

GOLD_HISTORY_FILE = Path(__file__).parent / "gullpris_historikk.json"
RING_HISTORY_FILE = Path(__file__).parent / "ring_historikk.json"
GOLD_HISTORY_LIMIT_DAYS = 730
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PrisvarslerBot/1.0; personlig prissjekk)"}
DEBUG = "--debug" in sys.argv


# ==========================================================================
# HJELPEFUNKSJONER
# ==========================================================================

def log(*args):
    if DEBUG:
        print(*args)


def send_ntfy(title, message, priority="default", tags="moneybag"):
    try:
        response = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
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


# ==========================================================================
# GULLPRIS
# ==========================================================================

def fetch_gold_nok_per_gram_24k():
    """Henter gullpris i USD/oz og USD/NOK-kurs, returnerer kr/gram for rent (24K) gull."""
    gold_res = requests.get("https://data-asg.goldprice.org/dbXRates/USD", headers=HEADERS, timeout=15)
    gold_res.raise_for_status()
    usd_per_oz = gold_res.json()["items"][0]["xauPrice"]

    fx_res = requests.get("https://api.frankfurter.app/latest?from=USD&to=NOK", headers=HEADERS, timeout=15)
    fx_res.raise_for_status()
    nok_rate = fx_res.json()["rates"]["NOK"]

    return (usd_per_oz * nok_rate) / 31.1034768


def check_gold_price():
    print("Sjekker gullpris …")
    history = load_json(GOLD_HISTORY_FILE, [])
    try:
        price_24k = fetch_gold_nok_per_gram_24k()
    except Exception as e:
        print(f"  Feil ved henting av gullpris: {e}")
        return

    price_karat = price_24k * GOLD_KARAT
    today = datetime.now().date().isoformat()
    print(f"  Pris nå ({GOLD_KARAT * 24:.0f}K): {price_karat:.1f} kr/g")

    if history:
        prev = history[-1]["price_24k"]
        diff_pct = (price_24k - prev) / prev * 100
        if diff_pct <= -GOLD_DROP_THRESHOLD_PCT:
            send_ntfy(
                "Gullprisen har gått ned 📉",
                f"Ned {abs(diff_pct):.2f}% — nå {price_karat:.0f} kr/g ({GOLD_KARAT*24:.0f}K)",
                priority="high",
            )
        else:
            log(f"  Endring: {diff_pct:+.2f}% (under terskel på {GOLD_DROP_THRESHOLD_PCT}%)")

    if history and history[-1]["date"] == today:
        history[-1]["price_24k"] = price_24k
    else:
        history.append({"date": today, "price_24k": price_24k})
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


def extract_price(html):
    soup = BeautifulSoup(html, "html.parser")

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

    text = soup.get_text(" ", strip=True)
    matches = re.findall(r"\d{1,3}(?:[ .]\d{3})*(?:,\d{2})?\s?(?:,-|kr)", text, flags=re.IGNORECASE)
    prices = [p for p in (parse_nok_number(m) for m in matches) if p and 500 < p < 500000]
    if prices:
        log(f"  [funnet via regex-fallback: {prices[:5]}]")
        return prices[0]

    return None


def fetch_ring_price(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return extract_price(resp.text)


def check_ring_prices():
    print("Sjekker ringpriser …")
    history = load_json(RING_HISTORY_FILE, {})
    today = datetime.now().date().isoformat()
    current_prices = {}

    for ring in RINGS:
        name, url = ring["name"], ring["url"]
        print(f"  {ring['shop']} – {name}")
        try:
            price = fetch_ring_price(url)
        except requests.RequestException as e:
            print(f"    Feil ved henting: {e}")
            continue
        if price is None:
            print("    Fant ikke pris automatisk (kjør med --debug for detaljer).")
            continue

        print(f"    Pris nå: {price:.0f} kr")
        current_prices[name] = price

        entries = history.setdefault(url, {"name": name, "shop": ring["shop"], "entries": []})["entries"]
        if entries and entries[-1]["date"] == today:
            entries[-1]["price"] = price
        else:
            entries.append({"date": today, "price": price})
        history[url]["entries"] = entries[-300:]

    save_json(RING_HISTORY_FILE, history)

    if len(current_prices) < 2:
        return  # trenger minst to priser for å si hva som er "billigst"

    cheapest_url = min(
        (u for u in history if history[u]["name"] in current_prices),
        key=lambda u: current_prices[history[u]["name"]],
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
    send_ntfy("✓ Prissjekk startet", "Gullpris-varsler kjører nå...", priority="default", tags="heart")
    check_gold_price()
    print()
    check_ring_prices()
    print("\nFerdig.")


if __name__ == "__main__":
    main()
