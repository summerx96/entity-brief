# Testing - Entity Brief

## Goal
Validate the add-on on public documents without relying on uploads.

## Preconditions
- DocumentCloud account credentials or access token.
- Add-on installed in DocumentCloud.

## 1) Find public docs with existing entities
Use the helper script to discover public documents that already have entities extracted:

```bash
python3 scripts/find_public_docs_with_entities.py --limit 10
```

Optional flags:
- `--query "access:public"` (default)
- `--query "access:public entity:*"` (if you want to narrow the scan)
- `--max-checked 800`
- `--per-page 100`
- `--sleep 0.12`
- `--token <DOCUMENTCLOUD_ACCESS_TOKEN>`

Record the doc IDs and canonical URLs returned by the script.

If the script returns none, run DocumentCloud's entity extraction on a public
document in the UI (Edit -> Entities -> Extract entities), then re-run the
script or test directly against that doc ID.

## 2) Run the add-on in the DocumentCloud UI
- Search by `id:<doc_id>` or use a query run with multiple IDs.
- Select the documents and run **Entity Brief**.

## 3) Acceptance checks
- Docs with entities appear in the report index and charts.
- Docs with missing entities (404 or empty results) are listed under **Skipped (no entities)**.
- Unhandled API errors are listed under **Failures**.
- Exactly one HTML report is returned via `upload_file()`.
- Low entity coverage warning appears when too few docs have entities.
- Filters and exports work in the HTML report (kind, coverage slider, stoplist, CSV/JSON downloads).
- Connections include page-level examples when page data is available.
- Support letter draft + feedback tools render (no external feedback form link).
- Duplicate suggestions render when similar entities are detected.
- Writeback summary shows status; if enabled, `data.entity_brief.tags` is updated per doc.
- Documents list shows IDs, page counts, and entity status for the run.

## 4) Log the run
Update this table after each verification run.

| Date | Doc IDs | Result | Notes |
| --- | --- | --- | --- |
| 2026-01-13 | none (finder) | fail | no public docs with entities found via API queries |
| 2026-01-13 | 26469884 | partial | public doc had no entities; verified Skipped section |
| 2026-01-13 | 26301227, 25943454, 25943453, 25943452 | pass | entities present; demo report regenerated (unique_entities: 62) |
| 2026-01-14 | 26301227, 25943454, 25943453, 25943452 | pass | regenerated demo report locally and verified charts in browser |
| 2026-01-14 | 26301227, 25943454, 25943453, 25943452 | pass | demo report regenerated with static image fallbacks |
| 2026-01-14 | 26301227, 25943454, 25943453, 25943452 | pass | regenerated demo with filters/exports and page-level connections |
| 2026-01-15 | 26301227, 25943454, 25943453, 25943452 | pass | regenerated demo with support letter UI, duplicates, and writeback tags |
