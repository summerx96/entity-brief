#!/usr/bin/env python3
"""Find public DocumentCloud docs that already have entities."""
import argparse
import getpass
import os
import time
from typing import Any, Dict, Optional

import requests

ACCOUNTS_TOKEN_URL = "https://accounts.muckrock.com/api/token/"
API_BASE = "https://api.www.documentcloud.org/api"


def get_token(username: str, password: str) -> str:
    resp = requests.post(
        ACCOUNTS_TOKEN_URL,
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access"]


def get_json(url: str, token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def entities_nonempty(doc_id: int, token: str) -> bool:
    url = f"{API_BASE}/documents/{doc_id}/entities/"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code in (403, 404):
        return False
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict) and "results" in data:
        return len(data["results"]) > 0
    return False


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    env_token = os.environ.get("DC_ACCESS_TOKEN") or os.environ.get("DOCUMENTCLOUD_ACCESS_TOKEN")
    if env_token:
        return env_token

    username = (
        os.environ.get("DC_USERNAME")
        or os.environ.get("DOCUMENTCLOUD_USERNAME")
        or input("MuckRock username/email: ").strip()
    )
    password = os.environ.get("DC_PASSWORD") or os.environ.get("DOCUMENTCLOUD_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ").strip()
    return get_token(username, password)


def main() -> None:
    parser = argparse.ArgumentParser(description="Find public docs with existing entities")
    parser.add_argument("--query", default="access:public", help="DocumentCloud search query")
    parser.add_argument("--limit", type=int, default=10, help="How many doc IDs to find")
    parser.add_argument("--max-checked", type=int, default=800, help="Max docs to scan")
    parser.add_argument("--per-page", type=int, default=100, help="Search page size")
    parser.add_argument("--sleep", type=float, default=0.12, help="Delay between entity checks")
    parser.add_argument("--token", help="DocumentCloud access token (skips login)")
    args = parser.parse_args()

    token = resolve_token(args)

    found = 0
    checked = 0
    next_url = f"{API_BASE}/documents/search/"
    params = {"q": args.query, "per_page": args.per_page}

    print("\nSearching for public docs that already have extracted entities...\n")

    while next_url and found < args.limit and checked < args.max_checked:
        page = get_json(next_url, token, params=params)
        params = {}
        for doc in page.get("results", []):
            doc_id = int(doc["id"])
            canonical = doc.get("canonical_url", "")
            checked += 1

            time.sleep(args.sleep)

            try:
                if entities_nonempty(doc_id, token):
                    found += 1
                    print(f"[FOUND] {doc_id}  {canonical}")
                    if found >= args.limit:
                        return
            except Exception:
                continue

        next_url = page.get("next")

    print("\nDone. If you found none, increase --max-checked or narrow --query.")


if __name__ == "__main__":
    main()
