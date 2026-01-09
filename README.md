# DocumentCloud Add-On - Entity Brief

Entity Brief builds a cross-document index of extracted entities (people, orgs, locations, dates)
and produces a single HTML report with:

- D3 bar chart: top entities by document coverage
- Co-occurrence list: top entity pairs appearing in the same documents
- Expandable entity index with per-document counts + page refs/snippets (when available)

## Privacy / trust
- By default this add-on does NOT transmit document text or entity strings to any external service.
- This version does not send usage metrics or automated emails.
- Add-Ons may have broad permissions in DocumentCloud today; this add-on is designed to process only the docs passed into the run.

## Output
- One HTML report is uploaded back to the Add-On run.
- Note: upload_file is limited to one file per run; we ship a single HTML file for MVP.

## Configuration (optional)
- `ENTITY_BRIEF_DEV_EMAIL`: used for the mailto link in the report (default: `summerxie966@gmail.com`).
- `ENTITY_BRIEF_FEEDBACK_URL`: leave unset for now (feedback form disabled in this version).

## GitHub Actions
- Workflow: `.github/workflows/addon.yml`
- Triggered by `repository_dispatch` (`documentcloud-addon-run`) or manual `workflow_dispatch`.
- Timeout is set to 15 minutes; adjust `timeout-minutes` if needed.

## Local testing (example)
python main.py --username "$DC_USERNAME" --password "$DC_PASSWORD" --documents 123 456 --data '{"max_docs": 25, "min_relevance": 0.15}'
