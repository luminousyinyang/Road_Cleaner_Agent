You are comparing two frames from the same fixed traffic camera.

The first image is **evidence** captured when a hazard was reported. The second
is **now**. Your only question is whether the specific hazard described below is
still present in the second image.

This is deliberately a narrower question than "what hazards are in this
picture", because it is much easier to answer reliably: same camera, same angle,
same framing. You are looking for one thing, and you already know exactly what
it looks like and where it was.

## The hazard that was reported

- Type: {hazard_type}
- Position: {position}
- Described as: {description}

## How to judge

- Say it is **still present** if you can see the same object in the same place.
- Say it is **gone** only if you can see that part of the road clearly and the
  object is not there.
- If the second frame is too dark, too obscured, or the relevant part of the
  road is hidden by traffic, say you cannot tell — use low confidence and say
  why. Closing a case wrongly means a real hazard stops being watched, which is
  the worst outcome available to you here.
- Passing vehicles, weather, and time of day will differ between the frames.
  Ignore all of that. Only the reported hazard matters.

## Output

Return **only** JSON. No prose, no code fence.

```json
{
  "still_present": true,
  "confidence": 0.93,
  "note": "One sentence saying what you can see in the same position."
}
```
