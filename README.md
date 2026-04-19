# Sundhedsrådene — interaktiv oversigt

Et journalistisk researchværktøj der giver et hurtigt overblik over Danmarks 17 nye sundhedsråd, deres deltagere, økonomi og referater.

## Indhold

- **`sundhedsraad.html`** — Selvstændig interaktiv hjemmeside. Dobbeltklik for at åbne i browseren.
- **`data.json`** — Datasættet der driver siden. Kan redigeres manuelt eller opdateres automatisk af scraperen.
- **`scraper.py`** — Python-script der henter opdaterede medlemslister og referater fra regionernes officielle sider.
- **`udvikler-brief.docx`** — Brief til en udvikler med mail-tekst og teknisk opgavebeskrivelse.
- **`README.md`** — Denne fil.

## Sådan kommer du i gang

### 1. Åbn siden
Dobbeltklik på `sundhedsraad.html`. Siden fungerer også uden internet, fordi datasættet er indlejret som fallback.

### 2. (Anbefalet) Kør via lokal webserver
For at sikre at siden altid bruger den nyeste `data.json`:

```bash
cd sti/til/mappen
python3 -m http.server 8000
# åbn http://localhost:8000/sundhedsraad.html
```

### 3. Deploy til web
Upload `sundhedsraad.html` og `data.json` til en hvilken som helst statisk host: GitHub Pages, Netlify, Cloudflare Pages, en S3-bucket eller et simpelt webhotel. Ingen backend påkrævet.

## Automatisk opdatering

### Manuel kørsel af scraperen

```bash
pip install requests beautifulsoup4 lxml
python3 scraper.py
```

Scraperen:
- Laver backup af `data.json` før ændringer (`data.json.backup.<timestamp>`)
- Henter medlemslister og referatlinks for alle 17 råd
- Fletter ind i `data.json` — manuelt indtastede data bliver *ikke* overskrevet af tomme scrape-resultater
- Logger til `scraper.log`

### Daglig opdatering via cron (Linux/macOS)

```cron
# Hver morgen kl. 06:00
0 6 * * * cd /sti/til/mappen && /usr/bin/python3 scraper.py >> scraper.log 2>&1
```

### Daglig opdatering via GitHub Actions

Opret `.github/workflows/update-data.yml`:

```yaml
name: Update sundhedsråd data
on:
  schedule:
    - cron: "0 5 * * *"   # 05:00 UTC = 06:00 / 07:00 DK
  workflow_dispatch:

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install requests beautifulsoup4 lxml
      - run: python3 scraper.py
      - name: Commit if changed
        run: |
          git config user.name  "sundhedsraad-bot"
          git config user.email "bot@example.org"
          git add data.json
          git diff --cached --quiet || (git commit -m "Daily update $(date -I)" && git push)
```

Hosting via GitHub Pages vil så automatisk deploye den opdaterede side hver dag.

## Datastruktur

`data.json` har disse hovedsektioner:

- **`meta`** — senest opdateret, datakilder, version
- **`economy`** — timeline (325 mio. i 2026 → 2 mia. i 2030) og ekstra nøgletal
- **`regions`** — de fire regioner med regionsrådsformand
- **`councils`** — de 17 sundhedsråd med formand, næstformand, medlemmer og links
- **`parties`** — partiforkortelser og farver (til chips i UI)

Hvert råd har feltet `id` som er nøglen scraperen bruger til at flette ind. Føj gerne manuelt medlemmer ind i `regionalMembers` og `municipalMembers` — de bevares ved næste scrape-kørsel.

## Begrænsninger ved nuværende datasæt

- **Formænd og næstformænd:** verificeret for alle 17 råd via pressemeddelelser og aktuelle kilder.
- **Regioner og geografi:** dækning pr. kommune er verificeret.
- **Fulde medlemslister:** foreløbigt delvist udfyldt. Scraperen bygger listerne ud, efterhånden som regionernes officielle sider publicerer dem i løbet af 2026.
- **Referater:** links peger på regionernes politik-sider. Scraperen udvider dette med direkte referat-links pr. møde.

Nogle rådssider er endnu ikke publiceret med fulde medlemslister (pr. april 2026), så scraperens output vil vokse i løbet af året.

## Tilpasning

- **Farver og design:** ret CSS-variablerne i `<style>` i toppen af `sundhedsraad.html`.
- **Flere kilder:** tilføj objekter til `meta.sources[]` i `data.json`.
- **Tilføj et råd manuelt:** append et nyt objekt til `councils[]` i `data.json`. UI'et opdateres automatisk.
- **Flere adaptere:** udvid `scraper.py` med yderligere `BaseAdapter`-klasser for kl.dk, sundhedsdatabank.dk osv.

## Juridiske / etiske overvejelser

- Scraperen identificerer sig via `User-Agent` og respekterer rate-limits (1,5 s mellem requests).
- Data hentes udelukkende fra offentlige politiske sider (referater, udvalgssammensætning).
- Ingen personoplysninger ud over hvad der er offentliggjort af regionerne selv.
- Verificér altid mod primære kilder før publicering.
