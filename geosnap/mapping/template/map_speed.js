// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  // Speed: the per-record figures the payload carries (spd km/h with its
  // interval spd_lo/spd_hi, dpm m and dts seconds to the previous analysed record of the same
  // source, the movement class cls, and the cumulative cum/cmb of the full list, all computed
  // in Python) as a tooltip row, and the Speed tool, which computes a range of one source
  // with a lower bound for its average speed, lets the examiner exclude records inside it and
  // records the calculation through the GEOSnap server.
  var G = window.GEOSNAP;
  var map = G.map;
  var element = G.element;
  function byId(id) { return document.getElementById(id); }

  // ---- speed figures: numbers and wording, free of DOM and Leaflet ---------------------
  // tests/test_map_scripts.py runs this block in node against geosnap.analysis.speed_range.
  var WALKING_MAX_KMH = 7;                 // analysis/segments.py WALKING_MAX_KMH
  var STATIONARY_MAX_KMH = 1;              // analysis/segments.py STATIONARY_MAX_KMH
  var CYCLING_MAX_KMH = 25;                // analysis/segments.py CYCLING_MAX_KMH
  var DEFAULT_IMPLAUSIBLE_KMH = 250;   // geosnap/settings.py AnalysisSettings
  // WGS84 geodesics by GeographicLib (Karney 2013), the library the Python side uses.
  var GEODESIC = window.geodesic.Geodesic;
  function implausibleKmh() {
    var parameters = (G.payload.analysis && G.payload.analysis.parameters) || {};
    return parameters.implausible_speed_kmh || DEFAULT_IMPLAUSIBLE_KMH;
  }
  function geodesicMetres(lat1, lon1, lat2, lon2) {
    return GEODESIC.WGS84.Inverse(lat1, lon1, lat2, lon2, GEODESIC.DISTANCE).s12;
  }
  // Below walking speed the value reads in m/s, above it in km/h, each with one decimal; a
  // speed at or above the implausible threshold says so, as the movement classes do.
  function unitFor(kmh) {
    return kmh < WALKING_MAX_KMH ? { factor: 1 / 3.6, suffix: " m/s" } : { factor: 1, suffix: " km/h" };
  }
  function speedText(kmh) {
    if (kmh === null || kmh === undefined) { return "-"; }
    var unit = unitFor(kmh);
    var text = (kmh * unit.factor).toFixed(1) + unit.suffix;
    return kmh >= implausibleKmh() ? text + " (implausible)" : text;
  }
  // Bounds are rounded outwards to one decimal, never to the nearest value
  // (analysis/segments.py lower_bound_text_value / upper_bound_text_value).
  function lowerText(value, unit) { return (Math.floor(value * unit.factor * 10 + 1e-9) / 10).toFixed(1); }
  function upperText(value, unit) { return (Math.ceil(value * unit.factor * 10 - 1e-9) / 10).toFixed(1); }
  // The interval of a step in the unit of its estimate: "between 1.2 and 3.4 m/s", "at least
  // 1.2 m/s" without an upper bound, null without an interval (no accuracy reported).
  function intervalText(lowKmh, highKmh, estimateKmh) {
    if (lowKmh === null || lowKmh === undefined) { return null; }
    var unit = unitFor(estimateKmh === null || estimateKmh === undefined ? lowKmh : estimateKmh);
    if (highKmh === null || highKmh === undefined) {
      return "at least " + lowerText(lowKmh, unit) + unit.suffix + " (no upper bound: elapsed time within the time resolution)";
    }
    return "between " + lowerText(lowKmh, unit) + " and " + upperText(highKmh, unit) + unit.suffix;
  }
  // The interval at the uncertainty radii followed by the one at the reported radii, the
  // latter only when it reads differently: "between 1.2 and 3.4 m/s (as reported: 1.5-3.1)".
  function intervalWithReportedText(lowKmh, highKmh, lowReportedKmh, highReportedKmh, estimateKmh) {
    var text = intervalText(lowKmh, highKmh, estimateKmh);
    if (text === null || lowReportedKmh === null || lowReportedKmh === undefined) { return text; }
    var unit = unitFor(estimateKmh === null || estimateKmh === undefined ? lowKmh : estimateKmh);
    var same = lowerText(lowKmh, unit) === lowerText(lowReportedKmh, unit) &&
      (highKmh === null || highKmh === undefined ? highReportedKmh === null || highReportedKmh === undefined
        : highReportedKmh !== null && highReportedKmh !== undefined && upperText(highKmh, unit) === upperText(highReportedKmh, unit));
    if (same) { return text; }
    var reported = highReportedKmh === null || highReportedKmh === undefined
      ? "at least " + lowerText(lowReportedKmh, unit)
      : lowerText(lowReportedKmh, unit) + "\u2013" + upperText(highReportedKmh, unit);
    return text + " (as reported: " + reported + ")";
  }
  // The movement class of a bare speed, for payloads without cls: the thresholds of
  // analysis/segments.py without its accuracy and silence rules.
  function speedClass(kmh) {
    if (kmh === null || kmh === undefined) { return "unknown"; }
    if (kmh >= implausibleKmh()) { return "implausible"; }
    if (kmh < STATIONARY_MAX_KMH) { return "stationary"; }
    if (kmh < WALKING_MAX_KMH) { return "walking"; }
    if (kmh < CYCLING_MAX_KMH) { return "cycling"; }
    return "vehicle";
  }
  // A lower bound of a distance, rounded down (formatDistance rounds to the nearest value).
  function minimumDistanceText(metres) {
    return metres >= 1000 ? (Math.floor(metres / 10) / 100).toFixed(2) + " km" : Math.floor(metres) + " m";
  }
  function atLeastSpeedText(kmh) {
    var unit = unitFor(kmh);
    return "at least " + lowerText(kmh, unit) + unit.suffix;
  }
  function elapsedText(seconds) {
    if (seconds === null || seconds === undefined) { return "-"; }
    return seconds < 60 ? (Math.round(seconds * 1000) / 1000) + " s" : G.formatDuration(seconds / 60);
  }
  function resolutionText(seconds) {
    if (seconds === null || seconds === undefined) { return "-"; }
    if (seconds >= 60) { return "1 min"; }
    if (seconds >= 1) { return "1 s"; }
    return seconds >= 0.001 ? "1 ms" : "1 µs";
  }
  // Elapsed seconds between two records from their UTC texts, exact to the microsecond:
  // Date.parse keeps milliseconds at best, so whole seconds and fractions are subtracted apart.
  function utcParts(text) {
    var match = /^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$/.exec(text);
    return { whole: Date.parse(match[1] + match[3]) / 1000, fraction: match[2] ? Number("0" + match[2]) : 0 };
  }
  function elapsedBetween(first, last) {
    var a = utcParts(first.utc), b = utcParts(last.utc);
    return (b.whole - a.whole) + (b.fraction - a.fraction);
  }
  // The reported radius, and the uncertainty radius the bounds use: the reported one times
  // the factor to the confidence level of the analysis (payload asc, 1 when absent).
  function accuracyOf(point) { return point.acc_known === false ? null : point.radius; }
  function uncertaintyOf(point) {
    var accuracy = accuracyOf(point);
    return accuracy === null ? null : accuracy * (typeof point.asc === "number" ? point.asc : 1);
  }
  // An ambiguous local time was read as its earlier occurrence: the true instant can lie up
  // to this much later (payload tam, analysis/segments.py time_uncertainty_seconds).
  function ambiguityOf(point) { return typeof point.tam === "number" ? point.tam : 0; }
  function pairMinimum(distance, first, second) {
    var a = uncertaintyOf(first), b = uncertaintyOf(second);
    return a === null || b === null ? 0 : Math.max(0, distance - a - b);
  }
  function pairMinimumReported(distance, first, second) {
    var a = accuracyOf(first), b = accuracyOf(second);
    return a === null || b === null ? 0 : Math.max(0, distance - a - b);
  }
  // The cumulative minimum path at the reported radii (payload cmb_rep; absent: equal to cmb).
  function minimumPathReported(record) {
    return record.cmb_rep === null || record.cmb_rep === undefined ? record.cmb : record.cmb_rep;
  }
  // The interval of a step for a sum of two radii, as analysis/segments.py speed_bounds_kmh.
  function speedBounds(distance, seconds, radiusSum, timeUncertainty) {
    if (radiusSum === null) { return { low: null, high: null }; }
    return {
      low: Math.max(0, distance - radiusSum) / (seconds + timeUncertainty) * 3.6,
      high: seconds > timeUncertainty ? (distance + radiusSum) / (seconds - timeUncertainty) * 3.6 : null
    };
  }
  // A step between any two records, as analysis/segments.py computes it: geodesic distance,
  // elapsed time, estimate and the interval at the uncertainty and at the reported radii.
  function stepFigures(first, second, resolution) {
    var distance = geodesicMetres(first.lat, first.lon, second.lat, second.lon);
    var seconds = Math.abs(elapsedBetween(first, second));
    var uncertainty = resolution + ambiguityOf(first) + ambiguityOf(second);
    var a = uncertaintyOf(first), b = uncertaintyOf(second);
    var bounds = speedBounds(distance, seconds, a === null || b === null ? null : a + b, uncertainty);
    var reportedA = accuracyOf(first), reportedB = accuracyOf(second);
    var reported = speedBounds(distance, seconds, reportedA === null || reportedB === null ? null : reportedA + reportedB, uncertainty);
    return {
      distance: distance, seconds: seconds, kmh: seconds > 0 ? distance / seconds * 3.6 : null,
      low: bounds.low, high: bounds.high, lowReported: reported.low, highReported: reported.high
    };
  }
  // Where a range runs: `seq` is the record's position in the source's full, unthinned list of
  // analysed records. A file line (`n`) says nothing about the order - a CSV may
  // be sorted any way, and every record of a KML gx:Track placemark shares one line.
  function excludedFromAnalysis(point) {
    return point.seq === null || point.seq === undefined || point.cum === null;
  }
  function comparePoints(from, to) {
    if (typeof from.seq === "number" && typeof to.seq === "number" && from.seq !== to.seq) {
      return from.seq - to.seq;
    }
    return from.utcSeconds - to.utcSeconds;
  }
  // The analysed records of one source that the payload carries, in sequence order.
  function analysedRecords(points, sourceId) {
    return points.filter(function (point) { return point.s === sourceId && !excludedFromAnalysis(point); })
      .sort(function (a, b) { return a.seq - b.seq; });
  }
  // The figures from `first` to `last` (analysed records of one source) without the records
  // whose seq is in `excluded` (an object used as a set): the same computation, field for
  // field, as analysis/speed_range.py speed_range_figures. {refusal: text} when it cannot run.
  function rangeFigures(records, first, last, excluded, resolution, threshold) {
    var inside = records.filter(function (record) { return record.seq >= first.seq && record.seq <= last.seq; });
    var excludedCount = Object.keys(excluded).length;
    var complete = inside.length === last.seq - first.seq + 1;
    if (excluded[first.seq] || excluded[last.seq]) {
      return { refusal: "an end of the range cannot be excluded; choose another end" };
    }
    if (excludedCount && !complete) {
      return { refusal: "this source is thinned on the map, so the records between the ends are not all present; exclusions need every record of the range" };
    }
    var elapsed = elapsedBetween(first, last);
    var straight = geodesicMetres(first.lat, first.lon, last.lat, last.lon);
    var straightMinimum = pairMinimum(straight, first, last);
    var straightMinimumReported = pairMinimumReported(straight, first, last);
    var path = 0, stepwiseMinimum = 0, stepwiseMinimumReported = 0, stepSpeeds = [];
    if (complete) {
      var retained = inside.filter(function (record) { return !excluded[record.seq]; });
      for (var index = 1; index < retained.length; index++) {
        var previous = retained[index - 1], record = retained[index];
        var distance = geodesicMetres(previous.lat, previous.lon, record.lat, record.lon);
        path += distance;
        stepwiseMinimum += pairMinimum(distance, previous, record);
        stepwiseMinimumReported += pairMinimumReported(distance, previous, record);
        var seconds = elapsedBetween(previous, record);
        if (seconds > 0) { stepSpeeds.push(distance / seconds * 3.6); }
      }
    } else {
      path = last.cum - first.cum;
      stepwiseMinimum = last.cmb - first.cmb;
      stepwiseMinimumReported = minimumPathReported(last) - minimumPathReported(first);
      // The payload's own steps of the records it carries (each to its full-list
      // predecessor); the first end's step lies before the range.
      inside.slice(1).forEach(function (record) { if (typeof record.spd === "number") { stepSpeeds.push(record.spd); } });
    }
    var uncertainty = resolution + ambiguityOf(first) + ambiguityOf(last);
    var stretched = elapsed + uncertainty;
    var slowest = null, fastest = null, implausible = 0;
    stepSpeeds.forEach(function (speed) {
      if (slowest === null || speed < slowest) { slowest = speed; }
      if (fastest === null || speed > fastest) { fastest = speed; }
      if (speed >= threshold) { implausible += 1; }
    });
    return {
      records_in_range: last.seq - first.seq + 1,
      excluded_records: excludedCount,
      complete: complete,
      elapsed_seconds: elapsed,
      time_resolution_seconds: resolution,
      time_uncertainty_seconds: uncertainty,
      straight_m: straight,
      straight_minimum_m: straightMinimum,
      stepwise_minimum_m: stepwiseMinimum,
      stepwise_premise_records: complete ? inside.length - excludedCount : last.seq - first.seq + 1,
      minimum_average_kmh: straightMinimum / stretched * 3.6,
      stepwise_minimum_average_kmh: stepwiseMinimum / stretched * 3.6,
      minimum_average_reported_kmh: straightMinimumReported / stretched * 3.6,
      stepwise_minimum_average_reported_kmh: stepwiseMinimumReported / stretched * 3.6,
      path_m: path,
      path_average_kmh: elapsed > 0 ? path / elapsed * 3.6 : null,
      straight_average_kmh: elapsed > 0 ? straight / elapsed * 3.6 : null,
      slowest_step_kmh: slowest,
      fastest_step_kmh: fastest,
      implausible_steps: implausible,
      known_steps: stepSpeeds.length
    };
  }
  // Why a record is outside the analysis: its positioning method or the accuracy limit.
  function outsideReason(point) {
    return G.leftOutByMethod && G.leftOutByMethod(point) ? "positioning method " + point.pm : "accuracy limit";
  }
  function excludedRefusal(point) {
    return "Refused: " + G.recordNoun(point.s) + " " + point.n + " of " + G.sourceName(point.s) +
      " is outside the analysis (" + outsideReason(point) + "), so it has no speed." +
      " Pick another record.";
  }
  // Why a second click cannot complete the range, or null when it can.
  function rangeRefusal(from, to) {
    var noun = G.recordNoun(from.s);
    // An end without figures would have to be estimated: a guess presented as a
    // measurement. It is refused instead, with the reason.
    if (excludedFromAnalysis(from)) { return excludedRefusal(from); }
    if (excludedFromAnalysis(to)) { return excludedRefusal(to); }
    if (to.s !== from.s) {
      return "Refused: " + G.recordNoun(to.s) + " " + to.n + " belongs to " + G.sourceName(to.s) +
        ". The range must stay within " + G.sourceName(from.s) + ".";
    }
    if (comparePoints(from, to) === 0) {
      return "Refused: " + noun + " " + from.n + " is already the other end of the range." +
        " Click another record of the same source.";
    }
    return null;
  }
  // The two ends in recorded order, whichever was clicked first: a range chosen backwards
  // is the same range.
  function orderedEnds(clickedFirst, clickedSecond) {
    return comparePoints(clickedFirst, clickedSecond) < 0 ? [clickedFirst, clickedSecond] : [clickedSecond, clickedFirst];
  }
  // Why a record cannot be excluded from the range, or null when it can.
  function exclusionRefusal(point, from, to) {
    var noun = G.recordNoun(point.s);
    if (point.s !== from.s) {
      return "Refused: only records of " + G.sourceName(from.s) + " between the two ends can be excluded.";
    }
    if (excludedFromAnalysis(point)) {
      return "Refused: " + noun + " " + point.n + " is already outside the analysis (" + outsideReason(point) + ").";
    }
    if (point.seq === from.seq || point.seq === to.seq) {
      return "Refused: an end of the range cannot be excluded; choose another end.";
    }
    if (point.seq < from.seq || point.seq > to.seq) {
      return "Refused: " + noun + " " + point.n + " lies outside the range " + noun + " " + from.n + " to " + to.n + ".";
    }
    return null;
  }
  // ---- end of speed figures ------------------------------------------------------------------

  G.geodesicMetres = geodesicMetres;
  G.speedIntervalText = intervalText;
  G.speedIntervalWithReportedText = intervalWithReportedText;
  G.speedText = speedText;
  G.stepFigures = function (first, second) {
    var source = G.sourceById[first.s];
    return stepFigures(first, second, source && typeof source.time_resolution_s === "number" ? source.time_resolution_s : 1);
  };

  var GAUGE_WIDTH = 64;
  var GAUGE_HEIGHT = 8;
  var SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  // A horizontal bar from 0 to the implausible threshold in the colour of the movement class.
  function speedGauge(kmh, movementClass) {
    var svg = document.createElementNS(SVG_NAMESPACE, "svg");
    svg.setAttribute("class", "speed-gauge");
    svg.setAttribute("width", String(GAUGE_WIDTH));
    svg.setAttribute("height", String(GAUGE_HEIGHT));
    svg.setAttribute("viewBox", "0 0 " + GAUGE_WIDTH + " " + GAUGE_HEIGHT);
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var ground = document.createElementNS(SVG_NAMESPACE, "rect");
    ground.setAttribute("width", String(GAUGE_WIDTH));
    ground.setAttribute("height", String(GAUGE_HEIGHT));
    ground.setAttribute("fill", "#ffffff");
    ground.setAttribute("stroke", G.inkColour);
    ground.setAttribute("stroke-opacity", "0.35");
    svg.appendChild(ground);
    var share = Math.max(0, Math.min(1, kmh / implausibleKmh()));
    var bar = document.createElementNS(SVG_NAMESPACE, "rect");
    bar.setAttribute("x", "1");
    bar.setAttribute("y", "1");
    bar.setAttribute("width", String(Math.max(1, share * (GAUGE_WIDTH - 2))));
    bar.setAttribute("height", String(GAUGE_HEIGHT - 2));
    bar.setAttribute("fill", G.CLASS_COLOURS[movementClass] || G.CLASS_COLOURS.unknown);
    svg.appendChild(bar);
    return svg;
  }
  // The tooltip rows of a record: gauge, value, the class and the step it came from, then its
  // interval. Records without the figures (the first of a source, or a payload without
  // them) get no rows.
  G.speedRows = function (point) {
    if (typeof point.spd !== "number") { return []; }
    var movementClass = point.cls || speedClass(point.spd);
    var box = element("span");
    box.appendChild(speedGauge(point.spd, movementClass));
    box.appendChild(G.textElement(speedText(point.spd) + " · " + movementClass +
      " (" + G.formatDistance(point.dpm) + " in " + elapsedText(point.dts) + ")"));
    var rows = [["Speed from previous record", box]];
    if (point.spd_lo !== undefined) {
      rows.push(["Speed interval", intervalWithReportedText(point.spd_lo, point.spd_hi, point.spd_lo_rep, point.spd_hi_rep, point.spd) ||
        "none: an accuracy is not reported"]);
    }
    return rows;
  };

  // ---- Speed tool: a range of one source ----------------------------------------------------
  var speedWindow = byId("speed-window");
  var speedButton = byId("speed-button");
  var status = byId("speed-status");
  var figures = byId("speed-figures");
  var exclusionsBox = byId("speed-exclusions");
  var excludeButton = byId("speed-exclude");
  var recordButton = byId("speed-record");
  var recordStatus = byId("speed-record-status");
  var rangeLayer = L.layerGroup().addTo(map);
  var CHOOSE_TEXT = "Click two records of one source, in either order.";
  var EXCLUSION_REASONS = [
    ["", "choose a reason"],
    ["position outlier", "Position outlier"],
    ["implausible jump", "Implausible jump"],
    ["duplicate position", "Duplicate position"],
    ["other", "Other (see note)"]
  ];
  // Recording needs the loopback server of GEOSnap (summary screen, key O), as the search
  // area of the crystal ball does; the route is a sibling of the page's own token path.
  var recordingPossible = location.protocol === "http:" && (location.hostname === "127.0.0.1" || location.hostname === "localhost");
  var RECORD_ROUTE = location.pathname.replace(/\/[^\/]*$/, "") + "/records/speed-range";
  var NOT_SERVED_TEXT = "Recording needs the running GEOSnap: open this map from its summary screen (key O).";
  var firstPoint = null;
  // The shown range: {from, to, records, excluded: {seq: {point, reason, note}}}.
  var shown = null;
  var excludeMode = false;
  var recording = false;

  G.speedToolActive = false;
  function sourceResolution(sourceId) {
    var source = G.sourceById[sourceId];
    return source && typeof source.time_resolution_s === "number" ? source.time_resolution_s : 1;
  }
  // A value is a text, or [text, secondary text] for a small second figure in the same cell.
  function figureRow(table, label, values, strong) {
    var row = element("tr", null, strong ? "speed-headline" : null);
    var head = element("th", label);
    head.scope = "row";
    row.appendChild(head);
    values.forEach(function (value) {
      if (!Array.isArray(value)) { row.appendChild(element("td", value)); return; }
      var cell = element("td", value[0]);
      if (value[1]) { cell.appendChild(element("span", value[1], "speed-reported")); }
      row.appendChild(cell);
    });
    table.appendChild(row);
  }
  // "as reported: at least X" beside a minimum, only when the reported radii give another figure.
  function reportedMinimumText(kmh, reportedKmh) {
    if (typeof reportedKmh !== "number") { return null; }
    var reported = atLeastSpeedText(reportedKmh);
    return reported === atLeastSpeedText(kmh) ? null : "as reported: " + reported;
  }
  function setExcludeMode(active) {
    excludeMode = active;
    excludeButton.setAttribute("aria-pressed", active ? "true" : "false");
  }
  // A map opened from disk says once, under the buttons, why it cannot record.
  function resetRecordStatus() {
    recordStatus.textContent = "";
    if (!recordingPossible && G.speedToolActive) { recordStatus.appendChild(element("p", NOT_SERVED_TEXT, "note")); }
  }
  function clearRange() {
    firstPoint = null;
    shown = null;
    setExcludeMode(false);
    excludeButton.disabled = true;
    rangeLayer.clearLayers();
    figures.textContent = "";
    exclusionsBox.textContent = "";
    resetRecordStatus();
    status.classList.remove("speed-refused");
    status.textContent = G.speedToolActive ? CHOOSE_TEXT : "";
    updateRecordButton();
  }
  function markPoint(point, label) {
    L.circleMarker([point.lat, point.lon], { radius: 10, color: "#1d5c8f", weight: 3, fill: false, interactive: false }).addTo(rangeLayer);
    L.marker([point.lat, point.lon], { icon: L.divIcon({ className: "", html: "", iconSize: null }), interactive: false })
      .on("add", function () {
        var node = this.getElement();
        if (!node) { return; }
        node.textContent = "";
        node.appendChild(G.textElement(label, "speed-range-label"));
      }).addTo(rangeLayer);
  }
  function markExcluded(point) {
    L.marker([point.lat, point.lon], { icon: L.divIcon({ className: "speed-excluded-mark", html: "", iconSize: [14, 14] }), interactive: false })
      .on("add", function () {
        var node = this.getElement();
        if (node) { node.textContent = "\u00d7"; }
      }).addTo(rangeLayer);
  }
  // Every exclusion needs a reason; "other" is no reason without its note.
  function allReasonsGiven() {
    return Object.keys(shown.excluded).every(function (seq) {
      var entry = shown.excluded[seq];
      return entry.reason !== "" && (entry.reason !== "other" || entry.note.trim() !== "");
    });
  }
  function updateRecordButton() {
    var ready = shown !== null && !shown.figures.refusal && allReasonsGiven();
    recordButton.disabled = !recordingPossible || recording || !ready;
    recordButton.setAttribute("data-tip", !recordingPossible ? NOT_SERVED_TEXT
      : shown === null ? "Choose a range first"
      : !allReasonsGiven() ? "Give a reason for every excluded record first"
      : "GEOSnap recomputes this range and writes it, with its exclusions, to speed_ranges/ in the project");
  }
  function renderExclusions() {
    exclusionsBox.textContent = "";
    var seqs = Object.keys(shown.excluded).map(Number).sort(function (a, b) { return a - b; });
    if (!seqs.length) { return; }
    var noun = G.recordNoun(shown.from.s);
    exclusionsBox.appendChild(element("p", "Excluded records (" + seqs.length + "): give a reason for each before recording.", "note"));
    var list = element("ul", null, "speed-exclusion-list");
    seqs.forEach(function (seq) {
      var entry = shown.excluded[seq];
      var item = element("li");
      item.appendChild(G.textElement(noun + " " + entry.point.n + " · " + G.localText(entry.point.local, entry.point.off) + " "));
      var reason = element("select");
      reason.setAttribute("aria-label", "Reason for excluding " + noun + " " + entry.point.n);
      EXCLUSION_REASONS.forEach(function (choice) {
        var option = element("option", choice[1]);
        option.value = choice[0];
        reason.appendChild(option);
      });
      reason.value = entry.reason;
      reason.addEventListener("change", function () { entry.reason = reason.value; updateRecordButton(); });
      var note = element("input");
      note.type = "text";
      note.maxLength = 500;
      note.placeholder = "note (required for other)";
      note.value = entry.note;
      note.setAttribute("aria-label", "Note on excluding " + noun + " " + entry.point.n);
      note.addEventListener("input", function () { entry.note = note.value; updateRecordButton(); });
      var restore = element("button", "Restore");
      restore.type = "button";
      restore.setAttribute("data-tip", "Take this record back into the calculation");
      restore.addEventListener("click", function () { toggleExclusion(entry.point); });
      item.appendChild(reason);
      item.appendChild(note);
      item.appendChild(restore);
      list.appendChild(item);
    });
    exclusionsBox.appendChild(list);
  }
  function computeShown() {
    var threshold = implausibleKmh();
    var resolution = sourceResolution(shown.from.s);
    var excludedSet = {};
    Object.keys(shown.excluded).forEach(function (seq) { excludedSet[seq] = true; });
    shown.figures = rangeFigures(shown.records, shown.from, shown.to, excludedSet, resolution, threshold);
    shown.allFigures = Object.keys(excludedSet).length
      ? rangeFigures(shown.records, shown.from, shown.to, {}, resolution, threshold) : null;
  }
  function renderRange() {
    var from = shown.from, to = shown.to, noun = G.recordNoun(from.s);
    computeShown();
    rangeLayer.clearLayers();
    var excludedSet = shown.excluded;
    var line = shown.records.filter(function (record) { return record.seq >= from.seq && record.seq <= to.seq && !excludedSet[record.seq]; });
    L.polyline(line.map(function (point) { return [point.lat, point.lon]; }),
      { color: "#1d5c8f", weight: 6, opacity: 0.45, interactive: false }).addTo(rangeLayer);
    markPoint(from, noun + " " + from.n);
    markPoint(to, noun + " " + to.n);
    Object.keys(excludedSet).forEach(function (seq) { markExcluded(excludedSet[seq].point); });
    status.classList.remove("speed-refused");
    status.textContent = G.sourceLabel(from.s) + " · " + noun + " " + from.n + " to " + to.n;
    figures.textContent = "";
    var result = shown.figures;
    if (result.refusal) {
      status.classList.add("speed-refused");
      status.textContent = "Refused: " + result.refusal + ".";
      updateRecordButton();
      return;
    }
    var all = shown.allFigures;
    var table = element("table");
    if (all) {
      var head = element("tr");
      [" ", "Without excluded", "All records"].forEach(function (text) { head.appendChild(element("th", text)); });
      table.appendChild(head);
    }
    function row(label, pick, strong) {
      figureRow(table, label, all ? [pick(result), pick(all)] : [pick(result)], strong);
    }
    figureRow(table, "First record", [noun + " " + from.n + " · " + G.localText(from.local, from.off)].concat(all ? [""] : []));
    figureRow(table, "Last record", [noun + " " + to.n + " · " + G.localText(to.local, to.off)].concat(all ? [""] : []));
    row("Records in the range", function (f) { return f.records_in_range + (f.excluded_records ? " (" + f.excluded_records + " excluded)" : ""); });
    row("Elapsed time", function (f) {
      var ambiguous = f.time_uncertainty_seconds - f.time_resolution_seconds;
      return elapsedText(f.elapsed_seconds) + " (resolution " + resolutionText(f.time_resolution_seconds) +
        (ambiguous > 0 ? "; up to " + elapsedText(ambiguous) + " more: ambiguous local time at an end" : "") + ")";
    });
    row("Minimum average speed", function (f) {
      return [atLeastSpeedText(f.minimum_average_kmh) + " (straight line, at least " + minimumDistanceText(f.straight_minimum_m) + "; premise: the 2 end records)",
        reportedMinimumText(f.minimum_average_kmh, f.minimum_average_reported_kmh)];
    }, true);
    row("Stepwise minimum", function (f) {
      return [atLeastSpeedText(f.stepwise_minimum_average_kmh) + " (at least " + minimumDistanceText(f.stepwise_minimum_m) + "; premise: all " + f.stepwise_premise_records + " records)",
        reportedMinimumText(f.stepwise_minimum_average_kmh, f.stepwise_minimum_average_reported_kmh)];
    });
    row("Straight line", function (f) { return G.formatDistance(f.straight_m) + " · average " + speedText(f.straight_average_kmh); });
    row("Path over the records (estimate)", function (f) { return G.formatDistance(f.path_m) + " · average " + speedText(f.path_average_kmh); });
    row("Slowest step", function (f) { return speedText(f.slowest_step_kmh); });
    row("Fastest step", function (f) { return speedText(f.fastest_step_kmh); });
    row("Implausible steps", function (f) {
      return f.implausible_steps + " of " + f.known_steps + (f.known_steps ? " (" + Math.round(100 * f.implausible_steps / f.known_steps) + " %)" : "");
    });
    figures.appendChild(table);
    var parameters = (G.payload.analysis && G.payload.analysis.parameters) || {};
    figures.appendChild(element("p", (parameters.accuracy_confidence === "p95"
      ? "The minimums assume each true position lies inside its 95 % circle."
      : "The minimums assume each true position lies inside its accuracy circle (often only 68 % likely).") +
      " The path is an estimate. More in Help → Speed tool.", "note"));
    if (!result.complete) {
      var source = G.sourceById[from.s];
      figures.appendChild(element("p", "Thinned source (1 of every " + (source ? source.thinning_stride : "?") +
        " records shown): the step figures cover the shown records only, and nothing can be excluded.", "note"));
    }
    var outsideAnalysis = G.allPoints.filter(function (point) {
      return point.s === from.s && excludedFromAnalysis(point) && point.utcSeconds >= from.utcSeconds && point.utcSeconds <= to.utcSeconds;
    });
    var byMethod = outsideAnalysis.filter(G.leftOutByMethod).length;
    var byAccuracy = outsideAnalysis.length - byMethod;
    if (byAccuracy) {
      figures.appendChild(element("p", byAccuracy + (byAccuracy === 1 ? " record" : " records") + " beyond the accuracy limit, not part of the range.", "note"));
    }
    if (byMethod) {
      figures.appendChild(element("p", byMethod + (byMethod === 1 ? " record" : " records") + " left out by positioning method, not part of the range.", "note"));
    }
    var withoutAccuracy = shown.records.filter(function (record) { return record.seq >= from.seq && record.seq <= to.seq && record.acc_known === false; }).length;
    if (withoutAccuracy) {
      figures.appendChild(element("p", withoutAccuracy + (withoutAccuracy === 1 ? " record" : " records") + " without accuracy, left out of the minimum.", "note"));
    }
    updateRecordButton();
  }
  function showRange(from, to) {
    shown = { from: from, to: to, records: analysedRecords(G.allPoints, from.s), excluded: {}, figures: null, allFigures: null };
    excludeButton.disabled = false;
    resetRecordStatus();
    exclusionsBox.textContent = "";
    renderRange();
  }
  function toggleExclusion(point) {
    if (shown.excluded[point.seq]) {
      delete shown.excluded[point.seq];
    } else {
      shown.excluded[point.seq] = { point: point, reason: "", note: "" };
    }
    resetRecordStatus();
    renderExclusions();
    renderRange();
  }
  function refuse(message) {
    status.classList.add("speed-refused");
    status.textContent = message;
  }
  map.on("geosnap:pointclick", function (event) {
    if (!G.speedToolActive) { return; }
    var point = event.point;
    if (excludeMode && shown !== null) {
      var refusal = exclusionRefusal(point, shown.from, shown.to);
      if (refusal) { refuse(refusal); return; }
      toggleExclusion(point);
      return;
    }
    if (excludedFromAnalysis(point)) { refuse(excludedRefusal(point)); return; }
    if (firstPoint === null) {
      clearRange();
      firstPoint = point;
      markPoint(point, G.recordNoun(point.s) + " " + point.n);
      status.textContent = "From " + G.sourceLabel(point.s) + " · " + G.recordNoun(point.s) + " " + point.n +
        ": click another record of this source (earlier or later).";
      return;
    }
    var rangeProblem = rangeRefusal(firstPoint, point);
    if (rangeProblem) { refuse(rangeProblem); return; }
    var ends = orderedEnds(firstPoint, point);
    showRange(ends[0], ends[1]);
    firstPoint = null;
  });
  // A record without a timestamp carries no step of its own: it can end no range.
  map.on("geosnap:undatedclick", function (event) {
    if (!G.speedToolActive) { return; }
    refuse("Refused: a record without a timestamp has no speed (" +
      G.recordNoun(event.record.s) + " " + event.record.n + " of " + G.sourceName(event.record.s) +
      "). Pick a record with a time.");
  });

  // ---- recording ----------------------------------------------------------------------------
  function endDocument(point) {
    return { seq: point.seq, record_number: point.n, utc: point.utc };
  }
  function buildSpeedRangeRecord() {
    var source = G.sourceById[shown.from.s] || {};
    var provenance = ((G.payload.provenance || {}).sources || []).filter(function (entry) { return entry.id === shown.from.s; })[0] || {};
    return {
      record_type: "geosnap-speed-range",
      record_version: 1,
      source: { id: shown.from.s, label: source.label || "", sha256: provenance.sha256 || "" },
      first: endDocument(shown.from),
      last: endDocument(shown.to),
      excluded: Object.keys(shown.excluded).map(Number).sort(function (a, b) { return a - b; }).map(function (seq) {
        var entry = shown.excluded[seq];
        return { seq: seq, record_number: entry.point.n, utc: entry.point.utc, reason: entry.reason, note: entry.note.slice(0, 500) };
      }),
      browser_figures: shown.figures
    };
  }
  function recordSpeedRange() {
    if (recordButton.disabled) { return; }
    var body = JSON.stringify(buildSpeedRangeRecord());
    recording = true;
    updateRecordButton();
    recordStatus.textContent = "Recording…";
    fetch(RECORD_ROUTE, { method: "POST", headers: { "Content-Type": "application/json" }, body: body, cache: "no-store", credentials: "omit" })
      .then(function (response) {
        return response.json().catch(function () { return { error: "HTTP " + response.status }; }).then(function (answer) {
          if (!response.ok) { throw new Error(answer.error || "HTTP " + response.status); }
          return answer;
        });
      })
      .then(function (answer) {
        recording = false;
        updateRecordButton();
        recordStatus.textContent = "";
        recordStatus.appendChild(element("p", "Recorded as speed range " + answer.record_number + " in speed_ranges/ (figures recomputed by GEOSnap, SHA-256 in records.jsonl):"));
        var list = element("ul", null, "crystal-record-files");
        Object.keys(answer.files).sort().forEach(function (name) { list.appendChild(element("li", name + " · " + String(answer.files[name]).slice(0, 12) + "…")); });
        recordStatus.appendChild(list);
        if (/^[0-9a-f]{64}$/.test(String(answer.last_line_sha256)) && G.chainAnchorBlock) {
          recordStatus.appendChild(G.chainAnchorBlock(answer.last_line_sha256));
        }
      })
      .catch(function (error) {
        recording = false;
        updateRecordButton();
        recordStatus.textContent = "Not recorded: " + (error && error.message ? error.message : "GEOSnap did not answer (is it still running?)") + ".";
      });
  }

  function setSpeedTool(active) {
    G.speedToolActive = active;
    speedButton.setAttribute("aria-pressed", active ? "true" : "false");
    speedWindow.hidden = !active;
    if (active) { G.setWindowCollapsed(speedWindow, false); }
    clearRange();
  }
  speedButton.addEventListener("click", function () { setSpeedTool(!G.speedToolActive); });
  byId("speed-close").addEventListener("click", function () { setSpeedTool(false); });
  byId("speed-clear").addEventListener("click", clearRange);
  excludeButton.addEventListener("click", function () { if (shown !== null) { setExcludeMode(!excludeMode); } });
  recordButton.addEventListener("click", recordSpeedRange);
  // The mode needs records with timestamps: when the gating switches it off, so does the tool.
  G.gateControl(speedButton, "time", function (ok) { if (!ok && G.speedToolActive) { setSpeedTool(false); } });
  // The figures do not depend on what the map shows, but a hidden source or a filtered-away end
  // would leave marks for points that are gone.
  G.onSourcesChange(clearRange);
  G.onTimeFilterChange(clearRange);

  G.addLegendEntry("Speed tool: chosen range of one source", { colour: "#1d5c8f", shape: "line", note: "derived, not recorded" });
})();
