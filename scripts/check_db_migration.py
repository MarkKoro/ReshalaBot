"""Read-only source -> temporary SQLite backup -> migration/data preservation check."""
import argparse
import asyncio
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def snapshot(con):
    result = {}
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for (table,) in tables:
        columns = [row[1] for row in con.execute(f"PRAGMA table_info({quoted(table)})")]
        # Compare a multiset, independent of row order and without displaying user data.
        rows = con.execute(f"SELECT * FROM {quoted(table)}").fetchall()
        result[table] = (columns, sorted(map(repr, rows)))
    return result


async def check(source):
    with tempfile.TemporaryDirectory(prefix="creative_brain_migration_") as temp:
        target = Path(temp) / "copy.db"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(target)) as copy:
                origin.backup(copy)
                before = snapshot(copy)
        db.DB_PATH = target
        try:
            await db.init_db()
            await db.close_db()
            await db.init_db()
        finally:
            await db.close_db()
        with closing(sqlite3.connect(target)) as copy:
            for table, (columns, rows) in before.items():
                selected = ",".join(map(quoted, columns))
                after = copy.execute(f"SELECT {selected} FROM {quoted(table)}").fetchall()
                if sorted(map(repr, after)) != rows:
                    raise RuntimeError(f"Existing rows changed in {table}")
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
        print(f"OK: migration twice; {len(before)} existing tables preserved; integrity_check=ok; source unchanged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Existing SQLite database (read-only source)")
    args = parser.parse_args()
    source = args.database.resolve(strict=True)
    if not source.is_file():
        parser.error("database must be a file")
    asyncio.run(check(source))
