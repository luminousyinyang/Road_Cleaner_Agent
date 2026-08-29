"""Render the mermaid blocks in `diagram.md` to PNGs in `img/`.

The markdown is the source. These images are a build artifact of it, for the
places that cannot render mermaid -- a Devpost submission form, a slide, a PDF.
Editing a PNG by hand puts it out of step with the diagram everyone else reads,
which is the failure mode `diagram.md` opens by complaining about.

    make diagrams

Needs node on the PATH; `npx` fetches mermaid-cli on first use.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

DOCS = pathlib.Path(__file__).parent
SOURCE = DOCS / "diagram.md"
OUT = DOCS / "img"

# Positional, matching the order the blocks appear in. A name per block, so the
# files are greppable rather than `diagram-3.png`.
NAMES = ["01-system", "02-dashcam", "03-drill", "04-deployment", "05-boundary"]


def main() -> int:
    blocks = re.findall(r"```mermaid\n(.*?)```", SOURCE.read_text(), re.S)
    if len(blocks) != len(NAMES):
        # Loud rather than silently renaming everything downstream of the gap.
        print(
            f"{SOURCE.name} has {len(blocks)} mermaid blocks but NAMES lists "
            f"{len(NAMES)}. Add a name for the new one.",
            file=sys.stderr,
        )
        return 1

    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, block in zip(NAMES, blocks, strict=True):
            src = pathlib.Path(tmp) / f"{name}.mmd"
            src.write_text(block)
            result = subprocess.run(
                [
                    "npx", "--yes", "@mermaid-js/mermaid-cli",
                    "-i", str(src),
                    "-o", str(OUT / f"{name}.png"),
                    "-b", "white",
                    "-s", "2",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"{name}: {result.stderr.strip()[:400]}", file=sys.stderr)
                return result.returncode
            print(f"  docs/img/{name}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
