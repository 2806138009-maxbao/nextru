"""Create/rebuild the isolated SQLite prototype database and export fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from nexus.db import Store, connect
from nexus.fixtures import reset_and_build

ROOT = Path(__file__).resolve().parents[1]
TABLES = [
    "scopes", "objects", "source_refs", "source_heads", "source_sections",
    "aspects", "authority_bindings", "relations", "observations", "conflicts",
    "events", "known_facts", "projections",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "var" / "nexus.db"))
    args = parser.parse_args()

    stats = reset_and_build(args.db)
    generated = ROOT / "fixtures" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "t7_long_doc.txt").write_text(stats["full_doc_text"], encoding="utf-8")

    conn = connect(args.db)
    store = Store(conn)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    meta = {
        "db_path": str(Path(args.db).resolve()),
        "database": "sqlite3",
        "sqlite_version": conn.execute("SELECT sqlite_version()").fetchone()[0],
        "tables": counts,
        "t7_full_doc_chars": stats["full_doc_chars"],
        "t7_section_chars": stats["section_chars"],
    }
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "db-build.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    conn.close()


if __name__ == "__main__":
    main()
