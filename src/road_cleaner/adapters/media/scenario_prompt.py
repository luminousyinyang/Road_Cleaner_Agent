"""Turning a real detection into a Veo prompt.

The value of this whole simulation surface rests on the clip being *derived from*
something the fleet actually saw, so the prompt is built from the stored case --
hazard type, lane, road, direction, the analyst's own description -- rather than
typed by hand.

Two things shape the wording:

* **Dashcam perspective, not camera perspective.** The evidence frame is a fixed
  pole-mounted view; what an AV perception stack needs is the same hazard seen
  from a moving vehicle at windscreen height. That reframing is the entire point
  of generating anything.

* **Plain, not dramatic.** Veo's safety filter rejects prompts that read as gore
  or spectacular crashes, and a filtered prompt returns no clip at all. Debris,
  stopped vehicles and flooding are all describable as road conditions; writing
  them as disasters gets them refused. `MediaUnavailableError` says as much when
  it happens.
"""

from __future__ import annotations

from road_cleaner.domain.models import Camera, Case

# Read as camera-operator direction rather than as narrative. Keeps the output
# looking like real footage instead of a film.
#
# The specificity about the *camera* is doing real work. Asked for "realistic
# footage" the model reaches for cinema -- clean, graded, well-composed. Naming a
# cheap fixed lens and its artefacts is what makes it look like a dashcam.
_STYLE = (
    "Authentic consumer dashcam footage, recorded through a windscreen from the "
    "driver's eye height. Fixed wide-angle lens, everything in focus, no camera "
    "moves and no zoom. Flat unedited colour, mild video compression, slight "
    "engine vibration. Overcast even daylight, no lens flare, no shallow depth of "
    "field, no colour grading. The car holds a steady lane at normal traffic speed. "
    # Veo stamps a date and time into the corner unprompted, because that is what
    # dashcam footage looks like -- but it renders as broken glyphs
    # ("201/0/03/01 13:16:8:41"), which reads as fake rather than authentic. The
    # negative prompt alone did not stop it; saying so positively helps more.
    "The image is completely clean of any on-screen text: no date, no time stamp, "
    "no counter, no logo in any corner."
)

# Veo takes a dedicated `negative_prompt` and it is far stronger than putting the
# same words in the prompt text, where "no collision" can read as a cue to render
# one. Scale words are here because oversizing was the single worst failure:
# the first pass rendered a tyre tread as a row of car-sized black masses.
NEGATIVE_PROMPT = (
    "oversized objects, giant debris, object filling the frame, object taller than "
    "the car, object rearing up off the ground, object growing or morphing, "
    "close-up of an object, duplicated objects, repeated identical objects in a row, "
    "intact wheel, chrome rim, hubcap, whole tyre, "
    "cinematic, film look, colour grading, slow motion, camera shake, zoom, "
    "crash, collision, impact, emergency, dramatic, people, pedestrians, faces, "
    "text overlay, timestamp overlay, watermark, garbled text, distorted lettering"
)

# The analyst's hazard vocabulary, rendered as something a camera could observe.
#
# Every entry carries a physical size anchor. Without one the model has no sense
# of how big the thing should be and defaults to dramatic, which is how a shed
# tyre tread became a wall of tyres. "Small", "flat" and comparisons to everyday
# objects are the parts that matter.
_HAZARD_PHRASING = {
    # Note what is *not* said: "tyre" and "wheel". Naming them put an intact
    # wheel with a chrome rim in the middle of the road, because a comparison
    # object is still an object -- see `_SIZE_CLAMP`.
    "debris": (
        "a torn scrap of black rubber, roughly 40 centimetres long, lying "
        "completely flat on the asphalt and barely raised off the road surface"
    ),
    "stalled_vehicle": (
        "one ordinary sedan halted at the roadside with its hazard lights blinking"
    ),
    "unreported_closure": (
        "a short line of small orange cones, each reaching only shin height"
    ),
    "flooding": (
        "a thin sheet of standing rainwater lying flat across the asphalt, only a "
        "few centimetres deep, reflecting the sky"
    ),
    # "guardrail" alone rendered a huge peeled sheet of metal rearing up beside
    # the car, so the phrasing leads with how small and low the damage is.
    "infrastructure_damage": (
        "a short dented segment of the low metal barrier at the very edge of the "
        "road, dented inward but still upright and flat against the roadside"
    ),
    "animal": (
        "a single deer standing still at the far edge of the road"
    ),
}

# Hazards we will not synthesise, whatever the case says.
#
# `pedestrian_on_highway` is a person. Generating footage of a person, seeded
# from a real frame of a real person walking on a real road, is the exact thing
# the "roads, not people" house rule exists to prevent -- and it would be
# refused anyway, since every render sets person_generation="dont_allow".
# Refusing it here, by name, is clearer than letting the safety filter do it.
UNSIMULATABLE = {"pedestrian_on_highway"}

_LANE_PHRASING = {
    "left_shoulder": "on the left shoulder",
    "right_shoulder": "on the right shoulder",
    "lane_1": "in the left-hand lane",
    "lane_2": "in the centre lane",
    "lane_3": "in the right-hand lane",
    "median": "on the median",
    "median_barrier": "against the median barrier",
    "all_lanes": "across all lanes",
    "intersection": "in the intersection",
}


class UnsimulatableHazardError(ValueError):
    """This hazard is one we have decided not to generate footage of."""


