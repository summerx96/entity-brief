# PRD - Entity Brief (NIW-MVP)

## Product name
- Name: Entity Brief
- Subtitle: Cross-document entity index + connection cues for FOIA / investigative workflows

## Problem
FOIA releases often arrive as dozens of documents. Even with DocumentCloud search/entity extraction, journalists still struggle to:
- Understand who/what matters across the whole set
- Find where those names appear (doc/page)
- Produce a shareable artifact they can send to editors/collaborators

## Users
- Investigative reporters handling FOIA releases
- Editors who want quick "who's involved" snapshots
- Civic transparency orgs analyzing large public record collections

## What journalists want (and hate)
They want:
- A lead list in 30-60 seconds: who/what/where/when to look at first
- Receipts: clickable doc/page context to verify quickly
- A single artifact they can share with an editor
- Outputs that do not require data plumbing

They hate:
- 5+ CSV files with unclear purpose
- Network graphs that turn into hairballs
- Anything that feels like surveillance or hidden telemetry

## Inputs
- Selected documents (<= 25 typical UI limit; query mode supported with a cap)
- Uses existing DocumentCloud entity extraction output (no heavy ML/GPU)

## Data dependency
- Requires DocumentCloud entity extraction to already exist per document (or run the Google Cloud Entity Extractor add-on first).
- Entities are accessed via GET /api/documents/<document_id>/entities/ (paginated).
- Entity fields include name, type, relevance, occurrences (page, content, offsets), and sometimes identifiers like mid or wikipedia_url.

## Outputs
- One HTML report (downloadable from the Add-On run)
- Report-first: primary output is a human-readable brief for sharing and verification (with optional client-side exports).
- Optional tags written back to documents (opt-in in this version; stored at `data.entity_brief.tags`).
- Add-On constraint: one file per run; ship a single HTML file for MVP
- Feedback form and telemetry are deferred to the next version.
- Report includes a "Skipped (no entities)" section for docs without extracted entities.
- Report shows a "Low entity coverage" warning when too few docs have entities and explains what to run first.

## Execution environment constraints
- Runs as a DocumentCloud Add-On (typically via GitHub Actions).
- Many add-ons default to ~5-minute timeouts unless increased in workflow config; GitHub Actions caps total runtime at 6 hours.
- Only one downloadable file per run; if multiple outputs, zip them. Uploaded file is available for five days.

## MVP features

### F1 - Cross-doc entity index (core value)
- Normalize entities across docs
- Primary key: (kind, mid/wiki_url if available else normalized name)
- For each entity show:
  - Kind (Person/Org/Location/Date/Other)
  - Total mentions
  - Doc coverage (# docs containing it)
  - Per-doc breakdown with doc links + page refs (receipts, linked to exact pages)
  - Heuristic duplicate suggestions for alias cleanup (report section)

### F2 - Two simple visuals (D3 constraint)
- Bar chart (D3): Top 15 entities by doc coverage
- List/table: Top 20 co-occurrence pairs ranked by page/doc count, with example page links
- No force-graph in MVP

### F3 - NIW "run certificate" block (evidence generator)
At the top of the report:
- Run UUID
- Add-On version
- Docs processed, total pages
- Unique entities count
- Runtime

Include:
- "Copy summary" button
- Mailto: "Send summary to developer" link
- Feedback form link (vNext; disabled in this version)
- Support letter draft + feedback notes (client-side only, no external form)

### F3b - Documents in this run
- List the documents included (title, ID, page count, entity status)

### F4 - Trust and permissions UX (critical for adoption)
- Clearly state: only selected/query docs are processed
- Clearly state: no document text is uploaded externally unless the user opts in

### F5 - Filters and exports (client-side)
- Filter by entity kind
- Minimum doc coverage slider
- Stoplist for noisy names (client-side)
- Exports from the HTML report (entity index CSV, connections CSV/JSON)

### F6 - Optional writeback tags (opt-in)
- Store per-document top entity tags in DocumentCloud metadata (`data.entity_brief.tags`)
- Report lists suggested tags and writeback summary

### F7 - Support letter draft + feedback (client-side)
- Draft a usage letter with run stats and optional improvement requests
- Copy or email draft via mailto (no automatic sending)

### F8 - Optional opt-in anonymous telemetry (vNext)
Deferred for the current version. When enabled, POST only:
- run_uuid
- addon_version
- docs_count, pages_count, entities_count
- runtime_seconds
- Optional user-provided org type

Never send:
- Doc titles or IDs
- Entity strings
- Any document text

### F9 - Email summary to the user (vNext)
Deferred for the current version.

## Success metrics (NIW-friendly)
- Runs (opt-in telemetry OR GitHub Actions run count screenshots)
- # unique orgs/users who voluntarily emailed the run summary
- # testimonial/letters collected
- Case studies: processed X pages, saved Y hours
- Public directory listing is third-party validation

## Risks / considerations
1. Trust risk: any "phone home" behavior must be opt-in and documented
2. Rate limiting: entity endpoints across many docs can be API-heavy
3. Large queries: cap max_docs; handle inaccessible docs
4. One-file return constraint: keep output to a single HTML file
5. Directory review: keep code transparent, minimal permissions, clear disclosure
