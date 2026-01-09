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


def _api_get_json(url: str, token: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _api_get_all_pages(url: str, token: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Handle DRF-style pagination: {results: [...], next: url}
    """
    out: List[Dict[str, Any]] = []
    next_url = url
    next_params = dict(params or {})
    while next_url:
        payload = _api_get_json(next_url, token, params=next_params)
        if isinstance(payload, dict) and "results" in payload:
            out.extend(payload.get("results", []))
            next_url = payload.get("next")
            next_params = {}  # next already includes query params
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
    wiki = ent.get("wiki_url") or ent.get("wikipedia_url")
    if mid:
        return (kind, f"mid:{mid}")
    if wiki:
        return (kind, f"wiki:{wiki}")
    val = str(ent.get("value") or ent.get("name") or "")
    return (kind, f"v:{_normalize_name(val)}")


def _entity_display(ent: Dict[str, Any]) -> str:
    return str(ent.get("value") or ent.get("name") or "").strip()


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

        # ---- Extract entities per doc ----
        token = _get_access_token(self)
        self.set_message(f"Fetching entities for {len(docs)} documents...")
        doc_entities: Dict[int, List[Dict[str, Any]]] = {}
        failures: List[Dict[str, Any]] = []

        for i, doc in enumerate(docs, start=1):
            try:
                doc_id = int(getattr(doc, "id"))
                url = f"{API_BASE}documents/{doc_id}/entities/"
                params = {
                    "expand": "occurrences",
                    "relevance__gt": min_rel,
                }
                ents = _api_get_all_pages(url, token, params=params)
                doc_entities[doc_id] = ents
                self.set_progress(10 + int(i / max(len(docs), 1) * 30))
            except Exception as e:
                failures.append({"doc_id": getattr(doc, "id", None), "error": str(e)})
                continue

        # ---- Build cross-doc clusters ----
        self.set_message("Normalizing and aggregating entities...")
        clusters: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Per-doc canonical set for co-occurrence
        doc_entity_keys: Dict[int, List[Tuple[str, str]]] = {}

        for doc_id, ents in doc_entities.items():
            keys_for_doc = []
            for ent in ents:
                try:
                    kind = str(ent.get("kind", "Other"))
                    display = _entity_display(ent)
                    if not display:
                        continue

                    key = _entity_key(ent)
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

                    count = int(ent.get("count") or 0)
                    c["total_mentions"] += count

                    if doc_id not in c["docs"]:
                        c["docs"][doc_id] = {"count": 0, "pages": set(), "samples": []}
                        c["doc_count"] += 1

                    c["docs"][doc_id]["count"] += count

                    # Occurrences may include page/context; best-effort
                    occs = ent.get("occurrences") or []
                    for occ in occs[:5]:
                        page = occ.get("page")
                        if isinstance(page, int):
                            c["docs"][doc_id]["pages"].add(page)
                        snippet = occ.get("context") or occ.get("snippet") or ""
                        if snippet:
                            c["docs"][doc_id]["samples"].append(str(snippet)[:200])

                except Exception:
                    continue

            doc_entity_keys[doc_id] = keys_for_doc

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
            pair_counts = Counter()
            for did, keys in doc_entity_keys.items():
                # Use only top entities per doc to avoid combinatorial blowup
                unique = list(dict.fromkeys(keys))  # stable unique
                unique = unique[:25]
                for a, b in itertools.combinations(sorted(unique), 2):
                    pair_counts[(a, b)] += 1
            for (a, b), dc in pair_counts.most_common(50):
                a_name = clusters.get(a, {}).get("display_names", Counter()).most_common(1)
                b_name = clusters.get(b, {}).get("display_names", Counter()).most_common(1)
                edges.append({
                    "a": a_name[0][0] if a_name else a[1],
                    "b": b_name[0][0] if b_name else b[1],
                    "doc_count": dc,
                })

        # ---- Build report data ----
        runtime_s = round(time.time() - start_ts, 2)
        report_data = {
            "run": {
                "uuid": run_uuid,
                "version": ADDON_VERSION,
                "runtime_seconds": runtime_s,
                "docs_processed": len(docs),
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
            "failures": failures,
        }

    def _render_html(self, data: Dict[str, Any]) -> str:
        # Embed data as JSON so the report is one file
        data_json = json.dumps(data, ensure_ascii=False)
        run = data["run"]
        meta = data["meta"]
        feedback_url = meta.get("feedback_url") or ""
        feedback_block = ""
        if feedback_url:
            feedback_block = f"""
        <p class="small">
          Feedback form: <a href="{_escape(feedback_url)}">{_escape(feedback_url)}</a>
        </p>"""

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
    code {{ background: #f6f6f6; padding: 2px 4px; border-radius: 4px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 6px 8px; vertical-align: top; }}
    th {{ text-align: left; }}
    details summary {{ cursor: pointer; }}
    .small {{ font-size: 0.9em; }}
    .warn {{ background: #fff6e5; border-color: #ffd28a; }}
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
        {feedback_block}
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

  <div class="row">
    <div class="card">
      <h2>Top Entities (by document coverage)</h2>
      <svg id="barChart" width="900" height="380"></svg>
      <p class="small muted">Bars show how many documents mention each entity (top list).</p>
    </div>

    <div class="card">
      <h2>Top Connections (co-occurrence)</h2>
      <div id="connections"></div>
      <p class="small muted">Pairs that appear together across the same documents (ranked by doc count).</p>
    </div>
  </div>

  <div class="card">
    <h2>Entity Index</h2>
    <p class="small muted">
      Expand an entity to see which documents/pages it appears in.
    </p>
    <div id="entityIndex"></div>
  </div>

  <div class="card">
    <h2>Failures / missing entities</h2>
    <div id="failures"></div>
  </div>

  <script id="data" type="application/json">{html.escape(data_json)}</script>
  <script>
    const DATA = JSON.parse(document.getElementById("data").textContent);

    // ---- Share helpers ----
    function runSummaryText() {{
      const r = DATA.run;
      return [
        "Entity Brief - Run Summary",
        `Run UUID: ${{r.uuid}}`,
        `Version: ${{r.version}}`,
        `Docs processed: ${{r.docs_processed}}`,
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

    const feedbackLine = DATA.meta.feedback_url ? "\\n\\nFeedback form: " + DATA.meta.feedback_url : "";
    const mailto = `mailto:${{encodeURIComponent(DATA.meta.developer_email)}}?subject=${{encodeURIComponent("Entity Brief feedback (" + DATA.run.uuid + ")")}}&body=${{encodeURIComponent(runSummaryText() + feedbackLine)}}`;
    document.getElementById("mailtoLink").setAttribute("href", mailto);

    // ---- Bar chart (D3) ----
    const top = (DATA.top_entities || []).slice(0, 15).map(d => ({{
      name: d.name,
      kind: d.kind,
      doc_count: d.doc_count,
      total_mentions: d.total_mentions
    }}));

    const svg = d3.select("#barChart");
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

    // ---- Connections list ----
    const edges = (DATA.edges || []).slice(0, 20);
    const connDiv = document.getElementById("connections");
    if (!edges.length) {{
      connDiv.innerHTML = "<p class='muted small'>No connections computed (or not enough entities).</p>";
    }} else {{
      let html = "<table><thead><tr><th>Entity A</th><th>Entity B</th><th># Docs together</th></tr></thead><tbody>";
      for (const e of edges) {{
        html += `<tr><td>${{e.a}}</td><td>${{e.b}}</td><td>${{e.doc_count}}</td></tr>`;
      }}
      html += "</tbody></table>";
      connDiv.innerHTML = html;
    }}

    // ---- Entity index ----
    const idx = document.getElementById("entityIndex");
    const ents = (DATA.entities || []).slice(0, 200);
    idx.innerHTML = ents.map(ent => {{
      const docs = (ent.docs || []).map(d => {{
        const pages = (d.pages || []).map(p => `p${{p}}`).join(", ");
        const samples = (d.samples || []).slice(0, 2).map(s => `<div class="muted small">...${{s}}...</div>`).join("");
        const link = d.url ? `<a href="${{d.url}}" target="_blank" rel="noreferrer">${{d.title}}</a>` : d.title;
        return `<div class="small"><strong>${{link}}</strong> - mentions: ${{d.count}}${{pages ? " - pages: " + pages : ""}}${{samples}}</div>`;
      }}).join("");
      return `
        <details class="card">
          <summary><strong>${{ent.name}}</strong> <span class="muted">(${{ent.kind}})</span> - docs: <strong>${{ent.doc_count}}</strong>, mentions: <strong>${{ent.total_mentions}}</strong></summary>
          <div class="small muted">Aliases (sample): ${{(ent.aliases||[]).slice(0,10).join(", ")}}</div>
          <div style="margin-top:8px;">${{docs || "<div class='muted small'>No doc details</div>"}}</div>
        </details>
      `;
    }}).join("");

    // ---- Failures ----
    const fDiv = document.getElementById("failures");
    const fails = DATA.failures || [];
    if (!fails.length) {{
      fDiv.innerHTML = "<p class='muted small'>No failures recorded.</p>";
    }} else {{
      fDiv.innerHTML = "<pre class='small'>" + JSON.stringify(fails, null, 2) + "</pre>";
    }}
  </script>
</body>
</html>"""


if __name__ == "__main__":
    EntityBrief().main()
