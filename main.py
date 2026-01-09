import html
import itertools
import json
import os
import re
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import requests
from documentcloud.addon import AddOn

# ---- Config ----
ADDON_VERSION = "0.1.0"

API_BASE = os.environ.get("DOCUMENTCLOUD_API_BASE", "https://api.www.documentcloud.org/api/")
# Optional endpoints you control (telemetry disabled for now)
METRICS_ENDPOINT = os.environ.get("ENTITY_BRIEF_METRICS_ENDPOINT")  # e.g. https://example.com/api/metrics
FEEDBACK_URL = os.environ.get("ENTITY_BRIEF_FEEDBACK_URL", "")
DEVELOPER_EMAIL = os.environ.get("ENTITY_BRIEF_DEV_EMAIL", "summerxie966@gmail.com")

D3_CDN = "https://d3js.org/d3.v7.min.js"


# ---- Helpers ----
def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _safe_float(x: Any, default: float) -> float:
    try:
        return float(x)
    except Exception:
        return default


class EntityBrief(AddOn):
    def main(self):
        start_ts = time.time()
        run_uuid = getattr(self, "id", None) or str(uuid.uuid4())

        # ---- Options ----
        data = self.data or {}
        max_docs = _safe_int(data.get("max_docs", 25), 25)
        min_rel = _safe_float(data.get("min_relevance", 0.15), 0.15)
        top_n = _safe_int(data.get("top_n_entities", 15), 15)
        include_connections = bool(data.get("include_connections", True))

        # ---- Fetch docs ----
        self.set_message("Collecting documents...")
        docs = list(self.get_documents())
        if max_docs and len(docs) > max_docs:
            docs = docs[:max_docs]

        doc_meta: Dict[int, Dict[str, Any]] = {}
        total_pages = 0

        for i, doc in enumerate(docs, start=1):
            doc_id = int(getattr(doc, "id"))
            title = str(getattr(doc, "title", "")) or f"Document {doc_id}"
            canonical_url = str(getattr(doc, "canonical_url", ""))
            page_count = int(getattr(doc, "page_count", 0) or 0)
            total_pages += page_count

            doc_meta[doc_id] = {
                "id": doc_id,
                "title": title,
                "url": canonical_url,
                "page_count": page_count,
            }

            self.set_progress(int(i / max(len(docs), 1) * 10))

        self.set_message(f"Collected {len(docs)} documents.")


if __name__ == "__main__":
    EntityBrief().main()
