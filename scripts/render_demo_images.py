#!/usr/bin/env python3
"""Generate demo chart SVG and text summary from the demo HTML report."""
import html
import json
import re
from pathlib import Path
from typing import List


def _ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii")


def load_report_data(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"<script id=\"data\" type=\"application/json\">(.*?)</script>", content, re.DOTALL)
    if not match:
        raise ValueError("Could not find report data script tag")
    raw = html.unescape(match.group(1))
    return json.loads(raw)


def build_svg(top_entities: List[dict], out_path: Path) -> None:
    width = 1200
    height = 600
    margin = 80
    inner_width = width - margin * 2
    inner_height = height - margin * 2
    if not top_entities:
        out_path.write_text("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1200\" height=\"600\"></svg>")
        return

    max_val = max(ent.get("doc_count", 0) for ent in top_entities) or 1
    bar_gap = 10
    bar_width = int((inner_width - bar_gap * (len(top_entities) - 1)) / len(top_entities))

    svg_parts = [
        f"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{width}\" height=\"{height}\">",
        "<style>text{font-family:Arial, sans-serif; font-size:14px;}</style>",
        f"<rect width=\"100%\" height=\"100%\" fill=\"#ffffff\" />",
        f"<line x1=\"{margin}\" y1=\"{height - margin}\" x2=\"{width - margin}\" y2=\"{height - margin}\" stroke=\"#333\" />",
        f"<line x1=\"{margin}\" y1=\"{margin}\" x2=\"{margin}\" y2=\"{height - margin}\" stroke=\"#333\" />",
        f"<text x=\"{margin}\" y=\"30\" font-size=\"18\" font-weight=\"bold\">Top Entities (Doc Coverage)</text>",
    ]

    for i, ent in enumerate(top_entities):
        value = ent.get("doc_count", 0)
        bar_height = int((value / max_val) * inner_height)
        x = margin + i * (bar_width + bar_gap)
        y = height - margin - bar_height
        svg_parts.append(f"<rect x=\"{x}\" y=\"{y}\" width=\"{bar_width}\" height=\"{bar_height}\" fill=\"#5b8def\" />")
        label = _ascii(str(ent.get("name", "")))
        if len(label) > 18:
            label = label[:18] + "..."
        svg_parts.append(f"<text x=\"{x + bar_width / 2}\" y=\"{height - margin + 18}\" text-anchor=\"middle\">{html.escape(label)}</text>")
        svg_parts.append(f"<text x=\"{x + bar_width / 2}\" y=\"{y - 8}\" text-anchor=\"middle\">{value}</text>")

    svg_parts.append("</svg>")
    out_path.write_text("\n".join(svg_parts), encoding="utf-8")


def build_index_text(entities: List[dict], out_path: Path, limit: int = 12) -> None:
    lines = ["Entity Index (sample)", ""]
    for ent in entities[:limit]:
        name = _ascii(str(ent.get("name", "")))
        doc_count = ent.get("doc_count", 0)
        mentions = ent.get("total_mentions", 0)
        lines.append(f"- {name} (docs: {doc_count}, mentions: {mentions})")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    report_path = Path("docs/demo/entity-brief-demo.html")
    data = load_report_data(report_path)
    top_entities = data.get("top_entities", [])[:10]
    entities = data.get("entities", [])

    svg_path = Path("docs/demo/top-entities.svg")
    text_path = Path("docs/demo/entity-index.txt")

    build_svg(top_entities, svg_path)
    build_index_text(entities, text_path)

    print(f"Wrote {svg_path}")
    print(f"Wrote {text_path}")


if __name__ == "__main__":
    main()
