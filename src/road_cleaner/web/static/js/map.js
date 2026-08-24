/* Dropping a pin on a road.
 *
 * Two places need the same thing: the drill, where you choose where a scenario
 * happens, and a case page, where you correct where one did. So this is one
 * component both of them mount, rather than two maps that drift apart.
 *
 * Leaflet and OpenStreetMap rather than Google Maps, which is a decision the
 * project already made and wrote down (`docs/data-sources.md`): the Maps terms
 * forbid extracting or deriving content, and this would be doing exactly that.
 * OSM asks for attribution instead, which Leaflet adds by default and which is
 * left alone.
 *
 * Every pin drop asks the server the two questions worth asking -- where is this
 * and who owns the road -- and shows both. A pin that lands somewhere no report
 * can be filed says so plainly rather than quietly picking the nearest agency.
 */

(function () {
  "use strict";

  // Roughly the lower 48, which is also what the server will accept.
  const HOME = { lat: 39.5, lng: -98.35, zoom: 4 };
  const PICKED_ZOOM = 11;

  function attach(element, options) {
    if (!element || !window.L) return null;

    const opts = options || {};
    const start = opts.lat != null && opts.lng != null;
    const map = window.L.map(element, { scrollWheelZoom: false }).setView(
      start ? [opts.lat, opts.lng] : [HOME.lat, HOME.lng],
      start ? PICKED_ZOOM : HOME.zoom
    );

    window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);

    let marker = start ? window.L.marker([opts.lat, opts.lng]).addTo(map) : null;
    let pending = 0;

    function place(lat, lng) {
      if (marker) marker.setLatLng([lat, lng]);
      else marker = window.L.marker([lat, lng]).addTo(map);
      ask(lat, lng);
    }

    /* Ask the server what is there. Every drop supersedes the one before it --
       drag a pin across three states and only the last answer should land. */
    async function ask(lat, lng) {
      const ticket = ++pending;
      say(opts.status, "Looking up…");
      try {
        const response = await fetch(
          `/api/where?lat=${encodeURIComponent(lat)}&lng=${encodeURIComponent(lng)}`
        );
        if (ticket !== pending) return;

        if (!response.ok) {
          const detail = await describe(response);
          say(opts.status, detail, true);
          if (opts.onPick) opts.onPick(null, { lat, lng, error: detail });
          return;
        }
        const found = await response.json();
        say(opts.status, summarise(found));
        if (opts.onPick) opts.onPick(found, null);
      } catch (err) {
        if (ticket !== pending) return;
        say(opts.status, `Could not look that up: ${(err && err.message) || err}`, true);
      }
    }

    map.on("click", (event) => place(event.latlng.lat, event.latlng.lng));

    // Sizing: a map mounted inside something hidden -- a collapsed panel, a
    // sidebar that has not laid out yet -- comes up grey until it is told to
    // measure itself again.
    setTimeout(() => map.invalidateSize(), 0);

    return {
      map,
      set: place,
      refresh: () => map.invalidateSize(),
    };
  }

  function summarise(found) {
    const where = found.short || found.state_name;
    if (!found.agency) {
      return `${where} — no agency on file, so a report here would be held.`;
    }
    const how = found.email ? found.email : "reports go through their web form";
    return `${where} · ${found.agency} · ${how}`;
  }

  function say(element, text, bad) {
    if (!element) return;
    element.textContent = text;
    element.hidden = false;
    element.classList.toggle("is-bad", Boolean(bad));
  }

  async function describe(response) {
    try {
      return (await response.json()).detail || `HTTP ${response.status}`;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  window.RoadCleanerMap = { attach };
})();
