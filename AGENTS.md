# AGENTS.md

## Purpose
This repo builds a DocumentCloud Add-On named "Entity Brief". Follow `docs/prd.md` as the product source of truth and keep docs aligned with code.

## Current decisions (v1)
- Keep the HTML report self-contained (no third-party JS/CSS loaded when viewed).
- Feedback form is disabled (no link shown in the report).
- Telemetry is disabled (no metrics POST).
- No automatic "email me when done" functionality.

## Non-negotiables
- Do not create or replace `config.yaml`.
- One output file per run (HTML only).
- No hidden telemetry or data exfiltration.
- Update docs whenever code behavior changes.

## Required workflow
- Before each commit: `git status`, `git diff`.
- Verify: `python3 -m py_compile main.py`.
- Commit message format: `feat: ...` or `chore: ...`.

## Environment variables
- `ENTITY_BRIEF_DEV_EMAIL` (mailto link in report): `summerxie966@gmail.com`
- `ENTITY_BRIEF_FEEDBACK_URL` (vNext): keep unset for v1. Planned value: https://docs.google.com/forms/d/e/1FAIpQLSclnbbJ730ojIIJt9Gl3xlGROxteElagUsIMrWFXi7cligvaw/viewform?usp=dialog
- `ENTITY_BRIEF_METRICS_ENDPOINT` (reserved for vNext; do not use yet)

## Stop conditions
- If a change would add telemetry, email sending, or additional outputs, stop and ask.
- If a doc/code mismatch is detected, update docs in the same commit.