def scenario_prompt(
    case: Case,
    camera: Camera | None = None,
    lane: str = "",
    description: str = "",
) -> str:
    """Build the Veo prompt for one case.

    Returns a single paragraph: what the driver sees, where they are, and how it
    should be shot. Raises `UnsimulatableHazardError` for hazards in
    `UNSIMULATABLE`.

    `description` is the vision model's account of the frame. Note it is the
    *detection's* description, not `Case.sentence` -- the latter narrates the
    case's progress through the workflow ("reported to GDOT, cleared 1d 6h
    later"), which tells a video model nothing about what the road looked like.
    """
    hazard_key = getattr(case.hazard_type, "value", str(case.hazard_type))
    if hazard_key in UNSIMULATABLE:
        raise UnsimulatableHazardError(
            f"{hazard_key} is not simulated: it depicts a person. "
            "See the 'roads, not people' rule."
        )
    subject = _HAZARD_PHRASING.get(hazard_key, f"{case.hazard_title.lower()}")

    where = _LANE_PHRASING.get(lane, "")
    if where:
        subject = f"{subject}, {where}"

    # Road *character*, not its name. Naming the road and the nearest exit made
    # the model paint gantry signs, and generated signage comes out as garbled
    # lettering -- one clip read "Howell Mill Road Road". The provenance sidecar
    # already records which real case this came from; the picture does not have
    # to spell it out, and looks markedly more plausible when it does not.
    road = _road_character(camera)

    # The analyst's own words, but only when they will not fight the size anchor.
    # Detection prose says things like "Large dark object, likely shed truck tyre
    # tread" -- and "large" is exactly the instruction that produced a wall of
    # car-sized tyres. When the description carries scale language it is dropped
    # rather than sanitised, because the curated phrasing above is already
    # accurate for the hazard type.
    seen = _safe_description(description)

    # The clamp has to match the hazard. "Never larger than a car wheel" is the
    # right instruction for a strip of tyre tread and a nonsensical one for a
    # stalled sedan or a sheet of standing water.
    clamp = _SIZE_CLAMP.get(hazard_key, "stays in proportion with the vehicles around it")

    # Where the hazard sits has to match what it is. Telling the model a damaged
    # roadside barrier was "directly ahead in the car's own lane" is a
    # contradiction, and it resolved it the wrong way -- by rearing the barrier up
    # into the carriageway. Roadside things stay at the roadside.
    placement = _PLACEMENT.get(hazard_key, _PLACEMENT_IN_LANE)

    return (
        f"{_STYLE} The car is driving on {road}. {placement}, about a hundred "
        f"metres away, is {subject}.{seen} It is visible but unremarkable, small "
        f"at first and growing only as the car gets nearer, the way a real object "
        f"does. The car approaches at a steady speed and continues past it without "
        f"stopping. Throughout the shot it keeps exactly the same size and shape, "
        f"and {clamp}."
    )


_PLACEMENT_IN_LANE = "Ahead on the road in the car's own lane"
_PLACEMENT_ROADSIDE = "Ahead at the side of the road, off the carriageway"

_PLACEMENT = {
    "debris": _PLACEMENT_IN_LANE,
    "flooding": _PLACEMENT_IN_LANE,
    "unreported_closure": _PLACEMENT_IN_LANE,
    "stalled_vehicle": _PLACEMENT_ROADSIDE,
    "infrastructure_damage": _PLACEMENT_ROADSIDE,
    "animal": _PLACEMENT_ROADSIDE,
}


# How big the hazard may get. Paired with `_HAZARD_PHRASING`.
#
# The hard-won rule here: **a comparison object is still an object.** "Never
# larger than a car wheel" did not cap the size of the debris -- it put a car
# wheel, rim and all, in the middle of the lane. "Normal guardrail height" gave
# a guardrail towering over the bonnet.
#
# So these clamps never name a new object. They refer only to things already in
# the scene (the lane, the traffic, the road surface) or to nothing at all.
_SIZE_CLAMP = {
    "debris": (
        "stays flat against the road surface, small enough that it covers only a "
        "narrow part of one lane and never rises higher than the kerb"
    ),
    "stalled_vehicle": "is the same size as the other cars in the traffic around it",
    "unreported_closure": "stay low, reaching nowhere near the height of passing traffic",
    "flooding": "stays a flat film of water on the road surface with no depth to it",
    "infrastructure_damage": (
        "stays low and close to the ground at the road edge, well below the "
        "height of the passing traffic, and never rears up or fills the frame"
    ),
    "animal": "stays small in the frame, far away at the roadside",
}


def _road_character(camera: Camera | None) -> str:
    """Describe the kind of road, without naming it."""
    if camera is None:
        return "a multi-lane highway"
    road = (camera.road or "").upper()
    if road.startswith("I-"):
        return "a wide three-lane interstate highway with a concrete median barrier"
    if road.startswith(("US-", "SR-", "GA-", "FL-", "NC-")):
        return "a two-lane divided state highway"
    return "a multi-lane highway"


# Words that inflate the hazard, either in size or in drama. Their presence is
# enough to drop the whole phrase.
#
# The second group matters as much as the first. An analyst note reading "rail
# end protruding toward the travel lanes" is accurate prose and a terrible
# instruction -- it is an invitation to swing the barrier into the carriageway,
# which is precisely the failure it produced.
_SCALE_WORDS = (
    "large", "huge", "massive", "big", "giant", "enormous", "oversized", "wide",
    "protruding", "torn open", "ripped", "sticking out", "blocking", "across the",
    "sheared", "collapsed", "destroyed", "mangled",
)


def _safe_description(description: str) -> str:
    text = " ".join((description or "").split()).strip()
    if not text:
        return ""
    if any(word in text.lower() for word in _SCALE_WORDS):
        return ""
    return f" {text}"
