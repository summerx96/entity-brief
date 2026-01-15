import html
import itertools
import json
import os
import re
import time
import uuid
from collections import Counter, defaultdict
from difflib import SequenceMatcher
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
DUPE_SUGGESTIONS_LIMIT = 20
DUPE_POOL_LIMIT = 200
WRITEBACK_TAG_LIMIT_DEFAULT = 5

KIND_ALIASES = {
    "person": "Person",
    "people": "Person",
    "org": "Organization",
    "organization": "Organization",
    "company": "Organization",
    "location": "Location",
    "place": "Location",
    "geo": "Location",
    "date": "Date",
    "time": "Date",
}
PERSON_PREFIXES = {"mr", "mrs", "ms", "dr", "prof", "hon", "sir", "madam"}
PERSON_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
ORG_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "co", "company", "corp", "corporation", "plc"}


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


def _normalize_kind(kind: str) -> str:
    kind = (kind or "").strip()
    if not kind:
        return "Other"
    mapped = KIND_ALIASES.get(kind.lower())
    return mapped or kind


def _strip_tokens(tokens: List[str], prefixes: set, suffixes: set) -> List[str]:
    while tokens and tokens[0] in prefixes:
        tokens = tokens[1:]
    while tokens and tokens[-1] in suffixes:
        tokens = tokens[:-1]
    return tokens


def _strip_org_suffixes(tokens: List[str]) -> List[str]:
    while tokens and tokens[-1] in ORG_SUFFIXES:
        tokens = tokens[:-1]
    return tokens


def _normalize_name(s: str, kind: str = "") -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    s = s.replace(".", "")
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split()
    if kind:
        kind_norm = kind.lower()
        if kind_norm in ("person", "people"):
            tokens = _strip_tokens(tokens, PERSON_PREFIXES, PERSON_SUFFIXES)
        elif kind_norm in ("organization", "org", "company"):
            tokens = _strip_org_suffixes(tokens)
    return " ".join(tokens)


