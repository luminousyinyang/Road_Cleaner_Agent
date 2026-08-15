"""Whose road is it?

This is the question that makes the whole product hard, and the reason the
existing tools stop at "here is a map of problems". A driver can see a pothole;
what they cannot do is work out that this particular stretch belongs to a state
DOT district rather than the county, or that the traffic signal is the city's
even though the pavement under it is the state's.

Rules run first and answer almost everything, because jurisdiction is mostly
stable, knowable facts rather than a judgement call. A model is consulted only
when the rules genuinely cannot decide -- and if there is no model, the case is
watched rather than guessed at. Filing with the wrong agency is worse than not
filing: it wastes a stranger's time and the hazard stays exactly where it was.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from road_cleaner.domain.enums import AgencyLevel, Channel, HazardType
from road_cleaner.domain.models import Agency, Camera, Detection
from road_cleaner.logging import get_logger
from road_cleaner.ports.reasoning import Reasoner

log = get_logger(__name__)


class JurisdictionResult:
    def __init__(
        self,
        agency: Agency | None,
        rule_id: str,
        rationale: str,
        confidence: float = 1.0,
    ) -> None:
        self.agency = agency
        self.rule_id = rule_id
        self.rationale = rationale
        self.confidence = confidence

    @property
    def resolved(self) -> bool:
        return self.agency is not None


class JurisdictionRegistry:
    def __init__(self, agencies: list[Agency], rules: list[dict]) -> None:
        self.agencies = {a.id: a for a in agencies}
        self.rules = rules

    @classmethod
    def load(cls, path: Path) -> JurisdictionRegistry:
        raw = yaml.safe_load(Path(path).read_text())
        agencies = [
            Agency(
                id=item["id"],
                name=item["name"],
                level=AgencyLevel(item["level"]),
                state=item["state"],
                channel=Channel(item["channel"]),
                endpoint=item.get("endpoint"),
                email=item.get("email"),
                ref_format=item.get("ref_format", "REF-#####"),
                ref_label=item.get("ref_label"),
                sla_overrides=item.get("sla_overrides", {}) or {},
                jurisdiction_note=item.get("jurisdiction_note"),
            )
            for item in raw.get("agencies", [])
        ]
        return cls(agencies, raw.get("rules", []))

    def all_agencies(self) -> list[Agency]:
        return list(self.agencies.values())

    def candidates_for(self, camera: Camera) -> list[Agency]:
        """Every agency that could plausibly own this camera's road."""
        return [a for a in self.agencies.values() if a.state == camera.state]

    def resolve_by_rules(self, camera: Camera, detection: Detection) -> JurisdictionResult:
        """Walk the rules in order; first match wins."""
        for rule in self.rules:
            agency = self._apply(rule, camera, detection)
            if agency is not None:
                return JurisdictionResult(
                    agency=agency,
                    rule_id=rule.get("id", "unnamed"),
                    rationale=self._rationale(rule, agency),
                    confidence=1.0,
                )
        return JurisdictionResult(None, "none", "No rule matched.", 0.0)

    def _apply(self, rule: dict, camera: Camera, detection: Detection) -> Agency | None:
        match = rule.get("match", {})

        if (contains := match.get("road_contains")) and not any(
            token.lower() in camera.road.lower() for token in contains
        ):
            return None

        if (prefixes := match.get("road_prefix")) and not any(
            camera.road.upper().startswith(p.upper()) for p in prefixes
        ):
            return None

        if (hazards := match.get("hazard_types")) and detection.hazard_type not in {
            HazardType(h) for h in hazards
        }:
            return None

        if (lanes := match.get("lane_positions")) and detection.lane_position not in lanes:
            return None

        if (counties := match.get("county_in")) and camera.county not in counties:
            return None

        if match.get("has_owner_agency") and not camera.owner_agency_id:
            return None

        # --- the rule matched; now work out which agency it names ---
        if rule.get("use_camera_owner"):
            return self.agencies.get(camera.owner_agency_id or "")

        if by_county := rule.get("agency_by_county"):
            return self.agencies.get(by_county.get(camera.county or "", ""))

        if by_state := rule.get("agency_by_state"):
            return self.agencies.get(by_state.get(camera.state, ""))

        if agency_id := rule.get("agency"):
            return self.agencies.get(agency_id)

        return None

    @staticmethod
    def _rationale(rule: dict, agency: Agency) -> str:
        description = (rule.get("description") or "").strip().replace("\n", " ")
        return f"{agency.name} — {description}" if description else agency.name

    async def resolve(
        self,
        camera: Camera,
        detection: Detection,
        reasoner: Reasoner | None = None,
    ) -> JurisdictionResult:
        """Full resolution: rules, then a model only if they came up empty."""
        result = self.resolve_by_rules(camera, detection)
        if result.resolved:
            return result

        if reasoner is None:
            return JurisdictionResult(
                None,
                "unresolved",
                "No rule matched and no reasoner available. Holding rather than guessing.",
                0.0,
            )

        candidates = self.candidates_for(camera)
        verdict = await reasoner.resolve_jurisdiction(camera, detection, candidates)
        if verdict is None:
            return JurisdictionResult(
                None, "unresolved", "Could not determine the responsible agency.", 0.0
            )

        agency = self.agencies.get(verdict.agency_id)
        if agency is None:
            log.warning(
                "Reasoner named an agency that isn't in the registry",
                extra={"agency_id": verdict.agency_id, "camera_id": camera.id},
            )
            return JurisdictionResult(
                None, "unresolved", f"Unknown agency '{verdict.agency_id}'.", 0.0
            )

        return JurisdictionResult(
            agency=agency,
            rule_id=f"reasoner:{reasoner.name}",
            rationale=verdict.rationale,
            confidence=verdict.confidence,
        )
