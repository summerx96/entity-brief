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
ENTITY_COVERAGE_WARN_THRESHOLD = 0.4


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


def _get_access_token(addon: AddOn) -> str:
    """
    DocumentCloud API uses an access token placed in Authorization: Bearer <token>.
    This function tries common locations that AddOn implementations tend to store it.
    """
    for attr in ("access_token", "token"):
        tok = getattr(addon, attr, None)
        if tok:
            return tok
    client = getattr(addon, "client", None)
    if client:
        for attr in ("access_token", "token"):
            tok = getattr(client, attr, None)
            if tok:
                return tok
    # Last resort: env (useful for local testing)
    tok = os.environ.get("DC_ACCESS_TOKEN") or os.environ.get("DOCUMENTCLOUD_ACCESS_TOKEN")
    if tok:
        return tok
    raise RuntimeError("Could not locate a DocumentCloud access token.")


def _api_get(url: str, token: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30,
             max_retries: int = 3) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(max_retries + 1):
        resp = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = _safe_int(resp.headers.get("Retry-After"), 1)
            time.sleep(max(retry_after, 1))
            continue
        return resp
    return resp


def _api_get_json(url: str, token: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
    resp = _api_get(url, token, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _api_get_all_pages(
    url: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
    first_payload: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    Handle DRF-style pagination: {results: [...], next: url}
    """
    out: List[Dict[str, Any]] = []
    payload = first_payload
    next_url = url
    next_params = dict(params or {})
    if payload is None:
        payload = _api_get_json(next_url, token, params=next_params)
    while True:
        if isinstance(payload, dict) and "results" in payload:
            out.extend(payload.get("results", []))
            next_url = payload.get("next")
            next_params = {}  # next already includes query params
            if not next_url:
                break
            payload = _api_get_json(next_url, token, params=next_params)
        elif isinstance(payload, list):
            out.extend(payload)
            break
        else:
            break
    return out


def _normalize_name(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    # Keep letters/numbers/basic punctuation; remove noisy quotes
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    return s


def _entity_key(ent: Dict[str, Any]) -> Tuple[str, str]:
    """
    Best-effort canonicalization:
    - Prefer stable IDs if present (mid/wiki_url)
    - Else normalized surface form
    """
    kind = str(ent.get("kind", "Other"))
    # Common fields from entity systems (may/may not exist depending on extractor)
    mid = ent.get("mid") or ent.get("knowledge_graph_mid")
    wikidata = ent.get("wikidata_id")
    wiki = ent.get("wiki_url") or ent.get("wikipedia_url")
    if mid:
        return (kind, f"mid:{mid}")
    if wikidata:
        return (kind, f"wikidata:{wikidata}")
    if wiki:
        return (kind, f"wiki:{wiki}")
    val = str(ent.get("value") or ent.get("name") or "")
    return (kind, f"v:{_normalize_name(val)}")


def _entity_display(ent: Dict[str, Any]) -> str:
    return str(ent.get("value") or ent.get("name") or "").strip()


def _entity_payload(ent: Dict[str, Any]) -> Dict[str, Any]:
    payload = ent.get("entity")
    if isinstance(payload, dict):
        return payload
    return ent


def _escape(s: str) -> str:
    return html.escape(s or "", quote=True)


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
        doc_iter = self.get_documents()
        if max_docs:
            doc_iter = itertools.islice(doc_iter, max_docs)
        docs = list(doc_iter)

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

        # ---- Extract entities per doc ----
        token = _get_access_token(self)
        self.set_message(f"Fetching entities for {len(docs)} documents...")
        doc_entities: Dict[int, List[Dict[str, Any]]] = {}
        failures: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        for i, doc in enumerate(docs, start=1):
            doc_id = getattr(doc, "id", None)
            try:
                doc_id = int(doc_id)
                url = f"{API_BASE}documents/{doc_id}/entities/"
                params = {
                    "expand": "entity,occurrences",
                    "relevance__gt": min_rel,
                }
                resp = _api_get(url, token, params=params)
                if resp.status_code == 404:
                    meta = doc_meta.get(doc_id, {"id": doc_id, "title": f"Document {doc_id}", "url": ""})
                    skipped.append({
                        "doc_id": doc_id,
                        "title": meta["title"],
                        "url": meta["url"],
                        "reason": "no entities (404)",
                    })
                    continue
                resp.raise_for_status()
                ents = _api_get_all_pages(url, token, params=params, first_payload=resp.json())
                if not ents:
                    meta = doc_meta.get(doc_id, {"id": doc_id, "title": f"Document {doc_id}", "url": ""})
                    skipped.append({
                        "doc_id": doc_id,
                        "title": meta["title"],
                        "url": meta["url"],
                        "reason": "no entities",
                    })
                    continue
                doc_entities[doc_id] = ents
            except Exception as e:
                failures.append({"doc_id": doc_id, "error": str(e)})
            finally:
                self.set_progress(10 + int(i / max(len(docs), 1) * 30))

        # ---- Build cross-doc clusters ----
        self.set_message("Normalizing and aggregating entities...")
        clusters: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Per-doc canonical set for co-occurrence
        doc_entity_keys: Dict[int, List[Tuple[str, str]]] = {}
        doc_page_entities: Dict[int, Dict[int, set]] = {}

        for doc_id, ents in doc_entities.items():
            keys_for_doc = []
            page_entities: Dict[int, set] = {}
            for ent in ents:
                try:
                    payload = _entity_payload(ent)
                    kind = str(payload.get("kind", "Other"))
                    display = _entity_display(payload)
                    if not display:
                        continue

                    key = _entity_key(payload)
                    keys_for_doc.append(key)

                    c = clusters.get(key)
                    if not c:
                        clusters[key] = {
                            "kind": kind,
                            "canonical_key": f"{key[0]}::{key[1]}",
                            "display_names": Counter(),
                            "aliases": set(),
                            "total_mentions": 0,
                            "doc_count": 0,
                            "docs": {},  # doc_id -> {count, pages:set, samples:[]}
                        }
                        c = clusters[key]

                    c["display_names"][display] += 1
                    c["aliases"].add(display)

                    count = int(ent.get("count") or ent.get("mentions") or 0)
                    occs = ent.get("occurrences") or []
                    if not count and occs:
                        count = len(occs)
                    c["total_mentions"] += count

                    if doc_id not in c["docs"]:
                        c["docs"][doc_id] = {"count": 0, "pages": set(), "samples": []}
                        c["doc_count"] += 1

                    c["docs"][doc_id]["count"] += count

                    # Occurrences may include page/context; best-effort
                    pages_for_ent = set()
                    for occ in occs:
                        page = occ.get("page")
                        if isinstance(page, int):
                            pages_for_ent.add(page)
                            page_entities.setdefault(page, set()).add(key)
                    if pages_for_ent:
                        c["docs"][doc_id]["pages"].update(pages_for_ent)
                    for occ in occs[:5]:
                        page = occ.get("page")
                        snippet = occ.get("context") or occ.get("snippet") or occ.get("content") or ""
                        if snippet:
                            c["docs"][doc_id]["samples"].append(str(snippet)[:200])

                except Exception:
                    continue

            doc_entity_keys[doc_id] = keys_for_doc
            if page_entities:
                doc_page_entities[doc_id] = page_entities

        # Finalize cluster display name
        cluster_list: List[Dict[str, Any]] = []
        for key, c in clusters.items():
            display = c["display_names"].most_common(1)[0][0] if c["display_names"] else key[1]
            # JSON-ify sets/counters
            docs_out = []
            for did, dd in c["docs"].items():
                meta = doc_meta.get(did, {"id": did, "title": f"Document {did}", "url": ""})
                docs_out.append({
                    "doc_id": did,
                    "title": meta["title"],
                    "url": meta["url"],
                    "count": dd["count"],
                    "pages": sorted(list(dd["pages"]))[:25],
                    "samples": dd["samples"][:5],
                })
            docs_out.sort(key=lambda x: (-x["count"], x["title"]))

            cluster_list.append({
                "key": c["canonical_key"],
                "kind": c["kind"],
                "name": display,
                "aliases": sorted(list(c["aliases"]))[:25],
                "total_mentions": c["total_mentions"],
                "doc_count": c["doc_count"],
                "docs": docs_out[:10],  # cap
            })

        cluster_list.sort(key=lambda x: (-x["doc_count"], -x["total_mentions"], x["name"].lower()))

        # ---- Connections (co-occurrence) ----
        edges: List[Dict[str, Any]] = []
        if include_connections:
            self.set_message("Computing co-occurrence connections...")
            pair_stats: Dict[Tuple[Tuple[str, str], Tuple[str, str]], Dict[str, Any]] = {}
            any_page_data = False

            def _display_name(key: Tuple[str, str]) -> str:
                display = clusters.get(key, {}).get("display_names", Counter()).most_common(1)
                return display[0][0] if display else key[1]

            def _key_score(key: Tuple[str, str]) -> int:
                return int(clusters.get(key, {}).get("total_mentions", 0))

            for did, page_map in doc_page_entities.items():
                if not page_map:
                    continue
                any_page_data = True
                for page, keys in page_map.items():
                    unique = sorted(set(keys), key=lambda k: (-_key_score(k), str(k)))
                    unique = unique[:25]
                    for a, b in itertools.combinations(sorted(unique), 2):
                        stat = pair_stats.get((a, b))
                        if not stat:
                            stat = {"docs": set(), "pages": set(), "examples": []}
                            pair_stats[(a, b)] = stat
                        stat["docs"].add(did)
                        stat["pages"].add((did, page))
                        if len(stat["examples"]) < 3:
                            stat["examples"].append((did, page))

            if not any_page_data:
                pair_counts = Counter()
                for did, keys in doc_entity_keys.items():
                    # Use only top entities per doc to avoid combinatorial blowup
                    unique = list(dict.fromkeys(keys))  # stable unique
                    unique = unique[:25]
                    for a, b in itertools.combinations(sorted(unique), 2):
                        pair_counts[(a, b)] += 1
                for (a, b), dc in pair_counts.most_common(50):
                    edges.append({
                        "a": _display_name(a),
                        "b": _display_name(b),
                        "a_key": f"{a[0]}::{a[1]}",
                        "b_key": f"{b[0]}::{b[1]}",
                        "doc_count": dc,
                        "page_count": 0,
                        "examples": [],
                    })
            else:
                for (a, b), stat in pair_stats.items():
                    examples = []
                    for did, page in stat["examples"]:
                        meta = doc_meta.get(did, {"id": did, "title": f"Document {did}", "url": ""})
                        examples.append({
                            "doc_id": did,
                            "title": meta["title"],
                            "url": meta["url"],
                            "page": page,
                        })
                    edges.append({
                        "a": _display_name(a),
                        "b": _display_name(b),
                        "a_key": f"{a[0]}::{a[1]}",
                        "b_key": f"{b[0]}::{b[1]}",
                        "doc_count": len(stat["docs"]),
                        "page_count": len(stat["pages"]),
                        "examples": examples,
                    })
                edges.sort(key=lambda x: (-x.get("page_count", 0), -x.get("doc_count", 0), x["a"], x["b"]))
                edges = edges[:50]

        # ---- Build report data ----
        runtime_s = round(time.time() - start_ts, 2)
        docs_with_entities = len(doc_entities)
        entity_coverage = (docs_with_entities / len(docs)) if docs else 0
        report_data = {
            "run": {
                "uuid": run_uuid,
                "version": ADDON_VERSION,
                "runtime_seconds": runtime_s,
                "docs_processed": len(docs),
                "entity_docs": docs_with_entities,
                "entity_coverage": round(entity_coverage, 3),
                "entity_coverage_threshold": ENTITY_COVERAGE_WARN_THRESHOLD,
                "pages_processed": total_pages,
                "unique_entities": len(cluster_list),
                "generated_at_epoch": int(time.time()),
            },
            "meta": {
                "feedback_url": FEEDBACK_URL,
                "developer_email": DEVELOPER_EMAIL,
            },
            "top_entities": cluster_list[: max(top_n, 5)],
            "entities": cluster_list[:500],
            "edges": edges,
            "skipped": skipped,
            "failures": failures,
        }

        # ---- Render HTML ----
        self.set_message("Generating HTML report...")
        html_report = self._render_html(report_data)
        filename = f"entity-brief-{run_uuid}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_report)

        # Attach report to add-on run (one file per run)
        with open(filename, "rb") as f:
            self.upload_file(f)

        self.set_progress(100)
        self.set_message("Done. Report uploaded.")

    def _render_html(self, data: Dict[str, Any]) -> str:
        # Embed data as JSON so the report is one file
        data_json = json.dumps(data, ensure_ascii=False)
        run = data["run"]
        meta = data["meta"]
        demo_mode = str(run.get("uuid", "")).startswith("demo")
        demo_chart_fallback = ""
        demo_index_fallback = ""
        if demo_mode:
            demo_chart_fallback = """
      <div id="chartFallback" class="small muted" style="margin: 8px 0;">
        <p class="muted">Demo preview (static image shown if JS/D3 is blocked):</p>
        <img src="screenshot-top-entities.png" alt="Top entities chart preview" style="width: 100%; max-width: 900px; border: 1px solid #eee; border-radius: 8px;" />
      </div>"""
            demo_index_fallback = """
      <div id="indexFallback" class="small muted" style="margin: 8px 0;">
        <p class="muted">Demo preview (static image shown if JS is blocked):</p>
        <img src="screenshot-entity-index.png" alt="Entity index preview" style="width: 100%; max-width: 900px; border: 1px solid #eee; border-radius: 8px;" />
      </div>"""
        coverage_warning_block = ""
        docs_processed = int(run.get("docs_processed", 0) or 0)
        entity_docs = int(run.get("entity_docs", 0) or 0)
        entity_coverage = float(run.get("entity_coverage", 0) or 0)
        if docs_processed and entity_coverage < ENTITY_COVERAGE_WARN_THRESHOLD:
            coverage_pct = int(entity_coverage * 100)
            coverage_warning_block = f"""
  <div class="card warn">
    <h3>Low entity coverage</h3>
    <p class="small">
      Only {entity_docs} of {docs_processed} documents have extracted entities ({coverage_pct}%).
    </p>
    <p class="small"><strong>What to run first:</strong></p>
    <ol class="small">
      <li>Open a document and run <em>Edit -> Entities -> Extract entities</em> (or run the Google Cloud Entity Extractor add-on).</li>
      <li>Wait for extraction to finish, then re-run Entity Brief.</li>
      <li>Docs without entities appear under <strong>Skipped (no entities)</strong>.</li>
    </ol>
  </div>"""

        # Note: we keep D3 via CDN for MVP. If you want fully offline reports, embed d3.v7.min.js later.
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Entity Brief - {html.escape(run["uuid"])}</title>
  <script src="{D3_CDN}"></script>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; line-height: 1.4; }}
    .muted {{ color: #555; }}
    .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 14px 16px; margin: 12px 0; }}
    .row {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .row > .card {{ flex: 1 1 360px; }}
    .btn {{ display: inline-block; padding: 8px 10px; border: 1px solid #888; border-radius: 8px; text-decoration: none; color: inherit; }}
    button.btn {{ background: #fff; cursor: pointer; }}
    code {{ background: #f6f6f6; padding: 2px 4px; border-radius: 4px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 6px 8px; vertical-align: top; }}
    th {{ text-align: left; }}
    details summary {{ cursor: pointer; }}
    .small {{ font-size: 0.9em; }}
    .warn {{ background: #fff6e5; border-color: #ffd28a; }}
    .controls {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    label {{ display: block; font-weight: 600; margin-bottom: 4px; }}
    select, textarea, input[type="text"], input[type="range"] {{ width: 100%; }}
  </style>
</head>
<body>
  <h1>Entity Brief</h1>
  <p class="muted">Cross-document entity index + connection cues for FOIA / investigative work.</p>

  <div class="card">
    <h2>Run Certificate</h2>
    <p class="small">
      <strong>Run UUID:</strong> <code id="runUuid">{_escape(run["uuid"])}</code><br/>
      <strong>Version:</strong> <code>{_escape(run["version"])}</code><br/>
      <strong>Docs processed:</strong> {run["docs_processed"]} &nbsp; | &nbsp;
      <strong>Docs with entities:</strong> {run.get("entity_docs", 0)} &nbsp; | &nbsp;
      <strong>Pages processed:</strong> {run["pages_processed"]} &nbsp; | &nbsp;
      <strong>Unique entities:</strong> {run["unique_entities"]} &nbsp; | &nbsp;
      <strong>Runtime:</strong> {run["runtime_seconds"]}s
    </p>

    <div class="row">
      <div class="card">
        <h3>Share (optional)</h3>
        <p class="small">
          If you found this useful, you can send the summary to the developer (helps improve the tool and supports impact documentation).
        </p>
        <p>
          <a class="btn" href="#" id="copyBtn">Copy run summary</a>
          &nbsp;
          <a class="btn" id="mailtoLink" href="#">Email summary to developer</a>
        </p>
      </div>

      <div class="card warn">
        <h3>Privacy / trust notes</h3>
        <ul class="small">
          <li>This report is generated from the documents selected for this run.</li>
          <li>By default, no document text is sent to any external service by this Add-On.</li>
          <li>This version does not send usage metrics.</li>
        </ul>
      </div>
    </div>
  </div>

  {coverage_warning_block}

  <div class="card">
    <h2>Filters & exports</h2>
    <div class="controls">
      <div>
        <label for="kindFilter">Entity type</label>
        <select id="kindFilter"></select>
      </div>
      <div>
        <label for="coverageFilter">Minimum doc coverage</label>
        <input type="range" id="coverageFilter" min="1" max="1" value="1" />
        <div class="small muted">Showing entities in at least <span id="coverageValue">1</span> docs.</div>
      </div>
      <div>
        <label for="stoplist">Exclude names (comma or line separated)</label>
        <textarea id="stoplist" rows="2" placeholder="Example: United States, City"></textarea>
      </div>
      <div>
        <label for="sortBy">Sort entities by</label>
        <select id="sortBy">
          <option value="doc_count">Doc coverage (default)</option>
          <option value="total_mentions">Total mentions</option>
          <option value="name">Name (A-Z)</option>
        </select>
      </div>
    </div>
    <p style="margin-top: 8px;">
      <button class="btn" id="applyFilters" type="button">Apply filters</button>
      <button class="btn" id="resetFilters" type="button">Reset</button>
    </p>
    <p class="small muted">Exports use the current filters.</p>
    <p>
      <button class="btn" id="exportEntities" type="button">Download entity index CSV</button>
      &nbsp;
      <button class="btn" id="exportConnectionsCsv" type="button">Download connections CSV</button>
      &nbsp;
      <button class="btn" id="exportConnectionsJson" type="button">Download connections JSON</button>
    </p>
  </div>

  <div class="row">
    <div class="card">
      <h2>Top Entities (by document coverage)</h2>
      {demo_chart_fallback}
      <svg id="barChart" width="900" height="380"></svg>
      <p class="small muted">Bars show how many documents mention each entity (top list).</p>
    </div>

    <div class="card">
      <h2>Top Connections (co-occurrence)</h2>
      <div id="connections"></div>
      <p class="small muted">Pairs that appear together on the same pages when page data is available.</p>
    </div>
  </div>

  <div class="card">
    <h2>Entity Index</h2>
    <p class="small muted">
      Expand an entity to see which documents/pages it appears in.
    </p>
    {demo_index_fallback}
    <div id="entityIndex"></div>
  </div>

  <div class="card">
    <h2>Skipped (no entities)</h2>
    <div id="skipped"></div>
  </div>

  <div class="card">
    <h2>Failures</h2>
    <div id="failures"></div>
  </div>

  <script id="data" type="application/json">{html.escape(data_json)}</script>
  <script>
    const DATA = JSON.parse(document.getElementById("data").textContent);

    function escapeHtml(value) {{
      if (value === null || value === undefined) {{
        return "";
      }}
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function safeUrl(value) {{
      if (!value) {{
        return "";
      }}
      const url = String(value).trim();
      if (url.startsWith("http://") || url.startsWith("https://")) {{
        return url;
      }}
      return "";
    }}

    function docPageUrl(baseUrl, page) {{
      if (!baseUrl) {{
        return "";
      }}
      const base = String(baseUrl).split("#")[0];
      return `${{base}}#document/p${{page}}`;
    }}

    const demoChartFallback = document.getElementById("chartFallback");
    const demoIndexFallback = document.getElementById("indexFallback");

    // ---- Share helpers ----
    function runSummaryText() {{
      const r = DATA.run;
      return [
        "Entity Brief - Run Summary",
        `Run UUID: ${{r.uuid}}`,
        `Version: ${{r.version}}`,
        `Docs processed: ${{r.docs_processed}}`,
        `Docs with entities: ${{r.entity_docs || 0}}`,
        `Pages processed: ${{r.pages_processed}}`,
        `Unique entities: ${{r.unique_entities}}`,
        `Runtime (s): ${{r.runtime_seconds}}`,
        "",
        "Optional (if you're willing):",
        "- What newsroom/org are you with?",
        "- What did this help you find faster?",
        "- Approx. minutes saved?"
      ].join("\\n");
    }}

    document.getElementById("copyBtn").addEventListener("click", async (e) => {{
      e.preventDefault();
      try {{
        await navigator.clipboard.writeText(runSummaryText());
        alert("Copied run summary to clipboard.");
      }} catch (err) {{
        alert("Copy failed (browser permission). You can manually select text in the Run Certificate block.");
      }}
    }});

    const mailto = `mailto:${{encodeURIComponent(DATA.meta.developer_email)}}?subject=${{encodeURIComponent("Entity Brief feedback (" + DATA.run.uuid + ")")}}&body=${{encodeURIComponent(runSummaryText())}}`;
    document.getElementById("mailtoLink").setAttribute("href", mailto);

    const ENTITIES = DATA.entities || [];
    const EDGES = DATA.edges || [];

    const kindFilter = document.getElementById("kindFilter");
    const coverageFilter = document.getElementById("coverageFilter");
    const coverageValue = document.getElementById("coverageValue");
    const stoplistInput = document.getElementById("stoplist");
    const sortBy = document.getElementById("sortBy");
    const applyFiltersBtn = document.getElementById("applyFilters");
    const resetFiltersBtn = document.getElementById("resetFilters");
    const exportEntitiesBtn = document.getElementById("exportEntities");
    const exportConnectionsCsvBtn = document.getElementById("exportConnectionsCsv");
    const exportConnectionsJsonBtn = document.getElementById("exportConnectionsJson");

    let currentEntities = ENTITIES.slice();
    let currentEdges = EDGES.slice();

    function normalizeTerm(value) {{
      return String(value || "").toLowerCase().trim();
    }}

    function parseStoplist(text) {{
      const terms = String(text || "")
        .split(/[,\\n]/)
        .map(term => term.trim())
        .filter(Boolean)
        .map(normalizeTerm);
      return new Set(terms);
    }}

    function csvEscape(value) {{
      const str = value === null || value === undefined ? "" : String(value);
      if (str.includes("\\\"") || str.includes(",") || str.includes("\\n")) {{
        return `"${{str.replace(/"/g, '""')}}"`;
      }}
      return str;
    }}

    function toCsv(rows) {{
      return rows.map(row => row.map(csvEscape).join(",")).join("\\n");
    }}

    function downloadText(filename, text) {{
      const blob = new Blob([text], {{type: "text/plain"}});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }}

    function buildEntityIndexCsv(entities) {{
      const rows = [[
        "entity_name",
        "entity_kind",
        "entity_doc_count",
        "entity_total_mentions",
        "doc_id",
        "doc_title",
        "doc_url",
        "doc_pages",
        "doc_mentions"
      ]];
      for (const ent of entities) {{
        const docs = ent.docs || [];
        if (!docs.length) {{
          rows.push([ent.name, ent.kind, ent.doc_count, ent.total_mentions, "", "", "", "", ""]);
          continue;
        }}
        for (const doc of docs) {{
          const pages = (doc.pages || []).join(";");
          rows.push([
            ent.name,
            ent.kind,
            ent.doc_count,
            ent.total_mentions,
            doc.doc_id || "",
            doc.title || "",
            doc.url || "",
            pages,
            doc.count || ""
          ]);
        }}
      }}
      return toCsv(rows);
    }}

    function buildConnectionsCsv(edges) {{
      const rows = [[
        "entity_a",
        "entity_b",
        "doc_count",
        "page_count",
        "example_pages"
      ]];
      for (const edge of edges) {{
        const examples = (edge.examples || []).map(ex => {{
          const title = ex.title || `Document ${{ex.doc_id || ""}}`;
          const page = ex.page !== undefined ? `p${{ex.page}}` : "";
          return `${{title}} ${{page}}`.trim();
        }}).join(" | ");
        rows.push([
          edge.a,
          edge.b,
          edge.doc_count || "",
          edge.page_count || "",
          examples
        ]);
      }}
      return toCsv(rows);
    }}

    function renderChart(entities) {{
      const chartSvg = document.getElementById("barChart");
      if (!chartSvg) {{
        return;
      }}
      const top = entities.slice(0, 15).map(d => ({{
        name: d.name,
        kind: d.kind,
        doc_count: d.doc_count,
        total_mentions: d.total_mentions
      }}));
      const hasD3 = typeof d3 !== "undefined";
      const existingNote = document.getElementById("chartNote");
      if (existingNote) {{
        existingNote.remove();
      }}
      if (!hasD3) {{
        if (!demoChartFallback) {{
          const note = document.createElement("p");
          note.id = "chartNote";
          note.className = "small muted";
          note.textContent = "Chart could not render (D3 failed to load).";
          chartSvg.insertAdjacentElement("beforebegin", note);
        }}
        return;
      }}
      if (demoChartFallback) {{
        demoChartFallback.style.display = "none";
      }}
      const svg = d3.select(chartSvg);
      svg.selectAll("*").remove();
      if (!top.length) {{
        const note = document.createElement("p");
        note.id = "chartNote";
        note.className = "small muted";
        note.textContent = "No entities available for chart.";
        chartSvg.insertAdjacentElement("beforebegin", note);
        return;
      }}

      const width = +svg.attr("width");
      const height = +svg.attr("height");
      const margin = {{top: 20, right: 20, bottom: 120, left: 60}};
      const innerW = width - margin.left - margin.right;
      const innerH = height - margin.top - margin.bottom;

      const g = svg.append("g").attr("transform", `translate(${{margin.left}},${{margin.top}})`);

      const x = d3.scaleBand()
        .domain(top.map(d => d.name))
        .range([0, innerW])
        .padding(0.15);

      const y = d3.scaleLinear()
        .domain([0, d3.max(top, d => d.doc_count) || 1])
        .nice()
        .range([innerH, 0]);

      g.append("g")
        .attr("transform", `translate(0,${{innerH}})`)
        .call(d3.axisBottom(x))
        .selectAll("text")
          .attr("transform", "rotate(-40)")
          .style("text-anchor", "end");

      g.append("g").call(d3.axisLeft(y).ticks(6));

      g.selectAll("rect")
        .data(top)
        .enter()
        .append("rect")
          .attr("x", d => x(d.name))
          .attr("y", d => y(d.doc_count))
          .attr("width", x.bandwidth())
          .attr("height", d => innerH - y(d.doc_count));
    }}

    function renderConnections(edges) {{
      const connDiv = document.getElementById("connections");
      if (!connDiv) {{
        return;
      }}
      const displayEdges = edges.slice(0, 20);
      if (!displayEdges.length) {{
        connDiv.innerHTML = "<p class='muted small'>No connections computed (or not enough entities).</p>";
        return;
      }}
      let html = "<table><thead><tr><th>Entity A</th><th>Entity B</th><th>Docs</th><th>Pages</th><th>Example pages</th></tr></thead><tbody>";
      for (const e of displayEdges) {{
        const examples = (e.examples || []).map(ex => {{
          const title = escapeHtml(ex.title || `Document ${{ex.doc_id || ""}}`);
          const url = safeUrl(ex.url);
          const pageLabel = ex.page !== undefined ? `p${{escapeHtml(ex.page)}}` : "";
          if (url && ex.page !== undefined) {{
            return `<a href="${{escapeHtml(docPageUrl(url, ex.page))}}" target="_blank" rel="noreferrer">${{title}} (${{pageLabel}})</a>`;
          }}
          if (url) {{
            return `<a href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{title}}</a>${{pageLabel ? " (" + pageLabel + ")" : ""}}`;
          }}
          return `${{title}}${{pageLabel ? " (" + pageLabel + ")" : ""}}`;
        }}).join("<br/>");
        html += `<tr><td>${{escapeHtml(e.a)}}</td><td>${{escapeHtml(e.b)}}</td><td>${{escapeHtml(e.doc_count)}}</td><td>${{escapeHtml(e.page_count || "")}}</td><td>${{examples || "-"}}</td></tr>`;
      }}
      html += "</tbody></table>";
      connDiv.innerHTML = html;
    }}

    function renderEntityIndex(entities) {{
      const idx = document.getElementById("entityIndex");
      if (!idx) {{
        return;
      }}
      const displayEntities = entities.slice(0, 200);
      if (!displayEntities.length) {{
        idx.innerHTML = "<p class='muted small'>No entities to display.</p>";
      }} else {{
        idx.innerHTML = displayEntities.map(ent => {{
          const docs = (ent.docs || []).map(d => {{
            const url = safeUrl(d.url);
            const title = escapeHtml(d.title || `Document ${{d.doc_id || ""}}`);
            const link = url ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{title}}</a>` : title;
            const pages = (d.pages || []).map(p => {{
              const label = `p${{escapeHtml(p)}}`;
              if (url) {{
                return `<a href="${{escapeHtml(docPageUrl(url, p))}}" target="_blank" rel="noreferrer">${{label}}</a>`;
              }}
              return label;
            }}).join(", ");
            const samples = (d.samples || []).slice(0, 2).map(s => `<div class="muted small">...${{escapeHtml(s)}}...</div>`).join("");
            return `<div class="small"><strong>${{link}}</strong> - mentions: ${{escapeHtml(d.count)}}${{pages ? " - pages: " + pages : ""}}${{samples}}</div>`;
          }}).join("");
          const aliases = (ent.aliases || []).slice(0, 10).map(a => escapeHtml(a)).join(", ");
          return `
            <details class="card">
              <summary><strong>${{escapeHtml(ent.name)}}</strong> <span class="muted">(${{escapeHtml(ent.kind)}})</span> - docs: <strong>${{escapeHtml(ent.doc_count)}}</strong>, mentions: <strong>${{escapeHtml(ent.total_mentions)}}</strong></summary>
              <div class="small muted">Aliases (sample): ${{aliases}}</div>
              <div style="margin-top:8px;">${{docs || "<div class='muted small'>No doc details</div>"}}</div>
            </details>
          `;
        }}).join("");
      }}
      if (demoIndexFallback) {{
        demoIndexFallback.style.display = "none";
      }}
    }}

    function applyFilters() {{
      const kindValue = kindFilter ? kindFilter.value : "All";
      const minDocs = coverageFilter ? parseInt(coverageFilter.value || "1", 10) : 1;
      const stoplist = parseStoplist(stoplistInput ? stoplistInput.value : "");
      const stopTerms = Array.from(stoplist);
      const sortValue = sortBy ? sortBy.value : "doc_count";

      currentEntities = ENTITIES.filter(ent => {{
        if (kindValue !== "All" && ent.kind !== kindValue) {{
          return false;
        }}
        if (ent.doc_count < minDocs) {{
          return false;
        }}
        if (stopTerms.length) {{
          const name = normalizeTerm(ent.name);
          if (stopTerms.some(term => name.includes(term))) {{
            return false;
          }}
          const aliases = (ent.aliases || []).map(normalizeTerm);
          if (aliases.some(alias => stopTerms.some(term => alias.includes(term)))) {{
            return false;
          }}
        }}
        return true;
      }});

      currentEntities.sort((a, b) => {{
        if (sortValue === "total_mentions") {{
          return (b.total_mentions - a.total_mentions) || (b.doc_count - a.doc_count) || a.name.localeCompare(b.name);
        }}
        if (sortValue === "name") {{
          return a.name.localeCompare(b.name);
        }}
        return (b.doc_count - a.doc_count) || (b.total_mentions - a.total_mentions) || a.name.localeCompare(b.name);
      }});

      const keySet = new Set(currentEntities.map(ent => ent.key));
      currentEdges = EDGES.filter(edge => keySet.has(edge.a_key) && keySet.has(edge.b_key));
      currentEdges.sort((a, b) => {{
        return (b.page_count - a.page_count) || (b.doc_count - a.doc_count) || a.a.localeCompare(b.a);
      }});

      renderChart(currentEntities);
      renderConnections(currentEdges);
      renderEntityIndex(currentEntities);
    }}

    function initControls() {{
      if (!kindFilter) {{
        renderChart(currentEntities);
        renderConnections(currentEdges);
        renderEntityIndex(currentEntities);
        return;
      }}
      const kinds = Array.from(new Set(ENTITIES.map(ent => ent.kind))).sort();
      kindFilter.innerHTML = `<option value="All">All</option>${{kinds.map(kind => `<option value="${{escapeHtml(kind)}}">${{escapeHtml(kind)}}</option>`).join("")}}`;

      const maxDocCount = Math.max(...ENTITIES.map(ent => ent.doc_count), 1);
      if (coverageFilter) {{
        coverageFilter.max = String(maxDocCount);
        coverageFilter.value = "1";
      }}
      if (coverageValue) {{
        coverageValue.textContent = "1";
      }}
      if (coverageFilter && coverageValue) {{
        coverageFilter.addEventListener("input", () => {{
          coverageValue.textContent = coverageFilter.value;
        }});
      }}

      if (applyFiltersBtn) {{
        applyFiltersBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          applyFilters();
        }});
      }}
      if (resetFiltersBtn) {{
        resetFiltersBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          if (kindFilter) {{
            kindFilter.value = "All";
          }}
          if (coverageFilter) {{
            coverageFilter.value = "1";
          }}
          if (coverageValue) {{
            coverageValue.textContent = "1";
          }}
          if (stoplistInput) {{
            stoplistInput.value = "";
          }}
          if (sortBy) {{
            sortBy.value = "doc_count";
          }}
          applyFilters();
        }});
      }}

      if (exportEntitiesBtn) {{
        exportEntitiesBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          const csv = buildEntityIndexCsv(currentEntities);
          downloadText(`entity-brief-entities-${{DATA.run.uuid}}.csv`, csv);
        }});
      }}

      if (exportConnectionsCsvBtn) {{
        exportConnectionsCsvBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          const csv = buildConnectionsCsv(currentEdges);
          downloadText(`entity-brief-connections-${{DATA.run.uuid}}.csv`, csv);
        }});
      }}

      if (exportConnectionsJsonBtn) {{
        exportConnectionsJsonBtn.addEventListener("click", (e) => {{
          e.preventDefault();
          const jsonText = JSON.stringify(currentEdges, null, 2);
          downloadText(`entity-brief-connections-${{DATA.run.uuid}}.json`, jsonText);
        }});
      }}

      applyFilters();
    }}

    initControls();

    // ---- Skipped ----
    const sDiv = document.getElementById("skipped");
    const skipped = DATA.skipped || [];
    if (!skipped.length) {{
      sDiv.innerHTML = "<p class='muted small'>No skipped documents.</p>";
    }} else {{
      let html = "<ul class='small'>";
      for (const s of skipped) {{
        const url = safeUrl(s.url);
        const title = escapeHtml(s.title || `Document ${{s.doc_id || ""}}`);
        const label = url ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{title}}</a>` : title;
        const reason = s.reason ? ` <span class="muted">(${{escapeHtml(s.reason)}})</span>` : "";
        html += `<li>${{label}}${{reason}}</li>`;
      }}
      html += "</ul>";
      sDiv.innerHTML = html;
    }}

    // ---- Failures ----
    const fDiv = document.getElementById("failures");
    const fails = DATA.failures || [];
    if (!fails.length) {{
      fDiv.innerHTML = "<p class='muted small'>No failures recorded.</p>";
    }} else {{
      const pre = document.createElement("pre");
      pre.className = "small";
      pre.textContent = JSON.stringify(fails, null, 2);
      fDiv.appendChild(pre);
    }}
  </script>
</body>
</html>"""


if __name__ == "__main__":
    EntityBrief().main()
