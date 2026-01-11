# Entity Brief

A DocumentCloud Add-On that aggregates extracted entities across documents into a single HTML report with visualizations and connection analysis.

## Requirements

Documents must have entities extracted before running this Add-On. Use DocumentCloud's built-in entity extraction (**Edit → Entities → Extract entities**) or another extraction Add-On first.

## Features

- **Top Entities Chart** — D3 bar chart showing entities by document coverage
- **Connection Analysis** — Co-occurrence pairs (entities appearing together)
- **Entity Index** — Expandable list with doc links, page refs, and evidence snippets

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| Max documents | 25 | Safety cap for query runs |
| Min relevance | 0.15 | Filter threshold (0.0–1.0) |
| Top N entities | 15 | Chart/list size limit |
| Include connections | true | Enable co-occurrence analysis |

## Output

Single HTML file: `entity-brief-<run_uuid>.html`

## Privacy

- Reads only document metadata and existing entity extractions
- No external API calls during execution
- No telemetry or data collection
- Report loads D3.js from CDN when viewed

## Local Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python main.py \
  --username "$DC_USERNAME" \
  --password "$DC_PASSWORD" \
  --documents 123456 789012 \
  --data '{"min_relevance": 0.15, "top_n_entities": 15}'
```

## License

BSD 3-Clause
