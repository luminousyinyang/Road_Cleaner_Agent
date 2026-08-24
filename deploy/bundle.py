"""Assemble the small slice of `data/` that the deployed dashboard needs.

`data/` is 1.3 GB — 28,041 evidence frames from every camera poll of the
simulated week, plus 77 MB of generated clips. The demo only ever renders the
frames that cases actually reference, which is 26 files and under a megabyte.

So rather than ship the working directory or rebuild the whole simulated week
inside the container, this copies exactly what the dashboard reads:

* `road_cleaner.db`  — the cases, detections, trail and filings
* `frames/`          — only blob keys referenced by a `Case.frame_refs`
* `media/`           — the generated clips, so the scenario library is populated

Run it before deploying; `deploy.sh` does that for you.

    python deploy/bundle.py

The result lands in `deploy/_bundle/`, which `.gcloudignore` explicitly allows
through and the Dockerfile copies to `/app/data`.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

BUNDLE = REPO / "deploy" / "_bundle"


async def referenced_frame_keys() -> set[str]:
    """Every blob key a case points at. Nothing else needs to ship."""
    from road_cleaner.config import get_settings
    from road_cleaner.container import build_container

    container = build_container(get_settings())
    await container.startup()
    try:
        keys: set[str] = set()
        for case in await container.repository.list_cases(limit=10_000):
            keys.update(ref.blob_key for ref in case.frame_refs if ref.blob_key)
        return keys
    finally:
        await container.shutdown()


def _copy(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.stat().st_size


async def main() -> None:
    from road_cleaner.config import get_settings

    settings = get_settings()
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True)

    db = Path(settings.sqlite_path)
    size = _copy(db, BUNDLE / db.name)
    if not size:
        raise SystemExit(
            f"No database at {db}. Run `make demo` first — the bundle ships the "
            "simulated week, it does not generate it."
        )
    print(f"  db      {db.name}  {size / 1_000_000:.1f} MB")

    frames_root = Path(settings.blob_local_path)
    keys = await referenced_frame_keys()
    total = sum(_copy(frames_root / k, BUNDLE / "frames" / k) for k in sorted(keys))
    print(f"  frames  {len(keys)} referenced  {total / 1000:.0f} KB")

    media_root = Path(settings.media_local_path)
    media_total = 0
    media_count = 0
    if media_root.exists():
        for path in media_root.rglob("*"):
            if path.is_file():
                media_total += _copy(path, BUNDLE / "media" / path.relative_to(media_root))
                media_count += 1
    print(f"  media   {media_count} files  {media_total / 1_000_000:.1f} MB")

    grand = sum(p.stat().st_size for p in BUNDLE.rglob("*") if p.is_file())
    print(f"\n  {BUNDLE.relative_to(REPO)}  {grand / 1_000_000:.1f} MB total")


if __name__ == "__main__":
    asyncio.run(main())
