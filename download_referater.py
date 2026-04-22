#!/usr/bin/env python3
"""
download_referater.py
---------------------
Scraper der dagligt tjekker alle 17 sundhedsråds referat-sider og henter nye PDF'er.

Arkitektur:
  - Config: referater-config.json (hvilke råd, hvilke URL'er, hvilken adapter).
  - Manifest: referater-manifest.json (hvad har vi allerede hentet).
  - Output: referater/<council-id>/<YYYY-MM-DD>_<slug>.pdf
  - Log: referater/_log.txt (sidste kørsels resultat).

Fail-soft: Hvis én regions URL er skiftet eller ét råd fejler, fortsætter scriptet
med resten. Fejl logges til _log.txt og til stdout.

Afhængigheder:  requests, beautifulsoup4, lxml
  pip install requests beautifulsoup4 lxml
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "referater-config.json"
MANIFEST_FILE = ROOT / "referater-manifest.json"
REFERATER_DIR = ROOT / "referater"
LOG_FILE = REFERATER_DIR / "_log.txt"

USER_AGENT = "SundhedsraadsReferater/1.0 (+github.com/afpihl/sundhedsraad)"
TIMEOUT = 30
MAX_PDF_SIZE_MB = 50  # sikkerhedsgrænse


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    """Dansk-venlig slugify."""
    s = (s or "").strip().lower()
    table = str.maketrans("æøå", "aoa", "")
    s = s.translate(table)
    s = re.sub(r"[^a-z0-9\-_ ]+", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s or "unknown"


def parse_date(text: str) -> Optional[str]:
    """Uddrag første dato i teksten som YYYY-MM-DD. Støtter DD-MM-YYYY, DD/MM-YYYY,
    DD. måned YYYY."""
    if not text:
        return None
    # DD-MM-YYYY eller DD/MM-YYYY eller DD.MM.YYYY
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
        return f"{year}-{int(mon):02d}-{int(day):02d}"
    # DD-MM-YY
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2})", text)
    if m:
        day, mon, yy = m.group(1), m.group(2), m.group(3)
        year = 2000 + int(yy) if int(yy) < 60 else 1900 + int(yy)
        return f"{year}-{int(mon):02d}-{int(day):02d}"
    # Danske månedsnavne
    months = {
        "januar": 1, "februar": 2, "marts": 3, "april": 4, "maj": 5, "juni": 6,
        "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dec": 12,
    }
    m = re.search(r"(\d{1,2})\.?\s+([a-zæøåA-ZÆØÅ]+)\.?\s+(\d{4})", text)
    if m:
        day, mon_word, year = m.group(1), m.group(2).lower(), m.group(3)
        mon = months.get(mon_word)
        if mon:
            return f"{year}-{mon:02d}-{int(day):02d}"
    return None


def http_get(url: str, session: requests.Session, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "*/*")
    return session.get(url, headers=headers, timeout=TIMEOUT, **kwargs)


def load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def load_manifest() -> Dict:
    m = load_json(MANIFEST_FILE, {"pdfs": []})
    m.setdefault("pdfs", [])
    return m


def seen_ids(manifest: Dict) -> set:
    """En PDF er 'set' hvis den har samme council_id + date + source_url."""
    return {(p.get("council_id"), p.get("date"), p.get("source_url")) for p in manifest["pdfs"]}


def add_to_manifest(manifest: Dict, entry: Dict):
    manifest["pdfs"].append(entry)


# ---------------------------------------------------------------------------
# Adapters (én per regions-system)
# ---------------------------------------------------------------------------

class BaseAdapter:
    """Grundklasse. Hver adapter implementerer list_referater() som returnerer
    en liste af dict'er med fældfelterne:
       { "date": "YYYY-MM-DD" | None,
         "title": str | None,
         "pdf_url": str | None,
         "source_url": str,
         "kind": "referat" | "dagsorden" | None }
    """
    name = "base"

    def __init__(self, council_id: str, cfg: Dict, session: requests.Session, log):
        self.council_id = council_id
        self.cfg = cfg
        self.session = session
        self.log = log

    def list_referater(self) -> List[Dict]:
        raise NotImplementedError


class DagsordenDkAdapter(BaseAdapter):
    """FirstAgenda-baserede dagsorden-portaler (dagsorden.regionoest.dk, dagsorden.rm.dk,
    dagsordener.rn.dk, dagsordener-referater.regionsyddanmark.dk).

    Forventer at 'referaterUrl' i config er den direkte link til committee-siden, fx:
      https://dagsorden.regionoest.dk/?request.kriterie.udvalgId=7f6ccbb0-...

    Scraperen ekstraherer UUID'et (udvalgId) og prøver kendte FirstAgenda
    API-endpoints for møder. Hvis API'et ikke svarer, fallbacker til HTML-parsing.
    """
    name = "dagsorden_dk"

    UUID_RE = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )

    def list_referater(self) -> List[Dict]:
        url = (self.cfg.get("referaterUrl") or "").strip()
        if not url:
            self.log(f"  ! ingen referaterUrl i config — springer over")
            return []

        m = self.UUID_RE.search(url)
        if not m:
            self.log(f"  ! kunne ikke finde udvalgId (UUID) i URL'en: {url}")
            return []
        committee_id = m.group(1).lower()

        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"

        # Kendte FirstAgenda-API-mønstre — prøves i rækkefølge
        api_endpoints = [
            f"{host}/api/publication/committees/{committee_id}/meetings",
            f"{host}/api/publication/meetings?committeeId={committee_id}",
            f"{host}/Api/Publication/Committees/{committee_id}/Meetings",
            f"{host}/Api/Publication/Meetings?committeeId={committee_id}",
            f"{host}/api/integrations/publication/meetings?committeeId={committee_id}",
        ]

        meetings = []
        working_base = None
        for ep in api_endpoints:
            try:
                r = http_get(ep, self.session,
                             headers={"Accept": "application/json"})
                if r.status_code != 200:
                    continue
                ct = r.headers.get("content-type", "").lower()
                if "json" not in ct:
                    continue
                data = r.json()
                items = (data if isinstance(data, list)
                         else data.get("items") or data.get("Meetings") or data.get("meetings") or [])
                if items:
                    meetings = items
                    working_base = host
                    self.log(f"  → API svarede via {ep} ({len(items)} møder)")
                    break
            except Exception:
                continue

        results = []
        if meetings:
            for mt in meetings:
                date_iso = (mt.get("startDate") or mt.get("date")
                            or mt.get("StartDate") or mt.get("meetingDate") or "")
                date_iso = str(date_iso)[:10] if date_iso else None
                title = mt.get("title") or mt.get("Title") or mt.get("name")
                meeting_id = (mt.get("id") or mt.get("Id")
                              or mt.get("meetingId") or mt.get("MeetingId"))
                pdf_url = (mt.get("agendaPdfUrl") or mt.get("pdfUrl")
                           or mt.get("publishedPdfUrl") or mt.get("AgendaPdfUrl"))
                if not pdf_url and meeting_id:
                    # Prøv de typiske FirstAgenda PDF-URL-mønstre
                    pdf_url = f"{working_base}/api/publication/meetings/{meeting_id}/pdf"
                source_url = (f"{working_base}/?request.kriterie.udvalgId={committee_id}"
                              f"&request.kriterie.mId={meeting_id}" if meeting_id
                              else f"{working_base}/?request.kriterie.udvalgId={committee_id}")
                kind = "dagsorden" if (title and "dagsorden" in str(title).lower()) else "referat"
                results.append({
                    "date": date_iso,
                    "title": title,
                    "pdf_url": pdf_url,
                    "source_url": source_url,
                    "kind": kind,
                })
            return results

        # Fallback: hent siden som HTML og find PDF-links
        self.log(f"  ~ API svarede ikke — prøver HTML-fallback")
        try:
            r = http_get(url, self.session)
            r.raise_for_status()
        except Exception as e:
            self.log(f"  ! kunne ikke hente {url}: {e}")
            return []

        soup = BeautifulSoup(r.content, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" not in href.lower():
                continue
            abs_url = urljoin(url, href)
            text = " ".join(a.get_text(" ", strip=True).split())
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
            date = parse_date(text) or parse_date(parent_text)
            kind = "dagsorden" if "dagsorden" in (text + parent_text).lower() else "referat"
            results.append({
                "date": date,
                "title": text or None,
                "pdf_url": abs_url,
                "source_url": url,
                "kind": kind,
            })
        return results


# Bagudkompatibilitet — hvis nogen opdaterer configfilen og bruger det gamle navn
FirstAgendaAdapter = DagsordenDkAdapter


class GenericHtmlAdapter(BaseAdapter):
    """Scraper der henter siden, finder PDF-links og gætter dato."""
    name = "generic_html"

    def list_referater(self) -> List[Dict]:
        url = self.cfg.get("referaterUrl")
        if not url:
            return []
        try:
            r = http_get(url, self.session)
            r.raise_for_status()
        except Exception as e:
            self.log(f"  ! kunne ikke hente {url}: {e}")
            return []

        soup = BeautifulSoup(r.content, "lxml")
        results = []
        council_tokens = self._council_tokens()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" not in href.lower():
                continue
            abs_url = urljoin(url, href)
            text = " ".join(a.get_text(" ", strip=True).split())
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
            combined = f"{text} {parent_text}"
            # Filtrér til kun dette råd
            if council_tokens and not any(tok in combined.lower() for tok in council_tokens):
                continue
            date = parse_date(combined)
            results.append({
                "date": date,
                "title": text or None,
                "pdf_url": abs_url,
                "source_url": url,
                "kind": "dagsorden" if "dagsorden" in text.lower() else "referat",
            })
        return results

    def _council_tokens(self):
        name = (self.cfg.get("name") or "").lower()
        # "Sundhedsråd Vendsyssel" -> ["vendsyssel"]
        tokens = re.findall(r"[a-zæøå]{4,}", name)
        return [t for t in tokens if t not in ("sundhedsråd", "sundhedsrad")]


class RmHtmlAdapter(GenericHtmlAdapter):
    """Region Midtjylland CMS. Samme principper som generic."""
    name = "rm_html"


class RsydHtmlAdapter(GenericHtmlAdapter):
    """Region Syddanmark CMS."""
    name = "rsyd_html"


ADAPTERS = {
    "dagsorden_dk": DagsordenDkAdapter,   # primær — brug denne for alle 4 regioner
    "firstagenda":  DagsordenDkAdapter,   # alias, bagudkompatibel
    "generic_html": GenericHtmlAdapter,
    "rm_html":      GenericHtmlAdapter,   # gammel adapter — brug dagsorden_dk i stedet
    "rsyd_html":    GenericHtmlAdapter,   # gammel adapter — brug dagsorden_dk i stedet
}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_pdf(url: str, dest: Path, session: requests.Session, log) -> bool:
    try:
        with session.get(url, stream=True, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT}) as r:
            if r.status_code != 200:
                log(f"    HTTP {r.status_code}")
                return False
            ctype = r.headers.get("content-type", "").lower()
            if "pdf" not in ctype and not url.lower().endswith(".pdf"):
                log(f"    ikke-PDF content-type: {ctype}")
                return False
            total = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        total += len(chunk)
                        if total > MAX_PDF_SIZE_MB * 1024 * 1024:
                            log(f"    PDF er for stor (>{MAX_PDF_SIZE_MB} MB) — springer over")
                            dest.unlink(missing_ok=True)
                            return False
                        f.write(chunk)
        return True
    except Exception as e:
        log(f"    fejl: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    REFERATER_DIR.mkdir(parents=True, exist_ok=True)
    log_lines = []

    def log(msg=""):
        print(msg, flush=True)
        log_lines.append(msg)

    log(f"=== Referat-scraper startet {datetime.now().isoformat(timespec='seconds')} ===")

    config = load_json(CONFIG_FILE, None)
    if not config or "councils" not in config:
        log(f"FEJL: {CONFIG_FILE} mangler eller er ugyldig.")
        return 1

    manifest = load_manifest()
    seen = seen_ids(manifest)
    newly_added = []

    session = requests.Session()

    for cid, ccfg in config["councils"].items():
        log(f"\n[{cid}] {ccfg.get('name', cid)}  →  adapter: {ccfg.get('adapter')}")
        adapter_cls = ADAPTERS.get(ccfg.get("adapter"))
        if not adapter_cls:
            log(f"  ! ukendt adapter: {ccfg.get('adapter')}")
            continue
        try:
            adapter = adapter_cls(cid, ccfg, session, log)
            items = adapter.list_referater()
        except Exception as e:
            log(f"  ! adapter fejlede: {e}\n{traceback.format_exc()}")
            continue

        log(f"  fandt {len(items)} referat-kandidater")
        for item in items:
            date = item.get("date") or "uden-dato"
            key = (cid, date, item.get("source_url"))
            if key in seen:
                continue
            pdf_url = item.get("pdf_url")
            if not pdf_url:
                continue
            fname = f"{date}_{slugify(item.get('title') or item.get('kind') or 'referat')}.pdf"
            fname = fname[:120]  # begræns længde
            dest = REFERATER_DIR / cid / fname
            if dest.exists():
                # allerede på disk men manglede i manifest — tilføj uden download
                pass
            else:
                log(f"  ↓ ny: {date}  {pdf_url}")
                ok = download_pdf(pdf_url, dest, session, log)
                if not ok:
                    continue
                time.sleep(1)  # venlig pause

            entry = {
                "council_id": cid,
                "council_name": ccfg.get("name"),
                "region": ccfg.get("region"),
                "date": item.get("date"),
                "title": item.get("title"),
                "kind": item.get("kind"),
                "source_url": item.get("source_url"),
                "pdf_url": pdf_url,
                "local_path": str(dest.relative_to(ROOT)),
                "downloaded_at": datetime.now().isoformat(timespec="seconds"),
                "size_bytes": dest.stat().st_size if dest.exists() else 0,
            }
            add_to_manifest(manifest, entry)
            newly_added.append(entry)
            seen.add(key)

    manifest["last_run"] = datetime.now().isoformat(timespec="seconds")
    manifest["last_new_count"] = len(newly_added)
    save_json(MANIFEST_FILE, manifest)

    log(f"\n=== Færdig. {len(newly_added)} nye PDF'er denne kørsel. ===")
    log(f"Manifest indeholder nu i alt {len(manifest['pdfs'])} referater.")

    # Skriv log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
