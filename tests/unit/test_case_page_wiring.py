"""The case page's run panel renders two different shapes. The script sees one.

`case.html` branches on `mode`, and the two branches did not define the same
element ids: the `auto` note is a fixed sentence with no id at all, while the
`assisted` one carries `id="run-note"` so the script can swap in a cache note.

`inspect.js` looked that id up unconditionally and assigned to it on every
painted result. In `auto` mode it was null, so the first poll threw, the throw
unwound the polling loop, and the page reported "Stopped." and reset the button
while the job carried on running server-side to completion. A page claiming a run
had stopped while it had not is the most misleading state this UI can reach.

Two things are pinned here: that the asymmetry is known about, and that painting
can never again abandon a run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "src" / "road_cleaner" / "web" / "templates" / "case.html"
SCRIPT = REPO / "src" / "road_cleaner" / "web" / "static" / "js" / "inspect.js"


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text()


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text()


def _mode_branches(template: str) -> list[tuple[set[str], set[str]]]:
    """The ids defined either side of each `{% if mode == 'auto' %}`."""
    blocks = re.findall(
        r"\{%\s*if mode == 'auto'\s*%\}(.*?)\{%\s*else\s*%\}(.*?)\{%\s*endif\s*%\}",
        template,
        re.S,
    )
    return [
        (set(re.findall(r'id="([^"]+)"', a)), set(re.findall(r'id="([^"]+)"', b)))
        for a, b in blocks
    ]


class TestTheTwoModesAgreeOnTheirElements:
    # The one known difference. Anything else appearing here is a new instance
    # of the same bug and needs a guard in `inspect.js` before it is added.
    KNOWN_ASYMMETRIC = {"run-note"}

    def test_only_the_known_id_differs_between_branches(self, template):
        differing: set[str] = set()
        for auto_ids, other_ids in _mode_branches(template):
            differing |= auto_ids ^ other_ids
        assert differing == self.KNOWN_ASYMMETRIC, (
            f"ids differ between the mode branches: {sorted(differing)}. "
            "An id that exists in only one branch is null in the other, and "
            "inspect.js must guard it before this list changes."
        )

    def test_the_script_guards_that_id(self, script):
        """`run-note` is optional, so every use of it has to be defensive."""
        assert "if (note) {" in script, (
            "inspect.js dereferences `note` without checking it exists; in "
            "auto mode that element is not rendered"
        )


class TestPaintingCannotAbandonARun:
    """The structural protection, independent of any single element.

    Even with `note` guarded, a future painting bug would otherwise unwind the
    poll loop and strand a running job behind a page that says it stopped.
    """

    def test_paint_is_called_inside_a_try_within_poll(self, script):
        poll = script.split("async function poll(")[1].split("\n  }")[0]
        assert "paint(job.result)" in poll, "poll no longer paints; update this test"
        painted = poll.split("paint(job.result)")[0]
        assert "try {" in painted, (
            "paint() is called from poll() without a try/catch — a painting "
            "error will abandon a run that is still going"
        )

    def test_the_failure_is_logged_rather_than_swallowed(self, script):
        poll = script.split("async function poll(")[1].split("\n  }")[0]
        assert "console.error" in poll, (
            "a swallowed painting error leaves no trace for whoever debugs it"
        )
