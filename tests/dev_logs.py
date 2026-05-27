"""Helpers for writing local development test logs."""

from datetime import UTC, datetime
import json
from pathlib import Path


def dev_log_path(step: str, stem: str, suffix: str) -> Path:
    """Return a timestamped path under logs/dev/<step>/."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path("logs") / "dev" / step / f"{timestamp}_{stem}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def summarize_source_log(jsonl_path: Path) -> Path:
    """Write a Markdown summary beside a step-5 source JSONL log."""

    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    md_path = jsonl_path.with_suffix(".md")
    lines = [f"# {jsonl_path.stem}", ""]
    for row in rows:
        lines.extend(
            [
                f"## {row['source']}",
                "",
                f"- Query: `{row['query']}`",
                f"- Results: {row['n_results']}",
                f"- Ingested PDFs: {len(row.get('ingested', []))}",
                "",
            ]
        )
        for index, result in enumerate(row.get("results", []), start=1):
            authors = ", ".join(result.get("authors") or [])
            identifiers = ", ".join(
                f"{key}: {value}" for key, value in (result.get("identifiers") or {}).items()
            )
            abstract = (result.get("abstract") or "").replace("\n", " ").strip()
            if len(abstract) > 500:
                abstract = f"{abstract[:497]}..."
            lines.extend(
                [
                    f"### {index}. {result.get('title')}",
                    "",
                    f"- URL: {result.get('url')}",
                    f"- PDF URL: {result.get('pdf_url')}",
                    f"- Published: {result.get('published')}",
                    f"- Authors: {authors or '(none)'}",
                    f"- Identifiers: {identifiers or '(none)'}",
                    f"- Keywords: {', '.join(result.get('keywords') or []) or '(none)'}",
                    f"- Abstract: {abstract or '(none)'}",
                    "",
                ]
            )
        if row.get("ingested"):
            lines.append("### Ingested")
            lines.append("")
            for paper in row["ingested"]:
                lines.append(f"- {paper['title']} -> `{paper['local_path']}`")
            lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def summarize_retrieval_log(jsonl_path: Path, *, query: str) -> Path:
    """Write a Markdown summary beside a step-6 retrieval JSONL log."""

    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    md_path = jsonl_path.with_suffix(".md")
    lines = [f"# {jsonl_path.stem}", "", f"- Query: `{query}`", f"- Hits: {len(rows)}", ""]
    for index, row in enumerate(rows, start=1):
        lines.extend(
            [
                f"## {index}. {row['title']}",
                "",
                f"- Kind: {row['kind']}",
                f"- Score: {row['score']}",
                f"- Source: {row.get('source')}",
                f"- Source ID: {row.get('source_id')}",
                f"- Local path: `{row.get('local_path')}`",
                f"- Snippet: {row.get('snippet') or '(none)'}",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def summarize_agent_log(jsonl_path: Path) -> Path:
    """Write a Markdown summary beside a step-8 live-agent JSONL log."""

    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    md_path = jsonl_path.with_suffix(".md")
    lines = [f"# {jsonl_path.stem}", "", f"- Calls: {len(rows)}", ""]
    for row in rows:
        lines.extend(
            [
                f"## {row['role']}",
                "",
                f"- Model member: `{row['model_member']}`",
                f"- Provider: `{row['provider']}`",
                f"- Model: `{row['model']}`",
                f"- Input tokens: {row['input_tokens']}",
                f"- Output tokens: {row['output_tokens']}",
                f"- Status: {row['status']}",
                "",
            ]
        )
        if row.get("error"):
            lines.extend(["### Error", "", f"`{row['error']}`", ""])
        else:
            lines.extend(["### Parsed Output", "", "```json", json.dumps(row["output"], indent=2), "```", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def summarize_graph_log(jsonl_path: Path) -> Path:
    """Write a Markdown summary beside a step-10 graph JSONL log."""

    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    md_path = jsonl_path.with_suffix(".md")
    lines = [f"# {jsonl_path.stem}", "", f"- Events: {len(rows)}", ""]
    for row in rows:
        lines.extend(
            [
                f"## {row['event']}",
                "",
                f"- Role: `{row.get('role')}`",
                f"- Member: `{row.get('model_member')}`",
                f"- Status: {row.get('status')}",
                f"- Input tokens: {row.get('input_tokens', 0)}",
                f"- Output tokens: {row.get('output_tokens', 0)}",
                "",
            ]
        )
        output = row.get("output")
        if output is not None:
            lines.extend(["```json", json.dumps(output, indent=2), "```", ""])
        if row.get("error"):
            lines.extend(["### Error", "", f"`{row['error']}`", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def summarize_consensus_log(jsonl_path: Path) -> Path:
    """Write a Markdown summary beside a step-9 consensus JSONL log."""

    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    md_path = jsonl_path.with_suffix(".md")
    lines = [f"# {jsonl_path.stem}", "", f"- Events: {len(rows)}", ""]
    for row in rows:
        lines.extend(
            [
                f"## {row['event']}",
                "",
                f"- Role: `{row.get('role')}`",
                f"- Member: `{row.get('model_member')}`",
                f"- Status: {row.get('status')}",
                f"- Input tokens: {row.get('input_tokens', 0)}",
                f"- Output tokens: {row.get('output_tokens', 0)}",
                "",
            ]
        )
        output = row.get("output")
        if output is not None:
            lines.extend(["```json", json.dumps(output, indent=2), "```", ""])
        if row.get("error"):
            lines.extend(["### Error", "", f"`{row['error']}`", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path
