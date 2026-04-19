"""
Scraper til automatisk opdatering af data.json for sundhedsraad.html.

Formål
------
Henter medlemslister, referater og links fra regionernes officielle
hjemmesider og opdaterer data.json, som sundhedsraad.html læser fra.

Kørsel
------
1) Manuelt:
   python3 scraper.py

2) På skema (cron, dagligt kl. 06:00):
   0 6 * * * cd /sti/til/mappe && /usr/bin/python3 scraper.py >> scraper.log 2>&1

3) GitHub Actions (daglig):
   Se eksempel i README.md -> "Automatisk opdatering".

Output
------
Opdaterer data.json in-place og skriver data.json.backup.<timestamp> før ændring.
Skriver også til scraper.log.

Afhængigheder
-------------
    pip install requests beautifulsoup4 lxml

Arkitektur
----------
Hver region har sin egen adapter-klasse, der ved:
  - hvor hvert sundhedsråds side/referater ligger
  - hvordan man parser medlemsliste og referatlinks
Adapterne returnerer struktureret data, der flettes ind i data.json.

De officielle sider ændrer sig jævnligt, så adapterne er skrevet
defensivt: en fejl i én adapter stopper ikke de andre.
"""

from __future__ import annotations
import json
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4 lxml")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
DATA_PATH = HERE / "data.json"
LOG_PATH = HERE / "scraper.log"

