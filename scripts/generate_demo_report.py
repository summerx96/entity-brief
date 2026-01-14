#!/usr/bin/env python3
"""Generate a demo HTML report from public DocumentCloud docs."""
import argparse
import getpass
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import EntityBrief

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


def fetch_doc(doc_id: int, token: str) -> Dict[str, Any]:
    resp = requests.get(
        f"{API_BASE}/documents/{doc_id}/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def build_docs(doc_ids: List[int], token: str) -> List[SimpleNamespace]:
    docs = []
    for doc_id in doc_ids:
        info = fetch_doc(doc_id, token)
        docs.append(
            SimpleNamespace(
                id=info.get("id"),
                title=info.get("title"),
                canonical_url=info.get("canonical_url", ""),
                page_count=info.get("page_count", 0),
            )
        )
    return docs


class DemoRunner(EntityBrief):
    def __init__(self, docs: List[SimpleNamespace], token: str, data: Dict[str, Any], run_uuid: str) -> None:
        self._docs = docs
        self.access_token = token
        self.data = data
        self.id = run_uuid

    def get_documents(self):
        return iter(self._docs)

    def set_message(self, msg: str) -> None:
        print(msg)

    def set_progress(self, pct: int) -> None:
        pass

    def upload_file(self, f) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a demo Entity Brief report")
    parser.add_argument("doc_ids", nargs="+", type=int, help="DocumentCloud document IDs")
    parser.add_argument("--token", help="DocumentCloud access token (skips login)")
    parser.add_argument("--min-relevance", type=float, default=0.15, help="Entity relevance cutoff")
    parser.add_argument("--top-n-entities", type=int, default=15, help="Top entities to show")
    parser.add_argument("--no-connections", action="store_true", help="Disable co-occurrence list")
    parser.add_argument("--run-uuid", default="demo", help="Run UUID to embed in the report")
    parser.add_argument(
        "--output",
        default="docs/demo/entity-brief-demo.html",
        help="Where to write the demo HTML report",
    )
    args = parser.parse_args()

    token = resolve_token(args)
    docs = build_docs(args.doc_ids, token)

    data = {
        "min_relevance": args.min_relevance,
        "top_n_entities": args.top_n_entities,
        "include_connections": not args.no_connections,
        "max_docs": len(docs),
    }

    output_path = Path(args.output).resolve()
    run_uuid = args.run_uuid or "demo"
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)

    prev_cwd = Path.cwd()
    try:
        os.chdir(run_dir)
        DemoRunner(docs, token, data, run_uuid).main()
    finally:
        os.chdir(prev_cwd)

    generated = run_dir / f"entity-brief-{run_uuid}.html"
    if generated != output_path:
        generated.replace(output_path)

    print(f"Wrote demo report to {output_path}")


if __name__ == "__main__":
    main()