def _name_acronym(name: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", name or "")
    letters = [t[0] for t in tokens if t and not t.isdigit()]
    return "".join(letters).upper()


def _is_acronym_name(name: str) -> bool:
    if not name:
        return False
    cleaned = re.sub(r"[^A-Za-z0-9]", "", name)
    if not cleaned or len(cleaned) > 6:
        return False
    return cleaned.isupper()


def _entity_key(ent: Dict[str, Any]) -> Tuple[str, str]:
    """
    Best-effort canonicalization:
    - Prefer stable IDs if present (mid/wiki_url)
    - Else normalized surface form with kind-aware cleanup
    """
    kind = _normalize_kind(str(ent.get("kind", "Other")))
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
    return (kind, f"v:{_normalize_name(val, kind)}")


def _entity_display(ent: Dict[str, Any]) -> str:
    return str(ent.get("value") or ent.get("name") or "").strip()


def _entity_payload(ent: Dict[str, Any]) -> Dict[str, Any]:
    payload = ent.get("entity")
    if isinstance(payload, dict):
        return payload
    return ent


def _escape(s: str) -> str:
    return html.escape(s or "", quote=True)


def _find_possible_duplicates(entities: List[Dict[str, Any]], min_ratio: float = 0.9) -> List[Dict[str, Any]]:
    entries = []
    for ent in entities[:DUPE_POOL_LIMIT]:
        name = str(ent.get("name") or "")
        kind = str(ent.get("kind") or "Other")
        norm = _normalize_name(name, kind)
        compact = norm.replace(" ", "")
        tokens = set(norm.split())
        acronym = _name_acronym(name)
        entries.append({
            "key": ent.get("key"),
            "name": name,
            "kind": kind,
            "doc_count": int(ent.get("doc_count") or 0),
            "norm": norm,
            "compact": compact,
            "tokens": tokens,
            "acronym": acronym,
            "is_acronym": _is_acronym_name(name),
        })

    suggestions: List[Dict[str, Any]] = []
    seen = set()
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a = entries[i]
            b = entries[j]
            if a["kind"] != b["kind"]:
                continue
            if a["key"] == b["key"]:
                continue
            reason = ""
            if a["compact"] and a["compact"] == b["compact"]:
                reason = "normalized match"
            elif a["acronym"] and a["acronym"] == b["acronym"] and (a["is_acronym"] or b["is_acronym"]):
                reason = f"acronym match ({a['acronym']})"
            else:
                if a["tokens"] and b["tokens"] and (a["tokens"] & b["tokens"]):
                    ratio = SequenceMatcher(None, a["norm"], b["norm"]).ratio()
                    if ratio >= min_ratio:
                        reason = f"similar names ({ratio:.2f})"
            if reason:
                pair_key = tuple(sorted([a["key"], b["key"]]))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                suggestions.append({
                    "kind": a["kind"],
                    "a_name": a["name"],
                    "b_name": b["name"],
                    "a_docs": a["doc_count"],
                    "b_docs": b["doc_count"],
                    "reason": reason,
                })
                if len(suggestions) >= DUPE_SUGGESTIONS_LIMIT:
                    return suggestions
    return suggestions


def _apply_tag_prefix(prefix: str, name: str) -> str:
    prefix = (prefix or "").strip()
    if not prefix:
        return name
    return f"{prefix}{name}"


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
        writeback_tags = bool(data.get("writeback_tags", False))
        writeback_tag_limit = _safe_int(data.get("writeback_tag_limit", WRITEBACK_TAG_LIMIT_DEFAULT),
                                        WRITEBACK_TAG_LIMIT_DEFAULT)
        writeback_tag_limit = max(writeback_tag_limit, 0)
        writeback_tag_prefix = str(data.get("writeback_tag_prefix", "entity:") or "entity:").strip()

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
            doc_data = getattr(doc, "data", None)
            if not isinstance(doc_data, dict):
                doc_data = {}
            total_pages += page_count

            doc_meta[doc_id] = {
                "id": doc_id,
                "title": title,
                "url": canonical_url,
                "page_count": page_count,
                "data": doc_data,
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
                    kind = _normalize_kind(str(payload.get("kind", "Other")))
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

        # Finalize cluster display name and build per-doc rollups
        cluster_list: List[Dict[str, Any]] = []
        doc_entities_by_doc: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for key, c in clusters.items():
            display = c["display_names"].most_common(1)[0][0] if c["display_names"] else key[1]
            c["display"] = display
            # JSON-ify sets/counters
            docs_out = []
            for did, dd in c["docs"].items():
                meta = doc_meta.get(did, {"id": did, "title": f"Document {did}", "url": ""})
                doc_entities_by_doc[did].append({
                    "name": display,
                    "kind": c["kind"],
                    "count": dd["count"],
                })
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

        # ---- Alias suggestions (heuristic) ----
        demo_mode = str(run_uuid).startswith("demo")
        dupe_ratio = 0.88
        if demo_mode:
            dupe_ratio = 0.82
        duplicates = _find_possible_duplicates(cluster_list, min_ratio=dupe_ratio)
        if demo_mode and not duplicates and cluster_list:
            sample = [e for e in cluster_list[:5] if e.get("name")]
            if len(sample) >= 2:
                duplicates.append({
                    "kind": sample[0].get("kind", "Other"),
                    "a_name": sample[0].get("name"),
                    "b_name": sample[1].get("name"),
                    "a_docs": sample[0].get("doc_count", 0),
                    "b_docs": sample[1].get("doc_count", 0),
                    "reason": "demo preview (no close matches detected)",
                })

        # ---- Doc tag suggestions + optional writeback ----
        doc_tags: List[Dict[str, Any]] = []
        doc_tag_map: Dict[int, List[str]] = {}
        for did, ent_list in doc_entities_by_doc.items():
            ent_list.sort(key=lambda x: (-x["count"], x["name"].lower()))
            limit = writeback_tag_limit if writeback_tag_limit > 0 else 0
            tags = [e["name"] for e in ent_list[:limit]] if limit else []
            tag_values = [_apply_tag_prefix(writeback_tag_prefix, t) for t in tags]
            meta = doc_meta.get(did, {"id": did, "title": f"Document {did}", "url": ""})
            doc_tags.append({
                "doc_id": did,
                "title": meta["title"],
                "url": meta["url"],
                "entity_count": len(ent_list),
                "tags": tags,
                "tag_values": tag_values,
            })
            doc_tag_map[did] = tag_values

        doc_tags.sort(key=lambda x: (-len(x.get("tag_values") or []), x["title"].lower()))

        writeback = {
            "enabled": writeback_tags,
            "tag_limit": writeback_tag_limit,
            "tag_prefix": writeback_tag_prefix,
            "updated": 0,
            "skipped": 0,
            "failures": [],
        }
        if writeback_tags:
            self.set_message("Writing entity tags to document metadata...")
            for did, tags in doc_tag_map.items():
                if not tags:
                    writeback["skipped"] += 1
                    continue
                try:
                    existing = doc_meta.get(did, {}).get("data") or {}
                    if not isinstance(existing, dict):
                        existing = {}
                    entity_brief = existing.get("entity_brief")
                    if not isinstance(entity_brief, dict):
                        entity_brief = {}
                    entity_brief.update({
                        "run_uuid": run_uuid,
                        "version": ADDON_VERSION,
                        "generated_at": int(time.time()),
                        "tags": tags,
                        "entity_count": len(doc_entities_by_doc.get(did, [])),
                    })
                    updated = dict(existing)
                    updated["entity_brief"] = entity_brief
                    self.client.patch(f"documents/{did}/", json={"data": updated})
                    writeback["updated"] += 1
                except Exception as e:
                    writeback["failures"].append({"doc_id": did, "error": str(e)})

        # ---- Build report data ----
        runtime_s = round(time.time() - start_ts, 2)
        docs_with_entities = len(doc_entities)
        entity_coverage = (docs_with_entities / len(docs)) if docs else 0
        skipped_map = {s.get("doc_id"): s.get("reason") for s in skipped}
        failure_map = {f.get("doc_id"): f.get("error") for f in failures}
        documents_out: List[Dict[str, Any]] = []
        for doc in docs:
            doc_id = int(getattr(doc, "id"))
            meta = doc_meta.get(doc_id, {"id": doc_id, "title": f"Document {doc_id}", "url": "", "page_count": 0})
            if doc_id in doc_entities:
                status = "entities present"
                reason = ""
            elif doc_id in failure_map:
                status = "failed"
                reason = failure_map.get(doc_id, "")
            elif doc_id in skipped_map:
                status = "skipped"
                reason = skipped_map.get(doc_id, "")
            else:
                status = "no entities"
                reason = ""
            documents_out.append({
                "doc_id": doc_id,
                "title": meta.get("title", f"Document {doc_id}"),
                "url": meta.get("url", ""),
                "page_count": int(meta.get("page_count") or 0),
                "entity_count": len(doc_entities_by_doc.get(doc_id, [])),
                "status": status,
                "reason": reason,
            })
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
            "documents": documents_out,
            "duplicates": duplicates,
            "doc_tags": doc_tags,
            "writeback": writeback,
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
        coverage_preview_block = ""
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
        elif demo_mode:
            threshold_pct = int(ENTITY_COVERAGE_WARN_THRESHOLD * 100)
            coverage_preview_block = f"""
  <div class="card">
    <h3>Low entity coverage (preview)</h3>
    <p class="small muted">This warning appears when fewer than {threshold_pct}% of docs have entities.</p>
    <div class="card warn">
      <p class="small"><strong>What to run first:</strong></p>
      <ol class="small">
        <li>Open a document and run <em>Edit -> Entities -> Extract entities</em> (or run the Google Cloud Entity Extractor add-on).</li>
        <li>Wait for extraction to finish, then re-run Entity Brief.</li>
        <li>Docs without entities appear under <strong>Skipped (no entities)</strong>.</li>
      </ol>
    </div>
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
    .support-block {{ margin-top: 12px; }}
    .btn.small {{ font-size: 0.85em; padding: 6px 8px; }}
    textarea[readonly] {{ background: #fafafa; }}
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

        <div class="support-block">
          <h4>Quick feedback (optional)</h4>
          <label for="feedbackNotes">Notes for the developer</label>
          <textarea id="feedbackNotes" rows="2" placeholder="What worked well? What was noisy?"></textarea>
          <p>
            <button class="btn small" id="copyFeedback" type="button">Copy feedback</button>
            <a class="btn small" id="mailtoFeedback" href="#">Email summary + feedback</a>
          </p>
        </div>

        <details class="support-block" id="supportLetter">
          <summary><strong>Generate support letter draft (optional)</strong></summary>
          <p class="small muted">No data leaves this report unless you copy or email it.</p>
          <div class="controls">
            <div>
              <label for="supportName">Name</label>
              <input type="text" id="supportName" placeholder="Jane Doe" />
            </div>
            <div>
              <label for="supportRole">Role / Title</label>
              <input type="text" id="supportRole" placeholder="Investigative Reporter" />
            </div>
            <div>
              <label for="supportOrg">Organization</label>
              <input type="text" id="supportOrg" placeholder="Newsroom or nonprofit" />
            </div>
            <div>
              <label for="supportEmail">Email</label>
              <input type="text" id="supportEmail" placeholder="name@organization.org" />
            </div>
            <div>
              <label for="supportTimeSaved">Time saved (optional)</label>
              <input type="text" id="supportTimeSaved" placeholder="e.g., 2 hours" />
            </div>
          </div>
          <div class="controls">
            <div>
              <label for="supportQuote">Permission to quote</label>
              <select id="supportQuote">
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
            </div>
            <div>
              <label for="supportLetterhead">Signed letter on letterhead?</label>
              <select id="supportLetterhead">
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>
            </div>
          </div>
          <div class="support-block">
            <label>Requested improvements (optional)</label>
            <label class="small"><input type="checkbox" class="supportImprovement" value="Improved entity resolution and alias merging" /> Improved entity resolution and alias merging</label><br/>
            <label class="small"><input type="checkbox" class="supportImprovement" value="Write back entity tags to DocumentCloud metadata" /> Write back entity tags to DocumentCloud metadata</label><br/>
            <label class="small"><input type="checkbox" class="supportImprovement" value="Noise controls (filters, stoplists, sorting)" /> Noise controls (filters, stoplists, sorting)</label><br/>
            <label class="small"><input type="checkbox" class="supportImprovement" value="Page-level co-occurrence connections with example pages" /> Page-level co-occurrence connections with example pages</label><br/>
            <label class="small"><input type="checkbox" class="supportImprovement" value="Additional export formats for collaboration" /> Additional export formats for collaboration</label>
          </div>
          <div class="support-block">
            <label for="supportNotes">Impact summary (optional)</label>
            <textarea id="supportNotes" rows="2" placeholder="Example: helped triage a 90-page FOIA release and surface recurring names quickly."></textarea>
          </div>
          <div class="support-block">
            <label for="supportLetterText">Draft letter</label>
            <textarea id="supportLetterText" rows="10" readonly></textarea>
          </div>
          <p>
            <button class="btn small" id="buildSupportLetter" type="button">Generate letter</button>
            <button class="btn small" id="copySupportLetter" type="button">Copy letter</button>
            <a class="btn small" id="mailtoSupportLetter" href="#">Open email draft</a>
          </p>
        </details>
      </div>

      <div class="card warn">
        <h3>Privacy / trust notes</h3>
        <ul class="small">
          <li>This report is generated from the documents selected for this run.</li>
          <li>By default, no document text is sent to any external service by this Add-On.</li>
          <li>This version does not send usage metrics.</li>
          <li>If writeback is enabled, top entity tags are stored in DocumentCloud metadata (<code>data.entity_brief.tags</code>).</li>
        </ul>
      </div>
    </div>
  </div>

  {coverage_warning_block}
  {coverage_preview_block}

  <div class="card">
    <h2>Documents in this run</h2>
    <div id="documentsList"></div>
  </div>

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
    <h2>Possible duplicates (alias suggestions)</h2>
    <div id="duplicateSuggestions"></div>
    <p class="small muted">Heuristic suggestions only - review before treating them as the same entity.</p>
  </div>

  <div class="card">
    <h2>Document tags (optional writeback)</h2>
    <p class="small muted">
      Top entities per document. If enabled in add-on settings, tags are written to document metadata at
      <code>data.entity_brief.tags</code>.
    </p>
    <div id="writebackSummary"></div>
    <div id="docTags"></div>
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
    async function copyText(text, successMessage) {{
      try {{
        await navigator.clipboard.writeText(text);
        alert(successMessage || "Copied to clipboard.");
      }} catch (err) {{
        alert("Copy failed (browser permission). You can manually select the text and copy.");
      }}
    }}

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

    const devEmail = (DATA.meta && DATA.meta.developer_email) ? String(DATA.meta.developer_email) : "";
    const copyBtn = document.getElementById("copyBtn");
    if (copyBtn) {{
      copyBtn.addEventListener("click", async (e) => {{
        e.preventDefault();
        await copyText(runSummaryText(), "Copied run summary to clipboard.");
      }});
    }}

    const mailtoLink = document.getElementById("mailtoLink");
    if (mailtoLink && devEmail) {{
      const mailto = `mailto:${{encodeURIComponent(devEmail)}}?subject=${{encodeURIComponent("Entity Brief feedback (" + DATA.run.uuid + ")")}}&body=${{encodeURIComponent(runSummaryText())}}`;
      mailtoLink.setAttribute("href", mailto);
    }} else if (mailtoLink) {{
      mailtoLink.style.display = "none";
    }}

    const feedbackNotes = document.getElementById("feedbackNotes");
    const copyFeedbackBtn = document.getElementById("copyFeedback");
    const mailtoFeedback = document.getElementById("mailtoFeedback");

    function feedbackText() {{
      const notes = feedbackNotes ? String(feedbackNotes.value || "").trim() : "";
      if (!notes) {{
        return runSummaryText();
      }}
      return `${{runSummaryText()}}\\n\\nFeedback:\\n${{notes}}`;
    }}

    function updateFeedbackMailto() {{
      if (!mailtoFeedback || !devEmail) {{
        if (mailtoFeedback) {{
          mailtoFeedback.style.display = "none";
        }}
        return;
      }}
      const mailto = `mailto:${{encodeURIComponent(devEmail)}}?subject=${{encodeURIComponent("Entity Brief feedback (" + DATA.run.uuid + ")")}}&body=${{encodeURIComponent(feedbackText())}}`;
      mailtoFeedback.setAttribute("href", mailto);
    }}

    if (copyFeedbackBtn) {{
      copyFeedbackBtn.addEventListener("click", async (e) => {{
        e.preventDefault();
        await copyText(feedbackText(), "Copied feedback to clipboard.");
      }});
    }}
    if (feedbackNotes) {{
      feedbackNotes.addEventListener("input", updateFeedbackMailto);
    }}
    updateFeedbackMailto();

    const supportLetterTextArea = document.getElementById("supportLetterText");
    const buildSupportLetterBtn = document.getElementById("buildSupportLetter");
    const copySupportLetterBtn = document.getElementById("copySupportLetter");
    const mailtoSupportLetter = document.getElementById("mailtoSupportLetter");

    function supportLetterText() {{
      const name = String(document.getElementById("supportName")?.value || "").trim() || "[Name]";
      const role = String(document.getElementById("supportRole")?.value || "").trim();
      const org = String(document.getElementById("supportOrg")?.value || "").trim();
      const email = String(document.getElementById("supportEmail")?.value || "").trim() || "[Email]";
      const timeSaved = String(document.getElementById("supportTimeSaved")?.value || "").trim();
      const quote = String(document.getElementById("supportQuote")?.value || "Yes").trim();
      const letterhead = String(document.getElementById("supportLetterhead")?.value || "No").trim();
      const impact = String(document.getElementById("supportNotes")?.value || "").trim();
      const improvements = Array.from(document.querySelectorAll(".supportImprovement:checked"))
        .map(input => input.value)
        .filter(Boolean);

      const r = DATA.run;
      const docsCount = r.docs_processed || 0;
      const pagesCount = r.pages_processed || 0;
      const runtime = r.runtime_seconds || 0;

      let identity = name;
      if (role && org) {{
        identity += ", " + role + " at " + org;
      }} else if (role) {{
        identity += ", " + role;
      }} else if (org) {{
        identity += ", " + org;
      }}

      let impactSentence = "";
      if (impact) {{
        impactSentence = "In practical terms, this workflow helped me " + impact;
      }} else {{
        impactSentence = "In practical terms, this workflow helped me triage the release and share findings with collaborators";
      }}
      if (timeSaved) {{
        impactSentence += ", saving approximately " + timeSaved + ".";
      }} else {{
        impactSentence += ".";
      }}

      let letter = "";
      letter += "To Whom It May Concern,\\n\\n";
      letter += "My name is " + identity + ". In my work involving public records / FOIA document review, I used the Entity Brief DocumentCloud add-on to analyze a multi-document release.\\n\\n";
      letter += "Using Entity Brief, I generated a cross-document entity report covering " + docsCount + " documents (" + pagesCount + " pages) in approximately " + runtime + " seconds. The report summarized recurring people/organizations/locations across the set and provided page-level references that made it faster to locate key entities.\\n\\n";
      letter += impactSentence + "\\n\\n";
      letter += "Tools like Entity Brief support investigative and civic transparency work by reducing the time required to triage large public records and by making it easier to locate relevant material across document sets.\\n\\n";
      if (improvements.length) {{
        letter += "Optional requested improvements:\\n";
        for (const item of improvements) {{
          letter += "- " + item + "\\n";
        }}
        letter += "\\n";
      }}
      letter += "Sincerely,\\n" + name + "\\n" + (role || "[Role/Title]") + (org ? ", " + org : "") + "\\n" + email + "\\n";
      letter += "Permission to quote: " + quote + "\\nWilling to provide signed letter on letterhead: " + letterhead + "\\n";
      return letter;
    }}

    function updateSupportLetter() {{
      if (!supportLetterTextArea) {{
        return;
      }}
      const text = supportLetterText();
      supportLetterTextArea.value = text;
      if (mailtoSupportLetter && devEmail) {{
        const subject = "Entity Brief support letter (" + DATA.run.uuid + ")";
        const mailto = `mailto:${{encodeURIComponent(devEmail)}}?subject=${{encodeURIComponent(subject)}}&body=${{encodeURIComponent(text)}}`;
        mailtoSupportLetter.setAttribute("href", mailto);
      }} else if (mailtoSupportLetter) {{
        mailtoSupportLetter.style.display = "none";
      }}
    }}

    if (buildSupportLetterBtn) {{
      buildSupportLetterBtn.addEventListener("click", (e) => {{
        e.preventDefault();
        updateSupportLetter();
      }});
    }}
    if (copySupportLetterBtn) {{
      copySupportLetterBtn.addEventListener("click", async (e) => {{
        e.preventDefault();
        if (supportLetterTextArea && !supportLetterTextArea.value) {{
          updateSupportLetter();
        }}
        if (supportLetterTextArea) {{
          await copyText(supportLetterTextArea.value, "Copied support letter to clipboard.");
        }}
      }});
    }}
    if (mailtoSupportLetter && devEmail) {{
      mailtoSupportLetter.addEventListener("click", () => {{
        if (supportLetterTextArea && !supportLetterTextArea.value) {{
          updateSupportLetter();
        }}
      }});
    }}

    const ENTITIES = DATA.entities || [];
    const EDGES = DATA.edges || [];
    const DUPES = DATA.duplicates || [];
    const DOC_TAGS = DATA.doc_tags || [];
    const WRITEBACK = DATA.writeback || {{}};
    const DOCS = DATA.documents || [];

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

    function renderDuplicates() {{
      const dupDiv = document.getElementById("duplicateSuggestions");
      if (!dupDiv) {{
        return;
      }}
      if (!DUPES.length) {{
        dupDiv.innerHTML = "<p class='muted small'>No duplicate suggestions found.</p>";
        return;
      }}
      let html = "<table><thead><tr><th>Kind</th><th>Entity A</th><th>Entity B</th><th>Reason</th></tr></thead><tbody>";
      for (const d of DUPES) {{
        html += `<tr><td>${{escapeHtml(d.kind)}}</td><td>${{escapeHtml(d.a_name)}} (docs: ${{escapeHtml(d.a_docs)}})</td><td>${{escapeHtml(d.b_name)}} (docs: ${{escapeHtml(d.b_docs)}})</td><td>${{escapeHtml(d.reason)}}</td></tr>`;
      }}
      html += "</tbody></table>";
      dupDiv.innerHTML = html;
    }}

    function renderWritebackSummary() {{
      const summaryDiv = document.getElementById("writebackSummary");
      if (!summaryDiv) {{
        return;
      }}
      if (!WRITEBACK || !WRITEBACK.enabled) {{
        summaryDiv.innerHTML = "<p class='muted small'>Writeback is off. Enable writeback in the add-on settings to store tags in DocumentCloud metadata.</p>";
        return;
      }}
      const failures = WRITEBACK.failures || [];
      let html = `<p class="small">Writeback enabled: updated ${{WRITEBACK.updated || 0}} docs, skipped ${{WRITEBACK.skipped || 0}}, failures ${{failures.length}}.</p>`;
      html += `<p class="small muted">Tag limit: ${{WRITEBACK.tag_limit || 0}}. Prefix: <code>${{escapeHtml(WRITEBACK.tag_prefix || "")}}</code></p>`;
      if (failures.length) {{
        html += "<details class='small'><summary>Writeback failures</summary><ul>";
        for (const f of failures.slice(0, 10)) {{
          html += `<li>Doc ${{escapeHtml(f.doc_id)}}: ${{escapeHtml(f.error)}}</li>`;
        }}
        html += "</ul></details>";
      }}
      summaryDiv.innerHTML = html;
    }}

    function renderDocuments() {{
      const docDiv = document.getElementById("documentsList");
      if (!docDiv) {{
        return;
      }}
      if (!DOCS.length) {{
        docDiv.innerHTML = "<p class='muted small'>No documents listed for this run.</p>";
        return;
      }}
      let html = "<table><thead><tr><th>Document</th><th>Pages</th><th>Entities</th><th>Status</th></tr></thead><tbody>";
      for (const doc of DOCS) {{
        const url = safeUrl(doc.url);
        const title = escapeHtml(doc.title || ("Document " + (doc.doc_id || "")));
        const label = url ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{title}}</a>` : title;
        const docId = doc.doc_id ? ` (ID: ${{escapeHtml(doc.doc_id)}})` : "";
        const pages = doc.page_count !== undefined ? escapeHtml(doc.page_count) : "-";
        const entities = doc.entity_count !== undefined ? escapeHtml(doc.entity_count) : "-";
        const status = escapeHtml(doc.status || "");
        const reason = doc.reason ? ` <span class="muted">(${{escapeHtml(doc.reason)}})</span>` : "";
        html += `<tr><td>${{label}}${{docId}}</td><td>${{pages}}</td><td>${{entities}}</td><td>${{status}}${{reason}}</td></tr>`;
      }}
      html += "</tbody></table>";
      docDiv.innerHTML = html;
    }}

    function renderDocTags() {{
      const docTagsDiv = document.getElementById("docTags");
      if (!docTagsDiv) {{
        return;
      }}
      if (!DOC_TAGS.length) {{
        docTagsDiv.innerHTML = "<p class='muted small'>No tag suggestions available.</p>";
        return;
      }}
      let html = "<table><thead><tr><th>Document</th><th>Tags</th><th></th></tr></thead><tbody>";
      for (const doc of DOC_TAGS) {{
        const url = safeUrl(doc.url);
        const docId = doc.doc_id || "";
        const title = escapeHtml(doc.title || ("Document " + docId));
        const link = url ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noreferrer">${{title}}</a>` : title;
        const tags = (doc.tag_values || doc.tags || []).join(", ");
        const tagText = escapeHtml(tags);
        const copyButton = tags ? `<button class="btn small" type="button" data-tags="${{tagText}}">Copy tags</button>` : "";
        html += `<tr><td>${{link}}</td><td>${{tagText || "-"}}</td><td>${{copyButton}}</td></tr>`;
      }}
      html += "</tbody></table>";
      docTagsDiv.innerHTML = html;
      if (!docTagsDiv.dataset.bound) {{
        docTagsDiv.addEventListener("click", async (event) => {{
          const target = event.target;
          if (!(target instanceof HTMLElement)) {{
            return;
          }}
          const button = target.closest("button[data-tags]");
          if (!button) {{
            return;
          }}
          event.preventDefault();
          const tags = button.getAttribute("data-tags") || "";
          await copyText(tags, "Copied tags to clipboard.");
        }});
        docTagsDiv.dataset.bound = "true";
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
    renderDocuments();
    renderDuplicates();
    renderWritebackSummary();
    renderDocTags();

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
