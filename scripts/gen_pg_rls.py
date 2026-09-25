#!/usr/bin/env python3
"""Generate store/pg/rls.sql (row-level security) from store/tenancy.py, via store/rls.py.

    python scripts/gen_pg_rls.py            print the SQL
    python scripts/gen_pg_rls.py --write    write store/pg/rls.sql
    python scripts/gen_pg_rls.py --check    exit 1 when the committed file is not what tenancy.py produces

rls.sql is a REPEATABLE step (store/pgmigrate.py): `salescoach migrate` re-applies it whenever its
checksum changes, and it drops and re-creates the whole policy set, so a change in classification (or a
table added by a new numbered migration) is: edit tenancy.py / store/rls.py, run this with --write,
commit, deploy, migrate. The app refuses to start on a database whose applied rls.sql is not this build's.
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