HTTP_TIMEOUT = 20
USER_AGENT = (
    "SundhedsraadMonitor/0.1 (+https://example.org) "
    "Python-requests research tool"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("scraper")


# ---------------------------------------------------------------------------
# Datamodel
# ---------------------------------------------------------------------------
@dataclass
class ScrapedCouncil:
    """Data hentet for ét sundhedsråd."""
    council_id: str
    formand: dict | None = None          # {"name": str, "party": str}
    naestformand: dict | None = None
    regional_members: list[dict] = field(default_factory=list)
    municipal_members: list[dict] = field(default_factory=list)
    referater_url: str | None = None
    official_url: str | None = None
    referater: list[dict] = field(default_factory=list)  # [{"title","date","url"}]


# ---------------------------------------------------------------------------
# HTTP helper med høflig rate-limit
# ---------------------------------------------------------------------------
class PoliteFetcher:
    def __init__(self, delay_seconds: float = 1.5):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = delay_seconds
        self._last = 0.0

    def get(self, url: str) -> BeautifulSoup | None:
        elapsed = time.time() - self._last
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        try:
            r = self.session.get(url, timeout=HTTP_TIMEOUT)
            self._last = time.time()
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("Fetch failed: %s (%s)", url, e)
            return None
        try:
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning("Parse failed: %s (%s)", url, e)
            return None


# ---------------------------------------------------------------------------
# Region-adaptere
# ---------------------------------------------------------------------------
class BaseAdapter:
    """Basis-klasse. Hver region implementerer fetch_all() -> list[ScrapedCouncil]."""

    def __init__(self, fetcher: PoliteFetcher):
        self.fetcher = fetcher

    def fetch_all(self) -> list[ScrapedCouncil]:
        raise NotImplementedError

    # Hjælpere til parsing af danske politiker-lister
    PARTY_RE = re.compile(r"\(([A-ZÆØÅ])\)\s*$")

    def split_name_party(self, text: str) -> tuple[str, str]:
        """'Lars Gaardhøj (A)' -> ('Lars Gaardhøj', 'A')"""
        text = text.strip().replace("\xa0", " ")
        m = self.PARTY_RE.search(text)
        if not m:
            return text, ""
        name = self.PARTY_RE.sub("", text).strip(" ,.")
        return name, m.group(1)


class RegionMidtjyllandAdapter(BaseAdapter):
    """
    Region Midtjylland: 5 sundhedsråd — Aarhus, Horsens, Kronjylland, Midt, Vestjylland.
    Hver har egen side under rm.dk/politik/udvalg-og-modefora/politiske-udvalg/sundhedsrad/.
    """
    BASE = "https://www.rm.dk"
    COUNCIL_URLS = {
        "aarhus":     f"{BASE}/politik/udvalg-og-modefora/politiske-udvalg/sundhedsrad/sundhedsrad-aarhus/",
        "horsens":    f"{BASE}/politik/udvalg-og-modefora/politiske-udvalg/sundhedsrad/sundhedsrad-horsens/",
        "kronjylland":f"{BASE}/politik/udvalg-og-modefora/politiske-udvalg/sundhedsrad/sundhedsrad-kronjylland/",
        "midt":       f"{BASE}/politik/udvalg-og-modefora/politiske-udvalg/sundhedsrad/sundhedsrad-midt/",
        "vestjylland":f"{BASE}/politik/udvalg-og-modefora/politiske-udvalg/sundhedsrad/sundhedsrad-vestjylland/",
    }

    def fetch_all(self) -> list[ScrapedCouncil]:
        results = []
        for cid, url in self.COUNCIL_URLS.items():
            log.info("[RM] Fetching %s", cid)
            soup = self.fetcher.get(url)
            if not soup:
                continue
            council = ScrapedCouncil(council_id=cid, official_url=url, referater_url=url)
            council = self._parse_council_page(soup, council)
            results.append(council)
        return results

    def _parse_council_page(self, soup: BeautifulSoup, council: ScrapedCouncil) -> ScrapedCouncil:
        # Heuristik: leder efter sektion med "medlemmer" eller liste under politikere.
        # Regionens sider ændrer sig, så vi tager enhver liste der ligner medlemmer.
        members = self._extract_members(soup)
        for m in members:
            if "formand" in (m.get("role") or "").lower() and "næst" not in m["role"].lower():
                council.formand = {"name": m["name"], "party": m["party"]}
            elif "næstformand" in (m.get("role") or "").lower():
                council.naestformand = {"name": m["name"], "party": m["party"]}
            if m.get("origin") == "municipal":
                council.municipal_members.append(m)
            else:
                council.regional_members.append(m)
        # Hent eventuelle referatlinks (pdf/dagsorden)
        council.referater = self._extract_referater(soup, council.official_url or "")
        return council

    def _extract_members(self, soup: BeautifulSoup) -> list[dict]:
        out: list[dict] = []
        # Led efter <h2|h3> som indeholder 'medlemmer' og tag efterfølgende liste
        for header in soup.find_all(["h2", "h3", "h4"]):
            if "medlem" in header.get_text(strip=True).lower():
                sib = header.find_next_sibling()
                while sib and sib.name not in ("h2", "h3"):
                    if sib.name in ("ul", "ol"):
                        for li in sib.find_all("li", recursive=False):
                            text = li.get_text(" ", strip=True)
                            name, party = self.split_name_party(text)
                            if name:
                                role = "municipal" if "kommune" in text.lower() else "regional"
                                out.append({
                                    "name": name,
                                    "party": party,
                                    "origin": role,
                                    "role": "",
                                })
                    sib = sib.find_next_sibling()
        return out

    def _extract_referater(self, soup: BeautifulSoup, base: str) -> list[dict]:
        referater = []
        for a in soup.find_all("a"):
            href = a.get("href", "")
            text = a.get_text(" ", strip=True)
            if not href or not text:
                continue
            if re.search(r"(referat|dagsorden|m\u00f8de)", text, re.I):
                referater.append({
                    "title": text[:200],
                    "url": urljoin(base, href),
                })
        return referater[:20]


class RegionSyddanmarkAdapter(BaseAdapter):
    """Region Syddanmark: 4 sundhedsråd."""
    BASE = "https://regionsyddanmark.dk"
    COUNCIL_URLS = {
        "fyn":            f"{BASE}/politik/politiske-udvalg-og-hverv/sundhedsrad-fyn",
        "lillebaelt":     f"{BASE}/politik/politiske-udvalg-og-hverv/sundhedsrad-lillebaelt",
        "sydvestjylland": f"{BASE}/politik/politiske-udvalg-og-hverv/sundhedsrad-sydvestjylland",
        "soenderjylland": f"{BASE}/politik/politiske-udvalg-og-hverv/sundhedsrad-sonderjylland",
    }

    def fetch_all(self) -> list[ScrapedCouncil]:
        results = []
        for cid, url in self.COUNCIL_URLS.items():
            log.info("[RSD] Fetching %s", cid)
            soup = self.fetcher.get(url)
            if not soup:
                continue
            council = ScrapedCouncil(council_id=cid, official_url=url, referater_url=url)
            # Samme pragmatiske tilgang som RM
            council = RegionMidtjyllandAdapter._parse_council_page(self, soup, council)  # genbrug
            results.append(council)
        return results


class RegionNordjyllandAdapter(BaseAdapter):
    """Region Nordjylland: 2 sundhedsråd — Limfjorden, Vendsyssel."""
    BASE = "https://rn.dk"
    COUNCIL_URLS = {
        "limfjorden": f"{BASE}/politik/sundhedsraad/sundhedsraad-limfjorden",
        "vendsyssel": f"{BASE}/politik/sundhedsraad/sundhedsraad-vendsyssel",
    }

    def fetch_all(self) -> list[ScrapedCouncil]:
        results = []
        for cid, url in self.COUNCIL_URLS.items():
            log.info("[RN] Fetching %s", cid)
            soup = self.fetcher.get(url)
            if not soup:
                continue
            council = ScrapedCouncil(council_id=cid, official_url=url, referater_url=url)
            council = RegionMidtjyllandAdapter._parse_council_page(self, soup, council)
            results.append(council)
        return results


class RegionOestdanmarkAdapter(BaseAdapter):
    """Region Østdanmark: 6 sundhedsråd (forberedende i 2026)."""
    BASE = "https://www.regionoest.dk"
    COUNCIL_URLS = {
        "hovedstaden":           f"{BASE}/politik/sundhedsraad/hovedstaden",
        "koebenhavns-omegn-nord":f"{BASE}/politik/sundhedsraad/koebenhavns-omegn-nord",
        "nordsjaelland":         f"{BASE}/politik/sundhedsraad/nordsjaelland",
        "amager-vestegnen":      f"{BASE}/politik/sundhedsraad/amager-og-vestegnen",
        "oestsjaelland-oerne":   f"{BASE}/politik/sundhedsraad/oestsjaelland-og-oerne",
        "midt-vestsjaelland":    f"{BASE}/politik/sundhedsraad/midt-og-vestsjaelland",
    }

    def fetch_all(self) -> list[ScrapedCouncil]:
        results = []
        for cid, url in self.COUNCIL_URLS.items():
            log.info("[RO] Fetching %s", cid)
            soup = self.fetcher.get(url)
            if not soup:
                continue
            council = ScrapedCouncil(council_id=cid, official_url=url, referater_url=url)
            council = RegionMidtjyllandAdapter._parse_council_page(self, soup, council)
            results.append(council)
        return results


# ---------------------------------------------------------------------------
# Orkestrering
# ---------------------------------------------------------------------------
def load_data() -> dict:
    if not DATA_PATH.exists():
        log.error("data.json not found at %s", DATA_PATH)
        sys.exit(1)
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def save_data(data: dict) -> None:
    # Backup
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = DATA_PATH.with_name(f"data.json.backup.{ts}")
    shutil.copy(DATA_PATH, backup)
    log.info("Backup written: %s", backup.name)
    DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("data.json updated.")


def merge_council(existing: dict, scraped: ScrapedCouncil) -> dict:
    """Flet scraped data ind over eksisterende råd.

    Politik: scraper-data vinder hvis det er non-empty; ellers bevares eksisterende.
    Dette betyder at manuelle data ikke overskrives af tomt scrape-resultat."""
    if scraped.formand and scraped.formand.get("name"):
        existing["formand"] = scraped.formand
    if scraped.naestformand and scraped.naestformand.get("name"):
        existing["naestformand"] = scraped.naestformand
    if scraped.regional_members:
        existing["regionalMembers"] = scraped.regional_members
    if scraped.municipal_members:
        existing["municipalMembers"] = scraped.municipal_members
    if scraped.referater_url:
        existing["referaterUrl"] = scraped.referater_url
    if scraped.official_url:
        existing["officialUrl"] = scraped.official_url
    if scraped.referater:
        existing["referater"] = scraped.referater
    return existing


def run():
    log.info("=== Scraper run started at %s ===", datetime.now().isoformat())
    data = load_data()
    fetcher = PoliteFetcher(delay_seconds=1.5)

    adapters: list[BaseAdapter] = [
        RegionMidtjyllandAdapter(fetcher),
        RegionSyddanmarkAdapter(fetcher),
        RegionNordjyllandAdapter(fetcher),
        RegionOestdanmarkAdapter(fetcher),
    ]

    all_scraped: list[ScrapedCouncil] = []
    for adapter in adapters:
        try:
            scraped = adapter.fetch_all()
            log.info("%s returned %d councils", adapter.__class__.__name__, len(scraped))
            all_scraped.extend(scraped)
        except Exception as e:
            log.exception("Adapter %s failed: %s", adapter.__class__.__name__, e)

    scraped_by_id = {s.council_id: s for s in all_scraped}
    updated = 0
    for c in data["councils"]:
        if c["id"] in scraped_by_id:
            before = json.dumps(c, ensure_ascii=False)
            c = merge_council(c, scraped_by_id[c["id"]])
            after = json.dumps(c, ensure_ascii=False)
            if before != after:
                updated += 1

    data["meta"]["lastUpdated"] = datetime.now().strftime("%Y-%m-%d")
    data["meta"]["lastScrapeRun"] = datetime.now(timezone.utc).isoformat()
    log.info("Merged: %d of %d councils updated", updated, len(data["councils"]))

    save_data(data)
    log.info("=== Scraper run finished ===")


if __name__ == "__main__":
    run()
