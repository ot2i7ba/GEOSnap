// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  var G = window.GEOSNAP;
  var analysis = G.payload.analysis;
  var places = G.payload.places || {};

  var CLASS_COLOURS = {
    unknown: "#9e9e9e",
    stationary: "#8c8c8c",
    walking: "#2e8b57",
    cycling: "#1e64c8",
    vehicle: "#e08a00",
    implausible: "#d01818"
  };
  // The place catalogue (label, group, colour, emoji, density, rules) comes from the payload.
  // Styles, canonical order and groups are derived from it here.
  var placeCatalogue = (G.payload.online && G.payload.online.place_catalogue) || [];
  var PLACE_STYLES = {};
  var PLACE_CATEGORY_ORDER = [];
  var PLACE_GROUPS = [];
  placeCatalogue.forEach(function (category) {
    PLACE_STYLES[category.key] = category;
    PLACE_CATEGORY_ORDER.push(category.key);
    var group = PLACE_GROUPS.filter(function (candidate) { return candidate.name === category.group; })[0];
    if (!group) {
      group = { name: category.group, colour: category.colour, categories: [] };
      PLACE_GROUPS.push(group);
    }
    group.categories.push(category.key);
  });
  var UNKNOWN_PLACE_STYLE = { colour: "#555555", emoji: "?", label: "unknown category", group: "unknown" };
  G.CLASS_COLOURS = CLASS_COLOURS;
  G.PLACE_STYLES = PLACE_STYLES;
  G.PLACE_CATEGORY_ORDER = PLACE_CATEGORY_ORDER;
  G.PLACE_GROUPS = PLACE_GROUPS;
  G.placeLayers = {};
  G.placeCounts = {};
  G.setEmbeddedPlacesVisible = function (activeCategories) {
    Object.keys(G.placeLayers).forEach(function (category) {
      var layer = G.placeLayers[category];
      var wanted = activeCategories.indexOf(category) >= 0;
      if (wanted && !G.map.hasLayer(layer)) { layer.addTo(G.map); }
      if (!wanted && G.map.hasLayer(layer)) { G.map.removeLayer(layer); }
    });
  };

  G.haversineMetres = function (lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180;
    var dLat = (lat2 - lat1) * toRad;
    var dLon = (lon2 - lon1) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * 6371000 * Math.asin(Math.sqrt(a));
  };

  G.bearingDegrees = function (lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180;
    var phi1 = lat1 * toRad, phi2 = lat2 * toRad, dLon = (lon2 - lon1) * toRad;
    var x = Math.sin(dLon) * Math.cos(phi2);
    var y = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLon);
    return (Math.atan2(x, y) * 180 / Math.PI + 360) % 360;
  };

  // Segments are keyed by source: line numbers repeat across sources.
  var segmentIndex = {};
  G.sources.forEach(function (source) {
    var sourceAnalysis = G.sourceAnalysis(source.id);
    (sourceAnalysis ? sourceAnalysis.segments : []).forEach(function (segment) {
      segmentIndex[source.id + ":" + segment.from_line + "-" + segment.to_line] = segment;
    });
  });
  G.segmentBetween = function (pointA, pointB) {
    if (pointA.s !== pointB.s) { return null; }
    return segmentIndex[pointA.s + ":" + pointA.n + "-" + pointB.n] || null;
  };

  var textTooltip = G.rowsElement;

  // styles: inline style properties for the label box (source colour); iconAnchor (optional)
  // moves the whole label, e.g. to stack labels that share a position.
  function labelMarker(latLng, className, text, tooltipRows, styles, iconAnchor) {
    var icon = L.divIcon({ className: "", html: "", iconSize: null, iconAnchor: iconAnchor || null });
    var marker = L.marker(latLng, { icon: icon, interactive: !!tooltipRows });
    marker.on("add", function () {
      var element = marker.getElement();
      if (!element) { return; }
      element.innerHTML = "";
      var box = document.createElement("div");
      box.className = className;
      box.textContent = text;
      Object.keys(styles || {}).forEach(function (property) { box.style[property] = styles[property]; });
      element.appendChild(box);
    });
    if (tooltipRows) { marker.bindRowsTooltip(function () { return textTooltip(tooltipRows); }); }
    return marker;
  }

  // A round badge whose ring shows the given colours in equal sectors around a symbol.
  function ringGradient(colours) {
    if (colours.length === 1) { return colours[0]; }
    var step = 360 / colours.length;
    return "conic-gradient(" + colours.map(function (colour, index) {
      return colour + " " + (index * step) + "deg " + ((index + 1) * step) + "deg";
    }).join(", ") + ")";
  }
  // Drawn above place and saved-place badges (below the time cursor at 1000).
  function ringBadge(latLng, size, colours, symbol, className) {
    var marker = L.marker(latLng, { icon: L.divIcon({ className: "", html: "", iconSize: [size, size], iconAnchor: [size / 2, size / 2] }), zIndexOffset: 500 });
    marker.on("add", function () {
      var element = marker.getElement();
      if (!element) { return; }
      element.textContent = "";
      var ring = document.createElement("div");
      ring.className = className;
      ring.style.background = ringGradient(colours);
      ring.appendChild(G.textElement(symbol));
      element.appendChild(ring);
    });
    return marker;
  }

  var classLayer = L.layerGroup();
  var arrowLayer = L.layerGroup();
  var numberLayer = L.layerGroup();
  var stayLayer = L.layerGroup();
  var gapLayer = L.layerGroup();
  var lastLayer = L.layerGroup();
  var ARROW_LIMIT = 1500;
  var NUMBER_LIMIT = 600;

  // Consecutive points of one source; sequence numbers count within the source. `parts` names
  // the layers to build (classes, arrows, numbers): each is rebuilt only while it is shown.
  function drawSourceMovement(points, arrowStep, numberStep, parts) {
    for (var i = 0; i < points.length - 1 && (parts.classes || parts.arrows); i++) {
      var a = points[i], b = points[i + 1];
      var segment = G.segmentBetween(a, b);
      var movementClass = segment ? segment.movement_class : "unknown";
      var recordWord = G.recordNoun(a.s, true) + " ";
      var rows = segment
        ? [["Source", G.sourceLabel(a.s)], ["Segment", recordWord + a.n + " → " + b.n], ["Class", movementClass],
           ["Distance", G.formatDistance(segment.distance_m)], ["Duration", G.formatDuration(segment.duration_seconds / 60)],
           ["Speed", G.formatSpeed(segment.speed_kmh)],
           ["Speed interval", segment.speed_low_kmh === undefined ? null
             : (G.speedIntervalWithReportedText(segment.speed_low_kmh, segment.speed_high_kmh, segment.speed_low_reported_kmh, segment.speed_high_reported_kmh, segment.speed_kmh) ||
                "none: an accuracy is not reported")],
           ["Bearing", segment.bearing_deg + "°"]]
        : [["Source", G.sourceLabel(a.s)], ["Segment", recordWord + a.n + " → " + b.n], ["Class", "not analysed (points between are hidden or excluded)"]];
      // `var` is function-scoped, so the tooltip closure must capture this iteration's rows.
      if (parts.classes) {
        (function (segmentRows) {
          L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
            color: CLASS_COLOURS[movementClass],
            weight: movementClass === "implausible" ? 4 : 3,
            opacity: 0.9,
            dashArray: movementClass === "implausible" ? "6 6" : (segment ? null : "2 6")
          }).bindRowsTooltip(function () { return textTooltip(segmentRows); }).addTo(classLayer);
        })(rows);
      }
      if (parts.arrows && i % arrowStep === 0) {
        var bearing = segment ? segment.bearing_deg : G.bearingDegrees(a.lat, a.lon, b.lat, b.lon);
        var midpoint = [(a.lat + b.lat) / 2, (a.lon + b.lon) / 2];
        var arrow = L.marker(midpoint, { icon: L.divIcon({ className: "", html: "", iconSize: [10, 12], iconAnchor: [5, 6] }), interactive: false });
        arrow.on("add", (function (bearingValue) {
          return function () {
            var element = this.getElement();
            if (!element) { return; }
            element.innerHTML = "";
            var shape = document.createElement("div");
            shape.className = "direction-arrow";
            shape.style.transform = "rotate(" + bearingValue.toFixed(0) + "deg)";
            element.appendChild(shape);
          };
        })(bearing));
        arrow.addTo(arrowLayer);
      }
    }
    for (var j = 0; parts.numbers && j < points.length; j += numberStep) {
      labelMarker([points[j].lat, points[j].lon], "sequence-label", String(j + 1), null, { borderColor: G.sourceColour(points[j].s) }).addTo(numberLayer);
    }
  }

  function movementBuilder(layer, parts) {
    return function (visible) {
      layer.clearLayers();
      var arrowStep = Math.max(1, Math.ceil(visible.length / ARROW_LIMIT));
      var numberStep = Math.max(1, Math.ceil(visible.length / NUMBER_LIMIT));
      G.visibleSources().forEach(function (source) {
        drawSourceMovement(G.lastVisibleBySource[source.id] || [], arrowStep, numberStep, parts);
      });
    };
  }
  G.deferredRender(classLayer, movementBuilder(classLayer, { classes: true }));
  G.deferredRender(arrowLayer, movementBuilder(arrowLayer, { arrows: true }));
  G.deferredRender(numberLayer, movementBuilder(numberLayer, { numbers: true }));

  G.registerOverlay("Movement classes", classLayer, false, { group: "Movement", dependsOn: "Route", requires: "route", tip: "Route segments coloured by movement class" });
  G.registerOverlay("Direction arrows", arrowLayer, false, { group: "Movement", dependsOn: "Route", requires: "route", tip: "Arrow at each segment midpoint in the direction of travel" });
  G.registerOverlay("Sequence numbers", numberLayer, false, { group: "Records", requires: "time", tip: "Chronological number of each shown record per source" });
  Object.keys(CLASS_COLOURS).forEach(function (name) {
    G.addLegendEntry("Movement: " + name, { colour: CLASS_COLOURS[name], shape: name === "implausible" ? "dashed" : "line" });
  });
  G.addLegendEntry("Direction of travel", { shape: "text", text: "▲" });
  G.addLegendEntry("Sequence number (chronological per source)", { shape: "text", text: "12" });

  function addressText(address) {
    return address && address.display_name ? address.display_name : "(no address)";
  }

  // Popup description for G.bindContextActions: title and one "Check surroundings" target.
  function contextDescription(label, lat, lon, extentMetres) {
    return function () { return { title: label, targets: [G.checkTarget(label, lat, lon, extentMetres)] }; };
  }

  // Stays, gaps and the last known position of every shown source, in the source colour.
  function drawSourceAnalysis(source) {
    var sourceAnalysis = G.sourceAnalysis(source.id);
    if (!sourceAnalysis) { return; }
    var colour = G.sourceColour(source.id);
    var sourceRow = ["Source", G.sourceLabel(source.id)];
    // Stays and last positions without an accuracy (KML, GPX, some CSV/Google records) show
    // "not reported" instead of "0 m", judged per entry.
    var recordWord = G.recordNoun(source.id, true) + " ";
    function accuracyKnown(entry) { return entry.accuracy_known !== false; }
    // The analysed record before (after) the stay bounds the arrival (departure); without
    // one outside the stop radius the device may have been there already (stayed on).
    function stayBoundRows(stay) {
      var rows = [];
      if (stay.arrived_after_local !== undefined) {
        rows.push(["Arrived between", stay.arrived_after_local === null ? "not bounded"
          : G.localText(stay.arrived_after_local, stay.arrived_after_offset) + " and " + G.localText(stay.arrived_by_local, stay.arrived_by_offset)]);
      }
      if (stay.left_before_local !== undefined) {
        rows.push(["Left between", stay.left_before_local === null ? "not bounded"
          : G.localText(stay.leave_local, stay.leave_offset) + " and " + G.localText(stay.left_before_local, stay.left_before_offset)]);
      }
      return rows;
    }
    sourceAnalysis.stays.forEach(function (stay) {
      var latLng = [stay.lat, stay.lon];
      var rows = [
        sourceRow,
        ["Stay", stay.id + (stay.is_last ? " (last stay)" : "")],
        ["Arrived (local)", G.localText(stay.arrive_local, stay.arrive_offset)],
        ["Left (local)", G.localText(stay.leave_local, stay.leave_offset)]
      ].concat(stayBoundRows(stay), [
        ["Duration", G.formatDuration(stay.duration_minutes)],
        ["Points", stay.point_count + " (" + recordWord + stay.first_line + "-" + stay.last_line + ")"],
        ["Mean accuracy", accuracyKnown(stay) ? stay.mean_accuracy_m + " m" : "not reported"],
        ["Address", addressText(stay.address)]
      ]);
      var stayActions = contextDescription("Stay " + stay.id + " · " + source.label, stay.lat, stay.lon, sourceAnalysis.parameters.stop_radius_m);
      G.bindContextActions(L.circle(latLng, { radius: sourceAnalysis.parameters.stop_radius_m, color: colour, weight: stay.is_last ? 4 : 2, fillColor: colour, fillOpacity: 0.25 })
        .bindRowsTooltip(function () { return textTooltip(rows); }), stayActions)
        .addTo(stayLayer);
      G.bindContextActions(labelMarker(latLng, "stay-label", "Stay " + stay.id + " · " + G.formatDuration(stay.duration_minutes), rows, { borderColor: colour }), stayActions).addTo(stayLayer);
    });
    sourceAnalysis.gaps.forEach(function (gap) {
      // Gap endpoints carry their own coordinates: the map may have collapsed or thinned them away.
      var from = { lat: gap.from_lat, lon: gap.from_lon }, to = { lat: gap.to_lat, lon: gap.to_lon };
      var rows = [
        sourceRow,
        ["Gap", gap.id],
        ["No records from (local)", G.localText(gap.start_local, gap.start_offset)],
        ["until (local)", G.localText(gap.end_local, gap.end_offset)],
        ["Duration", G.formatDuration(gap.duration_minutes)],
        ["Distance between the two points", G.formatDistance(gap.distance_m)]
      ];
      L.polyline([[from.lat, from.lon], [to.lat, to.lon]], { color: colour, weight: 3, dashArray: "8 8", opacity: 0.9 })
        .bindRowsTooltip(function () { return textTooltip(rows); })
        .addTo(gapLayer);
    });
    var last = sourceAnalysis.last;
    if (last) {
      var lastRows = [
        sourceRow,
        ["Last known position (local)", G.localText(last.local, last.offset)],
        ["UTC", last.utc],
        ["Age when generated", G.formatDuration(last.age_minutes)],
        ["Accuracy", accuracyKnown(last) ? "±" + last.accuracy_m + " m" : "not reported"],
        [G.recordNounTitle(source.id), last.line],
        ["Address", addressText(last.address)]
      ];
      var lastActions = contextDescription("Last known · " + source.label, last.lat, last.lon, accuracyKnown(last) ? last.accuracy_m * G.accuracyScaleOf(source.id) : 0);
      G.bindContextActions(L.circleMarker([last.lat, last.lon], { radius: 11, color: "#ffffff", weight: 3, fillColor: colour, fillOpacity: 1 })
        .bindRowsTooltip(function () { return textTooltip(lastRows); }), lastActions)
        .addTo(lastLayer);
      G.bindContextActions(labelMarker([last.lat, last.lon], "last-label", "Last known · " + source.label + " · " + G.localText(last.local, last.offset), lastRows, { background: colour }), lastActions).addTo(lastLayer);
    }
  }
  function drawAnalysisLayers() {
    stayLayer.clearLayers();
    gapLayer.clearLayers();
    lastLayer.clearLayers();
    G.visibleSources().forEach(drawSourceAnalysis);
  }
  G.registerOverlay("Stays", stayLayer, false, { group: "Movement", requires: "stays", tip: "Places where a source stayed a while (analysis)" });
  G.registerOverlay("Gaps", gapLayer, false, { group: "Movement", requires: "gaps", tip: "Spans without records between two positions (analysis)" });
  G.registerOverlay("Last known position", lastLayer, true, { group: "Movement", requires: "last_known", tip: "Newest analysable position per source" });
  G.addLegendEntry("Stay (circle = stop radius, label = duration; source colour)", { colour: "rgba(91,104,117,0.35)" });
  G.addLegendEntry("Gap (no records between the two points; source colour)", { colour: "#5b6875", shape: "dashed" });
  G.addLegendEntry("Last known position per source", { colour: "#5b6875" });

  // Records without a timestamp have no layer of their own: they sit in the Points layer as
  // hollow rings (map_core.js).

  // ---- encounters and shared places (across sources) ----------------------------------
  var encounters = (analysis && analysis.encounters) || [];
  var sharedPlaces = (analysis && analysis.shared_places) || [];
  var encounterLayer = L.layerGroup();
  var sharedPlaceLayer = L.layerGroup();
  var encounterDetailLayer = L.layerGroup().addTo(G.map);

  // "40 s", "2 min 5 s": a time offset between two reports, rounded down so the smallest
  // offset is never overstated.
  function offsetText(seconds) {
    var whole = Math.floor(seconds + 1e-9);
    if (whole < 60) { return whole + " s"; }
    if (whole >= 3600) { return G.formatDuration(Math.floor(whole / 60)); }
    return Math.floor(whole / 60) + " min" + (whole % 60 ? " " + whole % 60 + " s" : "");
  }
  // The smallest time offset of the coinciding report pairs and that of the closest pair,
  // and how much later a local time that occurred twice may lie; null for an analysis
  // without them.
  G.encounterOffsetText = function (encounter) {
    if (typeof encounter.min_time_offset_seconds !== "number") { return null; }
    var ambiguity = encounter.time_ambiguity_seconds;
    return "smallest time offset " + offsetText(encounter.min_time_offset_seconds) +
      " (closest pair " + offsetText(encounter.closest_pair_offset_seconds) + ")" +
      (typeof ambiguity === "number" && ambiguity > 0 ? "; a local time occurred twice: may lie up to " + Math.ceil(ambiguity / 60) + " min later" : "");
  };
  // The class, "(only within accuracy)" added when the reports never came within the
  // encounter distance and the class does not already say so.
  G.encounterClassText = function (encounter) {
    return encounter.movement + (encounter.coincided_within_accuracy_only === true && encounter.movement !== "within accuracy" ? " (only within accuracy)" : "");
  };
  function encounterRows(encounter) {
    return [
      ["Encounter", encounter.id],
      ["Sources", G.sourceLabel(encounter.source_a) + " ↔ " + G.sourceLabel(encounter.source_b)],
      ["From (local)", G.localText(encounter.start_local, encounter.start_offset)],
      ["Until (local)", G.localText(encounter.end_local, encounter.end_offset)],
      ["Duration", G.formatDuration(encounter.duration_minutes)],
      ["Minimum distance", G.formatDistance(encounter.min_distance_m)],
      ["Time offsets", G.encounterOffsetText(encounter)],
      ["Class", G.encounterClassText(encounter) + (encounter.movement === "joint movement" ? ": both sources' reports moved while within the encounter time and distance, or their combined accuracy"
        : encounter.movement === "within accuracy" ? ": the reports never came within the encounter distance, only within it plus their accuracy" : "")],
      ["Path of the midpoints", G.formatDistance(encounter.path_length_m) + " in plausible steps · displacement " + G.formatDistance(encounter.displacement_m)],
      ["Excluded", encounter.implausible_steps ? G.formatDistance(encounter.path_excluded_m) + " in " + encounter.implausible_steps + (encounter.implausible_steps === 1 ? " implausible step" : " implausible steps") +
        " (no time passed, or " + analysis.parameters.implausible_speed_kmh + " km/h or faster: a position jump or real travel at that speed)" : "nothing"],
      ["Reports", G.sourceName(encounter.source_a) + ": " + encounter.hits_a + " · " + G.sourceName(encounter.source_b) + ": " + encounter.hits_b],
      ["Click", "shows both sources' reports during the encounter"]
    ];
  }
  // Both sources' reports inside the encounter window and a line between the closest pair;
  // cleared by a click on the map.
  G.showEncounter = function (encounter) {
    encounterDetailLayer.clearLayers();
    var jointParts = jointMovementLatLngs(encounter);
    if (jointParts.length) { G.map.fitBounds(L.latLngBounds(Array.prototype.concat.apply([], jointParts)).pad(0.2), { maxZoom: 17 }); }
    // Everything is drawn next to the encounter centre, also across the antimeridian.
    function latLngOf(point) { return [point.lat, G.longitudeNear(point.lon, encounter.lon)]; }
    var start = Date.parse(encounter.start_utc) / 1000, end = Date.parse(encounter.end_utc) / 1000;
    var inside = {};
    [encounter.source_a, encounter.source_b].forEach(function (sourceId) {
      var colour = G.sourceColour(sourceId);
      inside[sourceId] = G.allPoints.filter(function (point) {
        if (point.s !== sourceId) { return false; }
        var at = point.utcSeconds;
        return at >= start && at <= end;
      });
      if (inside[sourceId].length > 1) {
        L.polyline(inside[sourceId].map(latLngOf), { color: colour, weight: 3, opacity: 0.8, interactive: false }).addTo(encounterDetailLayer);
      }
      inside[sourceId].forEach(function (point) {
        L.circleMarker(latLngOf(point), { radius: 7, color: "#ffffff", weight: 2, fillColor: colour, fillOpacity: 1 })
          .bindRowsTooltip(function () { return G.tooltipElement(point); }).addTo(encounterDetailLayer);
      });
    });
    var closest = null;
    inside[encounter.source_a].forEach(function (pointA) {
      inside[encounter.source_b].forEach(function (pointB) {
        var distance = G.haversineMetres(pointA.lat, pointA.lon, pointB.lat, pointB.lon);
        if (closest === null || distance < closest.distance) { closest = { a: pointA, b: pointB, distance: distance }; }
      });
    });
    if (closest) {
      var minutesApart = Math.abs(closest.a.utcSeconds - closest.b.utcSeconds) / 60;
      L.polyline([latLngOf(closest.a), latLngOf(closest.b)], { color: "#1c2733", weight: 2, dashArray: "4 4" })
        .bindTooltip(G.textElement("Closest reports on this map: " + G.formatDistance(closest.distance) + ", " + G.formatDuration(minutesApart) + " apart"), { sticky: true })
        .addTo(encounterDetailLayer);
    }
    document.getElementById("status").textContent = "Encounter " + encounter.id + ": " + inside[encounter.source_a].length + " + " +
      inside[encounter.source_b].length + " reports on this map during the encounter (click the map to clear)";
  };
  G.map.on("click", function () { encounterDetailLayer.clearLayers(); });

  // The thinned midpoint path of a joint movement: one line per part (the path is broken at
  // position jumps), drawn next to the encounter centre, also across the antimeridian.
  function jointMovementLatLngs(encounter) {
    return (encounter.path_parts || []).map(function (part) {
      return part.map(function (vertex) { return [vertex[0], G.longitudeNear(vertex[1], encounter.lon)]; });
    });
  }
  function drawEncounters() {
    encounterLayer.clearLayers();
    encounterDetailLayer.clearLayers();
    encounters.forEach(function (encounter) {
      if (!G.isSourceVisible(encounter.source_a) || !G.isSourceVisible(encounter.source_b)) { return; }
      var colours = [G.sourceColour(encounter.source_a), G.sourceColour(encounter.source_b)];
      var shape;
      if (encounter.movement === "joint movement" && encounter.path_parts && encounter.path_parts.length) {
        // A line through the hit midpoints instead of the badge: one source colour, the
        // other dashed on top, on a dark casing. Its click must not reach the map, which
        // clears the detail layer.
        var latLngs = jointMovementLatLngs(encounter);
        L.polyline(latLngs, { color: G.inkColour, weight: 9, opacity: 0.85, interactive: false }).addTo(encounterLayer);
        L.polyline(latLngs, { color: colours[0], weight: 5, opacity: 1, interactive: false }).addTo(encounterLayer);
        shape = L.polyline(latLngs, { color: colours[1], weight: 5, opacity: 1, dashArray: "10 10", lineCap: "butt", bubblingMouseEvents: false });
      } else {
        shape = ringBadge([encounter.lat, encounter.lon], 26, colours, "🤝", "encounter-marker");
      }
      shape.bindRowsTooltip(function () { return textTooltip(encounterRows(encounter)); });
      shape.on("click", function () { G.showEncounter(encounter); });
      G.bindContextActions(shape, contextDescription("Encounter " + encounter.id + " · " + G.sourceName(encounter.source_a) + " ↔ " + G.sourceName(encounter.source_b),
        encounter.lat, encounter.lon, encounter.min_distance_m));
      shape.addTo(encounterLayer);
    });
  }
  function drawSharedPlaces() {
    sharedPlaceLayer.clearLayers();
    sharedPlaces.forEach(function (place) {
      var shown = place.sources.filter(G.isSourceVisible);
      if (shown.length < 2) { return; }
      var rows = [["Shared place", place.id], ["Sources", shown.map(G.sourceLabel).join(", ")], ["Overlapping visits", place.overlapping ? "yes" : "no"]];
      place.visits.forEach(function (visit) {
        if (!G.isSourceVisible(visit.source_id)) { return; }
        rows.push([G.sourceName(visit.source_id), G.localText(visit.arrive_local, visit.arrive_offset) + " – " + G.localText(visit.leave_local, visit.leave_offset) + " (stay " + visit.stay_id + ")"]);
      });
      var marker = ringBadge([place.lat, place.lon], 22, shown.map(G.sourceColour), "", "shared-place-marker");
      marker.bindRowsTooltip(function () { return textTooltip(rows); });
      marker.addTo(sharedPlaceLayer);
    });
  }
  // Registered only when present, so maps without them do not list the layer.
  if (encounters.length) {
    G.registerOverlay("Encounters", encounterLayer, true, { group: "Between sources", requires: "encounters", tip: "Where two sources reported close in time and place" });
    G.addLegendEntry("Encounter (ring = the two sources; click shows their reports)", { shape: "text", text: "🤝" });
    if (encounters.some(function (encounter) { return encounter.movement === "joint movement"; })) {
      G.addLegendEntry("Joint movement (both sources moved; line in both source colours, broken at position jumps)", { colour: G.inkColour, shape: "dashed" });
    }
  }
  if (sharedPlaces.length) {
    G.registerOverlay("Shared places", sharedPlaceLayer, false, { group: "Between sources", requires: "shared_places", tip: "Places where stays of two or more sources lie together" });
    G.addLegendEntry("Shared place (ring = sources that stayed there)", { shape: "text", text: "◎" });
  }

  // ---- case places: locations from the case file, checked at generation -----------------
  // A verdict describes the reports of a device, never a person. The texts match the report.
  var CASE_PLACE_VERDICT_TEXTS = {
    "present": "At least one record of this source lies inside the radius during the window.",
    "possibly present": "No record of this source shows the device inside the radius during the window, but at least one could: its accuracy circle reaches the radius, it carries no accuracy value and lies within the assumed accuracy, or it comes from an excluded positioning method and lies inside the radius or within the radius plus the larger of its accuracy and the assumed accuracy.",
    "elsewhere": "This source has records during the window, and every one lies farther from the place than the radius plus its accuracy. This covers only the moments of the records: the longest span of the window without a record is given with the verdict.",
    "no reports in window": "This source has no record during the window: nothing can be said about where the device was. This is not absence."
  };
  var CASE_PLACE_COLOUR = G.inkColour;
  var casePlaceBlock = (analysis && analysis.case_places) || null;
  var casePlaces = casePlaceBlock ? casePlaceBlock.places : [];
  var casePlaceLayer = L.layerGroup();
  G.casePlaces = casePlaces;
  G.CASE_PLACE_VERDICT_TEXTS = CASE_PLACE_VERDICT_TEXTS;

  function casePlaceWindowText(place) {
    if (!place.window) { return "none (visits and closest approach only)"; }
    return G.localText(place.window.from_local, place.window.from_offset) + " – " + G.localText(place.window.to_local, place.window.to_offset);
  }
  // "14:03:10, 35 m from the centre, ±10 m, line 7"
  function reportDistanceText(report, sourceId) {
    if (!report) { return "none"; }
    return G.localText(report.local, report.offset) + ", " + G.formatDistance(report.distance_m) + " from the centre, " +
      (report.accuracy_known ? "±" + report.accuracy_m + " m" : "accuracy not reported") +
      (typeof report.positioning_method === "string" ? ", " + report.positioning_method : "") + ", " + G.recordNoun(sourceId, false) + " " + report.line;
  }
  function casePlaceVerdictText(check) {
    if (!check.window) { return null; }
    return check.window.verdict + (check.window.basis ? " (" + check.window.basis + ")" : "");
  }
  G.casePlaceVerdictText = casePlaceVerdictText;
  // A verdict covers only the moments of the reports: what the window holds besides them.
  function casePlaceCoverageText(check) {
    var windowCheck = check.window;
    if (!windowCheck || !windowCheck.reports_in_window || windowCheck.longest_unobserved_minutes === null || windowCheck.longest_unobserved_minutes === undefined) { return null; }
    return "reports from " + G.localText(windowCheck.first_report_local, windowCheck.first_report_offset) + " to " +
      G.localText(windowCheck.last_report_local, windowCheck.last_report_offset) + "; longest span without a report " + G.formatDuration(windowCheck.longest_unobserved_minutes);
  }
  G.casePlaceCoverageText = casePlaceCoverageText;
  function accuracyText(accuracy) { return accuracy === null || accuracy === undefined ? "accuracy not reported" : "±" + accuracy + " m"; }
  function bestVisitAccuracy(check) {
    var best = null;
    check.visits.forEach(function (visit) {
      if (visit.best_accuracy_m !== null && visit.best_accuracy_m !== undefined && (best === null || visit.best_accuracy_m < best)) { best = visit.best_accuracy_m; }
    });
    return best;
  }
  function shownChecks(place) {
    return place.checks.filter(function (check) { return G.isSourceVisible(check.source_id); });
  }
  function casePlaceTooltipRows(place) {
    var rows = [["Case place", place.label], ["Radius", G.formatDistance(place.radius_m)], ["Window (local)", casePlaceWindowText(place)]];
    shownChecks(place).forEach(function (check) {
      rows.push([G.sourceName(check.source_id), (casePlaceVerdictText(check) || "no window") + " · " + check.visit_count + (check.visit_count === 1 ? " visit" : " visits")]);
    });
    rows.push(["Click", "verdicts, visits and closest approach per source"]);
    return rows;
  }
  function popupLine(container, label, value) {
    var line = G.element("div", null, "case-place-line");
    line.appendChild(G.element("span", label + ": ", "label"));
    line.appendChild(G.element("span", value === null || value === undefined || value === "" ? "-" : String(value)));
    container.appendChild(line);
  }
  function casePlacePopup(place) {
    var box = G.element("div", null, "case-place-popup");
    box.appendChild(G.element("div", "Case place " + place.id + " · " + place.label, "context-title"));
    popupLine(box, "Position", place.lat + ", " + place.lon + " (" + place.location + ")");
    if (place.geocoded_display_name) { popupLine(box, "Address search answer", place.geocoded_display_name); }
    if (place.address) { popupLine(box, "Address", place.address); }
    if (place.note) { popupLine(box, "Note", place.note); }
    popupLine(box, "Radius", G.formatDistance(place.radius_m));
    popupLine(box, "Window (local)", casePlaceWindowText(place));
    var checks = shownChecks(place);
    if (!checks.length) { box.appendChild(G.element("p", "No source is shown: tick a source in the Sources window.", "case-place-note")); }
    checks.forEach(function (check) {
      var head = G.element("div", null, "case-place-source");
      var key = G.element("span", null, "case-place-key");
      key.style.background = G.sourceColour(check.source_id);
      head.appendChild(key);
      head.appendChild(G.element("strong", G.sourceLabel(check.source_id)));
      box.appendChild(head);
      if (check.window) {
        popupLine(box, "Window verdict", casePlaceVerdictText(check));
        // The verdict with the radii as reported, only where it differs.
        if (check.window.verdict_as_reported) {
          popupLine(box, "As reported", check.window.verdict_as_reported + (check.window.basis_as_reported ? " (" + check.window.basis_as_reported + ")" : ""));
        }
        box.appendChild(G.element("p", CASE_PLACE_VERDICT_TEXTS[check.window.verdict] || "", "case-place-note"));
        popupLine(box, "Reports in the window", check.window.reports_in_window);
        if (check.window.reports_in_window) {
          popupLine(box, "Closest report in the window", reportDistanceText(check.window.closest, check.source_id));
          popupLine(box, "First and last report in the window", G.localText(check.window.first_report_local, check.window.first_report_offset) + " to " +
            G.localText(check.window.last_report_local, check.window.last_report_offset));
          popupLine(box, "Longest span of the window without a report", G.formatDuration(check.window.longest_unobserved_minutes) + " (also before the first and after the last report)");
          if (check.window.reports_inside_radius) { popupLine(box, "Best accuracy inside the radius in the window", accuracyText(check.window.best_inside_accuracy_m)); }
        } else {
          popupLine(box, "Nearest report before the window", reportDistanceText(check.window.before, check.source_id));
          popupLine(box, "Nearest report after the window", reportDistanceText(check.window.after, check.source_id));
        }
      }
      popupLine(box, "Visits", check.visit_count + " (" + check.reports_inside_radius + " reports inside the radius" +
        (check.visit_count ? "; best accuracy of a visit: " + accuracyText(bestVisitAccuracy(check)) : "") + ")");
      popupLine(box, "Possible visits", check.possible_count + " reports that could lie inside the radius");
      if (check.unrated_count) { popupLine(box, "Without accuracy near the radius", check.unrated_count + " reports"); }
      popupLine(box, "Closest approach overall", check.closest ? reportDistanceText(check.closest, check.source_id) : "the source has no reports");
    });
    box.appendChild(G.element("p", "A verdict describes the reports of a device, never a person. Every visit is listed in case_places_<stamp>.csv.", "case-place-note"));
    if (G.surroundingsAvailable) {
      var actions = G.contextActionsElement("Surroundings of the case place", [G.checkTarget("Case place " + place.label, place.lat, place.lon, place.radius_m)]);
      box.appendChild(actions);
    }
    return box;
  }
  // maxHeight: Leaflet scrolls a long popup (several sources) inside the map.
  var casePlacePopupOptions = { className: "context-popup", maxWidth: 380, maxHeight: 300 };
  // The popup scrolls inside the map, whatever the map height.
  G.map.on("popupopen", function (event) {
    var popupElement = event.popup.getElement();
    if (!popupElement || !popupElement.querySelector(".case-place-popup")) { return; }
    var available = Math.max(140, G.map.getSize().y - 90);
    if (event.popup.options.maxHeight !== available) {
      event.popup.options.maxHeight = available;
      event.popup.update();
    }
  });
  function drawCasePlaces() {
    casePlaceLayer.clearLayers();
    // An open popup would keep the sources shown when it was opened.
    if (document.querySelector(".leaflet-popup .case-place-popup")) { G.map.closePopup(); }
    // The same position entered with several windows: the labels stack instead of covering
    // each other, so each stays readable and clickable.
    var labelsAtPosition = {};
    casePlaces.forEach(function (place) {
      if (place.lat === null || place.lon === null) { return; }
      var latLng = [place.lat, place.lon];
      var positionKey = place.lat.toFixed(5) + "," + place.lon.toFixed(5);
      var stackIndex = labelsAtPosition[positionKey] || 0;
      labelsAtPosition[positionKey] = stackIndex + 1;
      L.circle(latLng, { radius: place.radius_m, color: CASE_PLACE_COLOUR, weight: 2, dashArray: "6 4", fillColor: CASE_PLACE_COLOUR, fillOpacity: 0.07 })
        .bindRowsTooltip(function () { return textTooltip(casePlaceTooltipRows(place)); })
        .bindPopup(function () { return casePlacePopup(place); }, casePlacePopupOptions)
        .addTo(casePlaceLayer);
      labelMarker(latLng, "case-place-label", "📌 " + place.label, casePlaceTooltipRows(place), {}, [0, -20 * stackIndex])
        .bindPopup(function () { return casePlacePopup(place); }, casePlacePopupOptions)
        .addTo(casePlaceLayer);
    });
  }
  // Flies to a case place and opens its popup (timeline, keyboard).
  G.showCasePlace = function (place) {
    if (place.lat === null || place.lon === null) { return; }
    if (!G.map.hasLayer(casePlaceLayer)) { casePlaceLayer.addTo(G.map); }
    G.map.setView([place.lat, place.lon], Math.max(G.map.getZoom(), 16));
    L.popup(casePlacePopupOptions).setLatLng([place.lat, place.lon]).setContent(casePlacePopup(place)).openOn(G.map);
  };
  // Registered only when present, like the encounters.
  if (casePlaces.length) {
    G.registerOverlay("Case places", casePlaceLayer, true, { group: "Case", requires: "case_places", tip: "Locations from the case file with their verdicts" });
    G.addLegendEntry("Case place (dashed circle = radius entered by the examiner; click for the verdicts)", { shape: "text", text: "📌" });
  }
  // ---- end of case places --------------------------------------------------------------

  function drawSourceLayers() {
    drawAnalysisLayers();
    drawEncounters();
    drawSharedPlaces();
    drawCasePlaces();
  }
  drawSourceLayers();
  G.onSourcesChange(drawSourceLayers);
  G.onSourceColourChange(drawSourceLayers);

  // A place badge: round, group colour, category emoji; live results dashed, far centres faded.
  window.GEOSNAP.placeMarker = function (place, category, live) {
    var style = PLACE_STYLES[category] || UNKNOWN_PLACE_STYLE;
    var marker = L.marker([place.lat, place.lon], { icon: L.divIcon({ className: "", html: "", iconSize: [24, 24], iconAnchor: [12, 12] }) });
    marker.on("add", function () {
      var element = marker.getElement();
      if (!element) { return; }
      element.innerHTML = "";
      var badge = document.createElement("div");
      badge.className = "place-marker" + (live ? " live" : "") + (place.far_centre ? " far-centre" : "");
      badge.style.background = style.colour;
      badge.textContent = style.emoji;
      element.appendChild(badge);
    });
    var rows = [["Place", place.name || "(unnamed)"], ["Category", style.label + " (" + style.group + ")"], ["OSM", place.osm_type + " " + place.osm_id], ["Source", live ? "live check, not recorded" : "embedded at generation"]];
    if (place.far_centre) { rows.push(["Note", "large feature, its centre lies outside the search radius"]); }
    Object.keys(place.tags || {}).slice(0, 8).forEach(function (key) { rows.push([key, place.tags[key]]); });
    marker.bindRowsTooltip(function () { return textTooltip(rows); });
    return marker;
  };

  // Embedded categories in catalogue order; keys unknown to the catalogue follow, so nothing is dropped.
  var embeddedCategories = PLACE_CATEGORY_ORDER.filter(function (category) { return places[category]; })
    .concat(Object.keys(places).filter(function (category) { return !PLACE_STYLES[category]; }).sort());
  embeddedCategories.forEach(function (category) {
    var layer = L.layerGroup();
    places[category].forEach(function (place) { G.placeMarker(place, category, false).addTo(layer); });
    G.placeLayers[category] = layer;
    G.placeCounts[category] = places[category].length;
  });
  // Legend: only groups that actually have embedded places; otherwise one line when live checks exist.
  var embeddedGroups = PLACE_GROUPS.filter(function (group) {
    return group.categories.some(function (category) { return (G.placeCounts[category] || 0) > 0; });
  });
  var hasUnknownEmbedded = embeddedCategories.some(function (category) { return !PLACE_STYLES[category]; });
  embeddedGroups.forEach(function (group) {
    G.addLegendEntry("Places: " + group.name, { colour: group.colour });
  });
  if (hasUnknownEmbedded) {
    G.addLegendEntry("Places: other (category not in this catalogue)", { colour: UNKNOWN_PLACE_STYLE.colour });
  }
  if (embeddedGroups.length || hasUnknownEmbedded) {
    var sampleEmoji = embeddedGroups.length ? PLACE_STYLES[embeddedGroups[0].categories[0]].emoji : UNKNOWN_PLACE_STYLE.emoji;
    G.addLegendEntry("Place badge: colour = group, symbol = category (see Places window)", { shape: "text", text: sampleEmoji });
  } else if (G.payload.online && G.payload.online.enabled) {
    G.addLegendEntry("Places: none embedded (live checks only)", { colour: UNKNOWN_PLACE_STYLE.colour });
  }
})();
