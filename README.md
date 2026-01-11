# Entity Brief (DocumentCloud Add-On)

**Entity Brief** turns per-document extracted entities into a **cross-document entity brief** for investigative and FOIA workflows.

It helps answer, quickly:

- **Who** shows up across many documents?
- Which **organizations / agencies / vendors** recur?
- Where are mentions located (doc links, page refs, evidence snippets)?
- What are the most common **connections** (entities that co-occur in the same docs)?

> Entity Brief produces a **single, editor-friendly HTML report** with a D3 bar chart + connection list.  
> It's not a "data dump" tool and does not require GPUs.

---

## What this Add-On does

Given a set of DocumentCloud documents (selected docs or a query):

1. **Reads existing entity extraction results** from DocumentCloud's Entities API for each document
2. **Aggregates + lightly normalizes** entities across documents
3. Produces **one downloadable HTML report** containing:
   - A run certificate (counts + runtime)
   - **Top Entities bar chart** (D3)
   - **Top Connections list** (co-occurrence pairs)
   - An expandable **Entity Index** with doc links, counts, and limited evidence snippets

---

## Important: entity extraction must already exist

Entity Brief does **not** run OCR or NER itself.

You must extract entities first using either:
- The DocumentCloud UI: **Edit → Entities → Extract entities**
- Or another Add-On (e.g., Google Cloud Entity Extractor)

If entities are missing for a document, Entity Brief will skip it and report "missing entities."

---

## Output

The Add-On uploads a single file to the Add-On run results:

```
entity-brief-<run_uuid>.html
```

This report is designed to be:
- Viewable in a browser
- Shareable with collaborators
- Printable / archivable

> Why HTML instead of CSVs?  
> Journalists and editors can use it immediately without spreadsheet cleanup.

---

## Configuration

When running the Add-On, you can configure:

| Option | Default | Description |
|--------|---------|-------------|
| **Max documents to process** | 25 | Safety cap for query runs |
| **Minimum entity relevance** | 0.15 | Higher = fewer entities, faster report (0.0–1.0) |
| **Top entities to show** | 15 | Controls bar chart + connection list size |
| **Include connections** | true | Shows entity pairs that appear together across documents |

---

## Data handling & privacy

Entity Brief is built to be safe for newsroom workflows.

### What it reads
- Document metadata needed for linking/reporting (id, title, canonical URL, page count)
- Entity extraction results stored in DocumentCloud (entity names, types/kinds, relevance, occurrence context)

### What it writes
- **Uploads an HTML report** as the Add-On run output (one file per run)

### What it does NOT do
- ❌ Does not upload documents anywhere
- ❌ Does not send document text to external services
- ❌ Does not modify document access settings
- ❌ Does not add annotations/notes/tags
- ❌ Does not send usage metrics (telemetry is disabled)
- ❌ Does not send automated emails

### External network calls
- **During Add-On execution:** None (only DocumentCloud API calls)
- **When viewing the HTML report:** The report loads **D3.js from a CDN** (`https://d3js.org/d3.v7.min.js`)
  - If your environment blocks external scripts, the report still includes fallback tables/lists

---

## Accuracy & limitations

- Entity extraction quality depends on the upstream extractor and the document's text/OCR quality
- Entity Brief is **conservative** about merging entities:
  - It prefers stable IDs (when present) and otherwise uses light normalization
  - It may under-merge (e.g., "U.S." vs "United States") rather than over-merge
- Co-occurrence "connections" are **heuristics** (shared document presence), not proof of a relationship

---

## Quick start (local development)

1. Create a virtualenv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run locally on selected docs:

```bash
python main.py \
  --username "YOUR_MUCKROCK_EMAIL" \
  --password "YOUR_MUCKROCK_PASSWORD" \
  --documents 123456 789012 \
  --data '{"min_relevance": 0.15, "top_n_entities": 15}'
```

3. Or run locally on a query:

```bash
python main.py \
  --username "YOUR_MUCKROCK_EMAIL" \
  --password "YOUR_MUCKROCK_PASSWORD" \
  --query 'access:public project:YOUR_PROJECT_ID' \
  --data '{"max_docs": 25, "include_connections": true}'
```

---

## Deploying to DocumentCloud

1. Push this repo to GitHub
2. Install the **GitHub DocumentCloud App** and grant it access to this repo
3. Ensure you have:
   - `config.yaml` describing UI fields
   - `main.py` using the DocumentCloud Add-On framework
   - The GitHub Actions workflow (`.github/workflows/addon.yml`)
4. In DocumentCloud: **Add-Ons → Browse → Your Add-Ons → run Entity Brief**

---

## Environment variables (optional)

| Variable | Description | Default |
|----------|-------------|---------|
| `ENTITY_BRIEF_DEV_EMAIL` | Mailto link in report | (set in code) |
| `ENTITY_BRIEF_FEEDBACK_URL` | Feedback form URL | (disabled) |
| `ENTITY_BRIEF_METRICS_ENDPOINT` | Reserved for future use | (disabled) |

---

## Contributing

Issues and PRs welcome. Please include:
- A short description of the workflow and expected behavior
- A small test case (doc ID if public, or a description if private)
- Screenshots of the output section (if relevant)

---

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
