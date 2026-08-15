"""The whole loop, end to end, with no credentials.

Runs the real four-agent pipeline over simulated time against the seeded
scenarios and asserts the behaviours the product actually promises:

* a hazard the state hasn't posted gets confirmed, routed and reported
* a hazard the state *has* posted gets deliberately suppressed
* one hazard never produces two reports
* a hazard nobody fixes gets escalated and then handed to a human
* a hazard that clears gets closed with a before/after pair

This is the test that would fail if any of the earlier bugs came back -- the
prefilter that killed every frame, the correlation key that spawned a thousand
duplicate cases, or the race that filed the same report twice.
"""

from __future__ import annotations

import pytest_asyncio

from road_cleaner.config import Settings
from road_cleaner.container import build_container
from road_cleaner.domain.enums import CaseKind, GateDecision, HazardType, Stage
from road_cleaner.pipeline.runner import PipelineRunner

# Long enough to reach the *third* escalation tier on the flooding case, which
# is where the agent stops filing and hands over to a human. That hazard starts
# at minute 2860 with a six-hour SLA, so tier 3 lands around minute 3600 -- plus
# slack for the decaying re-check schedule.
SIMULATED_MINUTES = 4800
STEP_SECONDS = 600


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def completed_run(tmp_path_factory):
    """Run the whole pipeline exactly once; every test inspects the same result.

    Module-scoped deliberately. This drives a couple of thousand camera polls
    through four agents, and re-running it per test turned a ten-second suite
    into a ten-minute one.
    """
    tmp = tmp_path_factory.mktemp("pipeline")
    settings = Settings(
        ROAD_CLEANER_MODE="local",
        DRY_RUN=True,
        DATA_DIR=str(tmp),
        SQLITE_PATH=str(tmp / "test.db"),
        BLOB_LOCAL_PATH=str(tmp / "frames"),
        FILING_SANDBOX_INBOX=str(tmp / "outbox"),
        LOG_LEVEL="ERROR",
    )
    container = build_container(settings, simulated=True)
    await container.startup()
    try:
        runner = PipelineRunner(container)
        await runner.seed()
        stats = await runner.run_simulated(
            minutes=SIMULATED_MINUTES, step_seconds=STEP_SECONDS, audit_every_minutes=30
        )
        cases = await container.repository.list_cases(limit=500)
        yield container, runner, stats, cases
    finally:
        await container.shutdown()


class TestPipelineRuns:
    async def test_cameras_were_seeded(self, completed_run):
        container, *_ = completed_run
        cameras = await container.repository.list_cameras()
        assert len(cameras) == 21
        assert {c.state for c in cameras} == {"GA", "FL", "NC"}

    async def test_frames_were_captured_and_stored(self, completed_run):
        container, runner, stats, _ = completed_run
        assert stats.polls > 100
        assert stats.frames_published > 0
        # Evidence must be real bytes on disk, not a placeholder.
        frame = await container.repository.latest_frame("GDOT-CCTV-0447")
        assert frame is not None
        data = await container.blobs.get(frame.blob_key)
        assert data.startswith(b"\xff\xd8")  # JPEG magic

    async def test_the_prefilter_saves_money_without_eating_everything(self, completed_run):
        """Regression: a prefilter seeded on the JPEG header killed every frame."""
        _, _, stats, _ = completed_run
        assert stats.prefilter_kills > 0, "prefilter is doing nothing"
        assert stats.frames_analyzed > 0, "prefilter killed every frame"
        assert stats.detections > 0

    async def test_case_count_is_sane(self, completed_run):
        """Regression: broken correlation produced 1,379 cases for 12 scenarios."""
        _, _, _, cases = completed_run
        assert 0 < len(cases) < 40, f"expected a handful of cases, got {len(cases)}"


class TestFiling:
    async def test_something_got_reported_with_a_reference(self, completed_run):
        _, _, _, cases = completed_run
        filed = [c for c in cases if c.was_filed]
        assert filed, "nothing was ever reported"
        assert all(c.reference for c in filed)
        assert all(c.agency_name for c in filed)

    async def test_reports_were_written_to_the_outbox_and_not_sent(self, completed_run):
        container, *_ = completed_run
        outbox = sorted(container.settings.filing_outbox.glob("*.txt"))
        assert outbox, "dry run produced no artifacts"
        body = outbox[0].read_text()
        assert "NOT SENT — DRY RUN" in body
        assert "Filed automatically by Road Cleaner" in body

    async def test_one_hazard_never_produces_two_reports_at_the_same_tier(
        self, completed_run
    ):
        """Regression: concurrent bus workers raced and filed twice."""
        container, _, _, cases = completed_run
        for case in cases:
            filings = await container.repository.get_filings(case.id)
            tiers = [f.tier for f in filings]
            assert len(tiers) == len(set(tiers)), f"{case.id} filed twice at one tier"

    async def test_jurisdiction_was_resolved_not_guessed(self, completed_run):
        container, _, _, cases = completed_run
        for case in (c for c in cases if c.was_filed):
            agency = await container.repository.get_agency(case.agency_id)
            assert agency is not None
            assert agency.state == case.state


