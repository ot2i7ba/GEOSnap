// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  // Crystal ball window: reference time, the estimate from map_forecast_model.js (ranking over
  // known places, rings, sector, transitions, dwell, silence, motion) and its map layer.
  // Only "Record search area" writes anything: it sends the current estimate as a search area
  // record to the loopback server of a map opened from GEOSnap, which validates
  // it and writes the files itself.
  var G = window.GEOSNAP;
  var map = G.map;
  var payload = G.payload;
  // The estimate runs on one source at a time: its analysis document.
  var selectedSourceId = null;
  var analysis = null;
  var windowElement = document.getElementById("crystal-window");
  var notice = document.getElementById("crystal-notice");
  var controls = document.getElementById("crystal-controls");
  var resultBox = document.getElementById("crystal-result");
  var showBox = document.getElementById("crystal-show");
  var recencyOption = document.getElementById("crystal-recency-option");
  var recencyBox = document.getElementById("crystal-recency");
  var corridorsOption = document.getElementById("crystal-corridors-option");
  var corridorsBox = document.getElementById("crystal-corridors");
  var layer = L.layerGroup();
  var acknowledged = false;
  var RING_STYLE = {
    p50: { colour: "#6a1b9a", opacity: 0.9, fill: 0.16, label: "50 %" },
    p80: { colour: "#8e44ad", opacity: 0.8, fill: 0.10, label: "80 %" },
    p95: { colour: "#b084cc", opacity: 0.7, fill: 0.06, label: "95 %" }
  };
  var BADGE_TEXT = { weak: "weak", medium: "medium", strong: "strong" };
  var ROSE_COLOUR = "#00838f";
  var CONE_COLOUR = "#37474f";
  var TENDENCY_COLOUR = "#e65100";
  var TENDENCY_FILL = "#ffb74d";
  var CORRIDOR_COLOURS = ["#00838f", "#c2185b", "#6d4c41", "#2e7d32", "#ef6c00"];
  // What the last drawing showed, for G.crystalBallDescription.
  var drawn = { rose: false, tendency: false, corridors: false, cone: false };

  var element = G.element;
  var table = G.table;
  var fittedExtent = 0;
  var shownReference = null;
  var forecastContext = null;
  function percent(fraction) { return Math.round(fraction * 100) + " %"; }
  // ---- reference time: display zone <-> UTC through payload.zone_transitions ---------
  // A repeated wall time means its earlier occurrence unless only the later one lies after
  // the last report; a skipped wall time means the next valid instant.
  function utcSecondsOf(local) {
    var earlier = G.localToUtcSeconds(local, false);
    var later = G.localToUtcSeconds(local, true);
    return earlier <= Date.parse(analysis.last.utc) / 1000 && later > earlier ? later : earlier;
  }
  function localNow() { return G.utcToLocal(Date.now() / 1000).local.slice(0, 16); }

  function analysedPoints() {
    var maxAccuracy = analysis.parameters.max_accuracy_m;
    return G.allPoints.filter(function (point) { return point.s === selectedSourceId && point.radius <= maxAccuracy && !G.leftOutByMethod(point); });
  }
  // The last known position with the factor to its uncertainty radius; the factor is one per
  // source, so any record of the source gives it when thinning left the last one out.
  function lastWithScale() {
    var record = G.pointAt(selectedSourceId, analysis.last.line) ||
      G.allPoints.filter(function (point) { return point.s === selectedSourceId; })[0];
    var last = {};
    Object.keys(analysis.last).forEach(function (key) { last[key] = analysis.last[key]; });
    last.asc = record && typeof record.asc === "number" ? record.asc : 1;
    return last;
  }

  // ---- map layer ---------------------------------------------------------------------
  function sectorLatLngs(centre, radius, bearing, halfAngle) {
    var toRad = Math.PI / 180;
    var latLngs = [[centre.lat, centre.lon]];
    var steps = 24;
    for (var i = 0; i <= steps; i++) {
      var angle = (bearing - halfAngle + (2 * halfAngle) * i / steps) * toRad;
      var dLat = radius * Math.cos(angle) / 111320;
      var dLon = radius * Math.sin(angle) / (111320 * Math.cos(centre.lat * toRad));
      latLngs.push([centre.lat + dLat, centre.lon + dLon]);
    }
    return latLngs;
  }
  // Point `distance` metres from `centre` along `bearing` (flat approximation, as the sectors).
  function offsetLatLng(centre, distance, bearing) {
    var angle = bearing * Math.PI / 180;
    return [centre.lat + distance * Math.cos(angle) / 111320, centre.lon + distance * Math.sin(angle) / (111320 * Math.cos(centre.lat * Math.PI / 180))];
  }
  // interactive: true for labels that open a context popup (candidates, rings).
  function labelMarker(latLng, className, text, size, interactive) {
    var icon = L.divIcon({
      className: "", html: "",
      iconSize: size ? [size, size] : null,
      iconAnchor: size ? [size / 2, size / 2] : null
    });
    var marker = L.marker(latLng, { icon: icon, interactive: !!interactive });
    marker.on("add", function () {
      var node = marker.getElement();
      if (!node) { return; }
      node.textContent = "";
      node.appendChild(element("div", text, className));
    });
    return marker;
  }

  // Sector names for 8 or 16 sectors (the model's compassName knows 8).
  var COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  function sectorName(bearing, count) {
    return count === 16 ? COMPASS_16[Math.round(bearing / 22.5) % 16] : G.forecast.compassName(bearing);
  }
  function roseSectorLabel(sector, rose) {
    return sectorName(sector.bearing, rose.sectors.length) + " · " + sector.days + " of " + rose.leavingDays + " d";
  }
  // Wedges with reach80 as radius (sectors with fewer than 3 days carry none) and opacity
  // proportional to the sector probability. Returns whether a wedge was drawn.
  function drawRose(rose, centre) {
    var halfAngle = 180 / rose.sectors.length;
    var highest = Math.max.apply(null, rose.sectors.map(function (sector) { return sector.probability; }));
    var any = false;
    rose.sectors.forEach(function (sector) {
      if (sector.reach80 === null || !(sector.reach80 > 0)) { return; }
      any = true;
      L.polygon(sectorLatLngs(centre, sector.reach80, sector.bearing, halfAngle), {
        color: ROSE_COLOUR, weight: 1, opacity: 0.8, fillColor: ROSE_COLOUR, fillOpacity: 0.08 + 0.5 * sector.probability / highest, interactive: false
      }).addTo(layer);
      labelMarker(offsetLatLng(centre, 1.12 * sector.reach80, sector.bearing), "crystal-rose-label", roseSectorLabel(sector, rose)).addTo(layer);
    });
    return any;
  }
  // Identical bearings give a confidence interval of 0°; the drawn sector keeps a visible width.
  var MIN_SECTOR_HALF_ANGLE = 3;
  // Established: needle and 95 % confidence sector; weak lean or opposite directions: dashed
  // needles; sparse: nothing. Reach: r80. Returns whether anything was drawn.
  function drawTendency(tendency, centre, reach) {
    var bearings = [];
    var pair = tendency.oppositeDirections;
    var established = tendency.evidence === "established";
    if (pair && isFinite(pair[0]) && isFinite(pair[1]) && pair[0] !== null && pair[1] !== null) {
      bearings = pair;
    } else if (tendency.evidence !== "sparse" && tendency.bearing !== null && isFinite(tendency.bearing)) {
      bearings = [tendency.bearing];
      if (established && tendency.confidenceHalfAngle !== null && isFinite(tendency.confidenceHalfAngle)) {
        L.polygon(sectorLatLngs(centre, reach, tendency.bearing, Math.max(MIN_SECTOR_HALF_ANGLE, Math.min(180, tendency.confidenceHalfAngle))), {
          color: TENDENCY_COLOUR, weight: 1.5, dashArray: "6 4", fillColor: TENDENCY_FILL, fillOpacity: 0.25, interactive: false
        }).addTo(layer);
      }
    }
    bearings.forEach(function (bearing) {
      L.polyline([[centre.lat, centre.lon], offsetLatLng(centre, reach, bearing)], {
        color: TENDENCY_COLOUR, weight: established ? 3 : 2, opacity: established ? 0.9 : 0.6, dashArray: established ? null : "6 6", interactive: false
      }).addTo(layer);
    });
    return bearings.length > 0;
  }
  function placeLabel(address, key) { return addressText(address) || "known place " + key; }
  // Map labels keep the first two parts of an address ("45, Bonnstraße").
  function shortPlaceLabel(address, key) {
    var text = addressText(address);
    return text ? text.split(", ").slice(0, 2).join(", ") : "known place " + key;
  }
  function totalDepartures(corridor) { return corridor.share > 0 ? Math.round(corridor.departures / corridor.share) : corridor.departures; }
  function corridorLabel(corridor, short) {
    return (short ? shortPlaceLabel(corridor.address, corridor.destinationKey) : placeLabel(corridor.address, corridor.destinationKey)) +
      " · " + corridor.departures + " of " + totalDepartures(corridor) + " departures";
  }
  function corridorBounds(corridor) {
    var bounds = L.latLngBounds([[corridor.lat, corridor.lon], [analysis.last.lat, analysis.last.lon]]);
    corridor.trips.concat(corridor.dashedTrips).forEach(function (trip) { trip.forEach(function (position) { bounds.extend(position); }); });
    return bounds;
  }
  // Earlier trips to one destination; trips with a silence inside are dashed. The model caches
  // the corridors per context: they are read, never changed.
  function drawCorridor(corridor, colour) {
    var label = corridorLabel(corridor, true);
    function centre() { map.fitBounds(corridorBounds(corridor), { padding: [30, 30] }); }
    [[corridor.trips, null], [corridor.dashedTrips, "6 6"]].forEach(function (group) {
      group[0].forEach(function (trip) {
        L.polyline(trip, { color: colour, weight: 3, opacity: 0.7, dashArray: group[1] })
          .bindTooltip(G.textElement(label), { sticky: true }).on("click", centre).addTo(layer);
      });
    });
    var marker = labelMarker([corridor.lat, corridor.lon], "crystal-corridor-label", label, null, true);
    marker.on("add", function () {
      var node = marker.getElement();
      if (node && node.firstChild) { node.firstChild.style.borderColor = colour; }
    });
    marker.on("click", centre).addTo(layer);
  }

  function drawEstimate(estimate, rankedRows) {
    layer.clearLayers();
    drawn = { rose: false, tendency: false, corridors: false, cone: false };
    if (!showBox.checked || windowElement.hidden) {
      if (map.hasLayer(layer)) { map.removeLayer(layer); }
      return;
    }
    var centre = { lat: analysis.last.lat, lon: analysis.last.lon };
    var sourceName = G.sourceName(selectedSourceId);
    if (estimate.radii) {
      // Clicking a ring label offers a check of each ring's area.
      var ringTargets = ["p50", "p80", "p95"].map(function (key) {
        var target = G.checkTarget("Crystal ball " + RING_STYLE[key].label + " ring · " + sourceName, centre.lat, centre.lon, estimate.radii[key]);
        target.fromCrystalBall = true;
        target.action = "Check the " + RING_STYLE[key].label + " ring";
        return target;
      });
      ["p95", "p80", "p50"].forEach(function (key) {
        var style = RING_STYLE[key];
        var radius = estimate.radii[key];
        L.circle([centre.lat, centre.lon], { radius: radius, color: style.colour, opacity: style.opacity, weight: 2, fillColor: style.colour, fillOpacity: style.fill, interactive: false }).addTo(layer);
        G.bindContextActions(labelMarker([centre.lat + radius / 111320, centre.lon], "crystal-ring-label", style.label + " · " + G.formatDistance(radius), null, G.surroundingsAvailable),
          function () { return { title: "Crystal ball rings · " + sourceName, targets: ringTargets }; }).addTo(layer);
      });
    }
    if (estimate.rose) {
      drawn.rose = drawRose(estimate.rose, centre);
    }
    // The tendency as in the window chart; without one the rose supersedes the single sector.
    var tendency = tendencyOf(estimate);
    if (tendency && estimate.radii) {
      drawn.tendency = drawTendency(tendency, centre, estimate.radii.p80);
    } else if (!drawn.rose && estimate.direction && estimate.direction.strength !== "none" && estimate.radii) {
      L.polygon(sectorLatLngs(centre, estimate.radii.p80, estimate.direction.bearing, estimate.direction.halfAngle), {
        color: TENDENCY_COLOUR, weight: 2, dashArray: "6 4", fillColor: TENDENCY_FILL, fillOpacity: 0.25, interactive: false
      }).addTo(layer);
    }
    if (estimate.cone) {
      L.polygon(sectorLatLngs(centre, estimate.cone.radius, estimate.cone.bearing, estimate.cone.halfAngle), {
        color: CONE_COLOUR, weight: 2, dashArray: "8 6", fillColor: CONE_COLOUR, fillOpacity: 0.05, interactive: false
      }).addTo(layer);
      labelMarker(offsetLatLng(centre, estimate.cone.radius, estimate.cone.bearing), "crystal-cone-label", "extrapolation · " + estimate.cone.compass + " · " + G.formatDistance(estimate.cone.radius)).addTo(layer);
      drawn.cone = true;
    }
    if (corridorsBox.checked && estimate.corridors.length) {
      estimate.corridors.forEach(function (corridor, index) { drawCorridor(corridor, CORRIDOR_COLOURS[index % CORRIDOR_COLOURS.length]); });
      drawn.corridors = true;
    }
    if (estimate.destinations) {
      estimate.destinations.rows.forEach(function (row, index) {
        labelMarker([row.lat, row.lon], "crystal-destination", String(index + 1), 20).addTo(layer);
      });
    }
    rankedRows.forEach(function (entry) {
      if (entry.row.kind !== "place") { return; }
      // Weak basis: faded marker, rank only.
      var weak = isWeakBasis(estimate);
      var text = String(entry.rank) + (estimate.percentAvailable && !weak ? " · " + percent(entry.row.probability) : "");
      var row = entry.row;
      G.bindContextActions(labelMarker([row.lat, row.lon], "crystal-candidate" + (weak ? " weak" : ""), text, 20, G.surroundingsAvailable), function () {
        var label = "Crystal ball candidate " + entry.rank + " · " + sourceName;
        var candidateTarget = G.checkTarget(label, row.lat, row.lon, 0);
        candidateTarget.fromCrystalBall = true;
        return { title: label + " · " + placeName(row), targets: [candidateTarget] };
      }).addTo(layer);
    });
    if (!map.hasLayer(layer)) { layer.addTo(map); }
    // Centre on the rings only when their extent changed, so edits do not keep moving the map.
    var extent = estimate.radii ? Math.round(estimate.radii.p95) : 0;
    if (extent && extent !== fittedExtent) {
      map.fitBounds(L.latLng(centre.lat, centre.lon).toBounds(2 * extent), { padding: [20, 20] });
    }
    fittedExtent = extent;
  }

  // ---- text pieces -------------------------------------------------------------------
  var TOP_PLACES = 3;
  function addressText(address) { return address && address.display_name ? address.display_name : null; }
  function bearingText(bearing) { return G.forecast.compassName(bearing) + " " + (Math.round(bearing) % 360) + "°"; }
  function placeName(row) {
    if (row.kind === "elsewhere") { return "Elsewhere / moving"; }
    var name = addressText(row.address) || ("known place " + row.key + " (" + row.lat.toFixed(5) + ", " + row.lon.toFixed(5) + ")");
    return row.kind === "here" ? "Still here: " + name : name;
  }
  // The window names places by the first two parts of the address; tables and records keep the full one.
  function shortPlaceName(row) {
    if (row.kind === "elsewhere") { return "Elsewhere / moving"; }
    var name = shortPlaceLabel(row.address, row.key);
    return row.kind === "here" ? "Still here (" + name + ")" : name;
  }
  function estimateText(estimate, row) {
    if (!estimate.percentAvailable) { return "–"; }
    var text = "≈ " + percent(row.probability);
    if (row.interval) { text += " (" + Math.round(row.interval.low * 100) + "–" + Math.round(row.interval.high * 100) + " %)"; }
    return text;
  }
  function presenceText(row) {
    if (row.kind === "elsewhere") { return "-"; }
    var parts = [];
    if (row.usualPresence) {
      parts.push("usually " + row.usualPresence.arrive + "–" + row.usualPresence.leave + (row.usualPresence.dayClass ? " (" + row.usualPresence.dayClass + "s)" : "") + ", " + row.usualPresence.visits + " visits");
    } else {
      parts.push(row.visits + " visit" + (row.visits === 1 ? "" : "s"));
    }
    if (row.lastVisitLocal && row.kind !== "here") { parts.push("last " + row.lastVisitLocal.slice(0, 16).replace("T", " ")); }
    return parts.join("; ");
  }
  function categoryLabel(category) {
    var style = G.PLACE_STYLES && G.PLACE_STYLES[category];
    return style && style.label ? style.label : category;
  }
  // Overpass context: counts per category within overpass_radius_m of the position, from the
  // places embedded at generation, or null when none lies there.
  function surroundingsCounts(lat, lon) {
    var radius = overpassRadius();
    var counts = [];
    Object.keys(payload.places || {}).forEach(function (category) {
      var count = payload.places[category].filter(function (place) {
        return G.forecast.haversineMetres(lat, lon, place.lat, place.lon) <= radius;
      }).length;
      if (count) { counts.push({ label: categoryLabel(category), count: count }); }
    });
    if (!counts.length) { return null; }
    counts.sort(function (a, b) { return b.count - a.count; });
    return counts.map(function (entry) { return entry.count + " × " + entry.label; }).join(", ");
  }
  function overpassRadius() { return (payload.online && payload.online.overpass_radius_m) || 250; }
  // The payload does not carry the query anchors: without hits a place reads "no embedded
  // places" when any were embedded and "no surroundings data" when none were.
  function noSurroundingsText() {
    return G.embeddedPlaceTotal ? "no embedded places within " + overpassRadius() + " m" : "no surroundings data";
  }
  function ringsText(estimate) {
    return "r50 " + G.formatDistance(estimate.radii.p50) + " · r80 " + G.formatDistance(estimate.radii.p80) + " · r95 " + G.formatDistance(estimate.radii.p95);
  }
  // r95 from fewer windows than it needs is the farthest reach seen so far.
  function r95IsMaximumObserved(estimate) { return !!(estimate.rings && estimate.rings.r95IsMaximumObserved); }
  // What the rings mean, and why two of them can coincide (compared on 1 m).
  // geosnap/project/search_area.py rings_explanation words the search area sheet the same way.
  function ringsExplanation(estimate) {
    var radii = estimate.radii;
    var parts = ["Of " + estimate.windowCount + " comparable windows of " + G.formatDuration(estimate.windowSeconds / 60) + ", 50 % stayed within " +
      G.formatDistance(radii.p50) + ", 80 % within " + G.formatDistance(radii.p80) + ", 95 % within " + G.formatDistance(radii.p95) +
      " (farthest distance reached, plus the uncertainty radius of the last report)."];
    if (r95IsMaximumObserved(estimate)) {
      parts.push("With " + estimate.windowCount + " windows r95 is the farthest reach observed: about " + estimate.windowCount + " of " + (estimate.windowCount + 1) + " new days stay inside it.");
    }
    var sameUpper = Math.round(radii.p80) === Math.round(radii.p95);
    var sameLower = Math.round(radii.p50) === Math.round(radii.p80);
    if (sameUpper && sameLower) {
      parts.push("r50 = r80 = r95: in the upper half of the windows the farthest reach was the same distance (typically one destination). More comparable days would separate them.");
    } else if (sameUpper) {
      parts.push("r80 = r95: in the upper fifth of the windows the farthest reach was the same distance (typically one destination). More comparable days would separate them.");
    } else if (sameLower) {
      parts.push("r50 = r80: between the middle and the upper fifth of the windows the farthest reach was the same distance (typically one destination). More comparable days would separate them.");
    }
    return parts.join(" ");
  }
  function stateLabel(estimate, day) {
    if (day.stateKey === "silent") { return "silent (gap)"; }
    if (day.stateKey === G.forecast.ELSEWHERE) { return "elsewhere / moving"; }
    var name = addressText(day.address) || "known place " + day.stateKey;
    return estimate.lastPlace && day.stateKey === estimate.lastPlace.key ? "still here (" + name + ")" : name;
  }

  // A backtest on real data (sparse KML over 19 years): the routine levels S4/S5 hit 0-31 % and
  // lost to "still at the last known position", so a weak basis names no "most likely".
  // Dense GPS logs agree for the first hour (S4/S5 top-1 0.27-0.40 vs 0.50-0.71 for staying).
  function isRoutineLevel(estimate) { return !!estimate.ladder && (estimate.ladder.level === "S4" || estimate.ladder.level === "S5"); }
  function isWeakBasis(estimate) {
    return !!estimate.ranking && (isRoutineLevel(estimate) || (estimate.confidence && estimate.confidence.level === "weak"));
  }
  function weakRankingHeading(estimate) {
    return isRoutineLevel(estimate) ? "Routine at this time of day (weak evidence)" : "Comparable days at this place (weak evidence)";
  }

  // The headline in two parts: the statement and its basis. nameOf words the top place.
  function headlineParts(estimate, nameOf) {
    if (!estimate.ranking) { return { main: "No ranking: " + estimate.rankingNote + ".", basis: null }; }
    if (isWeakBasis(estimate)) {
      return {
        main: "Weak basis: search from the last known position first",
        basis: isRoutineLevel(estimate) ? "fewer than 3 comparable days at this place" : "only " + estimate.ladder.days + " comparable days at this place"
      };
    }
    var top = estimate.ranking[0];
    var figure = estimate.percentAvailable ? "≈ " + percent(top.probability) : top.days + " of " + top.ofDays + " days";
    var dayNoun = estimate.ladder.dayClass === "weekday" ? "weekdays" : estimate.ladder.dayClass === "weekend" ? "weekend days" : "days";
    return {
      main: "Most likely: " + nameOf(top) + " (" + figure + ")",
      basis: estimate.ladder.days + " comparable " + dayNoun + (estimate.ladder.silentDays ? ", silent on " + estimate.ladder.silentDays + " more" : "")
    };
  }
  function confidenceBadge(estimate) {
    var badge = element("span", BADGE_TEXT[estimate.confidence.level], "crystal-badge " + estimate.confidence.level);
    badge.title = "Confidence: " + estimate.confidence.reason;
    return badge;
  }
  function headline(estimate) {
    var box = element("div", null, "crystal-headline");
    var parts = headlineParts(estimate, shortPlaceName);
    var main = element("strong", parts.main);
    if (estimate.ranking && !isWeakBasis(estimate)) { main.title = placeName(estimate.ranking[0]); }
    box.appendChild(main);
    if (estimate.confidence) { box.appendChild(confidenceBadge(estimate)); }
    if (parts.basis) { box.appendChild(element("span", parts.basis, "crystal-headline-basis")); }
    return box;
  }
  // The headline as recorded: full place name, without the badge (the confidence is a field of its own).
  function headlineText(estimate) {
    var parts = headlineParts(estimate, placeName);
    return parts.main + (parts.basis ? ". " + parts.basis.charAt(0).toUpperCase() + parts.basis.slice(1) + "." : "");
  }

  function lastReportLine(estimate, referenceLocal, referenceSeconds) {
    var last = analysis.last;
    var line = element("p", null, "crystal-last-report");
    line.appendChild(document.createTextNode("Last report " + G.localText(last.local, last.offset) +
      (isAccuracyReported() ? " ±" + last.accuracy_m + " m" : " (accuracy not reported)") + " at "));
    var place = element("span", estimate.lastPlace ? shortPlaceLabel(estimate.lastPlace.address, estimate.lastPlace.key) : "no known place");
    place.title = lastPlaceText(estimate);
    line.appendChild(place);
    line.appendChild(document.createTextNode(" · reference " + G.localText(referenceLocal, G.utcToLocal(referenceSeconds).offset) +
      " (" + payload.display_zone + "), " + G.formatDuration(estimate.elapsedSeconds / 60) + " later"));
    return line;
  }

  // ---- tendency direction ------------------------------------------------------------
  // estimate.tendency: the direction from the last position by the first tier with evidence
  // (T1 departures from this place, T2 windows starting near it, T4 bearings of the other known
  // places); motion is a separate heading and never mixed in.
  // A tendency without a tier carries no direction: the window shows the empty compass.
  function tendencyOf(estimate) { return estimate.tendency && estimate.tendency.tier ? estimate.tendency : null; }
  function stayedText(tendency) {
    return tendency && tendency.stayedDays ? " · stayed here on all " + tendency.stayedDays + " comparable days" : "";
  }
  function hasBearing(value) { return typeof value === "number" && isFinite(value); }
  function degreesText(bearing) { return G.forecast.compassName(bearing) + " " + ((Math.round(bearing) % 360 + 360) % 360) + "°"; }
  function directionWithInterval(tendency) {
    return degreesText(tendency.bearing) + (hasBearing(tendency.confidenceHalfAngle) ? " ± " + Math.round(tendency.confidenceHalfAngle) + "°" : "");
  }
  function isKnownPlacesTier(tendency) { return tendency.tier === "T4"; }
  function dayCountText(tendency) {
    if (tendency.leftDays) { return "left on " + tendency.leftDays.left + " of " + tendency.leftDays.total + " days"; }
    return tendency.days + (tendency.days === 1 ? " day" : " days");
  }
  function pText(p) { return hasBearing(p) ? (p < 0.001 ? "p < 0.001" : "p " + p.toFixed(p < 0.01 ? 3 : 2)) : "p –"; }
  function listedBearings(tendency) {
    var names = [];
    (tendency.dayBearings || []).forEach(function (day) {
      var name = G.forecast.compassName(day.bearing);
      if (names.indexOf(name) < 0) { names.push(name); }
    });
    return names.join(", ");
  }
  function isEstablished(tendency) { return tendency.evidence === "established" && hasBearing(tendency.bearing); }
  function opposites(tendency) {
    var pair = tendency.oppositeDirections;
    return pair && hasBearing(pair[0]) && hasBearing(pair[1]) ? pair : null;
  }
  // The short form for the summary line.
  function tendencySummary(tendency) {
    if (!tendency) { return "Tendency: none"; }
    var label = isKnownPlacesTier(tendency) ? "Known places lie " : "Tendency ";
    if (opposites(tendency)) { return label + degreesText(opposites(tendency)[0]) + " / " + degreesText(opposites(tendency)[1]); }
    if (isEstablished(tendency)) { return label + directionWithInterval(tendency); }
    if (tendency.evidence === "sparse") { return "Tendency: " + tendency.days + (tendency.days === 1 ? " day" : " days") + (listedBearings(tendency) ? " (" + listedBearings(tendency) + ")" : ""); }
    return "Tendency: not established" + (hasBearing(tendency.bearing) ? ", weak lean " + G.forecast.compassName(tendency.bearing) : "");
  }
  // The one-line chart caption.
  function tendencyCaption(tendency) {
    var basis = tendency.basis ? " · " + tendency.basis : "";
    var pair = opposites(tendency);
    if (pair) {
      return "Two opposite directions: " + degreesText(pair[0]) + " and " + degreesText(pair[1]) + " (" + dayCountText(tendency) + ")" + basis;
    }
    if (isEstablished(tendency)) {
      return (isKnownPlacesTier(tendency) ? "Known places lie " : "Tendency ") + directionWithInterval(tendency) + " · " + dayCountText(tendency) + basis + stayedText(tendency);
    }
    if (tendency.evidence === "sparse") {
      return tendency.days + (tendency.days === 1 ? " day" : " days") + (listedBearings(tendency) ? ": " + listedBearings(tendency) : "") + basis + stayedText(tendency);
    }
    return "No preferred direction established (" + pText(tendency.rayleighP) + ", " + dayCountText(tendency) + ")" +
      (hasBearing(tendency.bearing) ? " · weak lean " + G.forecast.compassName(tendency.bearing) : "") + basis + stayedText(tendency);
  }
  function motionText(motion) {
    return degreesText(motion.bearing) + (hasBearing(motion.speedKmh) ? ", ~" + Math.round(motion.speedKmh) + " km/h" : "") +
      (hasBearing(motion.ageMinutes) ? ", " + G.formatDuration(motion.ageMinutes) + " old" : "");
  }
  // Without a tendency the rose alone gives the caption.
  var NAMED_SECTORS = 3;
  function roseCaption(rose) {
    var ordered = rose.sectors.filter(function (sector) { return sector.days > 0; }).sort(function (a, b) { return b.days - a.days; });
    var named = ordered.slice(0, NAMED_SECTORS).map(function (sector) { return sectorName(sector.bearing, rose.sectors.length) + " " + sector.days; });
    return "Left the place: " + named.join(", ") + " of " + rose.leavingDays + " days" + (rose.preferred ? "" : " · no preferred direction");
  }
  function noTendencyReason(estimate) {
    return "No direction from the last position: " + (estimate.radii ? "no earlier day gives one from here, and there is no other known place" : (estimate.radiiNote || "no search rings")) + ".";
  }

  // ---- direction chart ---------------------------------------------------------------
  // A compass centred on the last position: day bearings as dots on the rim (size = day weight),
  // the tendency needle (solid with its 95 % confidence wedge when established, faint and dashed
  // when weak, two needles for opposite directions, none when sparse), the last movement as a
  // thin second needle, matching destinations as numbered marks and, underneath, the rose
  // wedges (area = share of leaving days). Directions only: not to map scale.
  var SVG_NS = "http://www.w3.org/2000/svg";
  var CHART = { size: 200, centre: 100, radius: 70 };
  var MINOR_TICK_DEGREES = 30;
  var DAY_DOT_RADIUS = { min: 2.2, extra: 2.3 };
  function svgNode(tag, attributes, className) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attributes || {}).forEach(function (name) { node.setAttribute(name, String(attributes[name])); });
    if (className) { node.setAttribute("class", className); }
    return node;
  }
  function chartPoint(radius, bearing) {
    var angle = bearing * Math.PI / 180;
    return [CHART.centre + radius * Math.sin(angle), CHART.centre - radius * Math.cos(angle)];
  }
  function pointText(point) { return point[0].toFixed(1) + " " + point[1].toFixed(1); }
  function wedgePath(radius, bearing, halfAngle) {
    var from = chartPoint(radius, bearing - halfAngle);
    var to = chartPoint(radius, bearing + halfAngle);
    return "M" + CHART.centre + " " + CHART.centre + " L" + pointText(from) +
      " A" + radius.toFixed(2) + " " + radius.toFixed(2) + " 0 " + (halfAngle > 90 ? 1 : 0) + " 1 " + pointText(to) + " Z";
  }
  // A needle from the centre with an arrow head at `length`.
  function needle(bearing, length, className) {
    var group = svgNode("g", {}, className);
    var shaft = chartPoint(length - 7, bearing);
    group.appendChild(svgNode("line", { x1: CHART.centre, y1: CHART.centre, x2: shaft[0].toFixed(1), y2: shaft[1].toFixed(1) }, "needle-shaft"));
    var tip = chartPoint(length, bearing);
    var left = chartPoint(length - 9, bearing - 5 * 70 / length);
    var right = chartPoint(length - 9, bearing + 5 * 70 / length);
    group.appendChild(svgNode("path", { d: "M" + pointText(tip) + " L" + pointText(left) + " L" + pointText(right) + " Z" }, "needle-head"));
    return group;
  }
  function shareText(share) { return share < 0.005 ? "< 1 %" : percent(share); }
  function sectorTooltip(sector, rose) {
    var parts = [sectorName(sector.bearing, rose.sectors.length), sector.days + " of " + rose.leavingDays + " leaving days"];
    if (rose.percentAvailable) { parts.push(shareText(sector.probability)); }
    parts.push(sector.reach80 !== null ? "80 % reach " + G.formatDistance(sector.reach80) : "80 % reach: under 3 days");
    return parts.join(" · ");
  }

  function compassFrame(svg, empty) {
    var frame = svgNode("g", {}, "rose-guides");
    if (empty) { frame.appendChild(svgNode("circle", { cx: CHART.centre, cy: CHART.centre, r: CHART.radius }, "rose-empty-disc")); }
    frame.appendChild(svgNode("circle", { cx: CHART.centre, cy: CHART.centre, r: CHART.radius / 2 }, "rose-guide"));
    frame.appendChild(svgNode("circle", { cx: CHART.centre, cy: CHART.centre, r: CHART.radius }, "rose-ring"));
    for (var bearing = 0; bearing < 360; bearing += MINOR_TICK_DEGREES) {
      var major = bearing % 90 === 0;
      var inner = chartPoint(CHART.radius - (major ? 6 : 3), bearing);
      var outer = chartPoint(CHART.radius + (major ? 4 : 0), bearing);
      frame.appendChild(svgNode("line", { x1: inner[0].toFixed(1), y1: inner[1].toFixed(1), x2: outer[0].toFixed(1), y2: outer[1].toFixed(1) }, major ? "rose-tick" : "rose-tick minor"));
    }
    ["N", "E", "S", "W"].forEach(function (letter, index) {
      var at = chartPoint(CHART.radius + 14, index * 90);
      var label = svgNode("text", { x: at[0].toFixed(1), y: (at[1] + 4).toFixed(1) }, "rose-compass");
      label.textContent = letter;
      frame.appendChild(label);
    });
    svg.appendChild(frame);
  }
  function lastPositionDot(svg) {
    svg.appendChild(svgNode("circle", { cx: CHART.centre, cy: CHART.centre, r: 3.5 }, "rose-origin"));
  }
  function chartSvg(label) {
    var svg = svgNode("svg", { width: CHART.size, height: CHART.size, viewBox: "0 0 " + CHART.size + " " + CHART.size, role: "img", "aria-label": label });
    var title = svgNode("title");
    title.textContent = label;
    svg.appendChild(title);
    return svg;
  }

  function directionChart(estimate) {
    var tendency = tendencyOf(estimate);
    var rose = estimate.rose;
    var caption = tendency ? tendencyCaption(tendency) : roseCaption(rose);
    var weak = isWeakBasis(estimate);
    var figure = element("figure", null, "rose-chart" + (weak ? " weak" : "") + (tendency ? " evidence-" + tendency.evidence : ""));
    var plot = element("div", null, "rose-plot");
    var svg = chartSvg("Direction chart around the last position: " + caption + ". Details in the Directions section.");
    var tooltip = element("div", null, "rose-tooltip");
    tooltip.hidden = true;
    compassFrame(svg, false);
    var layers = {};
    ["wedges", "interval", "days", "needles", "markers", "hits"].forEach(function (name) {
      layers[name] = svgNode("g", {}, "rose-" + name);
      svg.appendChild(layers[name]);
    });

    function showTooltip(text, x, y) {
      tooltip.textContent = text;
      tooltip.hidden = false;
      var left = x + 12;
      if (left + tooltip.offsetWidth > CHART.size) { left = Math.max(0, x - 12 - tooltip.offsetWidth); }
      tooltip.style.left = left + "px";
      tooltip.style.top = (y + 12) + "px";
    }
    function hideTooltip() { tooltip.hidden = true; }
    // Pointer and keyboard focus show the same text; mark (optional) lifts while active.
    function bindTooltip(target, text, mark, anchorPoint) {
      target.setAttribute("tabindex", "0");
      target.setAttribute("aria-label", text);
      function activate(on) { if (mark) { mark.classList.toggle("active", on); } }
      target.addEventListener("pointermove", function (event) {
        var box = plot.getBoundingClientRect();
        activate(true);
        showTooltip(text, event.clientX - box.left, event.clientY - box.top);
      });
      target.addEventListener("pointerleave", function () { activate(false); hideTooltip(); });
      target.addEventListener("focus", function () { activate(true); showTooltip(text, anchorPoint[0], anchorPoint[1]); });
      target.addEventListener("blur", function () { activate(false); hideTooltip(); });
    }

    // Rose wedges underneath: area proportional to the sector share.
    if (rose) {
      var halfAngle = 180 / rose.sectors.length;
      rose.sectors.forEach(function (sector) {
        var radius = CHART.radius * Math.sqrt(Math.max(0, Math.min(1, sector.probability)));
        var wedge = svgNode("path", { d: wedgePath(radius, sector.bearing, halfAngle) }, "rose-wedge");
        layers.wedges.appendChild(wedge);
        var hit = svgNode("path", { d: wedgePath(CHART.radius, sector.bearing, halfAngle) }, "rose-hit");
        layers.hits.appendChild(hit);
        bindTooltip(hit, sectorTooltip(sector, rose), wedge, chartPoint(radius, sector.bearing));
      });
    }
    if (tendency) {
      var heaviest = Math.max.apply(null, (tendency.dayBearings || []).map(function (day) { return day.weight; }).concat([1e-9]));
      (tendency.dayBearings || []).forEach(function (day) {
        if (!hasBearing(day.bearing)) { return; }
        var at = chartPoint(CHART.radius, day.bearing);
        // Sparse evidence has no needle: faint spokes show the listed bearings instead.
        if (tendency.evidence === "sparse") {
          layers.days.appendChild(svgNode("line", { x1: CHART.centre, y1: CHART.centre, x2: at[0].toFixed(1), y2: at[1].toFixed(1) }, "rose-spoke"));
        }
        layers.days.appendChild(svgNode("circle", { cx: at[0].toFixed(1), cy: at[1].toFixed(1), r: (DAY_DOT_RADIUS.min + DAY_DOT_RADIUS.extra * Math.max(0, day.weight) / heaviest).toFixed(1) }, "rose-day"));
      });
      var pair = opposites(tendency);
      var needleClass = "rose-needle " + (tendency.evidence === "established" ? "established" : "weak");
      if (pair) {
        pair.forEach(function (bearing) { layers.needles.appendChild(needle(bearing, CHART.radius - 10, needleClass)); });
      } else if (isEstablished(tendency)) {
        if (hasBearing(tendency.confidenceHalfAngle)) {
          layers.interval.appendChild(svgNode("path", { d: wedgePath(CHART.radius - 10, tendency.bearing, Math.max(MIN_SECTOR_HALF_ANGLE, Math.min(180, tendency.confidenceHalfAngle))) }, "rose-interval"));
        }
        layers.needles.appendChild(needle(tendency.bearing, CHART.radius - 10, needleClass));
      } else if (tendency.evidence === "weak" && hasBearing(tendency.bearing)) {
        layers.needles.appendChild(needle(tendency.bearing, CHART.radius - 10, needleClass));
      }
      if (tendency.motion && hasBearing(tendency.motion.bearing)) {
        layers.needles.appendChild(needle(tendency.motion.bearing, CHART.radius * 0.72, "rose-motion"));
      }
    }
    var destinationRows = estimate.destinations ? estimate.destinations.rows : [];
    destinationRows.forEach(function (row, index) {
      var at = chartPoint(CHART.radius, row.bearing);
      var marker = svgNode("g", {}, "rose-destination-marker");
      marker.appendChild(svgNode("rect", { x: (at[0] - 6.5).toFixed(1), y: (at[1] - 6.5).toFixed(1), width: 13, height: 13, rx: 2 }, "rose-destination"));
      var number = svgNode("text", { x: at[0].toFixed(1), y: (at[1] + 3.5).toFixed(1) }, "rose-destination-number");
      number.textContent = String(index + 1);
      marker.appendChild(number);
      layers.markers.appendChild(marker);
      bindTooltip(marker, (index + 1) + " · " + placeLabel(row.address, row.key) + " · ≈ " + percent(row.probability) + " · " + bearingText(row.bearing), null, at);
    });
    lastPositionDot(svg);

    plot.appendChild(svg);
    plot.appendChild(tooltip);
    figure.appendChild(plot);
    var captionNode = element("figcaption", caption, "rose-caption");
    figure.appendChild(captionNode);
    var keys = [];
    if (tendency && tendency.motion && hasBearing(tendency.motion.bearing)) { keys.push(keyLine("motion", "thin needle: last movement " + motionText(tendency.motion))); }
    if (destinationRows.length) { keys.push(keyLine("destination", "numbered: destinations while moving")); }
    if (rose) { keys.push(keyLine("wedge", "shaded: share of leaving days")); }
    keys.forEach(function (line) { figure.appendChild(line); });
    return figure;
  }
  // A small SVG sample for a key line.
  function keySample(kind) {
    var sample = svgNode("svg", { width: 18, height: 12, viewBox: "0 0 18 12", "aria-hidden": "true" }, "rose-key-sample");
    if (kind === "motion") { sample.appendChild(svgNode("line", { x1: 1, y1: 6, x2: 17, y2: 6 }, "rose-motion-sample")); }
    if (kind === "destination") { sample.appendChild(svgNode("rect", { x: 3, y: 0.5, width: 11, height: 11, rx: 2 }, "rose-destination")); }
    if (kind === "wedge") { sample.appendChild(svgNode("path", { d: "M2 11 L16 11 A14 14 0 0 0 11 1 Z" }, "rose-wedge")); }
    return sample;
  }
  function keyLine(kind, text) {
    var line = element("div", null, "rose-key-line");
    line.appendChild(keySample(kind));
    line.appendChild(element("span", text));
    return line;
  }
  // Without any direction the compass keeps its place, grey and empty, with the reason.
  function emptyDirectionChart(estimate) {
    var figure = element("figure", null, "rose-chart empty");
    var plot = element("div", null, "rose-plot");
    var svg = chartSvg(noTendencyReason(estimate));
    compassFrame(svg, true);
    lastPositionDot(svg);
    plot.appendChild(svg);
    figure.appendChild(plot);
    figure.appendChild(element("figcaption", noTendencyReason(estimate), "rose-caption"));
    return figure;
  }
  function directionFigure(estimate) {
    return tendencyOf(estimate) || estimate.rose ? directionChart(estimate) : emptyDirectionChart(estimate);
  }

  // ---- overview: summary line, chart, top places ---------------------------------------
  function summaryLine(estimate) {
    var line = element("p", null, "crystal-summary");
    if (estimate.radii) {
      line.appendChild(element("span", "Search", "crystal-summary-label"));
      [["p50", "r50"], ["p80", "r80"], ["p95", "r95"]].forEach(function (ring) {
        var item = element("span", null, "crystal-summary-ring");
        item.appendChild(element("span", null, "crystal-ring-swatch " + ring[0]));
        item.appendChild(document.createTextNode(ring[1] + " " + G.formatDistance(estimate.radii[ring[0]]) + (ring[0] === "p95" && r95IsMaximumObserved(estimate) ? " (max. seen)" : "")));
        line.appendChild(item);
      });
    } else {
      line.appendChild(element("span", "No search rings: " + estimate.radiiNote, "crystal-summary-label"));
    }
    line.appendChild(element("span", tendencySummary(tendencyOf(estimate)), "crystal-summary-tendency"));
    return line;
  }
  function topPlacesList(estimate, rankedRows) {
    var box = element("div", null, "crystal-top");
    var weak = isWeakBasis(estimate);
    box.appendChild(element("h4", weak ? weakRankingHeading(estimate) : "Top places", "crystal-list-heading"));
    var list = element("ol", null, "crystal-top-list");
    rankedRows.slice(0, TOP_PLACES).forEach(function (entry) {
      var row = entry.row;
      var item = element("li", null, "crystal-top-row");
      item.appendChild(element("span", String(entry.rank), "crystal-rank" + (weak ? " weak" : "") + (row.kind === "elsewhere" ? " elsewhere" : "")));
      var text = element("span", null, "crystal-top-text");
      var name = element("span", shortPlaceName(row), "crystal-top-name");
      name.title = placeName(row);
      text.appendChild(name);
      var detail = [weak || !estimate.percentAvailable ? row.days + " of " + row.ofDays + " days" : estimateText(estimate, row)];
      if (row.kind === "place") { detail.push(G.formatDistance(row.distance) + " " + G.forecast.compassName(row.bearing)); }
      text.appendChild(element("span", detail.join(" · "), "crystal-top-detail"));
      item.appendChild(text);
      if (row.lat !== null && row.kind !== "elsewhere") {
        item.classList.add("clickable");
        item.tabIndex = 0;
        item.setAttribute("role", "button");
        item.setAttribute("aria-label", "Show " + placeName(row) + " on the map");
        var show = function () { map.setView([row.lat, row.lon], 16); };
        item.addEventListener("click", show);
        item.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); show(); } });
      }
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }
  function overviewBlock(estimate, rankedRows) {
    var box = element("div", null, "crystal-overview");
    box.appendChild(directionFigure(estimate));
    if (rankedRows.length) { box.appendChild(topPlacesList(estimate, rankedRows)); }
    return box;
  }

  // ---- collapsed sections ------------------------------------------------------------
  // Which sections the user opened, kept across recomputations.
  var openSections = {};
  function section(key, title, nodes) {
    var parts = nodes.filter(function (node) { return !!node; });
    if (!parts.length) { return null; }
    var details = element("details", null, "crystal-section crystal-" + key);
    details.open = !!openSections[key];
    details.addEventListener("toggle", function () { openSections[key] = details.open; });
    details.appendChild(element("summary", title));
    parts.forEach(function (node) { details.appendChild(node); });
    return details;
  }
  function scrollingTable(tableNode) {
    var box = element("div", null, "crystal-table-scroll");
    box.appendChild(tableNode);
    return box;
  }
  function rankingSection(estimate, rankedRows) {
    if (!rankedRows.length) { return null; }
    var surroundings = rankedRows.map(function (entry) { return entry.row.kind === "place" || entry.row.kind === "here" ? surroundingsCounts(entry.row.lat, entry.row.lon) : null; });
    var anySurroundings = surroundings.some(function (text) { return text !== null; });
    var headers = ["#", "Where", "Days", "Estimate", "Distance", "Bearing", "Usual presence"];
    if (anySurroundings) { headers.push("Surroundings (" + overpassRadius() + " m)"); }
    var rankingTable = table(headers, rankedRows.map(function (entry, index) {
      var row = entry.row;
      var place = row.kind === "place";
      var cells = [String(entry.rank), placeName(row), row.days + " of " + row.ofDays, estimateText(estimate, row),
        place ? G.formatDistance(row.distance) : "-", place ? bearingText(row.bearing) : "-", presenceText(row)];
      if (anySurroundings) { cells.push(row.kind === "elsewhere" ? "-" : surroundings[index] || noSurroundingsText()); }
      return { cells: cells, row: row };
    }), function (tableRow) {
      if (tableRow.row.lat !== null) { map.setView([tableRow.row.lat, tableRow.row.lon], 16); }
    });
    var notes = [];
    if (!anySurroundings) { notes.push("Surroundings: " + noSurroundingsText() + " of any ranked place."); }
    if (estimate.dayClassesEnabled && (estimate.ladder.level === "S3" || estimate.ladder.level === "S5")) {
      notes.push("Days counts every day; in the estimate, days of the other day class count 0.3.");
    }
    return section("ranking", "Ranking (" + rankedRows.length + ")", [scrollingTable(rankingTable)].concat(notes.map(function (text) { return element("p", text, "crystal-note"); })));
  }
  function tendencyTable(tendency) {
    var rows = [
      ["Basis", (tendency.tier ? tendency.tier + " · " : "") + (tendency.basis || "-")],
      ["Days", tendency.days + (hasBearing(tendency.nEffective) ? " (effective " + tendency.nEffective.toFixed(1) + ")" : "") + (tendency.leftDays ? "; left on " + tendency.leftDays.left + " of " + tendency.leftDays.total : "")],
      ["Evidence", tendency.evidence + " (" + pText(tendency.rayleighP) + (hasBearing(tendency.resultantLength) ? ", R " + tendency.resultantLength.toFixed(2) : "") + ")"],
      ["Mean direction", hasBearing(tendency.bearing) ? degreesText(tendency.bearing) : "–"],
      ["Direction uncertain by", hasBearing(tendency.confidenceHalfAngle) ? "± " + Math.round(tendency.confidenceHalfAngle) + "° (95 % confidence of the mean direction, not of where the device goes)" : "not defined"]
    ];
    if (opposites(tendency)) { rows.push(["Opposite directions", degreesText(opposites(tendency)[0]) + " and " + degreesText(opposites(tendency)[1])]); }
    if (tendency.motion && hasBearing(tendency.motion.bearing)) { rows.push(["Last movement", motionText(tendency.motion)]); }
    var bearings = (tendency.dayBearings || []).filter(function (day) { return hasBearing(day.bearing); }).map(function (day) {
      return degreesText(day.bearing) + (day.weight !== 1 ? " (×" + (Math.round(day.weight * 100) / 100) + ")" : "");
    });
    rows.push(["Day bearings", bearings.length ? bearings.join(", ") : "–"]);
    return table(["", "Tendency from the last position"], rows.map(function (cells) { return { cells: cells }; }));
  }
  function roseTable(rose) {
    var count = rose.sectors.length;
    var headers = rose.percentAvailable ? ["Direction", "Days", "Share", "80 % reach"] : ["Direction", "Days", "80 % reach"];
    return table(headers, rose.sectors.map(function (sector) {
      var cells = [sectorName(sector.bearing, count) + " " + Math.round(sector.bearing) + "°", sector.days + " of " + rose.leavingDays];
      if (rose.percentAvailable) { cells.push(shareText(sector.probability)); }
      cells.push(sector.reach80 !== null ? G.formatDistance(sector.reach80) : "–");
      return { cells: cells };
    }));
  }
  function directionsSection(estimate) {
    var tendency = tendencyOf(estimate);
    var nodes = [];
    if (tendency) { nodes.push(scrollingTable(tendencyTable(tendency))); }
    if (estimate.rose) {
      nodes.push(element("p", "Leaving days per direction (" + estimate.rose.basis + "). On the map each wedge reaches the 80 % distance in its direction (only with 3 days or more).", "crystal-note"));
      nodes.push(scrollingTable(roseTable(estimate.rose)));
    }
    if (!nodes.length) { nodes.push(element("p", noTendencyReason(estimate))); }
    return section("directions", "Directions", nodes);
  }
  function transitionsLine(estimate) {
    var transitions = estimate.transitions;
    if (!transitions) { return null; }
    var hereKey = estimate.lastPlace ? estimate.lastPlace.key : null;
    var parts = transitions.rows.map(function (row) {
      var name = row.key === hereKey ? "back here" : (addressText(row.address) || "known place " + row.key);
      return name + " " + row.count + " of " + transitions.departures + " (≈ " + percent(row.probability) + ")";
    });
    var leaving = transitions.leaveWindow
      ? " Usually leaving " + (transitions.leaveWindow.from === transitions.leaveWindow.to ? transitions.leaveWindow.from : transitions.leaveWindow.from + "–" + transitions.leaveWindow.to) + "."
      : "";
    return element("p", "From here the device went on to " + parts.join(", ") + " (shares weighted by time of day)." + leaving);
  }
  function dwellLine(estimate) {
    var text = "Time here before the last report: " + G.formatDuration(estimate.dwellSeconds / 60);
    if (!estimate.dwellIsStay) { return element("p", text + " (in motion)."); }
    text += " (established stay). ";
    if (estimate.dwell && estimate.dwell.probability !== null && estimate.dwell.method === "leave-hazard") {
      text += "Visits here at this time of day went on for the elapsed time in " + percent(estimate.dwell.probability) + " of cases (" + estimate.dwell.basis + ").";
    } else if (estimate.dwell && estimate.dwell.probability !== null) {
      text += "Stays this long went on for the elapsed time in " + percent(estimate.dwell.probability) + " of cases (" + estimate.dwell.basis + ").";
    } else {
      text += "No comparison of stay lengths: " + (estimate.dwell ? estimate.dwell.basis : "no completed stays") + ".";
    }
    return element("p", text);
  }
  function motionLine(estimate) {
    var motion = estimate.motion;
    if (!motion) { return null; }
    var text = "Moving at the last report: ~" + Math.round(motion.speedKmh) + " km/h (" + motion.movementClass + ")";
    if (motion.heading) { text += ", heading " + motion.heading.compass; }
    if (motion.typicalTrip) { text += ". 80 % of earlier trips ended within " + G.formatDuration(motion.typicalTrip.seconds / 60) + " (" + motion.typicalTrip.basis + ")"; }
    return element("p", text + ".");
  }
  function coneLine(estimate) {
    var cone = estimate.cone;
    if (!cone) { return null; }
    return element("p", "Extrapolation cone (dashed on the map): heading " + cone.compass + " at ~" + Math.round(cone.speedKmh) + " km/h, ± " + Math.round(cone.halfAngle) +
      "°, up to " + G.formatDistance(cone.radius) + " by the reference time. Basis: " + cone.basis + ".");
  }
  function silenceBlock(estimate) {
    var silence = estimate.silence;
    if (!silence) { return null; }
    var box = element("div", null, "crystal-silence");
    box.appendChild(element("h4", "If the device stays silent", "crystal-list-heading"));
    var profile = [];
    if (silence.gapsPerVisitHere !== null) { profile.push(silence.gapsHere + " gaps started here (" + silence.gapsPerVisitHere.toFixed(1) + " per visit)"); }
    if (silence.gapsPerStayAnywhere !== null) { profile.push(silence.gapsPerStayAnywhere.toFixed(1) + " per stay anywhere"); }
    if (silence.timeProfileShare !== null) { profile.push(percent(silence.timeProfileShare) + " of all gaps began within 90 min of this time of day"); }
    if (profile.length) { box.appendChild(element("p", "Gaps: " + profile.join("; ") + ".")); }
    if (silence.routineSentence) { box.appendChild(element("p", silence.routineSentence)); }
    var comparable = silence.comparable;
    if (!comparable) {
      box.appendChild(element("p", "Fewer than 3 comparable gaps as long as the elapsed time: no estimate of how the silence ends."));
      return box;
    }
    var ends = comparable.endPlaces.slice(0, 3).map(function (entry) {
      return (entry.key === G.forecast.ELSEWHERE ? "elsewhere" : shortPlaceLabel(entry.address, entry.key)) + " " + percent(entry.share);
    });
    box.appendChild(element("p", comparable.count + " comparable gaps" + (comparable.anyStart ? " (started anywhere)" : " that started here") + " ended at " + ends.join(", ") +
      "; at the start place in " + percent(comparable.sameEndShare) + ". The silence ended within " + G.formatDuration(comparable.remainingSeconds.p50 / 60) +
      " in half of them (median) and within " + G.formatDuration(comparable.remainingSeconds.p80 / 60) + " in 80 %. This assumes the device stayed put while silent."));
    return box;
  }
  function corridorsBlock(estimate) {
    var corridors = estimate.corridors;
    if (!corridors.length) { return null; }
    var box = element("div", null, "crystal-corridors-list");
    box.appendChild(element("h4", "Routes from here", "crystal-list-heading"));
    box.appendChild(element("p", "Earlier trips from this place, grouped by destination (3 trips or more each); dashed where the device was silent. " +
      (corridorsBox.checked ? "Click a line, label or row to centre it." : "Tick \"Routes from here\" to draw them."), "crystal-note"));
    box.appendChild(scrollingTable(table(["Destination", "Departures", "Drawn", "Not drawn"], corridors.map(function (corridor) {
      return { cells: [placeLabel(corridor.address, corridor.destinationKey), corridor.departures + " of " + totalDepartures(corridor),
        corridor.trips.length + (corridor.dashedTrips.length ? " + " + corridor.dashedTrips.length + " dashed" : ""), String(corridor.excluded)], corridor: corridor };
    }), function (row) { map.fitBounds(corridorBounds(row.corridor), { padding: [30, 30] }); })));
    var sparse = corridors.reduce(function (sum, corridor) { return sum + corridor.sparseTrips; }, 0);
    if (sparse) {
      box.appendChild(element("p", sparse + (sparse === 1 ? " trip has" : " trips have") + " reports more than 5 min apart: the lines cut corners.", "warning"));
    }
    return box;
  }
  function destinationsBlock(estimate) {
    var destinations = estimate.destinations;
    if (!destinations || !destinations.rows.length) { return null; }
    var heading = estimate.motion && estimate.motion.heading ? " heading " + estimate.motion.heading.compass : "";
    var box = element("div", null, "crystal-destinations");
    box.appendChild(element("h4", "Moving for " + G.formatDuration(destinations.travelledSeconds / 60) + heading + " from " + shortPlaceLabel(destinations.origin.address, destinations.origin.key), "crystal-list-heading"));
    box.appendChild(scrollingTable(table(["#", "Destination", "Estimate", "Ahead", "Time", "Direction"], destinations.rows.map(function (row, index) {
      return { cells: [String(index + 1), placeLabel(row.address, row.key), "≈ " + percent(row.probability), row.lateral ? "off to the side" : G.formatDistance(row.distanceAhead),
        "≈ " + Math.round(row.minutesAhead) + " min", bearingText(row.bearing)], row: row };
    }), function (tableRow) { map.setView([tableRow.row.lat, tableRow.row.lon], 16); })));
    box.appendChild(element("p", "Unknown destination (visited less than twice): ≈ " + percent(destinations.unknownShare) + ". Prior: " + destinations.priorBasis +
      ". Trip time: " + destinations.timeBasis + ". Shown as numbered squares on the map.", "crystal-note"));
    return box;
  }
  // The routine change in one line; its figures (change.text, as recorded) are in "Why?".
  var ROUTINE_RECENT_DAYS = 7; // map_forecast_model.js ROUTINE_RECENT_DAYS
  function routineChangeLine(change) {
    var signs = change.indicators.map(function (indicator) {
      var name = shortPlaceLabel(indicator.address, indicator.key);
      return indicator.kind === "new-place" ? "new place: " + name : "less time at " + name + " at night";
    });
    return "Routine may have changed: the last " + ROUTINE_RECENT_DAYS + " days differ from earlier days" + (signs.length ? " (" + signs.join("; ") + ")" : "") + ".";
  }
  function routineChangeItem(estimate) {
    var change = estimate.routineChange;
    var item = element("li", null, "crystal-routine-change");
    item.appendChild(element("span", routineChangeLine(change)));
    if (estimate.recencyAvailable && !recencyBox.checked) {
      var useRecent = element("button", "Let recent days count more");
      useRecent.type = "button";
      useRecent.addEventListener("click", function () {
        recencyBox.checked = true;
        recompute();
      });
      item.appendChild(useRecent);
    }
    return item;
  }
  // The warnings as one compact list; the routine change once, with its remedy.
  function warningsList(estimate) {
    var texts = shownWarnings(estimate).filter(function (text) { return !estimate.routineChange || text !== estimate.routineChange.text; });
    if (!texts.length && !estimate.routineChange) { return null; }
    var list = element("ul", null, "crystal-warnings");
    list.setAttribute("aria-label", "Warnings");
    texts.forEach(function (text) { list.appendChild(element("li", text)); });
    if (estimate.routineChange) { list.appendChild(routineChangeItem(estimate)); }
    return list;
  }
  // The embedded place of the hazard profile nearest to the last position within r95, or null;
  // undefined when there is nothing to search (no rings, no embedded places).
  function nearestHazard(estimate) {
    var hazards = (G.PLACE_PROFILES || []).filter(function (profile) { return profile.key === "hazards"; })[0];
    if (!estimate.radii || !hazards || !G.embeddedPlaceTotal) { return undefined; }
    var r95 = estimate.radii.p95;
    var last = analysis.last;
    var nearest = null;
    hazards.categories.forEach(function (category) {
      ((payload.places || {})[category] || []).forEach(function (place) {
        var distance = G.haversineMetres(last.lat, last.lon, place.lat, place.lon);
        if (distance <= r95 && (nearest === null || distance < nearest.distance)) { nearest = { place: place, category: category, distance: distance }; }
      });
    });
    return nearest;
  }
  function hazardsBlock(estimate) {
    if (!estimate.radii || !G.surroundingsAvailable) { return null; }
    var r95 = estimate.radii.p95;
    var last = analysis.last;
    var box = element("div", null, "crystal-hazard-line");
    var nearest = nearestHazard(estimate);
    if (nearest !== undefined) {
      box.appendChild(element("p", nearest
        ? "Nearest embedded hazard within r95 (" + G.formatDistance(r95) + "): " + categoryLabel(nearest.category) + (nearest.place.name ? " \"" + nearest.place.name + "\"" : "") + " " +
          G.formatDistance(nearest.distance) + " " + G.forecast.compassName(G.bearingDegrees(last.lat, last.lon, nearest.place.lat, nearest.place.lon)) + "."
        : "No embedded hazard within r95 (" + G.formatDistance(r95) + "). Embedded places cover only " + overpassRadius() + " m around the places queried at generation."));
    }
    var check = element("button", "Check hazards in r95", "crystal-check-hazards");
    check.type = "button";
    check.addEventListener("click", function () {
      G.checkSurroundings({ label: "Crystal ball r95 · " + G.sourceName(selectedSourceId), lat: last.lat, lon: last.lon, radius: r95, profile: "hazards", fromCrystalBall: true });
    });
    box.appendChild(check);
    return box;
  }

  // The time cursor is clamped to the From/To range; a day outside it needs the full span.
  function placeCursorAt(seconds) {
    var outsideRange = seconds < G.filters.fromSeconds || seconds > G.filters.toSeconds;
    if (outsideRange) { G.resetTimeFilter(); }
    G.setCursorSeconds(seconds);
    if (outsideRange) {
      var status = document.getElementById("status");
      status.textContent = status.textContent + " · time filter reset to reach " + G.localTextAt(seconds);
    }
  }

  // "Why?": the basis of the rings and of the ranking, and the comparable days.
  var LISTED_DAYS = 30;
  function whyNodes(estimate) {
    var nodes = [];
    if (estimate.radiiBasis) {
      nodes.push(element("p", "Search rings: " + estimate.radiiBasis.note + " (" + estimate.windowCount + " windows of " + G.formatDuration(estimate.windowSeconds / 60) + ")."));
      if (tendencyOf(estimate)) { nodes.push(element("p", "Tendency: " + tendencyOf(estimate).basis + stayedText(tendencyOf(estimate)) + ".")); }
    }
    if (estimate.radii) { nodes.push(element("p", ringsExplanation(estimate), "crystal-rings-explanation")); }
    if (estimate.routineChange) { nodes.push(element("p", estimate.routineChange.text)); }
    var ladder = estimate.ladder;
    if (ladder) {
      nodes.push(element("p", "Ranking basis " + ladder.level + " (" + ladder.label + "): " + ladder.days + " days (effective " + ladder.effectiveDays.toFixed(1) + ")" +
        (ladder.smoothedWith.length ? "; smoothed with " + ladder.smoothedWith.join(", ") : "") + ". Weighting: " +
        (estimate.dayWeighting === "recent" ? "recent days count more (half-life 14 d)" : "all days equal") + "."));
      nodes.push(element("p", "Levels: " + ladder.levels.map(function (level) { return level.level + " " + level.days + " d"; }).join(" · ") + ". Click a day to move the time cursor there."));
      var timeLabel = ladder.level === "S2" || ladder.level === "S3" ? "State " + G.formatDuration(ladder.elapsedSeconds / 60) + " later" : "State at the reference time of day";
      // Newest first; years of data give thousands of days, so the window lists the latest only.
      nodes.push(scrollingTable(table(["Date", "Class", timeLabel], estimate.dayList.slice(0, LISTED_DAYS).map(function (day) {
        return { cells: [day.date, day.dayClass, G.localTextAt(day.atSeconds).slice(11, 16) + " " + stateLabel(estimate, day)], day: day };
      }), function (row) { placeCursorAt(row.day.atSeconds); })));
      if (estimate.dayList.length > LISTED_DAYS) {
        var earlierDays = estimate.dayList.length - LISTED_DAYS;
        nodes.push(element("p", "… and " + earlierDays + (earlierDays === 1 ? " earlier day." : " earlier days."), "crystal-note"));
      }
    }
    var selectedSource = G.sourceById[selectedSourceId];
    if (selectedSource && G.undatedTotalOf(selectedSource)) {
      nodes.push(element("p", "Records without timestamp in this source: " + G.undatedCountText(selectedSource) + " (hollow rings in the Points layer, not used by the estimate).", "crystal-note"));
    }
    return nodes;
  }

  // Nearest report of a source within ±tolerance of a UTC instant (rendered points only).
  function nearestReport(sourceId, atSeconds, toleranceSeconds) {
    var nearest = null;
    var nearestDelta = Infinity;
    G.allPoints.forEach(function (point) {
      if (point.s !== sourceId) { return; }
      var delta = Math.abs(point.utcSeconds - atSeconds);
      if (delta <= toleranceSeconds && delta < nearestDelta) { nearest = point; nearestDelta = delta; }
    });
    return nearest;
  }
  // Latest report of a source at or before a UTC instant (rendered points only).
  function latestReportBefore(sourceId, atSeconds) {
    var latest = null;
    G.allPoints.forEach(function (point) {
      if (point.s === sourceId && point.utcSeconds <= atSeconds) { latest = point; }
    });
    return latest;
  }
  function otherSourcesBlock() {
    var others = G.visibleSources().filter(function (source) { return source.id !== selectedSourceId; });
    if (!others.length) { return null; }
    var last = analysis.last;
    var parameters = (payload.analysis && payload.analysis.parameters) || {};
    var toleranceMinutes = parameters.encounter_max_minutes || 10;
    var box = element("div");
    box.appendChild(element("p", "The other shown sources at " + G.sourceName(selectedSourceId) + "'s last report (" + G.localText(last.local, last.offset) + "): their report within ± " +
      toleranceMinutes + " min, or else their latest earlier one. Distance and bearing are from the last position.", "crystal-note"));
    box.appendChild(scrollingTable(table(["Source", "Own last report", "Report near that time", "Distance", "Bearing"], others.map(function (source) {
      var otherAnalysis = G.sourceAnalysis(source.id);
      var lastSeconds = Date.parse(last.utc) / 1000;
      var nearest = nearestReport(source.id, lastSeconds, toleranceMinutes * 60);
      var earlier = nearest ? null : latestReportBefore(source.id, lastSeconds);
      var shown = nearest || earlier;
      var cells = [G.sourceLabel(source.id), otherAnalysis && otherAnalysis.last ? G.localText(otherAnalysis.last.local, otherAnalysis.last.offset) : "-"];
      if (shown) {
        cells.push(nearest ? G.localText(nearest.local, nearest.off)
          : "report " + G.formatDuration((lastSeconds - earlier.utcSeconds) / 60) + " earlier (" + G.localText(earlier.local, earlier.off) + ")",
          G.formatDistance(G.haversineMetres(last.lat, last.lon, shown.lat, shown.lon)),
          bearingText(G.bearingDegrees(last.lat, last.lon, shown.lat, shown.lon)));
      } else {
        cells.push("no report at or before that time", "-", "-");
      }
      return { cells: cells, point: shown };
    }), function (row) {
      if (row.point) { map.setView([row.point.lat, row.point.lon], 16); }
    })));
    return box;
  }

  function isAccuracyReported() {
    // Per record: a CSV or Google source may report an accuracy for some records only.
    return analysis.last.accuracy_known !== false;
  }
  function lastPlaceText(estimate) {
    return estimate.lastPlace ? (addressText(estimate.lastPlace.address) || "known place " + estimate.lastPlace.key) : "no known place";
  }
  // The warnings as shown: the model's and the map's own thinning note.
  function shownWarnings(estimate) {
    var warnings = estimate.warnings.slice();
    var source = G.sourceById[selectedSourceId];
    if (analysis.points_total > source.points_rendered || source.thinning_stride > 1) {
      warnings.push("This map holds " + source.points_rendered + " of " + analysis.points_total + " analysed reports of this source (duplicates removed or thinned). The rings, the time here and the point count use these; the ranking uses all stays and gaps.");
    }
    return warnings;
  }

  function renderResult(estimate, referenceLocal, referenceSeconds) {
    resultBox.textContent = "";
    shownEstimate = null;
    updateRecordControls("");
    var last = analysis.last;
    if (!(estimate.elapsedSeconds > 0)) {
      resultBox.appendChild(element("p", "The reference time must be after the last known position (" + G.localText(last.local, last.offset) + ").", "warning"));
      layer.clearLayers();
      return;
    }
    var rankedRows = (estimate.ranking || []).map(function (row, index) { return { rank: index + 1, row: row }; });
    [warningsList(estimate), headline(estimate), lastReportLine(estimate, referenceLocal, referenceSeconds), summaryLine(estimate), overviewBlock(estimate, rankedRows),
      rankingSection(estimate, rankedRows), directionsSection(estimate), section("transitions", "Transitions", [transitionsLine(estimate)]),
      section("stay", "Stay & silence", [dwellLine(estimate), motionLine(estimate), coneLine(estimate), silenceBlock(estimate)]),
      section("routes", "Routes & destinations", [corridorsBlock(estimate), destinationsBlock(estimate)]),
      section("hazards", "Hazards", [hazardsBlock(estimate)]), section("others", "Other sources", [otherSourcesBlock()]),
      section("why", "Why?", whyNodes(estimate))].forEach(function (node) {
      if (node) { resultBox.appendChild(node); }
    });
    recencyOption.hidden = !estimate.recencyAvailable;
    corridorsOption.hidden = !estimate.corridors.length;
    shownEstimate = { estimate: estimate, referenceSeconds: referenceSeconds, rankedRows: rankedRows };
    updateRecordControls("");
    drawEstimate(estimate, rankedRows);
  }

  // ---- search area record -----------------------------------------------------------------
  // What renderResult showed last; the record is built from exactly that estimate.
  var shownEstimate = null;
  var recordButton = document.getElementById("crystal-record");
  var downloadButton = document.getElementById("crystal-download");
  var recordStatus = document.getElementById("crystal-record-status");
  // Recording needs the loopback server of GEOSnap (summary screen, key O); its token route
  // is this page's own path, so the record route is its sibling /<token>/records/search-area.
  var recordingPossible = location.protocol === "http:" && (location.hostname === "127.0.0.1" || location.hostname === "localhost");
  var RECORD_ROUTE = location.pathname.replace(/\/[^\/]*$/, "") + "/records/search-area";
  var NOT_SERVED_TEXT = "Recording needs the running GEOSnap: open this map from its summary screen (key O).";
  var recording = false;
  var MAX_TEXT = 2000;

  function updateRecordControls(statusText) {
    recordButton.disabled = !recordingPossible || recording || shownEstimate === null;
    downloadButton.disabled = shownEstimate === null;
    recordButton.title = recordingPossible ? "Check this estimate and write it as search area files to the project (search_areas/)" : NOT_SERVED_TEXT;
    if (statusText !== null) {
      recordStatus.textContent = "";
      if (!recordingPossible) { recordStatus.appendChild(element("p", NOT_SERVED_TEXT, "crystal-record-hint")); }
      if (statusText) { recordStatus.appendChild(element("p", statusText)); }
    }
  }
  function clip(text, length) { return String(text).slice(0, length || MAX_TEXT); }
  function roundTo(value, decimals) { var factor = Math.pow(10, decimals); return Math.round(value * factor) / factor; }
  function normalBearing(bearing) { return roundTo(((bearing % 360) + 360) % 360, 1); }
  function utcText(seconds) { return new Date(Math.floor(seconds) * 1000).toISOString().slice(0, 19) + "Z"; }
  function recordTime(seconds) {
    var local = G.utcToLocal(Math.floor(seconds));
    return { local: local.local, offset_minutes: local.offset, utc: utcText(seconds) };
  }
  function provenanceOf(sourceId) {
    var sources = (payload.provenance && payload.provenance.sources) || [];
    return sources.filter(function (entry) { return entry.id === sourceId; })[0] || {};
  }
  // Every field of the search area record; geosnap/project/search_area.py validates it strictly and
  // renders the files from it.
  function buildSearchAreaRecord() {
    var estimate = shownEstimate.estimate;
    var last = analysis.last;
    var source = G.sourceById[selectedSourceId];
    var provenance = provenanceOf(selectedSourceId);
    var parameters = analysis.parameters;
    var runParameters = (payload.analysis && payload.analysis.parameters) || {};
    var accuracyScale = lastWithScale().asc;
    var hazard = nearestHazard(estimate);
    var elsewhere = shownEstimate.rankedRows.filter(function (entry) { return entry.row.kind === "elsewhere"; })[0];
    return {
      record_type: "geosnap-search-area",
      record_version: 1,
      created_utc: utcText(Date.now() / 1000),
      display_zone: clip(payload.display_zone || "not recorded", 64),
      source: { id: selectedSourceId, label: clip(source.label, 200), file_name: provenance.file_name || null, sha256: provenance.sha256 || null },
      reference: recordTime(shownEstimate.referenceSeconds),
      last_position: {
        lat: last.lat, lon: last.lon,
        accuracy_m: isAccuracyReported() ? last.accuracy_m : null,
        uncertainty_m: isAccuracyReported() ? roundTo(last.accuracy_m * accuracyScale, 1) : null,
        time: recordTime(Date.parse(last.utc) / 1000),
        place: estimate.lastPlace ? clip(lastPlaceText(estimate)) : null
      },
      rings: estimate.radii ? {
        r50_m: roundTo(estimate.radii.p50, 1), r80_m: roundTo(estimate.radii.p80, 1), r95_m: roundTo(estimate.radii.p95, 1),
        basis: clip(estimate.radiiBasis.note), basis_kind: estimate.radiiBasis.kind, days: estimate.radiiBasis.days,
        window_count: estimate.windowCount, window_seconds: Math.round(estimate.windowSeconds), extrapolated: !!estimate.extrapolated
      } : null,
      rings_note: estimate.radiiNote ? clip(estimate.radiiNote) : null,
      rose: estimate.rose ? {
        basis: clip(estimate.rose.basis), leaving_days: estimate.rose.leavingDays, analog_days: estimate.rose.analogDays,
        preferred: estimate.rose.preferred, share_available: estimate.rose.percentAvailable,
        sectors: estimate.rose.sectors.map(function (sector) {
          return { bearing_deg: normalBearing(sector.bearing), days: sector.days, share: roundTo(sector.probability, 4), reach80_m: sector.reach80 === null ? null : roundTo(sector.reach80, 1) };
        })
      } : null,
      cone: estimate.cone ? {
        bearing_deg: normalBearing(estimate.cone.bearing), half_angle_deg: roundTo(estimate.cone.halfAngle, 1), radius_m: roundTo(estimate.cone.radius, 1),
        compass: clip(estimate.cone.compass, 8), speed_kmh: roundTo(estimate.cone.speedKmh, 1), basis: clip(estimate.cone.basis)
      } : null,
      candidates: shownEstimate.rankedRows.filter(function (entry) { return entry.row.kind !== "elsewhere" && entry.row.lat !== null; }).slice(0, 20).map(function (entry) {
        var row = entry.row;
        var here = row.kind === "here";
        return {
          rank: entry.rank, kind: here ? "here" : "place", label: clip(placeName(row)), address: addressText(row.address) ? clip(addressText(row.address)) : null,
          lat: row.lat, lon: row.lon, days: row.days, of_days: row.ofDays,
          share: estimate.percentAvailable ? roundTo(row.probability, 4) : null,
          interval: row.interval ? [roundTo(row.interval.low, 4), roundTo(row.interval.high, 4)] : null,
          distance_m: here ? null : roundTo(row.distance, 1), bearing_deg: here ? null : normalBearing(row.bearing)
        };
      }),
      elsewhere_share: elsewhere && estimate.percentAvailable ? roundTo(elsewhere.row.probability, 4) : null,
      destinations: (estimate.destinations ? estimate.destinations.rows : []).slice(0, 20).map(function (row, index) {
        return { number: index + 1, label: clip(placeLabel(row.address, row.key)), lat: row.lat, lon: row.lon, share: roundTo(row.probability, 4) };
      }),
      hazards: hazard ? [{
        category: clip(categoryLabel(hazard.category), 200), name: hazard.place.name ? clip(hazard.place.name, 200) : null,
        lat: hazard.place.lat, lon: hazard.place.lon, distance_m: roundTo(hazard.distance, 1),
        bearing_deg: normalBearing(G.bearingDegrees(last.lat, last.lon, hazard.place.lat, hazard.place.lon))
      }] : [],
      headline: clip(headlineText(estimate)),
      confidence: estimate.confidence ? { level: estimate.confidence.level, reason: clip(estimate.confidence.reason) } : null,
      warnings: shownWarnings(estimate).slice(0, 30).map(function (text) { return clip(text); }),
      model: {
        name: "GEOSnap crystal ball",
        application: clip((payload.provenance && payload.provenance.application) || "GEOSnap", 100),
        parameters: {
          stop_radius_m: parameters.stop_radius_m, stop_min_minutes: parameters.stop_min_minutes, max_accuracy_m: parameters.max_accuracy_m,
          day_weighting: estimate.dayWeighting, ladder_level: estimate.ladder ? estimate.ladder.level : null,
          comparable_days: estimate.ladder ? estimate.ladder.days : null, analysed_points: estimate.analysedPoints,
          accuracy_confidence: runParameters.accuracy_confidence || null, accuracy_scale: roundTo(accuracyScale, 4),
          excluded_positioning_methods: runParameters.excluded_positioning_methods ? runParameters.excluded_positioning_methods.join(", ") || "none" : null
        }
      }
    };
  }
  function shortHash(digest) { return digest.slice(0, 12) + "…"; }
  var SHA256_PATTERN = /^[0-9a-f]{64}$/;
  // The SHA-256 of the new last line of records.jsonl, to be noted outside the project: a
  // later rewrite of the whole chain would change it.
  function chainAnchorBlock(lastLineDigest) {
    var box = element("div", null, "crystal-chain-anchor");
    box.appendChild(element("p", "SHA-256 of the last line of records.jsonl. Note this value in the case file: it anchors the hash chain."));
    var row = element("div", null, "crystal-chain-row");
    var digestText = element("code", lastLineDigest, "crystal-chain-digest");
    var copyButton = element("button", "Copy");
    copyButton.type = "button";
    copyButton.title = "Copy the SHA-256 to the clipboard";
    function selectDigest() {
      var range = document.createRange();
      range.selectNodeContents(digestText);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copyButton.textContent = "Selected: press Ctrl+C";
    }
    copyButton.addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(lastLineDigest).then(function () { copyButton.textContent = "Copied"; }, selectDigest);
      } else {
        selectDigest();
      }
    });
    row.appendChild(digestText);
    row.appendChild(copyButton);
    box.appendChild(row);
    return box;
  }
  // The Speed tool shows the same anchor after recording a speed range.
  G.chainAnchorBlock = chainAnchorBlock;
  function recordSearchArea() {
    if (!recordingPossible || recording || shownEstimate === null) { return; }
    var body = JSON.stringify(buildSearchAreaRecord());
    recording = true;
    updateRecordControls("Recording…");
    fetch(RECORD_ROUTE, { method: "POST", headers: { "Content-Type": "application/json" }, body: body, cache: "no-store", credentials: "omit" })
      .then(function (response) {
        return response.json().catch(function () { return { error: "HTTP " + response.status }; }).then(function (answer) {
          if (!response.ok) { throw new Error(answer.error || "HTTP " + response.status); }
          return answer;
        });
      })
      .then(function (answer) {
        recording = false;
        updateRecordControls("Recorded as search area " + answer.record_number + " in search_areas/ (SHA-256 in records.jsonl):");
        var list = element("ul", null, "crystal-record-files");
        Object.keys(answer.files).sort().forEach(function (name) { list.appendChild(element("li", name + " · " + shortHash(answer.files[name]))); });
        recordStatus.appendChild(list);
        if (SHA256_PATTERN.test(String(answer.last_line_sha256))) { recordStatus.appendChild(chainAnchorBlock(answer.last_line_sha256)); }
      })
      .catch(function (error) {
        recording = false;
        updateRecordControls("Not recorded: " + (error && error.message ? error.message : "GEOSnap did not answer (is it still running?)") + ".");
      });
  }
  function downloadSearchArea() {
    if (shownEstimate === null) { return; }
    G.downloadBlob(G.derivedName("json", "search_area"), new Blob([JSON.stringify(buildSearchAreaRecord(), null, 2)], { type: "application/json" }));
    updateRecordControls("Downloaded as a _derived file: not recorded, not hashed.");
  }
  recordButton.addEventListener("click", recordSearchArea);
  downloadButton.addEventListener("click", downloadSearchArea);

  var reference = null;
  function recompute() {
    if (!analysis || !analysis.last) {
      resultBox.textContent = "";
      resultBox.appendChild(element("p", selectedSourceId === null ? "No source shown: tick a source in the Sources window." : "No analysable last known position for this source.", "warning"));
      layer.clearLayers();
      if (map.hasLayer(layer)) { map.removeLayer(layer); }
      shownReference = null;
      shownEstimate = null;
      updateRecordControls("");
      return;
    }
    var referenceLocal = reference.get();
    if (referenceLocal === null) { return; }
    if (forecastContext === null) {
      forecastContext = G.forecast.prepare({
        points: analysedPoints(),
        last: lastWithScale(),
        stays: analysis.stays,
        gaps: analysis.gaps,
        segments: analysis.segments,
        stopRadiusM: analysis.parameters.stop_radius_m,
        stopMinMinutes: analysis.parameters.stop_min_minutes,
        zoneTransitions: payload.zone_transitions
      });
    }
    var referenceSeconds = utcSecondsOf(referenceLocal);
    var estimate = G.forecast.estimate({
      context: forecastContext,
      referenceUtcSeconds: referenceSeconds,
      referenceLocal: referenceLocal,
      dayWeighting: recencyBox.checked ? "recent" : "equal"
    });
    renderResult(estimate, referenceLocal, referenceSeconds);
    shownReference = estimate.elapsedSeconds > 0 ? G.localText(referenceLocal, G.utcToLocal(referenceSeconds).offset) : null;
  }
  G.crystalBallDescription = function () {
    if (!map.hasLayer(layer) || shownReference === null) { return null; }
    var extras = [];
    if (drawn.rose) { extras.push("direction rose"); }
    if (drawn.tendency) { extras.push("tendency direction"); }
    if (drawn.corridors) { extras.push("routes from here"); }
    if (drawn.cone) { extras.push("extrapolation cone"); }
    return "crystal ball estimate for " + G.sourceName(selectedSourceId) + " shown (reference " + shownReference +
      (extras.length ? "; " + extras.join(", ") : "") + ")";
  };

  // ---- source select: shown sources, default the first -------------------------------
  var sourceSelect = document.getElementById("crystal-source");
  function selectSource(sourceId) {
    selectedSourceId = sourceId;
    analysis = sourceId === null ? null : G.sourceAnalysis(sourceId);
    forecastContext = null;
    fittedExtent = 0;
  }
  function fillSourceSelect() {
    var shown = G.visibleSources();
    var stillShown = shown.some(function (source) { return source.id === selectedSourceId; });
    var chosen = stillShown ? selectedSourceId : (shown.length ? shown[0].id : null);
    sourceSelect.textContent = "";
    shown.forEach(function (source) {
      var option = element("option", G.sourceLabel(source.id));
      option.value = String(source.id);
      sourceSelect.appendChild(option);
    });
    if (chosen !== null) { sourceSelect.value = String(chosen); }
    if (chosen !== selectedSourceId) { selectSource(chosen); }
    return chosen;
  }
  fillSourceSelect();
  sourceSelect.addEventListener("change", function () {
    selectSource(Number(sourceSelect.value));
    if (reference !== null) { recompute(); }
  });
  G.onSourcesChange(function () {
    fillSourceSelect();
    if (reference !== null && !windowElement.hidden) { recompute(); }
  });

  // Folding goes through the title bar's chevron (G.setWindowCollapsed, map_core.js); a check
  // started here folds this window, the Crystal button expands it again.
  function open() {
    document.getElementById("help-window").hidden = true;
    windowElement.hidden = false;
    G.setWindowCollapsed(windowElement, false);
    var anyLast = G.sources.some(function (source) {
      var sourceAnalysis = G.sourceAnalysis(source.id);
      return sourceAnalysis && sourceAnalysis.last;
    });
    if (!anyLast) {
      notice.textContent = "";
      notice.appendChild(element("p", "No analysable last known position in this project.", "warning"));
      controls.hidden = true;
      return;
    }
    if (acknowledged) { recompute(); }
  }
  function close() {
    windowElement.hidden = true;
    layer.clearLayers();
    fittedExtent = 0;
    if (map.hasLayer(layer)) { map.removeLayer(layer); }
  }
  G.closeCrystalBall = close;

  document.getElementById("crystal-button").addEventListener("click", function () {
    if (windowElement.hidden) { open(); } else if (windowElement.classList.contains("collapsed")) { G.setWindowCollapsed(windowElement, false); } else { close(); }
  });
  document.getElementById("crystal-close").addEventListener("click", close);
  document.getElementById("crystal-acknowledge").addEventListener("click", function () {
    acknowledged = true;
    notice.hidden = true;
    controls.hidden = false;
    reference = G.dateTimeControl(document.getElementById("crystal-reference"));
    reference.set(localNow());
    reference.onChange(recompute);
    document.getElementById("crystal-now").addEventListener("click", function () {
      reference.set(localNow());
      recompute();
    });
    showBox.addEventListener("change", recompute);
    recencyBox.addEventListener("change", recompute);
    corridorsBox.addEventListener("change", recompute);
    recompute();
  });

  G.addLegendEntry("Crystal ball: 50 / 80 / 95 % search rings", { colour: "rgba(106,27,154,0.5)", note: "estimate, not evidence" });
  G.addLegendEntry("Crystal ball: tendency direction (sector = its uncertainty)", { colour: TENDENCY_COLOUR, shape: "dashed" });
  G.addLegendEntry("Crystal ball: ranked known place", { colour: "#6a1b9a", shape: "text", text: "1" });
  G.addLegendEntry("Crystal ball: direction rose (length = 80 % reach, shade = share of days)", { colour: ROSE_COLOUR });
  G.addLegendEntry("Crystal ball: routes from here (dashed = silent during the trip)", { colour: CORRIDOR_COLOURS[1], shape: "line" });
  G.addLegendEntry("Crystal ball: matching destination while moving", { colour: ROSE_COLOUR, shape: "text", text: "▣" });
  G.addLegendEntry("Crystal ball: extrapolation cone", { colour: CONE_COLOUR, shape: "dashed" });
})();
