#!/usr/bin/env python3
"""Generate store/pg/0003_rls.sql (row-level security) from store/tenancy.py, via store/rls.py.

    python scripts/gen_pg_rls.py            print the SQL
    python scripts/gen_pg_rls.py --write    write store/pg/0003_rls.sql
    python scripts/gen_pg_rls.py --check    exit 1 when the committed file is not what tenancy.py produces

The file is what a deployment applies once (store/pgmigrate.py). Regenerate it only while 0003 is
unreleased; after that a change in classification is a NEW numbered migration written by hand
(DROP POLICY / CREATE POLICY for the tables that moved), and this script's output is the reference
for what the live policies must add up to.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from salescoach.store import rls  # noqa: E402


def main(argv) -> int:
    text = rls.generate()
    if "--write" in argv:
        rls.OUT.write_text(text)
        print(f"wrote {rls.OUT.relative_to(ROOT)}")
        return 0
    if "--check" in argv:
        if rls.OUT.read_text() != text:
            print(f"{rls.OUT.relative_to(ROOT)} is stale: run scripts/gen_pg_rls.py --write", file=sys.stderr)
            return 1
        print(f"{rls.OUT.relative_to(ROOT)} is current")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
