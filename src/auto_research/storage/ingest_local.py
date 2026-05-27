"""Developer utility for ingesting local PDF papers."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from auto_research.storage.database import Storage, database_path, ingest_local_papers


async def ingest_and_log(papers_dir: Path) -> dict:
    """Ingest local PDFs and write a step-4 development log."""

    load_dotenv()
    db_path = database_path()
    db = Storage(db_path)
    await db.initialize()
    papers = await ingest_local_papers(db, papers_dir)

    log_dir = Path("logs") / "dev" / "step-4"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_local_paper_ingest.json"
    payload = {
        "db_path": str(db_path),
        "papers_dir": str(papers_dir),
        "n_papers": len(papers),
        "papers": [paper.model_dump(mode="json") for paper in papers],
    }
    log_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "db_path": str(db_path),
        "log_path": str(log_path),
        "n_papers": len(papers),
        "titles": [paper.title for paper in papers],
    }


def main() -> None:
    """CLI entry point for local development ingestion."""

    parser = argparse.ArgumentParser()
    parser.add_argument("papers_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(ingest_and_log(args.papers_dir)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
