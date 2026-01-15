# Implementation Checklist - Commit-Driven Plan (Entity Brief)

This document is a commit-by-commit instruction set. Follow the commits in order. Each commit should be ~300 lines of code or less; if you exceed that, stop and split the remaining tasks into the next commit.

## Global rules (read before starting)
- Do not create or replace `config.yaml`. It already exists. Update it only when adding new user-facing options.
- Only one output file per run (HTML).
- Telemetry and feedback form are disabled in this version.
- Document tag writeback is optional and must be opt-in.
- Keep changes limited to the files listed in each commit.
- Always update docs when code changes (same commit).
- Run the verification command listed for each commit before committing.
- Use the exact git commands listed under each commit.

## Commit 1 - Scaffold add-on entrypoint
Goal: establish a minimal Add-On entrypoint and documentation.

Files to touch:
- `main.py`
- `requirements.txt`
- `README.md`

Inputs:
- `docs/prd.md`
- Existing `config.yaml` (read-only)

Outputs:
- A minimal AddOn class with a stub `main()` method.
- Requirements and README populated.

Implementation steps (do exactly in this order):
1. Create `main.py` if missing. Add imports: `os`, `time`, `uuid`, `json`, `html`, `re`, `itertools`, `requests`, `collections`, `typing`, `documentcloud.addon.AddOn`.
2. Define constants near top: `ADDON_VERSION`, `API_BASE`, `D3_CDN`, `METRICS_ENDPOINT`, `FEEDBACK_URL` (blank by default), `DEVELOPER_EMAIL` (default to `summerxie966@gmail.com`).
3. Add `class EntityBrief(AddOn):` with a `main(self)` that sets a message and exits.
4. Add the `if __name__ == "__main__": EntityBrief().main()` block.
5. Ensure `requirements.txt` contains `python-documentcloud>=4.5.0` and `requests>=2.31.0`.
6. Update `README.md` with: short description, privacy note (no external text), and example local run command.

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py requirements.txt README.md`
- `git commit -m "chore: scaffold addon entrypoint"`

Acceptance checklist:
- `main.py` compiles.
- `requirements.txt` has the two dependencies.
- README mentions privacy and a local test command.

## Commit 2 - Parse config and collect documents
Goal: read Add-On options and collect document metadata.

Files to touch:
- `main.py`

Inputs:
- `self.data` (Add-On properties)
- `self.get_documents()`

Outputs:
- `docs` list (capped by `max_docs`)
- `doc_meta` dict with `id`, `title`, `canonical_url`, `page_count`
- `total_pages` integer

Implementation steps:
1. Add helper functions `_safe_int` and `_safe_float` for option parsing.
2. In `main()`, read options from `self.data`: `max_docs`, `min_relevance`, `top_n_entities`, `include_connections`.
3. Record `start_ts = time.time()` and `run_uuid` (use `self.id` if present, else `uuid.uuid4()`).
4. Collect documents via `docs = list(self.get_documents())`. If `max_docs` is set and len(docs) > max_docs, slice the list.
5. For each doc, store metadata into `doc_meta` and sum `total_pages`.
6. Use `self.set_message` and `self.set_progress` to indicate collection status.

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py`
- `git commit -m "feat: parse options and collect docs"`

Acceptance checklist:
- Options are read without errors when `self.data` is empty.
- No crash when there are zero documents.
- `doc_meta` contains expected keys.

## Commit 3 - Access token + entity retrieval
Goal: fetch entities for each document with pagination and error handling.

Files to touch:
- `main.py`

Inputs:
- Document IDs from Commit 2
- `min_relevance` option

Outputs:
- `doc_entities` dict `{doc_id: [entities...]}`
- `failures` list with doc_id + error

Implementation steps:
1. Add `_get_access_token(addon)` to locate token from common places or env vars.
2. Add `_api_get_json(url, token, params, timeout)` and `_api_get_all_pages(url, token, params)` helpers.
3. In `main()`, call `_get_access_token(self)` once.
4. For each document:
   - Build URL: `${API_BASE}documents/<id>/entities/`
   - Params: `expand=occurrences`, `relevance__gt=min_relevance`
   - Call `_api_get_all_pages`
   - Store results in `doc_entities[doc_id]`
