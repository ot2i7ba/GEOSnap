// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  var G = window.GEOSNAP;
  var map = G.map;
  var payload = G.payload;
  var element = G.element;

  function byId(id) { return document.getElementById(id); }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function stampNow() {
    var d = new Date();
    return d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "_" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
  }
  // infix (optional) names the kind of export, e.g. "surroundings".
  function derivedName(extension, infix) {
    return payload.project + "_" + stampNow() + (infix ? "_" + infix : "") + "_derived." + extension;
  }
  G.derivedName = derivedName;

  window.GEOSNAP.downloadBlob = function (name, blob) {
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 5000);
  };

  // ---- accuracy filter -------------------------------------------------
  var maxRadius = 1;
  G.allPoints.forEach(function (p) { if (p.radius > maxRadius) { maxRadius = p.radius; } });
  var accuracySlider = byId("accuracy-filter");
  var accuracyValue = byId("accuracy-filter-value");
  accuracySlider.max = String(Math.ceil(maxRadius));
  accuracySlider.value = accuracySlider.max;
  accuracySlider.addEventListener("input", function () {
    var value = Number(accuracySlider.value);
    if (value >= Number(accuracySlider.max)) {
      G.filters.maxAccuracy = null;
      accuracyValue.textContent = "all";
    } else {
      G.filters.maxAccuracy = value;
      accuracyValue.textContent = value + " m";
    }
    G.render();
  });

  // The record number with its noun ("line 12", "GPX point 3"), as the source format names it.
  function recordName(point) {
    return G.recordNoun(point.s) + " " + point.n;
  }

  // ---- neighbours on click ---------------------------------------------
  var highlightLayer = L.layerGroup().addTo(map);
  var infoBox = byId("neighbour-info");
  var infoText = byId("neighbour-info-text");
  // The step to a neighbour with the same geodesic and interval as the Speed tool;
  // the movement class only where the two are consecutive analysed records.
  function describeStep(from, to, label) {
    var segment = G.segmentBetween(from, to) || G.segmentBetween(to, from);
    var step = G.stepFigures(from, to);
    var interval = G.speedIntervalWithReportedText(step.low, step.high, step.lowReported, step.highReported, step.kmh);
    return label + " " + recordName(to) + "  " + G.localText(to.local, to.off) + "\n  " + G.formatDistance(step.distance) +
      ", " + G.formatDuration(step.seconds / 60) + ", " + G.speedText(step.kmh) +
      (segment ? " (" + segment.movement_class + ")" : "") + (interval ? "\n  " + interval : "");
  }
  function clearNeighbours() {
    highlightLayer.clearLayers();
    infoBox.hidden = true;
  }
  // Neighbours are the previous and next record of the same source that the map shows: the
  // filters and thinning decide which records those are, and the labels say so.
  map.on("geosnap:pointclick", function (event) {
    if (G.speedToolActive) { return; }
    var point = event.point;
    var visible = G.lastVisibleBySource[point.s] || [];
    var index = event.index;
    highlightLayer.clearLayers();
    var lines = ["Selected " + G.sourceName(point.s) + " " + recordName(point) + "  " + G.localText(point.local, point.off)];
    L.circleMarker([point.lat, point.lon], { radius: 10, color: "#ffd600", weight: 3, fill: false }).addTo(highlightLayer);
    if (index > 0) {
      var previous = visible[index - 1];
      L.circleMarker([previous.lat, previous.lon], { radius: 9, color: "#00897b", weight: 3, fill: false }).addTo(highlightLayer);
      L.polyline([[previous.lat, previous.lon], [point.lat, point.lon]], { color: "#00897b", weight: 4, opacity: 0.8 }).addTo(highlightLayer);
      lines.push(describeStep(point, previous, "Previous shown:"));
    }
    if (index < visible.length - 1) {
      var next = visible[index + 1];
      L.circleMarker([next.lat, next.lon], { radius: 9, color: "#6a1b9a", weight: 3, fill: false }).addTo(highlightLayer);
      L.polyline([[point.lat, point.lon], [next.lat, next.lon]], { color: "#6a1b9a", weight: 4, opacity: 0.8 }).addTo(highlightLayer);
      lines.push(describeStep(point, next, "Next shown:"));
    }
    infoText.textContent = lines.join("\n");
    infoBox.hidden = false;
  });
  map.on("click", clearNeighbours);
  // A new render or source choice may have removed the chosen point or its neighbours.
  G.onRender(clearNeighbours);
  G.onSourcesChange(clearNeighbours);
  G.addLegendEntry("Selected point / previous / next", { colour: "#ffd600", note: "click a point" });

  // ---- time cursor -----------------------------------------------------
  // The cursor runs over the time filter range. It is off until the slider moves or Play
  // starts; Stop switches it off again. Its position is always marked, even when no point
  // falls inside the trail window.
  var cursorSlider = byId("time-cursor");
  var cursorValue = byId("time-cursor-value");
  var playButton = byId("time-play");
  var pauseButton = byId("time-pause");
  var stopButton = byId("time-stop");
  var previousButton = byId("time-prev");
  var nextButton = byId("time-next");
  var speedSelect = byId("time-speed");
  var trailInput = byId("trail-minutes");
  var followBox = byId("time-follow");
  var trailLayer = L.layerGroup().addTo(map);
  var TICK_MILLISECONDS = 200;
  var timer = null;
  var rangeStart = 0;
  var rangeEnd = 0;
  var cursorSeconds = null;

  function readRange() {
    rangeStart = G.filters.fromSeconds;
    rangeEnd = G.filters.toSeconds;
  }
  readRange();

  function applyCursor(seconds, skipRender) {
    if (seconds === null) {
      cursorSeconds = null;
      G.filters.cursorSeconds = null;
      cursorSlider.value = "0";
      cursorValue.textContent = "off";
    } else {
      cursorSeconds = Math.min(rangeEnd, Math.max(rangeStart, seconds));
      G.filters.cursorSeconds = cursorSeconds;
      cursorSlider.value = String(Math.round(1000 * (cursorSeconds - rangeStart) / Math.max(rangeEnd - rangeStart, 1)));
      cursorValue.textContent = G.localTextAt(cursorSeconds);
    }
    if (!skipRender) { G.render(); }
  }
  cursorSlider.addEventListener("input", function () {
    applyCursor(rangeStart + Number(cursorSlider.value) / 1000 * (rangeEnd - rangeStart));
  });
  // Transport icons drawn as inline SVG (own code, no external resources under the CSP).
  var TRANSPORT_SHAPES = {
    previous: "M3 2h2v12H3zM13 2v12L6 8z",
    play: "M4 2l10 6-10 6z",
    pause: "M3 2h4v12H3zM9 2h4v12H9z",
    stop: "M3 3h10v10H3z",
    next: "M11 2h2v12h-2zM3 2v12l7-6z"
  };
  function transportIcon(button, shape) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", TRANSPORT_SHAPES[shape]);
    svg.appendChild(path);
    button.appendChild(svg);
  }
  transportIcon(previousButton, "previous");
  transportIcon(playButton, "play");
  transportIcon(pauseButton, "pause");
  transportIcon(stopButton, "stop");
  transportIcon(nextButton, "next");

  function stopPlayback() {
    if (timer) { clearTimeout(timer); timer = null; }
    playButton.classList.remove("active");
  }
  // The next step is scheduled only after the render of this one: on a large map a render can
  // take longer than a tick, and fixed intervals would pile up and freeze the page.
  // The data time per step stays the chosen speed.
  function scheduleStep() {
    timer = setTimeout(function () {
      if (timer === null) { return; }
      applyCursor(cursorSeconds + Number(speedSelect.value) * TICK_MILLISECONDS / 1000);
      if (cursorSeconds >= rangeEnd) { stopPlayback(); } else if (timer !== null) { scheduleStep(); }
    }, TICK_MILLISECONDS);
  }
  playButton.addEventListener("click", function () {
    if (timer) { return; }
    if (cursorSeconds === null || cursorSeconds >= rangeEnd) { applyCursor(rangeStart); }
    playButton.classList.add("active");
    scheduleStep();
  });
  pauseButton.addEventListener("click", stopPlayback);
  G.switchCursorOff = function () {
    stopPlayback();
    applyCursor(null);
  };
  // Other modules (crystal ball day list) place the cursor at a UTC instant (seconds).
  G.setCursorSeconds = function (seconds) {
    stopPlayback();
    applyCursor(seconds);
  };
  stopButton.addEventListener("click", G.switchCursorOff);
  // When the time features become unavailable (e.g. the dated source is hidden), their
  // controls are disabled: playback, cursor and a narrowed range must not stay in force
  // without a way to undo them.
  var timeAvailable = null;
  G.gateControl(playButton, "time", function (ok) {
    if (timeAvailable === true && !ok) {
      G.switchCursorOff();
      G.resetTimeFilter();
    }
    timeAvailable = ok;
  });
  // Step to the previous/next report inside the filter range (ignoring the cursor itself).
  function pointsInRange() {
    var saved = G.filters.cursorSeconds;
    G.filters.cursorSeconds = null;
    var points = G.filteredPoints();
    G.filters.cursorSeconds = saved;
    return points;
  }
  function stepCursor(direction) {
    stopPlayback();
    var points = pointsInRange();
    if (!points.length) { return; }
    var target = null;
    if (direction > 0) {
      for (var i = 0; i < points.length; i++) {
        if (cursorSeconds === null || points[i].utcSeconds > cursorSeconds) { target = points[i]; break; }
      }
    } else {
      for (var j = points.length - 1; j >= 0; j--) {
        if (cursorSeconds === null || points[j].utcSeconds < cursorSeconds) { target = points[j]; break; }
      }
    }
    if (target) { applyCursor(target.utcSeconds); }
  }
  // The timeline steps the cursor from the keyboard the same way.
  G.stepCursor = stepCursor;
  previousButton.addEventListener("click", function () { stepCursor(-1); });
  nextButton.addEventListener("click", function () { stepCursor(1); });
  trailInput.addEventListener("change", function () { if (cursorSeconds !== null) { G.render(); } });
  G.onTimeFilterChange(function () {
    stopPlayback();
    readRange();
    // Reset clears cursorSeconds; a narrowed range keeps the cursor and clamps it. The
    // filter renders afterwards, so only the state is updated here.
    applyCursor(G.filters.cursorSeconds === null ? null : cursorSeconds, true);
  });

  function cursorLabel(latest, atSeconds) {
    if (!latest) {
      return G.localTextAt(atSeconds) + " · no report before cursor" + (G.anyThinnedSource() ? " on this map (thinned sources show 1 of every N reports)" : "");
    }
    var minutesAgo = (atSeconds - latest.utcSeconds) / 60;
    // With "Remove duplicates" a point stands for every report at its position.
    var sourceAnalysis = G.sourceAnalysis(latest.s);
    var source = G.sourceById[latest.s];
    var reports = latest.dup > 1 && sourceAnalysis && source && sourceAnalysis.points_total > source.points_total
      ? " (" + latest.dup + " reports at this position)" : "";
    return G.sourceName(latest.s) + " · report" + G.thinnedMapNote(latest.s) + " " + G.localText(latest.local, latest.off).slice(11) + (minutesAgo >= 1 ? " (" + G.formatDuration(minutesAgo) + " ago)" : "") + reports;
  }
  // Pulsing dot in the source colour with the source's kind icon; the label is plain text.
  function cursorMarker(latLng, labelText, source) {
    var colour = G.sourceColour(source.id);
    var marker = L.marker(latLng, { icon: L.divIcon({ className: "", html: "", iconSize: [26, 26], iconAnchor: [13, 13] }), interactive: false, zIndexOffset: 1000 });
    marker.bindTooltip(G.textElement(labelText), { permanent: true, direction: "top", offset: [0, -14], className: "cursor-label" });
    marker.on("add", function () {
      var node = marker.getElement();
      if (!node) { return; }
      node.textContent = "";
      var dot = G.textElement(source.icon, "cursor-marker");
      dot.style.setProperty("--cursor-colour", colour);
      node.appendChild(dot);
      var tooltip = marker.getTooltip() && marker.getTooltip().getElement();
      if (tooltip) { tooltip.style.setProperty("--cursor-colour", colour); }
    });
    return marker;
  }
  // One current position and one trail per shown source; Follow keeps the newest in view.
  G.onRender(function () {
    trailLayer.clearLayers();
    if (G.filters.cursorSeconds === null) { return; }
    var trailMinutes = Math.max(1, Number(trailInput.value) || 60);
    var cutoff = G.filters.cursorSeconds - trailMinutes * 60;
    var newest = null;
    G.visibleSources().forEach(function (source) {
      var points = G.lastVisibleBySource[source.id] || [];
      if (!points.length) { return; }
      var latest = points[points.length - 1];
      var colour = G.sourceColour(source.id);
      var latLngs = points.filter(function (point) { return point.utcSeconds >= cutoff; }).map(function (point) { return [point.lat, point.lon]; });
      // The trail fades towards its tail: each segment is a little more transparent than the next.
      for (var i = 1; i < latLngs.length; i++) {
        var opacity = 0.25 + 0.6 * i / (latLngs.length - 1);
        L.polyline([latLngs[i - 1], latLngs[i]], { color: colour, weight: 6, opacity: opacity, interactive: false }).addTo(trailLayer);
      }
      latLngs.forEach(function (latLng) {
        L.circleMarker(latLng, { radius: 5, color: colour, fillColor: "#ffffff", fillOpacity: 0.9, weight: 2, interactive: false }).addTo(trailLayer);
      });
      cursorMarker([latest.lat, latest.lon], cursorLabel(latest, G.filters.cursorSeconds), source).addTo(trailLayer);
      if (newest === null || latest.utcSeconds > newest.utcSeconds) { newest = latest; }
    });
    if (newest) {
      var position = [newest.lat, newest.lon];
      if (followBox.checked) { map.panInside(position, { padding: [60, 60] }); }
    } else if (G.filters.fromSeconds <= G.filters.toSeconds) {
      byId("status").textContent = cursorLabel(null, G.filters.cursorSeconds);
    }
  });
  G.addLegendEntry("Time cursor: current position per source (source icon) and trail", { colour: "#5b6875" });

  // ---- sources window ----------------------------------------------------
  // Show/hide and colour per source are view choices in this browser and are not recorded.
  var sourcesWindow = byId("sources-window");
  var sourcesViewBox = byId("view-sources");
  var sourcesSelected = byId("sources-selected");
  var sourceBoxes = {};
  var COLOUR_INPUT_DELAY_MILLISECONDS = 120;
  var FORMAT_NAMES = { kml: "KML", kmz: "KMZ", gpx: "GPX", csv: "CSV", geojson: "GeoJSON", "google-records": "Google Records.json", "google-timeline": "Google Timeline.json", "google-semantic": "Google Semantic Location History" };
  function timeSpanText(source) {
    return source.first_local ? G.localText(source.first_local, source.first_offset) + " – " + G.localText(source.last_local, source.last_offset) : "no points";
  }
  function pointCountText(source) {
    return source.points_total + " points" + (source.points_rendered !== source.points_total ? " (" + source.points_rendered + " on this map)" : "");
  }
  function sourceRow(source) {
    var row = element("div", null, "sources-row");
    var head = element("div", null, "sources-row-head");
    var show = element("label", null, "sources-show");
    var box = element("input");
    box.type = "checkbox";
    box.checked = true;
    box.setAttribute("data-source", String(source.id));
    box.addEventListener("change", function () { G.setSourcesVisible([source.id], box.checked); });
    sourceBoxes[source.id] = box;
    show.appendChild(box);
    show.appendChild(element("span", "show"));
    var colour = element("input", null, "source-colour");
    colour.type = "color";
    colour.value = source.colour;
    G.controlTip(colour, "Colour of this source on the map (view choice, not recorded)");
    // Dragging in the picker fires many input events: the map is recoloured at most every 120 ms.
    var pendingColour = null;
    colour.addEventListener("input", function () {
      if (pendingColour === null) {
        setTimeout(function () {
          G.setSourceColour(source.id, pendingColour);
          pendingColour = null;
        }, COLOUR_INPUT_DELAY_MILLISECONDS);
      }
      pendingColour = colour.value;
    });
    head.appendChild(show);
    head.appendChild(colour);
    head.appendChild(element("span", source.icon, "source-icon"));
    head.appendChild(element("strong", source.label, "source-label"));
    head.appendChild(element("span", source.kind, "source-kind"));
    row.appendChild(head);
    var details = [pointCountText(source), timeSpanText(source)];
    if (source.format !== "text") { details.push(FORMAT_NAMES[source.format] || source.format); }
    var accuracyNote = G.accuracyReportingNote(source);
    if (accuracyNote) { details.push(accuracyNote); }
    if (source.thinning_stride > 1) { details.push("thinned: 1 of every " + source.thinning_stride + " reports on this map"); }
    row.appendChild(element("div", details.join(" · "), "sources-row-details"));
    return row;
  }
  G.sources.forEach(function (source) { byId("sources-list").appendChild(sourceRow(source)); });
  function allSourceIds() { return G.sources.map(function (source) { return source.id; }); }
  function syncSourcesWindow() {
    G.sources.forEach(function (source) { sourceBoxes[source.id].checked = G.isSourceVisible(source.id); });
    sourcesSelected.textContent = G.visibleSources().length + " of " + G.sources.length + " sources shown";
  }
  G.onSourcesChange(syncSourcesWindow);
  syncSourcesWindow();
  byId("sources-all").addEventListener("click", function () { G.setSourcesVisible(allSourceIds(), true); });
  byId("sources-none").addEventListener("click", function () { G.setSourcesVisible(allSourceIds(), false); });
  function applySourcesWindowVisibility() {
    sourcesWindow.hidden = !sourcesViewBox.checked;
    if (!sourcesWindow.hidden) { G.setWindowCollapsed(sourcesWindow, false); }
  }
  sourcesViewBox.checked = G.sources.length >= 2;
  sourcesViewBox.addEventListener("change", applySourcesWindowVisibility);
  byId("sources-close").addEventListener("click", function () {
    sourcesViewBox.checked = false;
    applySourcesWindowVisibility();
  });
  applySourcesWindowVisibility();

  // ---- briefing panel --------------------------------------------------
  var briefing = byId("briefing");
  function table(headers, rows, onRowClick) {
    var tableNode = element("table");
    var head = element("tr");
    headers.forEach(function (header) { head.appendChild(element("th", header)); });
    tableNode.appendChild(head);
    rows.forEach(function (row) {
      var tr = element("tr", null, onRowClick ? "clickable" : "");
      row.cells.forEach(function (cell) { tr.appendChild(element("td", cell)); });
      if (onRowClick) { tr.addEventListener("click", function () { onRowClick(row); }); }
      tableNode.appendChild(tr);
    });
    return tableNode;
  }
  G.table = table;
  function addressText(address) { return address && address.display_name ? address.display_name : "-"; }
  var UNDATED_NAMES_LISTED = 20;
  function sourceSwatch(source) {
    var swatch = element("span", null, "source-swatch");
    swatch.style.background = G.sourceColour(source.id);
    swatch.setAttribute("data-source-swatch", String(Number(source.id)));
    return swatch;
  }
  // "68 %, scaled to 95 % · gnss 120, cell 4 (4 left out)": the confidence level of the
  // source's radii and its records per positioning method, when any method is known;
  // [label, text], or null when neither is known.
  function accuracyLevelRow(source, analysis) {
    var parameters = (payload.analysis && payload.analysis.parameters) || {};
    var levelText = G.accuracyLevelTexts[source.accuracy_level];
    var parts = [];
    if (levelText) {
      parts.push(levelText +
        (parameters.accuracy_confidence === "p95" && source.accuracy_level !== "95" ? ", scaled to 95 %" : ""));
    }
    var methods = analysis.positioning_methods || {};
    var names = Object.keys(methods);
    var methodsKnown = names.some(function (name) { return name !== "unknown"; });
    if (methodsKnown) {
      var leftOut = analysis.points_excluded_positioning_method;
      parts.push(names.map(function (name) { return name + " " + methods[name]; }).join(", ") + (leftOut ? " (" + leftOut + " left out)" : ""));
    }
    if (!parts.length) { return null; }
    return [levelText ? (methodsKnown ? "Accuracy level · positioning" : "Accuracy level") : "Positioning", parts.join(" · ")];
  }
  // Records after the last known position that the positioning method filter left out:
  // [{cells}] with one row, or none.
  function newerLeftOutRows(analysis) {
    var count = analysis.points_excluded_positioning_method_after_last;
    if (typeof count !== "number" || count <= 0) { return []; }
    var methods = ((payload.analysis && payload.analysis.parameters) || {}).excluded_positioning_methods || [];
    return [{ cells: ["Newer " + (methods.length ? methods.join("/") + " " : "") + "records left out", String(count)] }];
  }
  // One collapsible section per shown source with its analysis tables.
  function sourceSection(source, open) {
    var analysis = G.sourceAnalysis(source.id);
    var section = element("details", null, "briefing-source");
    section.open = open;
    section.setAttribute("data-source", String(source.id));
    var summary = element("summary");
    summary.appendChild(sourceSwatch(source));
    summary.appendChild(element("span", G.sourceLabel(source.id) + " (" + source.kind + ")"));
    section.appendChild(summary);
    if (!analysis) {
      section.appendChild(element("p", "No analysis available for this source."));
      return section;
    }
    section.appendChild(element("h3", "Last known position"));
    if (analysis.last) {
      section.appendChild(table(["Field", "Value"], [
        { cells: ["Local time", G.localText(analysis.last.local, analysis.last.offset) + " (" + payload.display_zone + ")"] },
        { cells: ["UTC", analysis.last.utc] },
        { cells: ["Age when generated", G.formatDuration(analysis.last.age_minutes)] },
        { cells: ["Position", analysis.last.lat + ", " + analysis.last.lon + (analysis.last.accuracy_known === false ? " (accuracy not reported)" : " ±" + analysis.last.accuracy_m + " m")] },
        { cells: ["Address", addressText(analysis.last.address)] }
      ].concat(newerLeftOutRows(analysis))));
    } else {
      section.appendChild(element("p", "No analysable point."));
    }
    section.appendChild(element("h3", "Stays (" + analysis.stays.length + ")"));
    section.appendChild(table(["#", "Arrived", "Left", "Duration", "Address"], analysis.stays.map(function (stay) {
      return { cells: [String(stay.id) + (stay.is_last ? " (last)" : ""), G.localText(stay.arrive_local, stay.arrive_offset), G.localText(stay.leave_local, stay.leave_offset), G.formatDuration(stay.duration_minutes), addressText(stay.address)], stay: stay };
    }), function (row) { map.setView([row.stay.lat, row.stay.lon], 16); }));
    section.appendChild(element("h3", "Gaps (" + analysis.gaps.length + ")"));
    section.appendChild(table(["#", "From", "Until", "Duration", "Distance"], analysis.gaps.map(function (gap) {
      return { cells: [String(gap.id), G.localText(gap.start_local, gap.start_offset), G.localText(gap.end_local, gap.end_offset), G.formatDuration(gap.duration_minutes), G.formatDistance(gap.distance_m)], gap: gap };
    }), function (row) { map.setView([row.gap.from_lat, row.gap.from_lon], 15); }));
    var totals = analysis.totals;
    section.appendChild(element("h3", "Key figures"));
    var figures = [
      { cells: ["Points analysed", analysis.points_analysed + " of " + analysis.points_total + " (" + analysis.points_excluded_accuracy + " beyond the accuracy limit" +
        (typeof analysis.points_excluded_positioning_method === "number" ? ", " + analysis.points_excluded_positioning_method + " left out by positioning method" : "") + ")"] },
      { cells: ["Time span", G.formatDuration(totals.time_span_minutes)] },
      { cells: ["Route distance", G.formatDistance(totals.distance_m)] },
      { cells: ["Max speed", G.formatSpeed(totals.max_speed_kmh)] },
      { cells: ["Implausible segments", String(totals.implausible_segments)] }
    ];
    var levelRow = accuracyLevelRow(source, analysis);
    if (levelRow) { figures.push({ cells: levelRow }); }
    var accuracyReporting = G.accuracyReporting(source);
    if (accuracyReporting === "none") { figures.push({ cells: ["Accuracy", "not reported by the source"] }); }
    if (accuracyReporting === "some") { figures.push({ cells: ["Accuracy", "partly reported: some records have no accuracy value"] }); }
    section.appendChild(table(["Figure", "Value"], figures));
    var undated = G.undatedPointsOf(source.id);
    // The total of the source, not the thinned selection this map draws.
    var undatedTotal = G.undatedTotalOf(source);
    if (undatedTotal) {
      section.appendChild(element("h3", "Records without timestamp (" + G.undatedCountText(source) + ")"));
      var names = undated.slice(0, UNDATED_NAMES_LISTED).map(function (record) { return record.lbl || "(unnamed)"; }).join(", ");
      section.appendChild(element("p", names + (undated.length > UNDATED_NAMES_LISTED ? " … and " + (undated.length - UNDATED_NAMES_LISTED) + " more" : "") +
        " · shown as hollow rings in the Points layer, not part of any time-based analysis"));
    }
    return section;
  }
  function sourcesTable() {
    return table(["Source", "Kind", "Points", "First – last (local)", "Shown"], G.sources.map(function (source) {
      return { cells: [G.sourceLabel(source.id), source.kind, pointCountText(source), timeSpanText(source), G.isSourceVisible(source.id) ? "yes" : "hidden"] };
    }));
  }
  function encountersSection(crossAnalysis) {
    var shown = (crossAnalysis.encounters || []).filter(function (encounter) {
      return G.isSourceVisible(encounter.source_a) && G.isSourceVisible(encounter.source_b);
    });
    briefing.appendChild(element("h2", "Encounters (" + shown.length + ")"));
    var parameters = crossAnalysis.parameters || {};
    if (!shown.length) {
      briefing.appendChild(element("p", "No encounters between the shown sources (reports within " + parameters.encounter_max_minutes + " min and " + parameters.encounter_max_distance_m + " m)."));
      return;
    }
    briefing.appendChild(table(["#", "Sources", "From", "Until", "Duration", "Min. distance", "Time offsets", "Class"], shown.map(function (encounter) {
      return { cells: [String(encounter.id), G.sourceName(encounter.source_a) + " ↔ " + G.sourceName(encounter.source_b), G.localText(encounter.start_local, encounter.start_offset),
        G.localText(encounter.end_local, encounter.end_offset), G.formatDuration(encounter.duration_minutes), G.formatDistance(encounter.min_distance_m),
        G.encounterOffsetText(encounter) || "-",
        !encounter.movement ? "-" : G.encounterClassText(encounter) + (encounter.movement === "joint movement" ? ", " + G.formatDistance(encounter.path_length_m) : "")], encounter: encounter };
    }), function (row) {
      map.setView([row.encounter.lat, row.encounter.lon], 17);
      if (G.showEncounter) { G.showEncounter(row.encounter); }
    }));
  }
  function sharedPlacesSection(crossAnalysis) {
    var shown = (crossAnalysis.shared_places || []).filter(function (place) {
      return place.sources.filter(G.isSourceVisible).length >= 2;
    });
    if (!shown.length) { return; }
    briefing.appendChild(element("h2", "Shared places (" + shown.length + ")"));
    briefing.appendChild(table(["#", "Sources", "Visits", "Overlapping"], shown.map(function (place) {
      var visits = place.visits.filter(function (visit) { return G.isSourceVisible(visit.source_id); });
      return { cells: [String(place.id), place.sources.filter(G.isSourceVisible).map(G.sourceName).join(", "), String(visits.length), place.overlapping ? "yes" : "no"], place: place };
    }), function (row) { map.setView([row.place.lat, row.place.lon], 16); }));
  }
  function buildBriefing() {
    var openSources = {};
    Array.prototype.forEach.call(briefing.querySelectorAll("details.briefing-source"), function (section) {
      openSources[section.getAttribute("data-source")] = section.open;
    });
    var firstBuild = !briefing.firstChild;
    briefing.textContent = "";
    var provenance = payload.provenance;
    briefing.appendChild(element("h2", "Briefing · " + payload.project));
    briefing.appendChild(element("h2", "Sources (" + G.sources.length + ")"));
    briefing.appendChild(sourcesTable());
    if (!payload.analysis) {
      briefing.appendChild(element("p", "No analysis available for this map."));
    } else {
      G.visibleSources().forEach(function (source, index) {
        var open = firstBuild || openSources[String(source.id)] === undefined ? index === 0 : openSources[String(source.id)];
        briefing.appendChild(sourceSection(source, open));
      });
      if (G.sources.length >= 2) { encountersSection(payload.analysis); }
      sharedPlacesSection(payload.analysis);
    }
    briefing.appendChild(element("h2", "Provenance"));
    var provenanceRows = [];
    (provenance.sources || []).forEach(function (source) {
      provenanceRows.push({ cells: ["Source " + source.id + " · " + source.label, source.file_name] });
      provenanceRows.push({ cells: ["SHA-256", source.sha256 || "(not available: run was cancelled)"] });
    });
    provenanceRows.push(
      { cells: ["Generated", provenance.generated_at] },
      { cells: ["Base map at generation", provenance.tile_source + (provenance.tile_provider ? " (" + provenance.tile_provider + ")" : "")] },
      { cells: ["Online services", provenance.online_enabled ? "on" : "off"] },
      { cells: ["Application", provenance.application] }
    );
    briefing.appendChild(table(["Field", "Value"], provenanceRows));
  }
  buildBriefing();
  G.onSourcesChange(buildBriefing);
  byId("briefing-toggle").addEventListener("click", function () {
    briefing.hidden = !briefing.hidden;
    setTimeout(function () { map.invalidateSize(); }, 50);
  });

  // ---- print -----------------------------------------------------------
  var printHeader = byId("print-header");
  function printHeaderText() {
    var generatedWith = payload.provenance.tile_source + (payload.provenance.tile_provider ? " (" + payload.provenance.tile_provider + ")" : "");
    var attributionNode = document.querySelector(".leaflet-control-attribution");
    var attribution = attributionNode ? attributionNode.textContent : "";
    var lines = ["GEOSnap map · project " + payload.project];
    (payload.provenance.sources || []).forEach(function (source) {
      lines.push("source " + source.id + " " + source.label + (G.isSourceVisible(source.id) ? "" : " (hidden in this view)") +
        ": " + source.file_name + " · SHA-256 " + (source.sha256 || "n/a"));
    });
    lines.push(
      "generated " + payload.provenance.generated_at + " · base map at generation " + generatedWith +
        " · shown now: " + G.baseMapKey + (G.usesTiles ? " (tiles print only when loaded)" : "") +
        " · times in " + payload.display_zone,
      "view: " + G.filterDescription()
    );
    if (attribution) { lines.push("attribution: " + attribution); }
    return lines.join("\n");
  }
  printHeader.textContent = printHeaderText();
  byId("print-button").addEventListener("click", function () {
    printHeader.textContent = printHeaderText();
    var wasHidden = briefing.hidden;
    briefing.hidden = false;
    window.print();
    briefing.hidden = wasHidden;
  });
  // Collapsed source sections would print empty: open them for the print, restore afterwards.
  var collapsedForPrint = [];
  window.addEventListener("beforeprint", function () {
    printHeader.textContent = printHeaderText();
    collapsedForPrint = Array.prototype.filter.call(briefing.querySelectorAll("details"), function (section) { return !section.open; });
    collapsedForPrint.forEach(function (section) { section.open = true; });
    map.invalidateSize();
  });
  window.addEventListener("afterprint", function () {
    collapsedForPrint.forEach(function (section) { section.open = false; });
    collapsedForPrint = [];
    map.invalidateSize();
  });

  // ---- exports ---------------------------------------------------------
  function csvCell(value) {
    var text = value === null || value === undefined ? "" : String(value);
    return /[",\n\r]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }
  G.csvCell = csvCell;
  byId("export-csv").addEventListener("click", function () {
    var header = ["line_number", "latitude", "longitude", "latitude_accuracy_m", "longitude_accuracy_m", "accuracy_radius_m", "timestamp_utc", "timestamp_local", "local_timezone", "duplicate_count",
      "source_id", "source_label", "record_label", "record_note", "accuracy_known", "original_line"];
    var rows = G.lastVisible.map(function (p) {
      return [p.n, p.lat, p.lon, p.lat_acc, p.lon_acc, p.radius, p.utc, G.localIso(p.local, p.off), p.zone, p.dup,
        p.s, G.sourceName(p.s), p.lbl, p.note, p.acc_known !== false, p.line].map(csvCell).join(",");
    });
    // Records without a timestamp: the same columns, the time and duplicate cells empty.
    rows = rows.concat(G.lastVisibleUndated.map(function (r) {
      return [r.n, r.lat, r.lon, r.acc, r.acc, r.acc, "", "", "", "",
        r.s, G.sourceName(r.s), r.lbl, r.note, r.acc_known !== false, r.line].map(csvCell).join(",");
    }));
    G.downloadBlob(derivedName("csv"), new Blob(["﻿" + [header.join(",")].concat(rows).join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" }));
    byId("status").textContent = "CSV exported (derived, not recorded): import it as text, do not open it by double-click";
  });
  byId("export-geojson").addEventListener("click", function () {
    var features = G.lastVisible.map(function (p) {
      return { type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] },
        properties: { source_id: p.s, source_label: G.sourceName(p.s), line_number: p.n, accuracy_radius_m: p.radius, accuracy_known: p.acc_known !== false,
          record_label: p.lbl, record_note: p.note, timestamp_utc: p.utc, timestamp_local: G.localIso(p.local, p.off), duplicate_count: p.dup, original_line: p.line } };
    });
    G.lastVisibleUndated.forEach(function (r) {
      features.push({ type: "Feature", geometry: { type: "Point", coordinates: [r.lon, r.lat] },
        properties: { source_id: r.s, source_label: G.sourceName(r.s), line_number: r.n, accuracy_radius_m: r.acc, accuracy_known: r.acc_known !== false,
          record_label: r.lbl, record_note: r.note, timestamp_utc: null, timestamp_local: null, duplicate_count: null, original_line: r.line } });
    });
    G.visibleSources().forEach(function (source) {
      var track = G.lastVisibleBySource[source.id] || [];
      if (track.length < 2) { return; }
      features.push({ type: "Feature", geometry: { type: "LineString", coordinates: track.map(function (p) { return [p.lon, p.lat]; }) },
        properties: { source_id: source.id, source_label: source.label, name: payload.project + " · " + source.label + " route (shown range)" } });
    });
    var document_ = { type: "FeatureCollection", features: features, properties: { derived: true, project: payload.project, view: G.filterDescription(),
      sources: (payload.provenance.sources || []).map(function (source) { return L.extend({}, source, { shown: G.isSourceVisible(source.id) }); }) } };
    G.downloadBlob(derivedName("geojson"), new Blob([JSON.stringify(document_)], { type: "application/geo+json" }));
  });
  function xmlEscape(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  byId("export-gpx").addEventListener("click", function () {
    var lines = ['<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="' + xmlEscape(payload.provenance.application) + ' (derived export)" xmlns="http://www.topografix.com/GPX/1/1">'];
    var view = G.filterDescription();
    // One track per shown source: devices of different people never form one track.
    G.visibleSources().forEach(function (source) {
      var track = G.lastVisibleBySource[source.id] || [];
      if (!track.length) { return; }
      lines.push("<trk><name>" + xmlEscape(payload.project + " · " + source.label + " (shown range, derived)") + "</name><desc>" + xmlEscape(view) + "</desc><trkseg>");
      track.forEach(function (p) {
        var accuracy = p.acc_known === false ? "accuracy not reported" : "±" + p.radius + " m";
        lines.push('<trkpt lat="' + p.lat + '" lon="' + p.lon + '"><time>' + xmlEscape(p.utc.replace("+00:00", "Z")) + "</time><desc>" + xmlEscape(accuracy + "; " + recordName(p)) + "</desc></trkpt>");
      });
      lines.push("</trkseg></trk>");
    });
    // Records without a timestamp are waypoints: a track point without a time says nothing
    // about the order of the track.
    G.lastVisibleUndated.forEach(function (r) {
      var accuracy = r.acc_known === false ? "accuracy not reported" : "±" + r.acc + " m";
      lines.push('<wpt lat="' + r.lat + '" lon="' + r.lon + '"><name>' + xmlEscape(r.lbl || (G.recordNoun(r.s) + " " + r.n)) + "</name>" +
        "<desc>" + xmlEscape("no timestamp in the record; " + accuracy + "; " + G.sourceName(r.s)) + "</desc></wpt>");
    });
    lines.push("</gpx>");
    G.downloadBlob(derivedName("gpx"), new Blob([lines.join("\n")], { type: "application/gpx+xml" }));
  });
  byId("export-png").addEventListener("click", function () {
    var container = map.getContainer();
    var bounds = container.getBoundingClientRect();
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(bounds.width);
    canvas.height = Math.round(bounds.height);
    var context = canvas.getContext("2d");
    context.fillStyle = G.usesTiles ? "#ffffff" : "#e9ecef";
    context.fillRect(0, 0, canvas.width, canvas.height);
    try {
      Array.prototype.forEach.call(container.querySelectorAll("img.leaflet-tile-loaded"), function (tile) {
        var rect = tile.getBoundingClientRect();
        context.drawImage(tile, rect.left - bounds.left, rect.top - bounds.top, rect.width, rect.height);
      });
      Array.prototype.forEach.call(container.querySelectorAll(".leaflet-overlay-pane canvas"), function (layerCanvas) {
        var rect = layerCanvas.getBoundingClientRect();
        context.drawImage(layerCanvas, rect.left - bounds.left, rect.top - bounds.top, rect.width, rect.height);
      });
      context.fillStyle = "#000000";
      context.font = "12px sans-serif";
      context.fillText("GEOSnap " + payload.project + " · " + payload.provenance.generated_at + " · " + G.filterDescription() + " · derived export, markers and labels not included", 8, canvas.height - 8);
      canvas.toBlob(function (blob) {
        if (!blob) { byId("status").textContent = "PNG export failed"; return; }
        G.downloadBlob(derivedName("png"), blob);
      }, "image/png");
    } catch (error) {
      byId("status").textContent = "PNG export not possible with this base map (" + error.name + ")";
    }
  });
})();
