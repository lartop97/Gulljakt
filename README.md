# Gulljakt — Gull & Gifteringer

En liten, personlig plattform som følger med på gullprisen og prisen på et
utvalg gifteringer, og sender push-varsler når det ser ut til å lønne seg å
kjøpe.

Prosjektet består av to deler:

- **`gullsmed_prisvarsler.py`** — et Python-script med vanlig internett-tilgang.
  Det henter dagens gullpris (USD/oz → kr/gram), sjekker prisen på ringene i
  [`rings.json`](rings.json), lagrer historikk, og sender push-varsler via
  [ntfy.sh](https://ntfy.sh) når gullprisen faller eller en ring er både
  billigst av de du følger *og* under sitt eget historiske snitt.
- **`index.html`** — en frittstående, statisk nettside (ingen backend) som
  viser gullpris, prisutvikling og ringliste. Den henter den automatisk
  genererte historikken (`gullpris_historikk.json`, `ring_historikk.json`)
  direkte fra GitHub Pages, og lar deg i tillegg registrere priser manuelt
  lokalt (lagret i nettleserens `localStorage`).

## Arkitektur / dataflyt

```
GitHub Actions (daglig, cron)
  └─ gullsmed_prisvarsler.py
       ├─ henter gullpris + valutakurs → gullpris_historikk.json
       ├─ henter ringpriser (rings.json) → ring_historikk.json
       ├─ sender push-varsler via ntfy.sh
       └─ committer oppdatert historikk til repoet

GitHub Pages (ved push til main)
  └─ publiserer index.html + rings.json + *_historikk.json

Nettleser
  └─ index.html henter rings.json + *_historikk.json fra Pages
       og kombinerer med eventuelle manuelle lokale registreringer
```

`rings.json` er **delt konfigurasjon** mellom Python-scriptet og nettsiden,
slik at ringlisten aldri kommer ut av synk mellom backend og frontend.

## Oppsett

### 1. Ringkonfigurasjon

Rediger [`rings.json`](rings.json) for å legge til/fjerne/endre ringer som
skal følges. Hvert element støtter:

```json
{
  "name": "Navn på modell",
  "shop": "Butikknavn",
  "material": "Materiale (valgfritt, kun for visning)",
  "width": "Bredde (valgfritt, kun for visning)",
  "price": "Startpris i kr (valgfritt)",
  "url": "https://butikk.no/produkt",
  "selector": "CSS-selector (valgfritt, overstyrer automatisk prisgjenkjenning)"
}
```

### 2. ntfy-varsler

1. Velg et hemmelig emnenavn på [ntfy.sh](https://ntfy.sh) (f.eks.
   `gulljakt-<tilfeldig-streng>`) og abonner på det i ntfy-appen.
2. Legg emnenavnet inn som repository-secret `NTFY_TOPIC`
   (Settings → Secrets and variables → Actions).
3. Scriptet nekter å sende varsler dersom `NTFY_TOPIC` fortsatt er
   plassholderverdien `sett-ditt-ntfy-emne-her` — dette forhindrer at
   varsler stille sendes til et offentlig, ubrukt emne.

### 3. Kjør lokalt

```bash
pip install -r requirements.txt
python3 gullsmed_prisvarsler.py --debug
```

### 4. GitHub Actions

To workflows kjører automatisk:

- **`.github/workflows/prisvarsler.yml`** — henter priser daglig (06:00 UTC)
  og committer oppdatert historikk.
- **`.github/workflows/pages.yml`** — publiserer nettsiden til GitHub Pages
  ved push til `main`.

For at Pages-workflowen skal virke må GitHub Pages være satt til kilde
**GitHub Actions** under Settings → Pages i repoet.

### 5. Tester

```bash
pip install pytest
pytest tests/
```

Testene dekker prisparsing (`parse_nok_number`), prisuthenting
(`extract_price`, inkl. regresjonstest for tidligere feil der regex-fallbacket
plukket opp feil beløp) og sanity-sjekkene som forhindrer at åpenbart
feilaktige skrapte priser lagres i historikken.

## Kjente begrensninger

- Enkelttbruker/personlig prosjekt: ingen innlogging, ingen flerbruker­støtte.
- Kun norske kroner og et lite, hardkodet utvalg gullsmeder.
- Manuelle prisregistreringer lagres kun lokalt i nettleseren
  (`localStorage`) og synkroniseres ikke mellom enheter.
