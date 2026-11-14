"""Re-index repositories so vector payloads match the current chunk format.

Why this exists
---------------
Symbol chunks used to store a label ("function foo in app/x.py") as their
payload ``content``. The Search Agent hands that field straight to the Test
Generator prompt, which meant the model saw no source code at all. Indexing now
stores real source slices — but Qdrant points are not migrated in place, so
existing repositories must be re-indexed once.

Usage (from ``backend/``)::

    python scripts/reindex_vectors.py                     # all indexed repos
    python scripts/reindex_vectors.py --repository-id <id>
    python scripts/reindex_vectors.py --sync              # run inline, no broker
    GITHUB_TOKEN=ghp_... python scripts/reindex_vectors.py # private clones

By default the work is dispatched to the Celery ``indexing`` queue. ``--sync``
runs the indexing task in this process instead (useful on a box without Redis,
at the cost of blocking until every repository is done).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow `python scripts/reindex_vectors.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks.indexing import reindex_all_repositories  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-id",
        action="append",
        dest="repository_ids",
        help="Re-index only this repository id (repeatable). Defaults to every indexed repo.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="VCS access token for private clones (defaults to $GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Run indexing inline instead of enqueuing Celery tasks.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not force a full re-clone/re-parse (force_reindex=False).",
    )
    args = parser.parse_args()

    result = reindex_all_repositories(
        repository_ids=args.repository_ids,
        access_token=args.token,
        force=not args.keep_existing,
        dispatch=not args.sync,
    )

    print(json.dumps(result, indent=2))
    if result["dispatched"] == 0:
        print("No repositories matched — nothing was re-indexed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
