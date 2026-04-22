#!/usr/bin/env python3
"""
send_referat_email.py
---------------------
Sender en pæn månedlig e-mail til Andreas med alle nye referater fra sidste 30 dage.

Læser referater-manifest.json, finder alle PDFs downloadet siden en given dato,
grupperer efter region og råd, og sender mailen via Gmail SMTP.

Miljøvariabler (sættes som GitHub Secrets):
  MAIL_FROM          — afsender (din Gmail, fx afpihl@gmail.com)
  MAIL_TO            — modtager (typisk dig selv)
  GMAIL_APP_PASSWORD — Gmail App Password (16 tegn uden mellemrum)
  GITHUB_REPO        — fx "afpihl/sundhedsraad" — bruges til at bygge links til GitHub

Afhængigheder: kun Python standardbibliotek.

Kør manuelt til test:
  MAIL_FROM=... MAIL_TO=... GMAIL_APP_PASSWORD=... GITHUB_REPO=afpihl/sundhedsraad python3 send_referat_email.py
"""

from __future__ import annotations
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import List, Dict

ROOT = Path(__file__).parent
MANIFEST_FILE = ROOT / "referater-manifest.json"
DEFAULT_DAYS = 35  # "sidste måned" + lidt slør

REGION_NAMES = {
    "oestdanmark": "Region Østdanmark",
    "nordjylland": "Region Nordjylland",
    "midtjylland": "Region Midtjylland",
    "syddanmark":  "Region Syddanmark",
}


def load_manifest() -> Dict:
    if not MANIFEST_FILE.exists():
        return {"pdfs": []}
    with MANIFEST_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def filter_recent(pdfs: List[Dict], days: int) -> List[Dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for p in pdfs:
        ts = p.get("downloaded_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt >= cutoff:
            recent.append(p)
    return recent


def build_email_html(pdfs: List[Dict], repo: str, period_label: str) -> str:
    if not pdfs:
        return f"""
<p>Hej Andreas,</p>
<p>Der er ingen nye referater i <strong>{period_label}</strong>.</p>
<p>Dette er sandsynligvis fordi sundhedsrådene ikke har holdt møder — de fleste
råd mødes én gang om måneden. Eller fordi scraperen ikke kunne læse referat-siderne;
tjek kørselsloggen på
<a href="https://github.com/{repo}/actions">github.com/{repo}/actions</a>.</p>
<p>— Din referat-robot 🤖</p>
"""

    # Group by region → council
    by_region: Dict[str, Dict[str, List[Dict]]] = {}
    for p in pdfs:
        r = p.get("region", "ukendt")
        c = p.get("council_name", p.get("council_id", "Ukendt råd"))
        by_region.setdefault(r, {}).setdefault(c, []).append(p)

    rows = []
    rows.append(f"<p>Hej Andreas,</p>")
    rows.append(
        f"<p>Her er alle nye referater fra {period_label} — i alt "
        f"<strong>{len(pdfs)}</strong> dokumenter på tværs af "
        f"<strong>{sum(len(c) for c in by_region.values())}</strong> sundhedsråd.</p>"
    )
    rows.append(
        '<p style="background:#f4f4f4;padding:12px 16px;border-radius:8px;font-size:14px;">'
        "<strong>Næste skridt:</strong> klik ind på hvert link nedenfor og åbn PDF'en. "
        "Træk dem over i din NotebookLM — eller hvis du har koblet Google Drive-mappen, "
        "ligger de der allerede.</p>"
    )

    for region_id in sorted(by_region.keys()):
        rows.append(f'<h2 style="font-size:18px;margin-top:28px;">{REGION_NAMES.get(region_id, region_id)}</h2>')
        for council_name in sorted(by_region[region_id].keys()):
            items = sorted(by_region[region_id][council_name],
                           key=lambda x: x.get("date") or "")
            rows.append(f'<h3 style="font-size:15px;margin:16px 0 6px;">{council_name}</h3>')
            rows.append("<ul style='margin:0;padding-left:20px;'>")
            for it in items:
                date = it.get("date") or "uden dato"
                kind = it.get("kind") or "referat"
                title = it.get("title") or ""
                local = it.get("local_path", "")
                # Link direkte til PDF i GitHub repo (raw)
                gh_link = f"https://github.com/{repo}/blob/main/{local}"
                gh_raw = f"https://raw.githubusercontent.com/{repo}/main/{local}"
                rows.append(
                    f'<li style="margin:4px 0;">'
                    f'<strong>{date}</strong> — {kind.capitalize()} '
                    f'{"— " + title if title else ""} '
                    f'[<a href="{gh_link}">se på GitHub</a> · '
                    f'<a href="{gh_raw}">åbn PDF</a>]'
                    f'</li>'
                )
            rows.append("</ul>")

    rows.append(
        '<p style="margin-top:24px;color:#666;font-size:13px;">'
        "Robotten kører automatisk hver morgen og henter nye referater. "
        "Denne mail kommer én gang om måneden med samlingen for den forgangne måned. "
        "Hvis noget ser forkert ud, kan du altid tjekke "
        f'<a href="https://github.com/{repo}/actions">github.com/{repo}/actions</a>.</p>'
    )
    rows.append("<p>— Din referat-robot 🤖</p>")
    return "\n".join(rows)


def send_email(to: str, subject: str, html_body: str, frm: str, app_pass: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg.set_content("Denne e-mail kræver HTML-visning. Åbn den i Gmail/Apple Mail.")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(frm, app_pass)
        s.send_message(msg)


def main():
    mail_from = os.environ.get("MAIL_FROM", "").strip()
    mail_to = os.environ.get("MAIL_TO", "").strip() or mail_from
    app_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    repo = os.environ.get("GITHUB_REPO", "afpihl/sundhedsraad").strip()
    days = int(os.environ.get("DAYS", str(DEFAULT_DAYS)))

    if not mail_from or not app_pass:
        print("SPRINGER OVER: MAIL_FROM / GMAIL_APP_PASSWORD er ikke sat.")
        return 0

    manifest = load_manifest()
    pdfs = manifest.get("pdfs", [])
    recent = filter_recent(pdfs, days)

    today = datetime.now().strftime("%B %Y")
    period_label = f"{today}"
    subject = f"Sundhedsråd-referater {today} — {len(recent)} nye dokumenter"

    html = build_email_html(recent, repo, period_label)

    try:
        send_email(mail_to, subject, html, mail_from, app_pass)
        print(f"Sendt email til {mail_to} ({len(recent)} dokumenter).")
    except Exception as e:
        print(f"FEJL ved afsendelse: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
