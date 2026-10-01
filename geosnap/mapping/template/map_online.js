// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  // Check surroundings: places around a centre or along a shown route. A question
  // inside the circles queried at generation is answered from the embedded places (documented in
  // overpass_<stamp>.json); otherwise Overpass is asked live from this browser.
  // Results are listed in the Places window, sorted by distance. Nothing here is recorded.
  var G = window.GEOSNAP;
  var map = G.map;
  var payload = G.payload;
  var online = payload.online || {};
  var catalogue = G.PLACE_STYLES;
  var element = G.element;
  var embeddedRadius = online.overpass_radius_m || 250;
  var queriedAtGeneration = online.overpass_categories || [];
  var liveAvailable = !!(online.enabled && online.overpass_endpoint);
  var outputLimit = online.overpass_output_limit || 2000;
  var timeoutSeconds = online.overpass_timeout_seconds || 60;
  var MAX_ROUTE_VERTICES = 100;   // the route is simplified to at most this many vertices
  var MAX_CORRIDOR_M = 500;
  var MIN_RADIUS_M = 50;
  var MAX_RADIUS_M = 20000;
  var LISTED_ROWS = 200;          // table rows; the export holds all results
  var LIVE_COLOUR = "#555555";

  function byId(id) { return document.getElementById(id); }
  var status = byId("surroundings-status");
  var targetLine = byId("surroundings-target");
  var radiusInput = byId("surroundings-radius");
  var corridorInput = byId("surroundings-corridor");
  var runButton = byId("surroundings-run");
  var pickButton = byId("surroundings-pick");
  var routeButton = byId("surroundings-route");
  var routeSourceSelect = byId("surroundings-route-source");
  var nearestBox = byId("surroundings-nearest");
  var resultsBox = byId("surroundings-results");
  var sortRow = byId("surroundings-sort-row");
  var sortSelect = byId("surroundings-sort");
  var actionsRow = byId("surroundings-actions");
  radiusInput.value = String(embeddedRadius);

  var resultLayer = L.layerGroup().addTo(map);
  var outlineLayer = L.layerGroup().addTo(map);
  G.addLegendEntry("Check surroundings: live place (dashed badge) and checked area", { colour: LIVE_COLOUR, shape: "dashed", note: "live, not recorded" });

  // ---- embedded places and the circles they were queried in --------------------------------
  var embeddedEntries = [];
  Object.keys(payload.places || {}).forEach(function (category) {
    payload.places[category].forEach(function (place) {
      embeddedEntries.push({ category: category, name: place.name || null, lat: Number(place.lat), lon: Number(place.lon),
        osm_type: place.osm_type, osm_id: place.osm_id, tags: place.tags || {} });
    });
  });

  // The centres of the circles queried at generation, as the payload records them
  // (rebuilding them from the rounded stay durations could pick other circles).
  function generationAnchors() {
    return (online.overpass_anchors || []).map(function (anchor) { return { lat: anchor.lat, lon: anchor.lon }; });
  }
  // Without any embedded place the generation query failed or found nothing: no offline answer.
  var anchors = embeddedEntries.length && queriedAtGeneration.length ? generationAnchors() : [];
  G.surroundingsAnchors = anchors;

  // A generation answer cut at the output limit or ended by a runtime error is incomplete: it
  // never answers a check alone.
  var generationIncomplete = online.overpass_truncated === true ||
    (typeof online.overpass_remark === "string" && /runtime error/i.test(online.overpass_remark));
  var INCOMPLETE_NOTE = "INCOMPLETE: the generation query was cut off";
  // Every sample must lie deep enough inside one anchor circle that the checked circle around
  // it (radius) is contained; all chosen categories must have been queried at generation.
  function insideEmbeddedCircles(samples, radius, categories) {
    if (!anchors.length) { return false; }
    if (!categories.every(function (category) { return queriedAtGeneration.indexOf(category) >= 0; })) { return false; }
    return samples.every(function (sample) {
      return anchors.some(function (anchor) { return G.haversineMetres(sample[0], sample[1], anchor.lat, anchor.lon) + radius <= embeddedRadius; });
    });
  }
  function answerableOffline(samples, radius, categories) {
    return !generationIncomplete && insideEmbeddedCircles(samples, radius, categories);
  }

  // ---- geometry: local metric projection, route simplification and distances --------------
  var METRES_PER_DEGREE = 111320;
  function projector(referenceLat) {
    var scale = Math.cos(referenceLat * Math.PI / 180);
    return function (lat, lon) { return [lon * METRES_PER_DEGREE * scale, lat * METRES_PER_DEGREE]; };
  }
  function segmentDistance(p, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1];
    var lengthSquared = dx * dx + dy * dy;
    var t = lengthSquared > 0 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / lengthSquared)) : 0;
    var x = a[0] + t * dx - p[0], y = a[1] + t * dy - p[1];
    return Math.sqrt(x * x + y * y);
  }
  // Douglas-Peucker on projected coordinates; returns the kept indices.
  function simplifiedIndices(xy, tolerance) {
    var keep = xy.map(function () { return false; });
    keep[0] = keep[xy.length - 1] = true;
    var stack = [[0, xy.length - 1]];
    while (stack.length) {
      var span = stack.pop();
      var farthest = -1, farthestDistance = tolerance;
      for (var i = span[0] + 1; i < span[1]; i++) {
        var distance = segmentDistance(xy[i], xy[span[0]], xy[span[1]]);
        if (distance > farthestDistance) { farthest = i; farthestDistance = distance; }
      }
      if (farthest >= 0) {
        keep[farthest] = true;
        stack.push([span[0], farthest], [farthest, span[1]]);
      }
    }
    return xy.map(function (point, index) { return index; }).filter(function (index) { return keep[index]; });
  }
  // Positions between consecutive route points at most `spacing` apart (for the coverage test).
  function densified(latLngs, spacing) {
    var samples = [latLngs[0]];
    for (var i = 1; i < latLngs.length; i++) {
      var a = latLngs[i - 1], b = latLngs[i];
      var steps = Math.ceil(G.haversineMetres(a[0], a[1], b[0], b[1]) / spacing);
      for (var step = 1; step <= steps; step++) {
        samples.push([a[0] + (b[0] - a[0]) * step / steps, a[1] + (b[1] - a[1]) * step / steps]);
      }
    }
    return samples;
  }

  // A check: {kind: "circle", label, lat, lon, radius} or {kind: "route", label, sourceId, points,
  // vertices, tolerance, radius (= corridor width)}. measure(place) adds distance (and bearing or
  // the nearest route report) to a result entry.
  function measure(check, entry) {
    if (check.kind === "circle") {
      entry.distance = G.haversineMetres(check.lat, check.lon, entry.lat, entry.lon);
      entry.bearing = G.bearingDegrees(check.lat, check.lon, entry.lat, entry.lon);
      return entry;
    }
    var place = check.project(entry.lat, entry.lon);
    var best = Infinity, nearestIndex = 0, nearestPointDistance = Infinity;
    for (var i = 0; i < check.xy.length; i++) {
      var here = check.xy[i];
      var pointDistance = Math.sqrt(Math.pow(here[0] - place[0], 2) + Math.pow(here[1] - place[1], 2));
      if (pointDistance < nearestPointDistance) { nearestPointDistance = pointDistance; nearestIndex = i; }
      if (i > 0) { best = Math.min(best, segmentDistance(place, check.xy[i - 1], here)); }
    }
    entry.distance = best;
    entry.passed = check.points[nearestIndex];
    return entry;
  }

  // ---- Overpass (live) ------------------------------------------------------------------
  function aroundFilter(check) {
    var radius = Math.round(check.radius);
    if (check.kind === "circle") { return "(around:" + radius + "," + check.lat.toFixed(6) + "," + check.lon.toFixed(6) + ")"; }
    // Linestring form of `around` (Overpass API 0.7.55+): distance to the polyline.
    return "(around:" + radius + "," + check.vertices.map(function (vertex) { return vertex[0].toFixed(6) + "," + vertex[1].toFixed(6); }).join(",") + ")";
  }
  function buildQuery(categories, check) {
    var filter = aroundFilter(check);
    var lines = ["[out:json][timeout:" + timeoutSeconds + "];", "("];
    categories.forEach(function (category) {
      catalogue[category].selectors.forEach(function (selector) { lines.push("  nwr" + selector + filter + ";"); });
    });
    lines.push(");", "out center " + outputLimit + ";");
    return lines.join("\n");
  }
  // Canonical catalogue order restricted to the requested categories: the first matching rule wins.
  function classify(tags, categories) {
    for (var i = 0; i < categories.length; i++) {
      var rules = catalogue[categories[i]].rules;
      for (var j = 0; j < rules.length; j++) {
        if (rules[j][1].indexOf(tags[rules[j][0]]) >= 0) { return categories[i]; }
      }
    }
    return null;
  }
  function labelsOf(categories) {
    return categories.map(function (category) { return catalogue[category].label; }).join(", ");
  }
  // overpass-api.de answers browser requests without a Referer with HTTP 406 and no CORS headers
  // (the browser then reports a TypeError, "Failed to fetch"); a map opened from disk (file://)
  // never sends one, a map served by GEOSnap on http://127.0.0.1 does. Such a refusal is asked
  // once more at the fallback endpoint (online.overpass_fallback_endpoint), which accepts it.
  var openedFromDisk = location.protocol === "file:";
  var fallbackEndpoint = online.overpass_fallback_endpoint || null;
  // The public fallback is often busy: measured 36-47 s answers and one 504 only after 173 s
  // (2026-09-18), so it gets more time than the primary request.
  var FALLBACK_TIMEOUT_SECONDS = 180;
  var BUSY_STATUSES = { 429: true, 504: true };
  var RETRY_DELAY_SECONDS = 8;
  function hostOf(endpoint) {
    try { return new URL(endpoint).host; } catch (error) { return String(endpoint); }
  }
  function refusedWithoutReferer(error) {
    return error.status === 406 || error.name === "TypeError";
  }
  function fallbackAllowed(error) {
    return openedFromDisk && fallbackEndpoint && fallbackEndpoint !== online.overpass_endpoint && refusedWithoutReferer(error);
  }
  function refusalHint(error) {
    if (error.fallbackFailed) {
      return "Live check failed: " + hostOf(online.overpass_endpoint) + " refuses maps opened from disk and the fallback " + hostOf(fallbackEndpoint) +
        (error.name === "AbortError" ? " gave no answer within " + FALLBACK_TIMEOUT_SECONDS + " s" : " failed too (" + error.message + ")") +
        ". Open the map from GEOSnap (summary screen, key O). Checks from embedded data still work.";
    }
    if (openedFromDisk && refusedWithoutReferer(error)) {
      return "Live check failed: the Overpass server refused the request. Maps opened from disk send no Referer, which overpass-api.de requires. " +
        "Open the map from GEOSnap (summary screen, key O) or set online.overpass_browser_endpoint. Checks from embedded data still work.";
    }
    return "Live check failed: " + error.message + ". The Overpass server may be busy: try again in a minute.";
  }
  // One request with its own time limit; an HTTP error carries its status.
  function requestOnce(query, endpoint, limitSeconds) {
    var abort = new AbortController();
    var fetchTimeout = setTimeout(function () { abort.abort(); }, (limitSeconds || timeoutSeconds + 10) * 1000);
    return fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "data=" + encodeURIComponent(query),
      signal: abort.signal
    }).then(function (response) {
      if (!response.ok) {
        var httpError = new Error("HTTP " + response.status);
        httpError.status = response.status;
        throw httpError;
      }
      var contentLength = Number(response.headers.get("content-length"));
      if (contentLength > 50000000) { throw new Error("response too large"); }
      return response.json();
    }).then(function (result) {
      clearTimeout(fetchTimeout);
      return result;
    }, function (error) {
      clearTimeout(fetchTimeout);
      throw error;
    });
  }
  // Exactly one further request: at the fallback when a map opened from disk is refused
  // (HTTP 406 or a CORS/network error), otherwise at the same server after RETRY_DELAY_SECONDS
  // when it is busy (HTTP 504/429). The fallback's own failure ends the check.
  // notify(kind): "fallback" or "retry", for the status line.
  function fetchLive(check, categories, notify) {
    var query = buildQuery(categories, check);
    var usedFallback = false;
    return requestOnce(query, online.overpass_endpoint).catch(function (error) {
      if (fallbackAllowed(error)) {
        usedFallback = true;
        notify("fallback");
        return requestOnce(query, fallbackEndpoint, FALLBACK_TIMEOUT_SECONDS).catch(function (fallbackError) {
          fallbackError.fallbackFailed = true;
          throw fallbackError;
        });
      }
      if (!BUSY_STATUSES[error.status]) { throw error; }
      notify("retry");
      return new Promise(function (resolve) { setTimeout(resolve, RETRY_DELAY_SECONDS * 1000); }).then(function () { return requestOnce(query, online.overpass_endpoint); });
    }).then(function (result) {
      if (!Array.isArray(result.elements)) { throw new Error("unexpected response shape"); }
      var entries = [], malformed = 0;
      result.elements.slice(0, outputLimit).forEach(function (item) {
        var tags = item.tags || {};
        var category = classify(tags, categories);
        if (!category) { return; }
        var position = item.lat !== undefined ? item : item.center;
        if (!position || !isFinite(Number(position.lat)) || !isFinite(Number(position.lon))) {
          malformed += 1;
          return;
        }
        entries.push({ category: category, name: typeof tags.name === "string" ? tags.name : null, lat: Number(position.lat), lon: Number(position.lon),
          osm_type: String(item.type), osm_id: item.id, tags: tags });
      });
      return { entries: entries, malformed: malformed, truncated: result.elements.length >= outputLimit,
        remark: typeof result.remark === "string" && result.remark ? result.remark : null,
        endpoint: usedFallback ? fallbackEndpoint : online.overpass_endpoint, usedFallback: usedFallback };
    });
  }
  // "answered by overpass.private.coffee (fallback: overpass-api.de refuses maps opened from disk)"
  function answeredByText(endpoint, usedFallback) {
    return "answered by " + hostOf(endpoint) + (usedFallback ? " (fallback: " + hostOf(online.overpass_endpoint) + " refuses maps opened from disk)" : "");
  }

  // ---- answering ------------------------------------------------------------------------
  function osmKey(entry) { return entry.osm_type + "/" + entry.osm_id; }
  // Embedded places of the categories whose centre lies within the checked area; areas and
  // lines (ways/relations) whose centre lies up to the generation radius outside are kept and
  // marked, because their edge may reach inside (only a live answer tests the geometry).
  function embeddedWithin(check, categories) {
    var found = [];
    embeddedEntries.forEach(function (entry) {
      if (categories.indexOf(entry.category) < 0) { return; }
      var measured = measure(check, { category: entry.category, name: entry.name, lat: entry.lat, lon: entry.lon, osm_type: entry.osm_type, osm_id: entry.osm_id, tags: entry.tags, origin: "embedded" });
      var extended = measured.osm_type !== "node" && measured.distance <= check.radius + embeddedRadius;
      if (measured.distance <= check.radius || extended) { found.push(measured); }
    });
    return found;
  }
  function markCentreOutside(check, entries) {
    entries.forEach(function (entry) { entry.centreOutside = entry.distance > check.radius; });
    return entries;
  }
  // Live hits merged with the embedded places over OSM type/id; an embedded place keeps its
  // documented origin. The live answer tested the geometry, so an embedded area or line with its
  // centre outside stays only when Overpass returned it too.
  function mergedEntries(check, categories, liveEntries) {
    var byKey = {};
    var embedded = embeddedWithin(check, categories);
    embedded.forEach(function (entry) { byKey[osmKey(entry)] = entry; });
    var liveOnly = [];
    liveEntries.forEach(function (entry) {
      var known = byKey[osmKey(entry)];
      if (known) {
        known.origin = "embedded + live";
        return;
      }
      entry.origin = "live";
      liveOnly.push(measure(check, entry));
    });
    return embedded.filter(function (entry) { return entry.origin === "embedded + live" || entry.distance <= check.radius; }).concat(liveOnly);
  }

  var currentCheck = null;
  var answer = null;
  var running = false;

  function checkedCategories() {
    return G.activePlaceCategories().filter(function (category) { return catalogue[category]; });
  }
  function describeCheck(check) {
    return check.kind === "circle"
      ? check.label + " · " + G.formatDistance(check.radius) + " around " + check.lat.toFixed(5) + ", " + check.lon.toFixed(5)
      : check.label + " · corridor ± " + G.formatDistance(check.radius) + " along " + check.points.length + " shown reports (" + check.vertices.length + " vertices)";
  }

  function run(check) {
    currentCheck = check;
    targetLine.textContent = describeCheck(check);
    drawOutline(check);
    var categories = checkedCategories();
    if (!categories.length) {
      status.textContent = "Choose a profile or tick categories, then press Check.";
      return;
    }
    // Route samples a quarter corridor apart, so no piece between two anchor circles slips through.
    var samples = check.kind === "circle" ? [[check.lat, check.lon]] : densified(check.points.map(function (point) { return [point.lat, point.lon]; }), check.radius / 4);
    var queryTime = new Date().toISOString();
    var profile = G.placeProfileLabel();
    if (answerableOffline(samples, check.radius, categories)) {
      finish({ check: check, categories: categories, profile: profile, queryTime: queryTime, from: "embedded", skipped: [], notes: [],
        entries: markCentreOutside(check, embeddedWithin(check, categories)) });
      return;
    }
    var inside = insideEmbeddedCircles(samples, check.radius, categories);
    var reason = !anchors.length ? "no embedded places in this project"
      : !categories.every(function (category) { return queriedAtGeneration.indexOf(category) >= 0; }) ? "not all chosen categories were queried at generation"
      : inside ? "the generation query was cut off"
      : "the area is not inside the circles queried at generation";
    if (!liveAvailable && inside) {
      finish({ check: check, categories: categories, profile: profile, queryTime: queryTime, from: "embedded", skipped: [], notes: [INCOMPLETE_NOTE],
        entries: markCentreOutside(check, embeddedWithin(check, categories)) });
      return;
    }
    if (!liveAvailable) {
      clearResults();
      status.textContent = "Cannot be answered offline (" + reason + "), and online services are off.";
      return;
    }
    // Dense categories flood large areas: the catalogue caps the radius per category; a corridor
    // is compared by area (length × 2 × width against the circle of the category's radius).
    var skipped = categories.filter(function (category) {
      var limit = catalogue[category].max_radius_m;
      return check.kind === "circle" ? limit < check.radius : check.length * 2 * check.radius > Math.PI * limit * limit;
    });
    var liveCategories = categories.filter(function (category) { return skipped.indexOf(category) < 0; });
    var skippedNote = skipped.length ? " (skipped at this radius: " + labelsOf(skipped) + ")" : "";
    if (!liveCategories.length) {
      var embeddedOnly = embeddedWithin(check, skipped);
      embeddedOnly.forEach(function (entry) { entry.origin = "embedded only (skipped live at this radius)"; });
      finish({ check: check, categories: categories, profile: profile, queryTime: queryTime, from: "embedded", skipped: skipped,
        notes: ["no category can be checked live at this " + (check.kind === "circle" ? "radius" : "corridor size") + "; reduce it"].concat(generationIncomplete ? [INCOMPLETE_NOTE] : []),
        entries: markCentreOutside(check, embeddedOnly) });
      return;
    }
    running = true;
    runButton.disabled = routeButton.disabled = true;
    status.textContent = "Querying Overpass (" + reason + ")…" + skippedNote;
    fetchLive(check, liveCategories, function (kind) {
      status.textContent = kind === "fallback"
        ? hostOf(online.overpass_endpoint) + " refuses maps opened from disk: asking the fallback " + hostOf(fallbackEndpoint) + " once (waiting up to " + FALLBACK_TIMEOUT_SECONDS + " s)…" + skippedNote
        : "Overpass busy, retrying once in " + RETRY_DELAY_SECONDS + " s…" + skippedNote;
    }).then(function (live) {
      var notes = [];
      if (live.malformed) { notes.push(live.malformed + " malformed skipped"); }
      if (live.truncated) { notes.push("answer cut at the output limit of " + outputLimit + ": reduce the radius or the categories"); }
      // A runtime error remark (e.g. the server's time limit) means the answer is incomplete.
      if (live.remark) { notes.push((/runtime error/i.test(live.remark) ? "INCOMPLETE answer, " : "") + "Overpass remark: " + live.remark); }
      // The live corridor follows the simplified route; distances use every shown report.
      if (check.kind === "route" && check.tolerance > check.radius / 2) {
        notes.push("route simplified with ± " + Math.round(check.tolerance) + " m: the live corridor may miss places near sharp bends");
      }
      // Skipped categories keep their embedded hits, marked as such.
      var embeddedOnly = embeddedWithin(check, skipped);
      embeddedOnly.forEach(function (entry) { entry.origin = "embedded only (skipped live at this radius)"; });
      if (embeddedOnly.length && generationIncomplete) { notes.push(INCOMPLETE_NOTE); }
      finish({ check: check, categories: categories, profile: profile, queryTime: queryTime, from: "live", skipped: skipped, notes: notes, reason: reason,
        endpoint: live.endpoint, usedFallback: live.usedFallback, entries: markCentreOutside(check, mergedEntries(check, liveCategories, live.entries).concat(embeddedOnly)) });
    }).catch(function (error) {
      status.textContent = error.name === "AbortError" && !error.fallbackFailed
        ? "Live check timed out: no answer from Overpass within " + (timeoutSeconds + 10) + " s"
        : refusalHint(error);
    }).then(function () {
      running = false;
      runButton.disabled = false;
      G.refreshGate(routeButton);
    });
  }

  function finish(result) {
    answer = result;
    result.entries.sort(function (a, b) { return a.distance - b.distance; });
    var origin = result.from === "embedded"
      ? "answered from embedded data (see overpass_<stamp>.json), not recorded"
      : "live from Overpass, " + answeredByText(result.endpoint, result.usedFallback) + ", not recorded; embedded places merged by OSM id";
    status.textContent = (result.notes.indexOf(INCOMPLETE_NOTE) >= 0 ? INCOMPLETE_NOTE + " · " : "") + result.entries.length + " place(s) · " + origin +
      (result.skipped.length ? " · skipped live by the density rule: " + labelsOf(result.skipped) : "") +
      result.notes.filter(function (note) { return note !== INCOMPLETE_NOTE; }).map(function (note) { return " · " + note; }).join("");
    render();
  }

  // ---- presentation ---------------------------------------------------------------------
  function drawOutline(check) {
    outlineLayer.clearLayers();
    var style = { color: LIVE_COLOUR, dashArray: "4 6", weight: 2, fillOpacity: 0.04, interactive: false };
    if (check.kind === "circle") {
      L.circle([check.lat, check.lon], L.extend({ radius: check.radius }, style)).addTo(outlineLayer);
      L.circleMarker([check.lat, check.lon], { radius: 6, color: LIVE_COLOUR, fillColor: "#ffffff", fillOpacity: 1, interactive: false }).addTo(outlineLayer);
    } else {
      L.polyline(check.vertices, { color: LIVE_COLOUR, dashArray: "4 6", weight: 2, interactive: false }).addTo(outlineLayer);
    }
  }
  function clearResults() {
    answer = null;
    resultLayer.clearLayers();
    nearestBox.textContent = "";
    resultsBox.textContent = "";
    actionsRow.hidden = true;
    sortRow.hidden = true;
  }
  function styleOf(category) { return catalogue[category] || { emoji: "?", label: category }; }
  function directionText(entry) { return G.formatDistance(entry.distance) + " " + G.forecast.compassName(entry.bearing); }
  function passedText(entry) { return entry.passed ? G.localText(entry.passed.local.slice(0, 16), entry.passed.off).slice(11) : "-"; }
  function passedSeconds(entry) { return entry.passed ? entry.passed.utcSeconds : -Infinity; }
  function whereText(check, entry) {
    var text = check.kind === "circle" ? directionText(entry) : G.formatDistance(entry.distance) + " from the route, passed " + passedText(entry);
    return entry.centreOutside ? text + " (centre outside; edge may be closer)" : text;
  }

  function render() {
    resultLayer.clearLayers();
    nearestBox.textContent = "";
    resultsBox.textContent = "";
    var check = answer.check;
    var byCategory = {};
    answer.entries.forEach(function (entry) { (byCategory[entry.category] = byCategory[entry.category] || []).push(entry); });
    // Nearest hit and count per category, in catalogue order.
    answer.categories.forEach(function (category) {
      var style = styleOf(category);
      var hits = byCategory[category] || [];
      var line = element("div", null, "surroundings-nearest-line");
      line.appendChild(element("span", style.emoji, "place-emoji"));
      line.appendChild(element("span", hits.length
        ? "nearest " + style.label + ": " + whereText(check, hits[0]) + (hits[0].name ? " (" + hits[0].name + ")" : "") + " · " + hits.length + " in total"
        : style.label + ": none within " + G.formatDistance(check.radius)));
      if (hits.length) {
        line.classList.add("clickable");
        line.addEventListener("click", function () { centreOn(hits[0]); });
      }
      nearestBox.appendChild(line);
    });
    var rows = answer.entries.slice();
    if (check.kind === "route" && sortSelect.value === "passed") {
      rows.sort(function (a, b) { return passedSeconds(a) < passedSeconds(b) ? -1 : passedSeconds(a) > passedSeconds(b) ? 1 : 0; });
    }
    var headers = check.kind === "circle" ? ["", "Category", "Name", "Distance", "Direction", "Origin"] : ["", "Category", "Name", "Distance", "Passed", "Origin"];
    var shown = rows.slice(0, LISTED_ROWS).map(function (entry) {
      var style = styleOf(entry.category);
      var distance = G.formatDistance(entry.distance) + (entry.centreOutside ? " (centre)" : "");
      return { cells: [style.emoji, style.label, entry.name || "(unnamed)", distance,
        check.kind === "circle" ? G.forecast.compassName(entry.bearing) : passedText(entry), entry.origin], entry: entry };
    });
    resultsBox.appendChild(G.table(headers, shown, function (row) { centreOn(row.entry); }));
    if (rows.length > LISTED_ROWS) { resultsBox.appendChild(element("p", "… " + (rows.length - LISTED_ROWS) + " more in the export.", "note")); }
    // Live-only hits get their own badges; embedded ones show through the ticked category layers.
    answer.entries.forEach(function (entry) {
      if (entry.origin !== "live") { return; }
      G.placeMarker({ name: entry.name, lat: entry.lat, lon: entry.lon, osm_type: entry.osm_type, osm_id: entry.osm_id, tags: entry.tags,
        far_centre: entry.distance > 3 * check.radius }, entry.category, true).addTo(resultLayer);
    });
    actionsRow.hidden = false;
    sortRow.hidden = check.kind !== "route";
  }
  function centreOn(entry) { map.setView([entry.lat, entry.lon], Math.max(map.getZoom(), 17)); }

  // ---- entry points -------------------------------------------------------------------
  function openPlacesWindow() {
    var viewBox = byId("view-places");
    if (!viewBox.checked) {
      viewBox.checked = true;
      viewBox.dispatchEvent(new Event("change"));
    }
  }
  function clampedRadius(value) { return Math.min(MAX_RADIUS_M, Math.max(MIN_RADIUS_M, Math.round(Number(value) || embeddedRadius))); }

  // target: {label, lat, lon, radius, profile?} from a context action or the crystal ball.
  G.checkSurroundings = function (target) {
    if (running) {
      status.textContent = "A live check is still running: wait for its answer.";
      return;
    }
    openPlacesWindow();
    // Started from the crystal ball: its window folds to the title bar and Places comes to the front.
    var placesWindow = byId("places-window");
    var crystalWindow = byId("crystal-window");
    G.setWindowCollapsed(placesWindow, false);
    placesWindow.classList.toggle("front", !!target.fromCrystalBall);
    if (target.fromCrystalBall && !crystalWindow.hidden) { G.setWindowCollapsed(crystalWindow, true); }
    if (target.profile) { G.applyPlaceProfile(target.profile); }
    var radius = clampedRadius(target.radius);
    radiusInput.value = String(radius);
    clearResults();
    run({ kind: "circle", label: target.label + (radius < target.radius ? " (radius capped at " + G.formatDistance(MAX_RADIUS_M) + ")" : ""), lat: target.lat, lon: target.lon, radius: radius });
  };

  runButton.addEventListener("click", function () {
    if (!currentCheck) {
      status.textContent = "Choose a centre first: a \"Check surroundings\" popup action or \"Check a point on the map…\".";
      return;
    }
    clearResults();
    if (currentCheck.kind === "circle") {
      // Every run works on its own copy: a later radius edit never changes a running check.
      run(L.extend({}, currentCheck, { radius: clampedRadius(radiusInput.value) }));
    } else {
      checkRoute();
    }
  });
  radiusInput.addEventListener("change", function () {
    if (currentCheck && currentCheck.kind === "circle" && !running) {
      currentCheck = L.extend({}, currentCheck, { radius: clampedRadius(radiusInput.value) });
      targetLine.textContent = describeCheck(currentCheck);
      drawOutline(currentCheck);
    }
  });

  var armed = false;
  pickButton.addEventListener("click", function () {
    armed = true;
    status.textContent = "Click on the map to set the centre.";
  });
  map.on("click", function (event) {
    if (!armed || running) { return; }
    armed = false;
    clearResults();
    run({ kind: "circle", label: "Map point", lat: event.latlng.lat, lon: event.latlng.lng, radius: clampedRadius(radiusInput.value) });
  });

  // ---- along the shown route of one source --------------------------------------------
  function fillRouteSources() {
    var chosen = routeSourceSelect.value;
    routeSourceSelect.textContent = "";
    G.visibleSources().forEach(function (source) {
      var option = element("option", G.sourceLabel(source.id));
      option.value = String(source.id);
      routeSourceSelect.appendChild(option);
    });
    if (chosen && routeSourceSelect.querySelector('option[value="' + Number(chosen) + '"]')) { routeSourceSelect.value = chosen; }
    routeSourceSelect.hidden = G.visibleSources().length < 2;
  }
  fillRouteSources();
  G.onSourcesChange(fillRouteSources);

  function checkRoute() {
    var sourceId = Number(routeSourceSelect.value);
    var points = G.lastVisibleBySource[sourceId] || [];
    if (points.length < 2) {
      status.textContent = "The shown route of this source has fewer than two reports.";
      return;
    }
    var width = Math.min(MAX_CORRIDOR_M, Math.max(10, Math.round(Number(corridorInput.value) || 100)));
    corridorInput.value = String(width);
    var project = projector(points[0].lat);
    var xy = points.map(function (point) { return project(point.lat, point.lon); });
    // Tolerance doubles from a quarter of the width until at most MAX_ROUTE_VERTICES remain.
    var tolerance = width / 4;
    var kept = simplifiedIndices(xy, tolerance);
    while (kept.length > MAX_ROUTE_VERTICES) {
      tolerance *= 2;
      kept = simplifiedIndices(xy, tolerance);
    }
    var length = 0;
    for (var i = 1; i < points.length; i++) { length += G.haversineMetres(points[i - 1].lat, points[i - 1].lon, points[i].lat, points[i].lon); }
    run({ kind: "route", label: "Route · " + G.sourceName(sourceId), sourceId: sourceId, points: points, xy: xy, project: project, radius: width, tolerance: tolerance, length: length,
      vertices: kept.map(function (index) { return [points[index].lat, points[index].lon]; }) });
  }
  routeButton.addEventListener("click", function () {
    if (running) { return; }
    clearResults();
    checkRoute();
  });
  sortSelect.addEventListener("change", function () { if (answer) { render(); } });
  byId("surroundings-clear").addEventListener("click", function () {
    clearResults();
    outlineLayer.clearLayers();
    currentCheck = null;
    status.textContent = "";
    targetLine.textContent = "No centre chosen.";
  });

  // ---- derived exports ------------------------------------------------------------------
  function checkMetadata() {
    var check = answer.check;
    return {
      check: check.kind === "circle" ? "circle" : "route corridor",
      centre_lat: check.kind === "circle" ? check.lat : null,
      centre_lon: check.kind === "circle" ? check.lon : null,
      radius_m: check.radius,
      route_source: check.kind === "route" ? G.sourceName(check.sourceId) : null,
      label: check.label,
      profile: answer.profile,
      categories: answer.categories.join(" "),
      query_time_utc: answer.queryTime,
      answered_from: answer.from === "embedded" ? "embedded data (overpass_<stamp>.json)" : "live Overpass",
      endpoint: answer.from === "embedded" ? "none (no request)" : answer.endpoint,
      recorded: "not recorded"
    };
  }
  byId("surroundings-export-csv").addEventListener("click", function () {
    if (!answer) { return; }
    var metadata = checkMetadata();
    var metadataKeys = Object.keys(metadata);
    var header = metadataKeys.concat(["category", "category_label", "name", "osm_type", "osm_id", "latitude", "longitude", "distance_m", "bearing_deg", "direction",
      "centre_outside", "passed_local", "origin"]);
    var entries = answer.entries.length ? answer.entries : [null];
    var rows = entries.map(function (entry) {
      var cells = metadataKeys.map(function (key) { return metadata[key]; });
      cells = cells.concat(entry === null ? ["", "", "(no results)", "", "", "", "", "", "", "", "", "", ""] : [
        entry.category, styleOf(entry.category).label, entry.name, entry.osm_type, entry.osm_id, entry.lat, entry.lon, Math.round(entry.distance),
        entry.bearing === undefined ? "" : Math.round(entry.bearing), entry.bearing === undefined ? "" : G.forecast.compassName(entry.bearing),
        entry.centreOutside ? "yes" : "no", entry.passed ? G.localIso(entry.passed.local, entry.passed.off) : "", entry.origin]);
      return cells.map(G.csvCell).join(",");
    });
    G.downloadBlob(G.derivedName("csv", "surroundings"), new Blob(["﻿" + [header.join(",")].concat(rows).join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" }));
    status.textContent = "Surroundings CSV exported (derived, not recorded): import it as text, do not open it by double-click";
  });
  byId("surroundings-export-geojson").addEventListener("click", function () {
    if (!answer) { return; }
    var check = answer.check;
    var features = answer.entries.map(function (entry) {
      return { type: "Feature", geometry: { type: "Point", coordinates: [entry.lon, entry.lat] },
        properties: { category: entry.category, category_label: styleOf(entry.category).label, name: entry.name, osm_type: entry.osm_type, osm_id: entry.osm_id,
          distance_m: Math.round(entry.distance), bearing_deg: entry.bearing === undefined ? null : Math.round(entry.bearing),
          centre_outside: !!entry.centreOutside, passed_local: entry.passed ? G.localIso(entry.passed.local, entry.passed.off) : null, origin: entry.origin } };
    });
    features.push(check.kind === "circle"
      ? { type: "Feature", geometry: { type: "Point", coordinates: [check.lon, check.lat] }, properties: { role: "checked centre", radius_m: check.radius } }
      : { type: "Feature", geometry: { type: "LineString", coordinates: check.vertices.map(function (vertex) { return [vertex[1], vertex[0]]; }) },
          properties: { role: "checked corridor (simplified route)", corridor_m: check.radius, simplification_m: Math.round(check.tolerance) } });
    var collection = { type: "FeatureCollection", features: features,
      properties: { derived: true, project: payload.project, surroundings: checkMetadata(), view: G.filterDescription() } };
    G.downloadBlob(G.derivedName("geojson", "surroundings"), new Blob([JSON.stringify(collection)], { type: "application/geo+json" }));
  });

  status.textContent = liveAvailable
    ? (anchors.length ? "" : "No embedded places: every check asks Overpass live (not recorded).")
    : (anchors.length ? "Online services are off: only checks inside the circles queried at generation can be answered." : "Online services are off and no places are embedded: this map cannot check surroundings.");
})();
