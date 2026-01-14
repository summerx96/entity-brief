# Privacy - Entity Brief

## Data read
- DocumentCloud document metadata for selected/query docs (id, title, canonical URL, page count).
- Extracted entity data from the DocumentCloud entities endpoint.

## Data written
- One HTML report generated per run and returned via `upload_file()`.
- No tags or document metadata are written back in v1.

## External calls
- DocumentCloud API for document metadata and entities.
- D3 library loaded from the official CDN: `https://d3js.org/d3.v7.min.js`.

## Telemetry and feedback
- Telemetry is disabled in v1 (no metrics POST).
- Feedback form is disabled in v1 (no link shown in the report).
- No automatic email sending; the report only offers an optional mailto link to the developer.

## Data retention
- The only output is the single HTML report produced by the add-on run.
