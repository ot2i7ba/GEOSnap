// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  // Shared timeline: one time axis over the From/To range and one lane per shown
  // source (or the one chosen source). Stays are bars, reports a thin line, gaps hatched,
  // encounters vertical connectors between two lanes, overlapping visits at shared places
  // outlined. Every position is a UTC instant in seconds; wall-clock text is display only.
  // The accuracy filter and the time cursor do not thin the timeline; the cursor is a line.
  var G = window.GEOSNAP;
  var payload = G.payload;
  var analysis = payload.analysis;
  var element = G.element;
  var SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  var ALL_SOURCES = "all";
  var LANE_HEIGHT = 30;
  var BAR_HEIGHT = 16;
  var GUTTER = 176;          // lane names
  var RIGHT_PAD = 14;
  var TOP_PAD = 8;
  var AXIS_HEIGHT = 34;
  var HEADING_HEIGHT = 16;   // "Sources", "Case places" above their block
  var MIN_TICK_SPACING = 110; // px between axis labels
  var LABEL_CHAR_WIDTH = 6.2; // estimate for 11px text; a label is drawn only when it fits
  var TICK_STEPS = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400];
  var MONTH_STEPS = [1, 2, 3, 6, 12, 24, 60, 120, 240];

  function byId(id) { return document.getElementById(id); }
  var panel = byId("timeline");
  var plot = byId("timeline-plot");
  var tooltip = byId("timeline-tooltip");
  var sourceSelect = byId("timeline-source");
  var viewTimeline = byId("view-timeline");
  var keyBox = byId("timeline-key");

  function utcSeconds(text) { return Date.parse(text) / 1000; }
  function byStart(a, b) { return a.start - b.start; }
  // Index of the last entry whose key is <= value (-1 when none).
  function lastAtOrBefore(entries, value, key) {
    var low = 0, high = entries.length - 1, found = -1;
    while (low <= high) {
      var middle = (low + high) >> 1;
      if (key(entries[middle]) <= value) { found = middle; low = middle + 1; } else { high = middle - 1; }
    }
    return found;
  }
  function startOf(entry) { return entry.start; }
  function itself(value) { return value; }

  // ---- data per source, prepared once --------------------------------------------------
  var laneData = {};
  G.sources.forEach(function (source) { laneData[source.id] = { reportTimes: [], stays: [], gaps: [] }; });
  G.allPoints.forEach(function (point) { if (laneData[point.s]) { laneData[point.s].reportTimes.push(point.utcSeconds); } });
  G.sources.forEach(function (source) {
    var sourceAnalysis = G.sourceAnalysis(source.id);
    if (!sourceAnalysis) { return; }
    laneData[source.id].stays = sourceAnalysis.stays.map(function (stay) {
      return { stay: stay, start: utcSeconds(stay.arrive_utc), end: utcSeconds(stay.leave_utc) };
    }).sort(byStart);
    laneData[source.id].gaps = sourceAnalysis.gaps.map(function (gap) {
      return { gap: gap, start: utcSeconds(gap.start_utc), end: utcSeconds(gap.end_utc) };
    }).sort(byStart);
  });
  var encounters = ((analysis && analysis.encounters) || []).map(function (encounter) {
    return { encounter: encounter, start: utcSeconds(encounter.start_utc), end: utcSeconds(encounter.end_utc) };
  });
  // Every pair of visits of two sources at one shared place that overlap in time.
  var overlaps = [];
  ((analysis && analysis.shared_places) || []).forEach(function (place) {
    var visits = place.visits.map(function (visit) {
      return { visit: visit, start: utcSeconds(visit.arrive_utc), end: utcSeconds(visit.leave_utc) };
    });
    for (var i = 0; i < visits.length; i++) {
      for (var j = i + 1; j < visits.length; j++) {
        var a = visits[i], b = visits[j];
        if (a.visit.source_id === b.visit.source_id || a.start > b.end || b.start > a.end) { continue; }
        overlaps.push({ place: place, a: a.visit, b: b.visit, start: Math.max(a.start, b.start), end: Math.min(a.end, b.end) });
      }
    }
  });

  // ---- case places: one row per place below the source lanes ------------------------------
  // The window is a bracket, the visits of every shown source are bars in its colour.
  var casePlaceRows = (G.casePlaces || []).map(function (place) {
    var visitsBySource = {};
    place.checks.forEach(function (check) {
      visitsBySource[check.source_id] = check.visits.map(function (visit) {
        return { visit: visit, start: utcSeconds(visit.first_utc), end: utcSeconds(visit.last_utc) };
      });
    });
    return {
      place: place,
      located: place.lat !== null && place.lon !== null,
      window: place.window ? { start: utcSeconds(place.window.from_utc), end: utcSeconds(place.window.to_utc) } : null,
      visitsBySource: visitsBySource
    };
  });
  function casePlaceCheck(row, sourceId) {
    return row.place.checks.filter(function (check) { return check.source_id === sourceId; })[0] || null;
  }
  // ---- end of case places ------------------------------------------------------------------

  function placeText(stay) {
    var name = stay.address && stay.address.display_name;
    if (!name) { return "Stay " + stay.id; }
    // Nominatim leads with the house number: "45, Bonnstraße, …" reads "Bonnstraße 45".
    var parts = name.split(",").map(function (part) { return part.trim(); });
    return /^\d+\w?$/.test(parts[0]) && parts.length > 1 ? parts[1] + " " + parts[0] : parts[0];
  }
  function timeText(seconds) { return G.localTextAt(seconds); }
  function clockText(seconds) { return G.localTextAt(seconds).slice(11); }
  function durationText(seconds) { return G.formatDuration(seconds / 60); }
  function utcIso(seconds) {
    return new Date(seconds * 1000).toISOString().replace(".000Z", "Z").replace("Z", "+00:00");
  }
  function localIso(seconds) {
    var local = G.utcToLocal(seconds);
    return G.localIso(local.local, local.offset);
  }

  // Report spans between the first and last report in the range, broken by the gaps.
  function movementSpans(lane, from, to) {
    var times = lane.reportTimes;
    var last = lastAtOrBefore(times, to, itself);
    var first = lastAtOrBefore(times, from - 1e-6, itself) + 1;
    if (last < 0 || first > last) { return []; }
    var spans = [];
    var current = times[first];
    var end = times[last];
    for (var i = 0; i < lane.gaps.length; i++) {
      var gap = lane.gaps[i];
      if (gap.end <= current) { continue; }
      if (gap.start >= end) { break; }
      if (gap.start > current) { spans.push({ start: current, end: gap.start }); }
      current = Math.max(current, gap.end);
    }
    if (current <= end) { spans.push({ start: current, end: end }); }
    return spans;
  }
  function within(from, to) {
    return function (entry) { return entry.end >= from && entry.start <= to; };
  }

  // ---- which lanes -----------------------------------------------------------------------
  function selection() { return sourceSelect.value || ALL_SOURCES; }
  function chosenSources() {
    var chosen = selection();
    return G.visibleSources().filter(function (source) { return chosen === ALL_SOURCES || String(source.id) === chosen; });
  }
  function fillSourceSelect() {
    var chosen = selection();
    sourceSelect.textContent = "";
    var all = element("option", "All sources");
    all.value = ALL_SOURCES;
    sourceSelect.appendChild(all);
    G.visibleSources().forEach(function (source) {
      var option = element("option", G.sourceLabel(source.id));
      option.value = String(source.id);
      sourceSelect.appendChild(option);
    });
    var stillShown = G.visibleSources().some(function (source) { return String(source.id) === chosen; });
    sourceSelect.value = stillShown ? chosen : ALL_SOURCES;
  }

  // Everything the lanes show for the current range and lanes.
  function timelineModel() {
    var from = G.filters.fromSeconds, to = G.filters.toSeconds;
    var sources = chosenSources();
    var inLanes = {};
    sources.forEach(function (source, index) { inLanes[source.id] = index; });
    var inRange = within(from, to);
    var lanes = sources.map(function (source) {
      var lane = laneData[source.id];
      return { source: source, stays: lane.stays.filter(inRange), gaps: lane.gaps.filter(inRange), movement: movementSpans(lane, from, to) };
    });
    var allSources = selection() === ALL_SOURCES;
    return {
      from: from,
      to: to,
      lanes: lanes,
      laneIndex: inLanes,
      casePlaces: casePlaceRows,
      // Connectors join two lanes: only with "All sources".
      encounters: allSources ? encounters.filter(function (entry) {
        return inRange(entry) && inLanes[entry.encounter.source_a] !== undefined && inLanes[entry.encounter.source_b] !== undefined;
      }) : [],
      // With one source, its overlaps with other shown sources are still marked on its lane.
      overlaps: overlaps.filter(function (entry) {
        return inRange(entry) && G.isSourceVisible(entry.a.source_id) && G.isSourceVisible(entry.b.source_id) &&
          (inLanes[entry.a.source_id] !== undefined || inLanes[entry.b.source_id] !== undefined);
      })
    };
  }

  // ---- drawing ---------------------------------------------------------------------------
  function svgNode(tag, attributes, parent) {
    var node = document.createElementNS(SVG_NAMESPACE, tag);
    Object.keys(attributes || {}).forEach(function (name) { node.setAttribute(name, attributes[name]); });
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function svgText(text, attributes, parent) {
    var node = svgNode("text", attributes, parent);
    node.textContent = text;
    return node;
  }
  // White or ink on a filled bar, by the fill's luminance.
  function textColourOn(hex) {
    var match = /^#([0-9a-f]{6})$/i.exec(hex || "");
    if (!match) { return "#1c2733"; }
    var value = parseInt(match[1], 16);
    var channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map(function (channel) {
      var c = channel / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    var luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    return luminance > 0.4 ? "#1c2733" : "#ffffff";
  }
  function truncated(text, maxCharacters) {
    return text.length > maxCharacters ? text.slice(0, maxCharacters - 1) + "…" : text;
  }
  // Shortens the text of an SVG node that is in the document until it is at most maxWidth
  // wide: glyph widths differ too much to cut by character count. Without a layout (no
  // measured length) it falls back to the count.
  function fitText(node, text, maxWidth, fallbackCharacters) {
    node.textContent = text;
    if (!node.getComputedTextLength || !node.getComputedTextLength()) {
      node.textContent = truncated(text, fallbackCharacters);
      return;
    }
    var length = text.length;
    while (length > 1 && node.getComputedTextLength() > maxWidth) {
      length -= 1;
      // Never end on the first half of a surrogate pair.
      var last = text.charCodeAt(length - 1);
      if (last >= 0xD800 && last <= 0xDBFF) { length -= 1; }
      node.textContent = text.slice(0, Math.max(1, length)) + "…";
    }
  }
  function fourDigits(year) {
    var text = String(year);
    while (text.length < 4) { text = "0" + text; }
    return text;
  }

  // Axis ticks at clean steps of the display zone's wall clock: minutes to weeks in seconds,
  // longer spans on calendar months and years. label(): the text under each tick.
  function axisTicks(from, to, plotWidth) {
    var wanted = Math.max(1, Math.floor(plotWidth / MIN_TICK_SPACING));
    var ticks = [];
    function add(wallText) {
      var at = G.localToUtcSeconds(wallText, false);
      if (at >= from && at <= to) { ticks.push({ at: at, wall: wallText }); }
      return at;
    }
    for (var i = 0; i < TICK_STEPS.length; i++) {
      var step = TICK_STEPS[i];
      if ((to - from) / step > wanted) { continue; }
      var wall = Math.ceil((from + G.utcToLocal(from).offset * 60) / step) * step;
      for (var guard = 0; guard < 400 && add(new Date(wall * 1000).toISOString().slice(0, 19)) <= to; guard++) { wall += step; }
      return { unit: step >= 86400 ? "day" : "time", ticks: ticks };
    }
    var months = (to - from) / (30.44 * 86400);
    var monthStep = MONTH_STEPS[MONTH_STEPS.length - 1];
    for (var j = 0; j < MONTH_STEPS.length; j++) {
      if (months / MONTH_STEPS[j] <= wanted) { monthStep = MONTH_STEPS[j]; break; }
    }
    var startWall = G.utcToLocal(from).local;
    var index = Math.ceil((Number(startWall.slice(0, 4)) * 12 + Number(startWall.slice(5, 7)) - 1) / monthStep) * monthStep;
    for (var count = 0; count < 400; count++, index += monthStep) {
      var year = Math.floor(index / 12), month = index % 12 + 1;
      if (add(fourDigits(year) + "-" + (month < 10 ? "0" : "") + month + "-01T00:00:00") > to) { break; }
    }
    return { unit: monthStep >= 12 ? "year" : "month", ticks: ticks };
  }
  function tickLabel(unit, wall) {
    if (unit === "year") { return wall.slice(0, 4); }
    if (unit === "month") { return wall.slice(0, 7); }
    if (unit === "day") { return wall.slice(0, 10); }
    return wall.slice(11, 16);
  }

  var lastKey = null;
  var geometry = null;   // {from, to, width, height, x(), seconds()}
  var cursorLine = null, cursorHandle = null, hoverLine = null;
  var currentModel = null;
  var lastHeight = 0;
  var fixedGroup = null;
  var fixedHeight = 0;

  function laneInputsKey() {
    var sources = chosenSources();
    return [G.filters.fromSeconds, G.filters.toSeconds, selection(), plot.clientWidth].concat(sources.map(function (source) {
      return source.id + ":" + G.sourceColour(source.id);
    })).join("|");
  }

  function draw() {
    var model = timelineModel();
    currentModel = model;
    plot.textContent = "";
    cursorLine = cursorHandle = hoverLine = null;
    geometry = null;
    if (!model.lanes.length) {
      plot.appendChild(element("p", "No source is shown: tick a source in the Sources window.", "timeline-empty"));
      return;
    }
    if (model.to <= model.from) {
      plot.appendChild(element("p", "From is after To: nothing to show.", "timeline-empty"));
      return;
    }
    var width = Math.max(plot.clientWidth, 480);
    var plotRight = width - RIGHT_PAD;
    // The time axis is the band on top and stays in view (see stickHeader);
    // below it the blocks scroll vertically, each under its own heading: the source lanes,
    // then the case place rows.
    var axisBottom = AXIS_HEIGHT;
    var lanesTop = axisBottom + HEADING_HEIGHT;
    var lanesBottom = lanesTop + model.lanes.length * LANE_HEIGHT;
    var caseTop = lanesBottom + (model.casePlaces.length ? HEADING_HEIGHT : 0);
    var height = caseTop + model.casePlaces.length * LANE_HEIGHT + (model.casePlaces.length ? 4 : 0);
    var fittedNames = [];
    var span = model.to - model.from;
    function x(seconds) { return GUTTER + (Math.min(model.to, Math.max(model.from, seconds)) - model.from) / span * (plotRight - GUTTER); }
    function laneCentre(index) { return lanesTop + index * LANE_HEIGHT + LANE_HEIGHT / 2; }
    geometry = { from: model.from, to: model.to, width: width, height: height, axisBottom: axisBottom, lanesBottom: lanesBottom, x: x,
      seconds: function (px) { return model.from + (px - GUTTER) / (plotRight - GUTTER) * span; } };

    var svg = svgNode("svg", { viewBox: "0 0 " + width + " " + height, width: "100%", role: "img",
      "aria-label": "Timeline of " + model.lanes.length + (model.lanes.length === 1 ? " source" : " sources") + " from " + timeText(model.from) + " to " + timeText(model.to) });
    var defs = svgNode("defs", {}, svg);
    var scrolling = svgNode("g", { "class": "tl-scrolling" }, svg);
    var fixed = svgNode("g", { "class": "tl-fixed" }, svg);
    fixedGroup = fixed;
    fixedHeight = AXIS_HEIGHT;
    svgNode("rect", { x: 0, y: 0, width: width, height: axisBottom, "class": "tl-fixed-ground" }, fixed);
    model.lanes.forEach(function (lane) {
      var colour = G.sourceColour(lane.source.id);
      var pattern = svgNode("pattern", { id: "tl-hatch-" + lane.source.id, width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
      svgNode("rect", { width: 6, height: 6, fill: colour, "fill-opacity": 0.06 }, pattern);
      svgNode("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: colour, "stroke-width": 1.5, "stroke-opacity": 0.5 }, pattern);
    });

    // Grid and axis.
    var axis = axisTicks(model.from, model.to, plotRight - GUTTER);
    var grid = svgNode("g", { "aria-hidden": "true" }, fixed);
    var bodyGrid = svgNode("g", { "aria-hidden": "true" }, scrolling);
    var previousDate = null;
    axis.ticks.forEach(function (tick) {
      var tickX = Math.round(x(tick.at)) + 0.5;
      svgNode("line", { x1: tickX, x2: tickX, y1: axisBottom - 5, y2: axisBottom, "class": "tl-grid" }, grid);
      svgNode("line", { x1: tickX, x2: tickX, y1: axisBottom, y2: height, "class": "tl-grid" }, bodyGrid);
      var date = tick.wall.slice(0, 10);
      svgText(tickLabel(axis.unit, tick.wall), { x: tickX, y: 12, "text-anchor": "middle", "class": "tl-axis-label" }, grid);
      // Times of day carry their date above the first tick of each day.
      if (axis.unit === "time" && date !== previousDate) { svgText(date, { x: tickX, y: 25, "text-anchor": "middle", "class": "tl-axis-label" }, grid); }
      previousDate = date;
    });
    svgNode("line", { x1: GUTTER, x2: plotRight, y1: axisBottom - 0.5, y2: axisBottom - 0.5, "class": "tl-grid" }, grid);
    svgText("Sources · " + model.lanes.length, { x: 3, y: axisBottom + HEADING_HEIGHT - 4, "class": "tl-heading" }, scrolling);
    if (model.casePlaces.length) {
      svgText("Case places · " + model.casePlaces.length, { x: 3, y: lanesBottom + HEADING_HEIGHT - 4, "class": "tl-heading" }, scrolling);
    }

    // Lanes.
    model.lanes.forEach(function (lane, index) {
      var source = lane.source;
      var colour = G.sourceColour(source.id);
      var centre = laneCentre(index);
      var group = svgNode("g", { "class": "tl-lane", "data-source": String(source.id) }, scrolling);
      if (index > 0) { svgNode("line", { x1: 0, x2: plotRight, y1: centre - LANE_HEIGHT / 2 + 0.5, y2: centre - LANE_HEIGHT / 2 + 0.5, "class": "tl-lane-rule" }, group); }
      svgNode("circle", { cx: 8, cy: centre, r: 5, fill: colour, stroke: "#ffffff", "stroke-width": 2 }, group);
      svgText(source.icon, { x: 18, y: centre + 4, "class": "tl-lane-icon" }, group);
      var name = svgText("", { x: 38, y: centre + 4, "class": "tl-lane-name" }, group);
      fittedNames.push({ node: name, text: source.label, width: GUTTER - 44, characters: 19 });
      svgNode("title", {}, name).textContent = source.label;
      var top = centre - BAR_HEIGHT / 2;
      if (!lane.stays.length && !lane.gaps.length && !lane.movement.length) {
        svgText("no reports in this time range" + G.thinnedMapNote(source.id), { x: GUTTER + 6, y: centre + 4, "class": "tl-axis-label" }, group);
      }
      lane.gaps.forEach(function (entry) {
        svgNode("rect", { x: x(entry.start), y: top, width: Math.max(1, x(entry.end) - x(entry.start)), height: BAR_HEIGHT,
          fill: "url(#tl-hatch-" + source.id + ")", "class": "tl-gap" }, group);
      });
      lane.movement.forEach(function (entry) {
        svgNode("line", { x1: x(entry.start), x2: x(entry.end), y1: centre, y2: centre, stroke: colour, "class": "tl-movement" }, group);
      });
      var labelColour = textColourOn(colour);
      lane.stays.forEach(function (entry) {
        var left = x(entry.start);
        var barWidth = Math.max(2, x(entry.end) - left);
        svgNode("rect", { x: left, y: top, width: barWidth, height: BAR_HEIGHT, rx: Math.min(3, barWidth / 2), fill: colour, "class": "tl-stay" }, group);
        var text = placeText(entry.stay);
        if (text.length * LABEL_CHAR_WIDTH + 8 <= barWidth) {
          svgText(text, { x: left + 4, y: centre + 4, fill: labelColour, "class": "tl-stay-label" }, group);
        }
      });
    });

    // Case places: window bracket and the visits of every lane's source, stacked per source.
    var inModelRange = within(model.from, model.to);
    model.casePlaces.forEach(function (row, rowIndex) {
      var place = row.place;
      var centre = caseTop + rowIndex * LANE_HEIGHT + LANE_HEIGHT / 2;
      var windowText = row.window ? ", window " + timeText(row.window.start) + " to " + timeText(row.window.end) : ", no window";
      var verdicts = model.lanes.map(function (lane) {
        var check = casePlaceCheck(row, lane.source.id);
        return check && check.window ? G.sourceName(lane.source.id) + ": " + check.window.verdict : null;
      }).filter(function (text) { return text !== null; });
      var group = svgNode("g", { "class": "tl-case-place", tabindex: "0", role: "button", "data-case-place": String(rowIndex),
        "aria-label": "Case place " + place.label + (row.located ? "" : ", not located") + windowText + (verdicts.length ? ", " + verdicts.join(", ") : "") }, scrolling);
      svgNode("rect", { x: 0, y: centre - LANE_HEIGHT / 2, width: plotRight, height: LANE_HEIGHT, "class": "tl-hit" }, group);
      svgNode("line", { x1: 0, x2: plotRight, y1: centre - LANE_HEIGHT / 2 + 0.5, y2: centre - LANE_HEIGHT / 2 + 0.5, "class": rowIndex === 0 ? "tl-grid" : "tl-lane-rule" }, group);
      svgText("📌", { x: 3, y: centre + 4, "class": "tl-lane-icon" }, group);
      var name = svgText("", { x: 22, y: centre + 4, "class": "tl-lane-name" }, group);
      fittedNames.push({ node: name, text: place.label, width: GUTTER - 28, characters: 21 });
      svgNode("title", {}, name).textContent = place.label;
      if (!row.located) {
        svgText("not located: no coordinates, no source was checked", { x: GUTTER + 6, y: centre + 4, "class": "tl-axis-label" }, group);
        return;
      }
      if (row.window && inModelRange(row.window)) {
        var left = x(row.window.start), right = Math.max(left + 2, x(row.window.end));
        var top = centre - LANE_HEIGHT / 2 + 3, bottom = centre + LANE_HEIGHT / 2 - 3;
        svgNode("rect", { x: left, y: top, width: right - left, height: bottom - top, "class": "tl-case-window-band" }, group);
        var tick = Math.min(5, (right - left) / 2);
        svgNode("path", { d: "M" + (left + tick) + " " + top + "H" + left + "V" + bottom + "H" + (left + tick) +
          "M" + (right - tick) + " " + top + "H" + right + "V" + bottom + "H" + (right - tick), "class": "tl-case-window" }, group);
      }
      var barHeight = Math.max(3, Math.min(BAR_HEIGHT, Math.floor((LANE_HEIGHT - 10) / Math.max(1, model.lanes.length))));
      var stackTop = centre - barHeight * model.lanes.length / 2;
      model.lanes.forEach(function (lane, laneIndex) {
        (row.visitsBySource[lane.source.id] || []).filter(inModelRange).forEach(function (entry) {
          var barLeft = x(entry.start);
          svgNode("rect", { x: barLeft, y: stackTop + laneIndex * barHeight, width: Math.max(2, x(entry.end) - barLeft), height: barHeight,
            fill: G.sourceColour(lane.source.id), "class": "tl-case-visit" }, group);
        });
      });
    });

    // Overlapping visits at shared places: outlined on every lane of the pair that is shown.
    model.overlaps.forEach(function (entry) {
      [entry.a.source_id, entry.b.source_id].forEach(function (sourceId) {
        var index = model.laneIndex[sourceId];
        if (index === undefined) { return; }
        var left = x(entry.start);
        svgNode("rect", { x: left - 2, y: laneCentre(index) - BAR_HEIGHT / 2 - 3, width: Math.max(4, x(entry.end) - left + 4), height: BAR_HEIGHT + 6, rx: 4,
          "class": "tl-overlap", "data-overlap": String(model.overlaps.indexOf(entry)) }, scrolling);
      });
    });

    // Encounters: a band over the encounter window and a connector between the two lanes.
    model.encounters.forEach(function (entry, index) {
      var encounter = entry.encounter;
      var laneA = model.laneIndex[encounter.source_a], laneB = model.laneIndex[encounter.source_b];
      var top = laneCentre(Math.min(laneA, laneB)), bottom = laneCentre(Math.max(laneA, laneB));
      var left = x(entry.start), right = x(entry.end), middle = (left + right) / 2;
      // A joint movement (both sources' reports moved) has a dashed connector.
      var jointMovement = encounter.movement === "joint movement";
      var group = svgNode("g", { "class": "tl-encounter" + (jointMovement ? " tl-joint-movement" : ""), tabindex: "0", "data-encounter": String(index), role: "button",
        "aria-label": "Encounter " + encounter.id + (jointMovement ? " (joint movement), " : ", ") + G.sourceName(encounter.source_a) + " and " + G.sourceName(encounter.source_b) + ", " + timeText(entry.start) }, scrolling);
      svgNode("rect", { x: middle - 12, y: top - 6, width: 24, height: bottom - top + 12, "class": "tl-hit" }, group);
      if (right - left >= 3) { svgNode("rect", { x: left, y: top, width: right - left, height: bottom - top, "class": "tl-encounter-band" }, group); }
      svgNode("line", { x1: middle, x2: middle, y1: top, y2: bottom, "class": "tl-encounter-line" }, group);
      svgNode("circle", { cx: middle, cy: top, r: 4, fill: G.sourceColour(laneA === Math.min(laneA, laneB) ? encounter.source_a : encounter.source_b), "class": "tl-encounter-end" }, group);
      svgNode("circle", { cx: middle, cy: bottom, r: 4, fill: G.sourceColour(laneA === Math.min(laneA, laneB) ? encounter.source_b : encounter.source_a), "class": "tl-encounter-end" }, group);
    });

    hoverLine = svgNode("line", { y1: axisBottom, y2: height, "class": "tl-hover", visibility: "hidden" }, svg);
    cursorLine = svgNode("line", { y1: axisBottom - 6, y2: height, "class": "tl-cursor", visibility: "hidden" }, svg);
    cursorHandle = svgNode("path", { d: "M-5 0h10l-5 6z", "class": "tl-cursor-handle", visibility: "hidden" }, fixed);
    plot.appendChild(svg);
    fittedNames.forEach(function (entry) { fitText(entry.node, entry.text, entry.width, entry.characters); });
    stickHeader();
  }

  // The time axis stays at the top of the scrolled plot: its group moves with the scroll
  // position, so the lanes and rows below it scroll under a readable axis.
  function stickHeader() {
    if (!fixedGroup || !geometry) { return; }
    var scale = geometry.width / Math.max(1, plot.clientWidth);
    fixedGroup.setAttribute("transform", "translate(0 " + plot.scrollTop * scale + ")");
  }
  plot.addEventListener("scroll", stickHeader);
  window.addEventListener("beforeprint", function () {
    plot.scrollTop = 0;
    stickHeader();
  });

  // One lane per step, so every lane of a long list is reachable from the keyboard.
  function scrollLanes(direction) {
    plot.scrollTop = Math.max(0, plot.scrollTop + direction * LANE_HEIGHT);
    stickHeader();
  }

  function moveCursorLine() {
    if (!geometry || !cursorLine) { return; }
    var seconds = G.filters.cursorSeconds;
    var shown = seconds !== null && seconds >= geometry.from && seconds <= geometry.to;
    cursorLine.setAttribute("visibility", shown ? "visible" : "hidden");
    cursorHandle.setAttribute("visibility", shown ? "visible" : "hidden");
    if (!shown) { return; }
    var cursorX = geometry.x(seconds);
    cursorLine.setAttribute("x1", cursorX);
    cursorLine.setAttribute("x2", cursorX);
    cursorHandle.setAttribute("transform", "translate(" + cursorX + " " + (AXIS_HEIGHT - 6) + ")");
  }

  // Redraws the lanes only when their inputs change; a cursor step only moves its line.
  G.renderTimeline = function (force) {
    if (panel.hidden) { return; }
    var key = laneInputsKey();
    if (force || key !== lastKey) {
      lastKey = key;
      draw();
      var height = plot.offsetHeight;
      if (height !== lastHeight) {
        lastHeight = height;
        G.map.invalidateSize();
      }
    }
    moveCursorLine();
  };

  // ---- tooltips: one readout for every lane at a time, and one per encounter/overlap ------
  function tipRow(colour, value, sourceName) {
    var row = element("div", null, "tl-tip-row");
    var key = element("span", null, "tl-tip-key");
    key.style.background = colour;
    row.appendChild(key);
    row.appendChild(element("span", value, "tl-tip-value"));
    if (sourceName) { row.appendChild(element("span", sourceName, "tl-tip-source")); }
    return row;
  }
  // " · arrived 09:40–10:00, left not bounded": the records before and after the stay; a
  // bound on another local day than the stay edge gives its date.
  function boundText(boundSeconds, edgeSeconds) {
    return timeText(boundSeconds).slice(0, 10) === timeText(edgeSeconds).slice(0, 10) ? clockText(boundSeconds) : timeText(boundSeconds);
  }
  function stayBoundsText(entry) {
    var stay = entry.stay;
    if (stay.arrived_after_utc === undefined) { return ""; }
    var arrived = stay.arrived_after_utc === null ? "not bounded" : boundText(utcSeconds(stay.arrived_after_utc), entry.start) + "–" + boundText(utcSeconds(stay.arrived_by_utc), entry.start);
    var left = stay.left_before_utc === null ? "not bounded" : clockText(entry.end) + "–" + boundText(utcSeconds(stay.left_before_utc), entry.end);
    return " · arrived " + arrived + ", left " + left;
  }
  function laneStateText(lane, seconds) {
    var data = laneData[lane.source.id];
    var stayIndex = lastAtOrBefore(data.stays, seconds, startOf);
    if (stayIndex >= 0 && data.stays[stayIndex].end >= seconds) {
      var stay = data.stays[stayIndex];
      return (stay.stay.address && stay.stay.address.display_name ? "Stay " + stay.stay.id + " · " : "") + placeText(stay.stay) + " · " + clockText(stay.start) + "–" + clockText(stay.end) + " (" + durationText(stay.end - stay.start) + ")" + stayBoundsText(stay);
    }
    var gapIndex = lastAtOrBefore(data.gaps, seconds, startOf);
    if (gapIndex >= 0 && data.gaps[gapIndex].end >= seconds) {
      var gap = data.gaps[gapIndex];
      return "No reports (gap " + gap.gap.id + ") · " + clockText(gap.start) + "–" + clockText(gap.end) + " (" + durationText(gap.end - gap.start) + ")";
    }
    // The report times are the drawn records: a thinned source says so.
    var onMap = G.thinnedMapNote(lane.source.id);
    var reportIndex = lastAtOrBefore(data.reportTimes, seconds, itself);
    if (reportIndex < 0) { return "No report yet" + onMap; }
    if (reportIndex === data.reportTimes.length - 1 && seconds - data.reportTimes[reportIndex] >= 60) {
      return "After the last report" + onMap + " (" + clockText(data.reportTimes[reportIndex]) + ")";
    }
    var ago = seconds - data.reportTimes[reportIndex];
    return "Reporting · last report" + onMap + " " + clockText(data.reportTimes[reportIndex]) + (ago >= 60 ? " (" + durationText(ago) + " before)" : "");
  }
  function readoutContent(seconds) {
    var box = element("div");
    box.appendChild(element("div", timeText(seconds) + " " + G.utcToLocal(seconds).abbreviation, "tl-tip-time"));
    currentModel.lanes.forEach(function (lane) {
      box.appendChild(tipRow(G.sourceColour(lane.source.id), laneStateText(lane, seconds), G.sourceName(lane.source.id)));
    });
    return box;
  }
  function encounterContent(entry) {
    var encounter = entry.encounter;
    var box = element("div");
    box.appendChild(element("div", "Encounter " + encounter.id + " · " + timeText(entry.start) + " – " + clockText(entry.end), "tl-tip-time"));
    var offset = G.encounterOffsetText ? G.encounterOffsetText(encounter) : null;
    box.appendChild(tipRow("#1c2733", durationText(entry.end - entry.start) + ", closest " + G.formatDistance(encounter.min_distance_m) +
      (offset ? ", " + offset : ""), G.sourceName(encounter.source_a) + " ↔ " + G.sourceName(encounter.source_b)));
    if (encounter.movement) {
      box.appendChild(tipRow(G.inkColour, G.encounterClassText(encounter) + ": path of the midpoints " + G.formatDistance(encounter.path_length_m) + " in plausible steps, displacement " + G.formatDistance(encounter.displacement_m) +
        (encounter.implausible_steps ? ", excluded " + G.formatDistance(encounter.path_excluded_m) + " in " + encounter.implausible_steps + (encounter.implausible_steps === 1 ? " implausible step" : " implausible steps") : ""), "Class"));
    }
    box.appendChild(element("div", "Click: time cursor to its start and its reports on the map", "tl-tip-source"));
    return box;
  }
  function overlapContent(entry) {
    var box = element("div");
    box.appendChild(element("div", "Shared place " + entry.place.id + " · both there " + timeText(entry.start) + " – " + clockText(entry.end), "tl-tip-time"));
    [entry.a, entry.b].forEach(function (visit) {
      box.appendChild(tipRow(G.sourceColour(visit.source_id), "Stay " + visit.stay_id + " · " + G.localText(visit.arrive_local, visit.arrive_offset).slice(11) + "–" +
        G.localText(visit.leave_local, visit.leave_offset).slice(11), G.sourceName(visit.source_id)));
    });
    return box;
  }
  // A case place at a moment: window, and per lane source its verdict and the visit then.
  function casePlaceContent(row, seconds) {
    var place = row.place;
    var box = element("div");
    box.appendChild(element("div", "Case place " + place.id + " · " + place.label + " · " + timeText(seconds), "tl-tip-time"));
    if (!row.located) {
      box.appendChild(element("div", "not located: no coordinates, no source was checked", "tl-tip-source"));
      return box;
    }
    box.appendChild(element("div", row.window ? "Window " + timeText(row.window.start) + " – " + timeText(row.window.end) : "No window: visits only", "tl-tip-source"));
    currentModel.lanes.forEach(function (lane) {
      var check = casePlaceCheck(row, lane.source.id);
      if (!check) { return; }
      var visits = row.visitsBySource[lane.source.id] || [];
      var visitIndex = lastAtOrBefore(visits, seconds, startOf);
      var visit = visitIndex >= 0 && visits[visitIndex].end >= seconds ? visits[visitIndex] : null;
      var text = (G.casePlaceVerdictText(check) || check.visit_count + (check.visit_count === 1 ? " visit" : " visits")) +
        (visit ? " · visit " + clockText(visit.start) + "–" + clockText(visit.end) + " (" + visit.visit.report_count + " reports, closest " + G.formatDistance(visit.visit.closest_distance_m) +
          ", best accuracy " + (visit.visit.best_accuracy_m === null || visit.visit.best_accuracy_m === undefined ? "not reported" : "±" + visit.visit.best_accuracy_m + " m") + ")" : "");
      box.appendChild(tipRow(G.sourceColour(lane.source.id), text, G.sourceName(lane.source.id)));
      var coverage = G.casePlaceCoverageText(check);
      if (coverage) { box.appendChild(element("div", "In the window: " + coverage, "tl-tip-source")); }
    });
    box.appendChild(element("div", "A verdict describes the reports of a device, never a person. Double click or Enter: show the place on the map", "tl-tip-source"));
    return box;
  }
  function showTooltip(content, clientX, clientY) {
    tooltip.textContent = "";
    tooltip.appendChild(content);
    tooltip.hidden = false;
    var box = tooltip.getBoundingClientRect();
    var left = Math.min(window.innerWidth - box.width - 8, clientX + 14);
    var top = clientY - box.height - 12;
    tooltip.style.left = Math.max(8, left) + "px";
    tooltip.style.top = (top < 8 ? clientY + 18 : top) + "px";
  }
  function hideTooltip() {
    tooltip.hidden = true;
    if (hoverLine) { hoverLine.setAttribute("visibility", "hidden"); }
  }
  // Plot x (in SVG units) of a pointer position.
  function plotX(clientX) {
    var svg = plot.querySelector("svg");
    var rect = svg.getBoundingClientRect();
    return (clientX - rect.left) * geometry.width / rect.width;
  }
  function screenPoint(svgX, svgY) {
    var rect = plot.querySelector("svg").getBoundingClientRect();
    return { x: rect.left + svgX * rect.width / geometry.width, y: rect.top + svgY * rect.width / geometry.width };
  }
  function markedEntry(target) {
    var encounterNode = target.closest && target.closest(".tl-encounter");
    if (encounterNode) { return { kind: "encounter", entry: currentModel.encounters[Number(encounterNode.getAttribute("data-encounter"))] }; }
    var overlapNode = target.closest && target.closest(".tl-overlap");
    if (overlapNode) { return { kind: "overlap", entry: currentModel.overlaps[Number(overlapNode.getAttribute("data-overlap"))] }; }
    var casePlaceNode = target.closest && target.closest(".tl-case-place");
    if (casePlaceNode) { return { kind: "case_place", entry: currentModel.casePlaces[Number(casePlaceNode.getAttribute("data-case-place"))] }; }
    return null;
  }

  plot.addEventListener("mousemove", function (event) {
    if (!geometry) { return; }
    var marked = markedEntry(event.target);
    if (marked && marked.kind !== "case_place") {
      if (hoverLine) { hoverLine.setAttribute("visibility", "hidden"); }
      showTooltip(marked.kind === "encounter" ? encounterContent(marked.entry) : overlapContent(marked.entry), event.clientX, event.clientY);
      return;
    }
    var svgX = plotX(event.clientX);
    if (svgX < GUTTER) {
      if (marked) { showTooltip(casePlaceContent(marked.entry, G.filters.cursorSeconds === null ? geometry.from : G.filters.cursorSeconds), event.clientX, event.clientY); }
      else { hideTooltip(); }
      return;
    }
    hoverLine.setAttribute("x1", svgX);
    hoverLine.setAttribute("x2", svgX);
    hoverLine.setAttribute("visibility", "visible");
    var hoverSeconds = geometry.seconds(svgX);
    showTooltip(marked ? casePlaceContent(marked.entry, hoverSeconds) : readoutContent(hoverSeconds), event.clientX, event.clientY);
  });
  plot.addEventListener("mouseleave", hideTooltip);
  plot.addEventListener("click", function (event) {
    if (!geometry) { return; }
    var marked = markedEntry(event.target);
    if (marked && marked.kind === "encounter") {
      openEncounter(marked.entry);
      return;
    }
    var svgX = plotX(event.clientX);
    if (svgX < GUTTER) { return; }
    G.setCursorSeconds(geometry.seconds(svgX));
  });
  plot.addEventListener("dblclick", function (event) {
    var marked = geometry && markedEntry(event.target);
    if (marked && marked.kind === "case_place") { G.showCasePlace(marked.entry.place); }
  });
  function openEncounter(entry) {
    G.setCursorSeconds(entry.start);
    G.map.setView([entry.encounter.lat, entry.encounter.lon], Math.max(G.map.getZoom(), 16));
    if (G.showEncounter) { G.showEncounter(entry.encounter); }
  }
  // Keyboard: arrows step the cursor report by report, Home/End jump to the range ends,
  // Escape switches it off; the readout follows the cursor.
  function showCursorReadout() {
    var seconds = G.filters.cursorSeconds;
    if (!geometry || seconds === null) { hideTooltip(); return; }
    var point = screenPoint(geometry.x(seconds), geometry.axisBottom);
    showTooltip(readoutContent(seconds), point.x, point.y);
  }
  plot.addEventListener("keydown", function (event) {
    if (!geometry) { return; }
    if (event.target !== plot) {
      var focused = (event.key === "Enter" || event.key === " ") ? markedEntry(event.target) : null;
      if (focused && focused.kind === "encounter") {
        event.preventDefault();
        openEncounter(focused.entry);
      } else if (focused && focused.kind === "case_place") {
        event.preventDefault();
        G.showCasePlace(focused.entry.place);
      }
      return;
    }
    var handled = true;
    if (event.key === "ArrowRight") { G.stepCursor(1); }
    else if (event.key === "ArrowLeft") { G.stepCursor(-1); }
    else if (event.key === "ArrowDown" || event.key === "PageDown") { scrollLanes(1); }
    else if (event.key === "ArrowUp" || event.key === "PageUp") { scrollLanes(-1); }
    else if (event.key === "Home") { G.setCursorSeconds(geometry.from); }
    else if (event.key === "End") { G.setCursorSeconds(geometry.to); }
    else if (event.key === "Escape") { G.switchCursorOff(); }
    else { handled = false; }
    if (!handled) { return; }
    event.preventDefault();
    showCursorReadout();
  });
  plot.addEventListener("focusin", function (event) {
    var marked = event.target !== plot && markedEntry(event.target);
    if (marked) {
      var rect = event.target.getBoundingClientRect();
      // A focused case place row must not hide under the axis that stays on top.
      var covered = geometry ? plot.getBoundingClientRect().top + fixedHeight * plot.clientWidth / geometry.width - rect.top : 0;
      if (covered > 0 && plot.scrollTop > 0) {
        plot.scrollTop = Math.max(0, plot.scrollTop - covered);
        rect = event.target.getBoundingClientRect();
      }
      var cursorOrStart = G.filters.cursorSeconds === null ? geometry.from : G.filters.cursorSeconds;
      showTooltip(marked.kind === "case_place" ? casePlaceContent(marked.entry, cursorOrStart) : encounterContent(marked.entry), marked.kind === "case_place" ? rect.left + GUTTER : rect.right, rect.top);
    } else if (event.target === plot) {
      showCursorReadout();
    }
  });
  plot.addEventListener("focusout", hideTooltip);

  // ---- key, source choice, View toggle, print, export -------------------------------------
  function keySample(draw) {
    var svg = svgNode("svg", { width: 22, height: 12, viewBox: "0 0 22 12", "aria-hidden": "true" });
    draw(svg);
    return svg;
  }
  function addKey(label, draw) {
    var entry = element("span");
    entry.appendChild(keySample(draw));
    entry.appendChild(document.createTextNode(label));
    keyBox.appendChild(entry);
  }
  var KEY_GREY = "#5b6875";
  addKey("Stay", function (svg) { svgNode("rect", { x: 1, y: 2, width: 20, height: 8, rx: 2, fill: KEY_GREY }, svg); });
  addKey("Reports", function (svg) { svgNode("line", { x1: 2, x2: 20, y1: 6, y2: 6, stroke: KEY_GREY, "stroke-width": 2, "stroke-linecap": "round" }, svg); });
  addKey("Gap (no reports)", function (svg) {
    [3, 8, 13, 18].forEach(function (offset) { svgNode("line", { x1: offset - 3, y1: 11, x2: offset + 3, y2: 1, stroke: KEY_GREY, "stroke-width": 1.5 }, svg); });
  });
  if (encounters.length) {
    addKey("Encounter", function (svg) {
      svgNode("line", { x1: 11, x2: 11, y1: 1, y2: 11, stroke: "#1c2733", "stroke-width": 1.5 }, svg);
      svgNode("circle", { cx: 11, cy: 2.5, r: 2.5, fill: KEY_GREY }, svg);
      svgNode("circle", { cx: 11, cy: 9.5, r: 2.5, fill: KEY_GREY }, svg);
    });
  }
  if (encounters.some(function (entry) { return entry.encounter.movement === "joint movement"; })) {
    addKey("Joint movement", function (svg) {
      svgNode("line", { x1: 11, x2: 11, y1: 0, y2: 12, stroke: G.inkColour, "stroke-width": 1.5, "stroke-dasharray": "3 2" }, svg);
    });
  }
  if (overlaps.length) {
    addKey("Both at a shared place", function (svg) { svgNode("rect", { x: 1.5, y: 1.5, width: 19, height: 9, rx: 3, fill: "none", stroke: "#1c2733", "stroke-width": 1.5 }, svg); });
  }
  if (casePlaceRows.length) {
    addKey("Case place window", function (svg) { svgNode("path", { d: "M6 1.5H2V10.5H6M16 1.5H20V10.5H16", fill: "none", stroke: "#1c2733", "stroke-width": 1.5 }, svg); });
    addKey("Visit at a case place", function (svg) { svgNode("rect", { x: 4, y: 3, width: 14, height: 6, fill: KEY_GREY }, svg); });
  }
  addKey("Time cursor", function (svg) { svgNode("line", { x1: 11, x2: 11, y1: 0, y2: 12, stroke: "#1c2733", "stroke-width": 2 }, svg); });

  fillSourceSelect();
  sourceSelect.addEventListener("change", function () { G.renderTimeline(); });
  G.onSourcesChange(fillSourceSelect);
  G.onRender(function () { G.renderTimeline(); });
  window.addEventListener("resize", function () { G.renderTimeline(); });

  function applyTimelineVisibility() {
    panel.hidden = !viewTimeline.checked;
    if (panel.hidden) { hideTooltip(); }
    G.renderTimeline(true);
    G.map.invalidateSize();
  }
  viewTimeline.checked = G.sources.length >= 2;
  if (casePlaceRows.length) { viewTimeline.checked = true; }
  viewTimeline.addEventListener("change", applyTimelineVisibility);
  applyTimelineVisibility();

  // The print always carries the timeline, also when it is switched off in the view.
  // Browsers may send beforeprint twice before one afterprint: the first state is kept.
  var hiddenBeforePrint = null;
  window.addEventListener("beforeprint", function () {
    if (hiddenBeforePrint === null) { hiddenBeforePrint = panel.hidden; }
    panel.hidden = false;
    G.renderTimeline(true);
  });
  window.addEventListener("afterprint", function () {
    if (hiddenBeforePrint === null) { return; }
    panel.hidden = hiddenBeforePrint;
    hiddenBeforePrint = null;
    G.renderTimeline(true);
  });

  byId("timeline-export-csv").addEventListener("click", function () {
    var model = timelineModel();
    var view = selection() === ALL_SOURCES ? "All sources" : G.sourceName(Number(selection()));
    var header = ["element", "source_id", "source_label", "id", "start_utc", "end_utc", "start_local", "end_local", "duration_minutes", "detail",
      "other_source_id", "other_source_label", "timeline_view", "range_from_local", "range_to_local"];
    var rows = [];
    function row(kind, sourceId, id, start, end, detail, otherSourceId) {
      rows.push([kind, sourceId, G.sourceName(sourceId), id, utcIso(start), utcIso(end), localIso(start), localIso(end), Math.round((end - start) / 6) / 10, detail,
        otherSourceId === null ? "" : otherSourceId, otherSourceId === null ? "" : G.sourceName(otherSourceId), view, localIso(model.from), localIso(model.to)]);
    }
    model.lanes.forEach(function (lane) {
      var sourceId = lane.source.id;
      lane.stays.forEach(function (entry) { row("stay", sourceId, entry.stay.id, entry.start, entry.end, placeText(entry.stay), null); });
      lane.movement.forEach(function (entry, index) { row("movement", sourceId, index + 1, entry.start, entry.end, "reports without a gap (stays lie on this span)", null); });
      lane.gaps.forEach(function (entry) { row("gap", sourceId, entry.gap.id, entry.start, entry.end, "no reports; " + G.formatDistance(entry.gap.distance_m) + " apart", null); });
    });
    model.encounters.forEach(function (entry) {
      var encounter = entry.encounter;
      row("encounter", encounter.source_a, encounter.id, entry.start, entry.end, "closest " + G.formatDistance(encounter.min_distance_m) + (encounter.movement ? "; " + G.encounterClassText(encounter) + ", path " + G.formatDistance(encounter.path_length_m) : ""), encounter.source_b);
    });
    model.overlaps.forEach(function (entry) {
      row("shared_place_overlap", entry.a.source_id, entry.place.id, entry.start, entry.end, "stays " + entry.a.stay_id + " and " + entry.b.stay_id + " at shared place " + entry.place.id, entry.b.source_id);
    });
    var inExportRange = within(model.from, model.to);
    model.casePlaces.forEach(function (entry) {
      if (entry.window && inExportRange(entry.window)) {
        rows.push(["case_place_window", "", "", entry.place.id, utcIso(entry.window.start), utcIso(entry.window.end), localIso(entry.window.start), localIso(entry.window.end),
          Math.round((entry.window.end - entry.window.start) / 6) / 10, "window of case place " + entry.place.label, "", "", view, localIso(model.from), localIso(model.to)]);
      }
      model.lanes.forEach(function (lane) {
        (entry.visitsBySource[lane.source.id] || []).filter(inExportRange).forEach(function (visit) {
          row("case_place_visit", lane.source.id, entry.place.id, visit.start, visit.end, "reports inside the radius of case place " + entry.place.label + ": " +
            visit.visit.report_count + ", closest " + G.formatDistance(visit.visit.closest_distance_m), null);
        });
      });
    });
    rows.sort(function (a, b) { return a[4] < b[4] ? -1 : a[4] > b[4] ? 1 : 0; });
    var lines = [header.join(",")].concat(rows.map(function (cells) { return cells.map(G.csvCell).join(","); }));
    G.downloadBlob(G.derivedName("csv", "timeline"), new Blob(["﻿" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" }));
    byId("status").textContent = "Timeline CSV exported (derived, not recorded): import it as text, do not open it by double-click";
  });
})();
