# Data sources

What the state DOT APIs actually look like as of August 2026, verified by hitting them
rather than by reading documentation. Several things in the original PRD turned out to be
out of date, and all of them matter for anyone trying to reproduce this.

## The headline: GA, FL and NC are one platform

All three run the same vendor software, on the same path shape:

```
https://511ga.org/api/v2/get/<resource>?key=<KEY>&format=json
https://fl511.com/api/v2/get/<resource>?key=<KEY>&format=json
https://www.drivenc.gov/api/v2/get/<resource>?key=<KEY>&format=json
```

Verified — all three return the identical error without a key:

```
$ curl -s "https://511ga.org/api/v2/get/cameras?key=TEST&format=json"
<Error><Message>Invalid Key</Message></Error>

$ curl -s "https://fl511.com/api/v2/get/cameras?format=json"
<Error><Message>Invalid Key</Message></Error>

$ curl -s "https://www.drivenc.gov/api/v2/get/cameras?format=json"
<Error><Message>Invalid Key</Message></Error>
```

Two consequences:

1. **One adapter covers all three launch states.** `adapters/camera/vendor511.py` is
   parameterised by `(base_url, key)`. The multi-state scalability story costs one client,
   not three.
2. **Nothing works without keys.** There is no anonymous read path on any of them, which is
   why the project ships a simulator and why the entire pipeline is designed to run against
   one.

Note the error is XML even when `format=json` is requested, so the client checks for a
leading `<` before attempting to parse.

## Corrections to the PRD

| PRD said | Actually |
|---|---|
| FL camera metadata available via public ArcGIS FeatureServer, no key needed | Returns `{"error":{"code":499,"message":"Token Required"}}`. Needs a token. |
| NCDOT at `eapps.ncdot.gov/services/traffic-prod/v1/*` | Deprecated May 2026. Returns a pointer to `drivenc.gov/help/endpoint/event`. Now on the v2 vendor platform. |
| SC likely the same vendor as GA | `511sc.org/api/v2/get/cameras` 404s. Different platform. |
| TN discoverable camera endpoints | TDOT SmartWay is a separate Angular application. Needs its own adapter. |

So the "same vendor across the Southeast" assumption holds for **three of six** states.
GA + FL + NC is the right launch scope, and AL/TN/SC each need real work rather than a
config entry.

## Getting keys

Register an account at each site, then request a developer key:

- **Georgia** — https://511ga.org
- **Florida** — https://fl511.com
- **North Carolina** — https://drivenc.gov

Approval can take days. Request all three early.

## Rate limits

The published throttle is **10 calls per 60 seconds per key**, enforced client-side in
`adapters/camera/rate_limit.py` with a per-state sliding window. Being throttled would be
our fault, and a revoked key means no product.

Ten calls a minute cannot poll thousands of cameras, so the API is used for two things
only:

- **Camera registry** — fetched daily. Metadata barely changes.
- **Incident feed** — fetched per polling pass, so the confidence gate can suppress hazards
  the state already knows about.

**Snapshots do not go through the API.** The registry hands back image CDN URLs, which are
ordinary public HTTPS image endpoints not behind the throttle. That is the polling path.

## Resources on the platform

| Resource | Used for |
|---|---|
| `cameras` | Registry: id, lat/lng, roadway, direction, owning organisation, image URL |
| `event` | Active incidents — the duplicate-suppression cross-reference |
| `videourl` | Short-lived m3u8 stream URLs. Only worth calling for cameras with an open case. |
| `messagesigns` | Not used |

## Field naming

Field names vary between deployments of the same platform, so the parser looks each value
up under several spellings (`Latitude`/`latitude`/`lat`, `ImageUrl`/`imageUrl`/`Url`, …).
A row missing coordinates, an image URL or an id is dropped rather than stored broken —
see `tests/unit/test_cloud_adapters.py::TestVendor511Parsing`.

## Compliance

- These are official developer APIs, free and designed for third-party use.
- **No Google Maps content in the pipeline** — Maps ToS §3.2.3 prohibits extraction,
  caching and derived content. A future map view would use Leaflet + OpenStreetMap.
- Frames are transient; only hazard-positive ones are kept as evidence, deleted after seven
  days by bucket lifecycle policy.
- Public infrastructure cameras only. No face or plate analysis.