5. Wrap per-doc fetch in try/except; append failures to `failures`.
6. Update `self.set_progress` during the loop.

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py`
- `git commit -m "feat: fetch entities with pagination"`

Acceptance checklist:
- Pagination works (`results` + `next`).
- A failure in one doc does not stop the run.
- `failures` list is populated with error messages.

## Commit 4 - Normalization + aggregation
Goal: normalize entities and build a cross-doc index.

Files to touch:
- `main.py`

Inputs:
- `doc_entities` from Commit 3
- `doc_meta` from Commit 2

Outputs:
- `cluster_list` (normalized entities with totals and per-doc breakdown)
- `doc_entity_keys` per doc for later co-occurrence

Implementation steps:
1. Add `_normalize_name`, `_entity_key`, `_entity_display`, `_escape` helpers.
2. Create `clusters` dict keyed by canonical entity key.
3. For each entity in each doc:
   - Skip if display name is empty.
   - Increment total mentions and doc_count.
   - Track per-doc counts, page numbers, and sample snippets (cap lists).
4. Build `cluster_list` from `clusters`, sort by `doc_count` and `total_mentions`.
5. Build `doc_entity_keys[doc_id]` list for each doc.

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py`
- `git commit -m "feat: normalize and aggregate entities"`

Acceptance checklist:
- Same entity across docs collapses to one cluster.
- Per-doc entries include doc title and URL.

## Commit 5 - Co-occurrence + report data model
Goal: compute co-occurrence pairs and assemble report data.

Files to touch:
- `main.py`

Inputs:
- `doc_entity_keys` from Commit 4
- `cluster_list` from Commit 4
- run stats from Commit 2

Outputs:
- `edges` list (top pairs by doc_count)
- `report_data` dict

Implementation steps:
1. If `include_connections` is true, compute co-occurrence pairs:
   - Unique keys per doc, cap to 25 entities per doc.
   - Count pairs with `itertools.combinations`.
2. Convert top pairs to `{a, b, doc_count}` with display names.
3. Assemble `report_data`:
   - `run` (uuid, version, docs/pages/entities counts, runtime)
   - `meta` (feedback URL, developer email)
   - `top_entities`, `entities`, `edges`, `failures`

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py`
- `git commit -m "feat: co-occurrence and report data"`

Acceptance checklist:
- `edges` is empty when there are not enough entities.
- `report_data` serializes via `json.dumps`.

## Commit 6 - HTML report skeleton + run certificate
Goal: render the report skeleton and run certificate (no charts yet).

Files to touch:
- `main.py`

Inputs:
- `report_data` from Commit 5

Outputs:
- `_render_html(report_data)` that returns full HTML string with embedded JSON.

Implementation steps:
1. Add `_render_html(self, data)` method.
2. Include Run Certificate block with:
   - run UUID, version, docs/pages/entities counts, runtime
   - copy button + mailto link
   - feedback link only when a URL is configured (disabled by default)
3. Add sections for: Top Entities chart placeholder, Connections placeholder, Entity Index placeholder, Failures placeholder.
4. Embed `report_data` JSON in a `<script type="application/json">` block.
5. Add minimal JS for copy button and mailto link.

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py`
- `git commit -m "feat: render report skeleton"`

Acceptance checklist:
- Report HTML contains Run Certificate fields.
- JSON is embedded and parseable in the page.

## Commit 7 - D3 visuals + entity index
Goal: add the bar chart, connections table, and entity index rendering.

Files to touch:
- `main.py`

Inputs:
- `top_entities`, `edges`, `entities` from `report_data`

Outputs:
- Interactive chart + connections list + entity index inside HTML.

Implementation steps:
1. Add D3 bar chart (Top 15 by doc coverage).
2. Add connections list/table (Top 20 pairs).
3. Render entity index with expandable sections:
   - Doc link, mention count, page refs, snippets.
4. Add failures section rendering.

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py`
- `git commit -m "feat: add visuals and entity index"`

Acceptance checklist:
- Chart renders when there is data.
- Empty states show a readable message.

## Commit 8 - Upload report + finalize docs
Goal: finish run flow with upload and update docs to match code.

Files to touch:
- `main.py`
- `README.md`

Inputs:
- `report_data`

Outputs:
- HTML report uploaded to the run.
- Docs updated to reflect disabled email, telemetry, and feedback form.

Implementation steps:
1. Write HTML to `entity-brief-<uuid>.html`.
2. Upload file with `self.upload_file`.
3. Do not implement `send_mail` in this version.
4. Do not POST telemetry in this version (leave `METRICS_ENDPOINT` unused).
5. Update README with env vars:
   - `ENTITY_BRIEF_DEV_EMAIL`
   - `ENTITY_BRIEF_FEEDBACK_URL` (disabled by default)

Verification:
- Run `python3 -m py_compile main.py`.

Git operations:
- `git status`
- `git diff`
- `git add main.py README.md docs/prd.md`
- `git commit -m "feat: upload report and sync docs"`

Acceptance checklist:
- Report uploads successfully.
- No email or telemetry code paths are present.
- Docs match current behavior.

## Optional final QA note (no code changes)
If asked, add a short "QA scenarios" section to `README.md` with the test cases in the PRD. Do not add this unless requested.
