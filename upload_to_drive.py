#!/usr/bin/env python3
"""
upload_to_drive.py
------------------
Uploader alle nye PDF'er til en delt Google Drive-mappe, som derefter kan kobles
direkte til NotebookLM.

Mappestruktur i Drive (oprettes automatisk ved første kørsel):
  <DRIVE_ROOT>/
    Region Østdanmark/
      Sundhedsråd Hovedstaden/
        2026-01-19_referat.pdf
        ...

Miljøvariabler (GitHub Secrets):
  DRIVE_ROOT_FOLDER_ID   — id på den mappe du har oprettet og delt med service-kontoen
  GDRIVE_SERVICE_ACCOUNT — hele service-account JSON-nøglen (hele filens indhold som én streng)

Afhængigheder:
  pip install google-api-python-client google-auth

Scriptet er idempotent: hvis en fil med samme navn findes i Drive-mappen, springer
den over (baseret på 'files().list()' per mappe).
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).parent
MANIFEST_FILE = ROOT / "referater-manifest.json"

REGION_NAMES = {
    "oestdanmark": "Region Østdanmark",
    "nordjylland": "Region Nordjylland",
    "midtjylland": "Region Midtjylland",
    "syddanmark":  "Region Syddanmark",
}


def _import_google():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return service_account, build, MediaFileUpload
    except ImportError as e:
        print("SPRINGER OVER: google-api-python-client ikke installeret. "
              "Kør: pip install google-api-python-client google-auth", file=sys.stderr)
        return None, None, None


def get_service():
    service_account, build, _ = _import_google()
    if not service_account:
        return None
    creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT", "").strip()
    if not creds_json:
        print("SPRINGER OVER: GDRIVE_SERVICE_ACCOUNT ikke sat.")
        return None
    try:
        info = json.loads(creds_json)
    except Exception as e:
        print(f"FEJL: GDRIVE_SERVICE_ACCOUNT er ikke gyldig JSON: {e}", file=sys.stderr)
        return None
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(service, name: str, parent_id: str) -> str:
    q = (f"name = '{name.replace(chr(39), chr(92)+chr(39))}' and "
         f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' "
         f"and trashed = false")
    resp = service.files().list(q=q, fields="files(id,name)",
                                supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute()
    for f in resp.get("files", []):
        return f["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    created = service.files().create(body=meta, fields="id",
                                     supportsAllDrives=True).execute()
    return created["id"]


def file_exists_in_folder(service, name: str, parent_id: str) -> bool:
    q = (f"name = '{name.replace(chr(39), chr(92)+chr(39))}' and "
         f"'{parent_id}' in parents and trashed = false")
    resp = service.files().list(q=q, fields="files(id)",
                                supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute()
    return bool(resp.get("files"))


def upload_file(service, local_path: Path, name: str, parent_id: str) -> Optional[str]:
    _, _, MediaFileUpload = _import_google()
    meta = {"name": name, "parents": [parent_id]}
    media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=True)
    created = service.files().create(body=meta, media_body=media, fields="id,name",
                                     supportsAllDrives=True).execute()
    return created.get("id")


def main():
    root_folder = os.environ.get("DRIVE_ROOT_FOLDER_ID", "").strip()
    if not root_folder:
        print("SPRINGER OVER: DRIVE_ROOT_FOLDER_ID ikke sat.")
        return 0

    service = get_service()
    if not service:
        return 0

    if not MANIFEST_FILE.exists():
        print("Ingen manifest — ingenting at uploade.")
        return 0
    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

    # Cache for folder-IDs
    region_folder_cache: Dict[str, str] = {}
    council_folder_cache: Dict[str, str] = {}
    uploaded = 0
    skipped = 0
    errors = 0

    for p in manifest.get("pdfs", []):
        local = ROOT / p.get("local_path", "")
        if not local.exists():
            continue
        region_id = p.get("region", "ukendt")
        council_name = p.get("council_name", p.get("council_id", "Ukendt råd"))
        filename = local.name

        # region-folder
        region_display = REGION_NAMES.get(region_id, region_id)
        rkey = region_display
        if rkey not in region_folder_cache:
            try:
                region_folder_cache[rkey] = find_or_create_folder(service, region_display, root_folder)
            except Exception as e:
                print(f"Fejl ved oprettelse af region-mappe '{region_display}': {e}")
                errors += 1
                continue
        region_folder = region_folder_cache[rkey]

        # council-folder
        ckey = f"{rkey}/{council_name}"
        if ckey not in council_folder_cache:
            try:
                council_folder_cache[ckey] = find_or_create_folder(service, council_name, region_folder)
            except Exception as e:
                print(f"Fejl ved oprettelse af council-mappe '{council_name}': {e}")
                errors += 1
                continue
        council_folder = council_folder_cache[ckey]

        try:
            if file_exists_in_folder(service, filename, council_folder):
                skipped += 1
                continue
            fid = upload_file(service, local, filename, council_folder)
            print(f"  ↑ uploadet: {council_name}/{filename} (id={fid})")
            uploaded += 1
        except Exception as e:
            print(f"  fejl ved upload af {filename}: {e}")
            errors += 1

    print(f"\nDrive-sync færdig. Uploadet: {uploaded}, sprunget over (eksisterede): {skipped}, fejl: {errors}")
    return 0 if errors == 0 else 0  # fejl må ikke stoppe workflow


if __name__ == "__main__":
    sys.exit(main())
