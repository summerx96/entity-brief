# Privacy - Entity Brief

## Data read
- DocumentCloud document metadata for selected/query docs (id, title, canonical URL, page count).
- Extracted entity data from the DocumentCloud entities endpoint.

## Data written
- One HTML report generated per run and returned via `upload_file()`.
- Optional metadata writeback (opt-in): top entity tags stored in `data.entity_brief.tags`.

## External calls
- DocumentCloud API for document metadata and entities.

## Report viewing
- The downloaded HTML report is self-contained and does not load third-party JavaScript or CSS when viewed.

## Telemetry and feedback
- Telemetry is disabled in v1 (no metrics POST).
- Feedback form is disabled in v1 (no link shown in the report).
- No automatic email sending; the report only offers an optional mailto link to the developer.

## Data retention
- The only output is the single HTML report produced by the add-on run.