class TestSuppression:
    async def test_a_hazard_the_state_already_knows_about_is_suppressed(
        self, completed_run
    ):
        """The whole point is catching what they missed, not repeating them."""
        _, _, _, cases = completed_run
        suppressed = [c for c in cases if c.gate_decision is GateDecision.SUPPRESS]
        assert suppressed, "the seeded duplicate event never suppressed anything"
        case = suppressed[0]
        assert case.hazard_type is HazardType.UNREPORTED_CLOSURE
        assert "210 metres" in (case.gate_reason or "")

    async def test_suppressed_cases_are_never_filed(self, completed_run):
        container, _, _, cases = completed_run
        for case in (c for c in cases if c.gate_decision is GateDecision.SUPPRESS):
            assert await container.repository.get_filings(case.id) == []
            assert case.reference == "duplicate"

    async def test_suppressed_does_not_masquerade_as_cleared(self, completed_run):
        _, _, _, cases = completed_run
        for case in (c for c in cases if c.gate_decision is GateDecision.SUPPRESS):
            assert case.kind is CaseKind.SUPPRESSED


class TestEscalation:
    async def test_a_hazard_nobody_fixes_gets_chased(self, completed_run):
        container, _, stats, cases = completed_run
        escalated = [c for c in cases if c.kind is CaseKind.ESCALATED]
        assert escalated, "nothing escalated despite a hazard that never clears"

        case = escalated[0]
        assert case.escalation_tier >= 2
        filings = await container.repository.get_filings(case.id)
        assert len(filings) >= 2, "escalation did not produce a follow-up filing"
        assert {f.tier for f in filings} >= {1, 2}

    async def test_escalation_stops_and_hands_over_to_a_human(self, completed_run):
        """An agent that re-sends forever is just spam with better manners."""
        _, _, stats, cases = completed_run
        assert stats.flagged_for_human > 0
        for case in cases:
            assert case.escalation_tier <= 3

    async def test_the_trail_records_the_push(self, completed_run):
        container, _, _, cases = completed_run
        escalated = [c for c in cases if c.kind is CaseKind.ESCALATED][0]
        stages = {t.stage for t in await container.repository.get_trail(escalated.id)}
        assert Stage.PUSH in stages


class TestClearance:
    async def test_a_fixed_road_closes_with_before_and_after(self, completed_run):
        container, _, _, cases = completed_run
        cleared = [c for c in cases if c.kind is CaseKind.CLEARED and c.was_filed]
        assert cleared, "nothing was ever confirmed fixed"

        case = cleared[0]
        assert case.closed_at is not None
        evidence = [f for f in case.frame_refs if f.mark and f.blob_key]
        after = [f for f in case.frame_refs if f.clear and f.blob_key]
        assert evidence and after, "no before/after pair"
        assert evidence[0].blob_key != after[0].blob_key

        # Both frames must actually exist and be real images.
        for ref in (evidence[0], after[0]):
            assert (await container.blobs.get(ref.blob_key)).startswith(b"\xff\xd8")


class TestAuditTrail:
    async def test_every_case_explains_itself(self, completed_run):
        container, _, _, cases = completed_run
        for case in cases:
            trail = await container.repository.get_trail(case.id)
            assert trail, f"{case.id} has no trail"
            assert case.sentence, f"{case.id} has no summary"
            assert case.gate_reason, f"{case.id} never recorded why"

    async def test_a_filed_case_walks_the_stages_in_order(self, completed_run):
        container, _, _, cases = completed_run
        case = next(c for c in cases if c.was_filed)
        trail = await container.repository.get_trail(case.id)
        stages = [t.stage for t in trail]
        assert stages.index(Stage.DETECT) < stages.index(Stage.RESOLVE)
        assert stages.index(Stage.RESOLVE) < stages.index(Stage.REPORT)

    async def test_trails_stay_readable(self, completed_run):
        """Regression: a routine entry per re-check produced hundreds per case."""
        container, _, _, cases = completed_run
        for case in cases:
            trail = await container.repository.get_trail(case.id)
            assert len(trail) < 25, f"{case.id} trail has {len(trail)} entries"

    async def test_stats_are_computable_for_the_dashboard(self, completed_run):
        container, *_ = completed_run
        stats = await container.repository.stats(container.clock.now())
        assert stats["total_cases"] > 0
        assert 0 <= stats["missed_by_feed_pct"] <= 100
