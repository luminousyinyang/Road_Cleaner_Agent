"""Files the running code reads must actually reach the image.

This exists because of a real deployment failure: `.gcloudignore` contained a
blanket `*.md` to skip documentation, which also excluded
`src/road_cleaner/agents/prompts/analyst.md`. Cloud Build uploaded a tree
without it, the image built cleanly, and the container died on startup with

    FileNotFoundError: '/app/src/road_cleaner/agents/prompts/analyst.md'

Nothing in the test suite noticed, because locally the file is right there. The
gap was between "the code works" and "the code was shipped", so that is what
these tests cover.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Non-Python files the code opens at runtime. Anything added here must also
# survive the ignore rules below.
RUNTIME_FILES = [
    "src/road_cleaner/agents/prompts/analyst.md",
    "src/road_cleaner/web/templates/dashcam.html",
    "src/road_cleaner/web/static/js/dashcam.js",
    "src/road_cleaner/agents/prompts/clearance.md",
    "src/road_cleaner/adapters/repo/schema.sql",
    "seeds/agencies.yaml",
    "src/road_cleaner/adapters/geo/data/us_states.json.gz",
    "src/road_cleaner/adapters/geo/data/us_places.tsv.gz",
    "seeds/cameras.json",
    "seeds/scenarios.json",
]


def _exclusion_rules(name: str) -> list[str]:
    path = REPO / name
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]


def _is_excluded(relative: str, rules: list[str]) -> str | None:
    """The rule that would exclude this path, if any.

    An approximation of gitignore semantics -- enough to catch the blanket
    glob that caused the outage, which is the point.
    """
    name = Path(relative).name
    for rule in rules:
        directory = rule.rstrip("/")
        if rule.endswith("/") and (relative == directory or relative.startswith(directory + "/")):
            return rule
        if fnmatch.fnmatch(relative, rule) or fnmatch.fnmatch(name, rule):
            return rule
    return None


@pytest.mark.parametrize("relative", RUNTIME_FILES)
def test_the_file_exists(relative: str):
    assert (REPO / relative).is_file(), f"{relative} is read at runtime but is not in the repo"


@pytest.mark.parametrize("ignore_file", [".gcloudignore", ".dockerignore"])
@pytest.mark.parametrize("relative", RUNTIME_FILES)
def test_runtime_files_are_not_excluded_from_the_image(relative: str, ignore_file: str):
    rules = _exclusion_rules(ignore_file)
    if not rules:
        pytest.skip(f"{ignore_file} not present")
    culprit = _is_excluded(relative, rules)
    assert culprit is None, (
        f"{ignore_file} rule {culprit!r} excludes {relative}, which the app reads at "
        "runtime. The image will build and then die on startup."
    )


def test_the_prompts_are_not_empty():
    """An empty prompt file would fail far less obviously than a missing one."""
    for relative in RUNTIME_FILES:
        if relative.endswith(".md"):
            assert (REPO / relative).read_text().strip(), f"{relative} is empty"


class TestFfmpegIsAvailableWhereverThisRuns:
    """Frame extraction shells out, so the binary is a deployment concern.

    `imageio-ffmpeg` ships a static binary inside its wheel, which is precisely
    why it was chosen over an `apt-get install ffmpeg` in the Dockerfile -- but
    that only holds if it stays a hard dependency. Demoted to an optional extra,
    every case page would 500 on the button that is the whole demo.
    """

    def test_it_is_a_required_dependency_not_an_optional_extra(self):
        text = (REPO / "pyproject.toml").read_text()
        required = text.split("[project.optional-dependencies]", 1)[0]
        assert "imageio-ffmpeg" in required, (
            "imageio-ffmpeg must be in [project].dependencies -- frame extraction "
            "is not optional, it is the case page's main feature"
        )

    def test_the_binary_resolves_and_runs(self):
        import subprocess

        from road_cleaner.adapters.media.frame_extract import ffmpeg_path

        exe = Path(ffmpeg_path())
        assert exe.is_file(), f"ffmpeg not found at {exe}"
        result = subprocess.run([str(exe), "-version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ffmpeg version" in result.stdout
