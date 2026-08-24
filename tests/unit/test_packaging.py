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
    """Every rule, negations included, in file order. Order decides the winner."""
    path = REPO / name
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _matches(relative: str, rule: str) -> bool:
    """Whether one gitignore rule matches a path.

    The bit that matters, and that the previous version of this helper got
    wrong: a rule containing no internal slash is **unanchored** and matches at
    any depth, while a rule with a leading slash is pinned to the root. So
    `data/` hits `src/road_cleaner/adapters/geo/data/` and `/data/` does not.
    gcloud reads these files with gitignore semantics, so that distinction is
    the whole difference between a working image and a broken one.
    """
    dir_only = rule.endswith("/")
    rule = rule.rstrip("/")
    anchored = rule.startswith("/") or "/" in rule.rstrip("/")
    rule = rule.lstrip("/")

    parts = relative.split("/")
    if anchored:
        # Match against the path from the root, allowing a directory rule to
        # carry everything beneath it.
        depth = rule.count("/") + 1
        head = "/".join(parts[:depth])
        if fnmatch.fnmatch(head, rule):
            return dir_only or len(parts) == depth or True
        return False

    # Unanchored: every path segment is a candidate. A directory-only rule can
    # only match a segment that has something below it.
    for i, part in enumerate(parts):
        if not fnmatch.fnmatch(part, rule):
            continue
        if dir_only and i == len(parts) - 1:
            continue  # names a file, but the rule insists on a directory
        return True
    return False


def _is_excluded(relative: str, rules: list[str]) -> str | None:
    """The rule that would exclude this path, if any -- last match wins."""
    culprit = None
    for rule in rules:
        if rule.startswith("!"):
            if _matches(relative, rule[1:]):
                culprit = None
            continue
        if _matches(relative, rule):
            culprit = rule
    return culprit


@pytest.mark.parametrize("relative", RUNTIME_FILES)
def test_the_file_exists(relative: str):
    assert (REPO / relative).is_file(), f"{relative} is read at runtime but is not in the repo"


@pytest.mark.parametrize("ignore_file", [".gcloudignore", ".dockerignore", ".gitignore"])
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


@pytest.mark.parametrize("relative", RUNTIME_FILES)
def test_runtime_files_are_tracked_by_git(relative: str):
    """Simulating the ignore rules is not the same as asking git.

    `test_the_file_exists` passes for a file that is only on this machine, and
    the deploy is `gcloud run deploy --source .`, which uploads the working
    tree -- so an untracked runtime asset ships fine from here and is simply
    absent from every other checkout. That is how both geo `.gz` files stayed
    missing from the repo while the app worked locally.
    """
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative],
        cwd=REPO, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"{relative} is read at runtime but is not tracked by git. It exists on this "
        "machine, so local runs and a --source deploy from here both work; any other "
        "checkout builds an image without it."
    )


class TestTheCheckerItself:
    """The checker above is the only thing standing between a bad ignore rule
    and a broken deploy, and it has already been wrong once.

    Its first version treated `data/` as root-anchored, which is Docker's
    reading, not gitignore's. `us_states.json.gz` was listed in RUNTIME_FILES
    the whole time and the test still passed, while the deployed image had no
    place lookup and every dropped pin answered `HTTP 500`.
    """

    GEO = "src/road_cleaner/adapters/geo/data/us_states.json.gz"

    def test_an_unanchored_rule_is_caught_at_any_depth(self):
        assert _is_excluded(self.GEO, ["data/"]) == "data/"

    def test_anchoring_the_rule_is_what_makes_it_safe(self):
        assert _is_excluded(self.GEO, ["/data/"]) is None

    def test_an_anchored_rule_still_excludes_the_real_data_directory(self):
        assert _is_excluded("data/frames/x.jpg", ["/data/"]) == "/data/"

    def test_a_later_negation_rescues_the_file(self):
        rules = ["data/", "!src/road_cleaner/adapters/geo/data/"]
        assert _is_excluded(self.GEO, rules) is None

    def test_the_blanket_glob_from_the_original_outage_is_still_caught(self):
        assert _is_excluded("src/road_cleaner/agents/prompts/analyst.md", ["*.md"]) == "*.md"

    def test_a_directory_rule_does_not_match_a_file_of_the_same_name(self):
        assert _is_excluded("src/road_cleaner/data", ["data/"]) is None


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
