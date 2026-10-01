// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  // Presence matrix: places as rows, the shown sources as columns, each cell the
  // visits of one source at one place, computed at generation time. Rows of shared and stay
  // places count stays, rows of case places count runs of reports inside the radius. A cell
  // describes the reports of a device, never a person. Hidden sources lose their column, and
  // "simultaneous" is judged among the shown sources only. The Tolerance field
  // regroups the stay centres in the browser with the run's algorithm (G.presenceMatrixRows);
  // rows with a simultaneous visit are highlighted and listed first, then by total dwell.
  var G = window.GEOSNAP;
  var runMatrix = (G.payload.analysis && G.payload.analysis.presence_matrix) || null;
  var matrix = runMatrix;
  var element = G.element;
  function byId(id) { return document.getElementById(id); }
  var matrixWindow = byId("matrix-window");
  var viewBox = byId("view-matrix");
  var sharedOnlyBox = byId("matrix-shared-only");
  var toleranceInput = byId("matrix-tolerance");
  var toleranceNote = byId("matrix-tolerance-note");
  var tableHolder = byId("matrix-table");
  var detail = byId("matrix-detail");
  var TOLERANCE_MAX_M = 3000;
  var VISITS_LISTED = 50;
  var KIND_MEASURES = {
    "shared place": "visit = stay of this source at the shared place",
    "stay place": "visit = stay of this source at this place",
    "case place": "visit = run of consecutive reports inside the radius entered by the examiner"
  };
  var highlightLayer = L.layerGroup().addTo(G.map);
  var selectedKey = null;
  // Scrolls with the table, so the detail below the table keeps its room.
  var note = element("p", null, "note");
  note.id = "matrix-note";

  // [matrix-rows]
  // The rows of the presence matrix from stays and case places, the algorithm of
  // analysis/presence_matrix.py and encounters.group_stays_into_places: stay centres within
  // the tolerance of a running place centre (greedy, in source order, the longitude moved the
  // short way round so a place across the antimeridian stays there) form one place; places of
  // two or more sources come first by total dwell, then the located case places as entered,
  // then the places of one source by total dwell; the cap keeps every case place and the
  // longest dwell. tests/test_map_matrix.py runs this block in node against the Python
  // result. No DOM, no Leaflet.
  // Microseconds since the epoch. Date.parse keeps only milliseconds, while the readers
  // keep microseconds: parsed with it, two visits 1 us apart would touch and the map would
  // claim a simultaneous visit that the report denies. Microsecond counts of present-day
  // dates stay below 2^53, so they are exact integers here.
  var FRACTION_OF_A_SECOND = /\.(\d+)/;
  function utcMicroseconds(iso) {
    var fraction = FRACTION_OF_A_SECOND.exec(iso);
    if (fraction === null) { return Date.parse(iso) * 1000; }
    var whole = iso.slice(0, fraction.index) + iso.slice(fraction.index + fraction[0].length);
    // Six digits exactly: ".5" is 500000 us, a longer fraction than the readers keep is cut.
    return Date.parse(whole) * 1000 + Number((fraction[1] + "000000").slice(0, 6));
  }
  function utcSeconds(iso) { return utcMicroseconds(iso) / 1e6; }
  // Minutes between two ISO timestamps, computed as Python does it (seconds, then minutes).
  function minutesBetween(fromIso, toIso) {
    return (utcMicroseconds(toIso) - utcMicroseconds(fromIso)) / 1e6 / 60;
  }
  function longitudeOffset(from, to) {
    var offset = to - from;
    if (offset > 180) { return offset - 360; }
    if (offset < -180) { return offset + 360; }
    return offset;
  }
  function normalisedLongitude(longitude) {
    if (longitude > 180) { return longitude - 360; }
    if (longitude < -180) { return longitude + 360; }
    return longitude;
  }
  // Visits (source, start, end; bounds included) of different sources intersect: one pass
  // in start order against the latest end seen per source.
  function overlapBetweenSources(intervals) {
    var ordered = intervals.slice().sort(function (a, b) { return a.start - b.start; });
    var latestEnd = {};
    for (var i = 0; i < ordered.length; i++) {
      var interval = ordered[i];
      var sources = Object.keys(latestEnd);
      for (var j = 0; j < sources.length; j++) {
        if (Number(sources[j]) !== interval.source && latestEnd[sources[j]] >= interval.start) { return true; }
      }
      if (latestEnd[interval.source] === undefined || interval.end > latestEnd[interval.source]) { latestEnd[interval.source] = interval.end; }
    }
    return false;
  }
  function visitIntervals(cells) {
    var intervals = [];
    cells.forEach(function (cell) {
      cell.visits.forEach(function (visit) {
        intervals.push({ source: cell.source_id, start: utcMicroseconds(visit.first_utc), end: utcMicroseconds(visit.last_utc) });
      });
    });
    return intervals;
  }
  function groupStaysIntoPlaces(sourceIds, staysBySource, toleranceM) {
    var clusters = [], centres = [];
    sourceIds.slice().sort(function (a, b) { return a - b; }).forEach(function (sourceId) {
      var stays = (staysBySource[String(sourceId)] && staysBySource[String(sourceId)].stays) || [];
      stays.forEach(function (stay) {
        var best = -1, bestDistance = toleranceM;
        for (var i = 0; i < centres.length; i++) {
          var distance = G.haversineMetres(centres[i].lat, centres[i].lon, stay.lat, stay.lon);
          if (distance <= bestDistance) { best = i; bestDistance = distance; }
        }
        var visit = { source_id: sourceId, stay: stay };
        if (best < 0) {
          clusters.push([visit]);
          centres.push({ lat: stay.lat, lon: stay.lon });
          return;
        }
        var count = clusters[best].length, centre = centres[best];
        centres[best] = {
          lat: (centre.lat * count + stay.lat) / (count + 1),
          lon: normalisedLongitude(centre.lon + longitudeOffset(centre.lon, stay.lon) / (count + 1))
        };
        clusters[best].push(visit);
      });
    });
    var shared = [], single = [];
    clusters.forEach(function (visits, index) {
      var sources = [];
      visits.forEach(function (visit) { if (sources.indexOf(visit.source_id) < 0) { sources.push(visit.source_id); } });
      var places = sources.length >= 2 ? shared : single;
      places.push({ identifier: places.length + 1, lat: centres[index].lat, lon: centres[index].lon, visits: visits });
    });
    return { shared: shared, single: single };
  }
  function stayVisit(stay) {
    return {
      first_utc: stay.arrive_utc, first_local: stay.arrive_local, first_offset: stay.arrive_offset,
      last_utc: stay.leave_utc, last_local: stay.leave_local, last_offset: stay.leave_offset,
      duration_minutes: minutesBetween(stay.arrive_utc, stay.leave_utc),
      report_count: stay.point_count, first_line: stay.first_line, last_line: stay.last_line, stay_id: stay.id
    };
  }
  function casePlaceVisit(visit) {
    return {
      first_utc: visit.first_utc, first_local: visit.first_local, first_offset: visit.first_offset,
      last_utc: visit.last_utc, last_local: visit.last_local, last_offset: visit.last_offset,
      duration_minutes: minutesBetween(visit.first_utc, visit.last_utc),
      report_count: visit.report_count, first_line: visit.first_line, last_line: visit.last_line, stay_id: null
    };
  }
  function matrixCells(columns, visitsBySource) {
    return columns.map(function (sourceId) {
      var visits = (visitsBySource[sourceId] || []).slice().sort(function (a, b) { return utcMicroseconds(a.first_utc) - utcMicroseconds(b.first_utc); });
      var dwell = 0, first = null, last = null;
      visits.forEach(function (visit) {
        dwell += visit.duration_minutes;
        if (first === null || utcMicroseconds(visit.first_utc) < utcMicroseconds(first.first_utc)) { first = visit; }
        if (last === null || utcMicroseconds(visit.last_utc) > utcMicroseconds(last.last_utc)) { last = visit; }
      });
      return {
        source_id: sourceId, visit_count: visits.length, dwell_minutes: dwell,
        first_utc: first ? first.first_utc : null, first_local: first ? first.first_local : null, first_offset: first ? first.first_offset : null,
        last_utc: last ? last.last_utc : null, last_local: last ? last.last_local : null, last_offset: last ? last.last_offset : null,
        visits: visits
      };
    });
  }
  function matrixRow(kind, label, referenceId, lat, lon, radiusM, cells) {
    var dwell = 0;
    cells.forEach(function (cell) { dwell += cell.dwell_minutes; });
    return { id: 0, kind: kind, label: label, reference_id: referenceId, lat: lat, lon: lon, radius_m: radiusM,
      simultaneous: overlapBetweenSources(visitIntervals(cells)), dwell_minutes: dwell, cells: cells };
  }
  function stayPlaceRows(columns, places, kind, noun, toleranceM) {
    var rows = places.map(function (place) {
      var visitsBySource = {};
      place.visits.forEach(function (visit) {
        (visitsBySource[visit.source_id] = visitsBySource[visit.source_id] || []).push(stayVisit(visit.stay));
      });
      return matrixRow(kind, noun + " " + place.identifier, place.identifier, place.lat, place.lon, toleranceM, matrixCells(columns, visitsBySource));
    });
    return rows.sort(function (a, b) { return b.dwell_minutes - a.dwell_minutes || a.reference_id - b.reference_id; });
  }
  // sourceIds: the columns; staysBySource: payload.analysis.per_source (stays per source id);
  // casePlaces: payload.analysis.case_places.places (checked at generation); rowCap as the run's.
  G.presenceMatrixRows = function (sourceIds, staysBySource, casePlaces, toleranceM, rowCap) {
    var columns = sourceIds.slice();
    var grouped = groupStaysIntoPlaces(columns, staysBySource, toleranceM);
    var caseRows = [];
    (casePlaces || []).forEach(function (place) {
      if (place.lat === null || place.lon === null || place.lat === undefined || place.lon === undefined) { return; }
      var visitsBySource = {};
      (place.checks || []).forEach(function (check) { visitsBySource[check.source_id] = check.visits.map(casePlaceVisit); });
      caseRows.push(matrixRow("case place", place.label, place.id, place.lat, place.lon, place.radius_m, matrixCells(columns, visitsBySource)));
    });
    var ordered = stayPlaceRows(columns, grouped.shared, "shared place", "Shared place", toleranceM)
      .concat(caseRows, stayPlaceRows(columns, grouped.single, "stay place", "Stay place", toleranceM));
    ordered.forEach(function (row, index) { row.id = index + 1; });
    var caseCount = ordered.filter(function (row) { return row.kind === "case place"; }).length;
    var stayRowsLeft = Math.max(0, rowCap - caseCount);
    var listed = ordered.filter(function (row) {
      if (row.kind === "case place") { return true; }
      if (stayRowsLeft === 0) { return false; }
      stayRowsLeft -= 1;
      return true;
    });
    return { row_cap: rowCap, rows_total: ordered.length, rows_omitted: ordered.length - listed.length,
      tolerance_m: toleranceM, sources: columns, rows: listed };
  };
  // [/matrix-rows]

  function cellOf(row, sourceId) {
    return row.cells.filter(function (cell) { return cell.source_id === sourceId; })[0] || null;
  }
  function shownColumns() { return matrix ? matrix.sources.filter(G.isSourceVisible) : []; }
  function visitingSources(row, columns) {
    return columns.filter(function (sourceId) {
      var cell = cellOf(row, sourceId);
      return cell && cell.visit_count > 0;
    });
  }
  // Visits of different shown sources overlap in time (bounds included).
  function simultaneousAmong(row, columns) {
    return overlapBetweenSources(visitIntervals(columns.map(function (sourceId) { return cellOf(row, sourceId); }).filter(Boolean)));
  }
  function shownDwell(row, columns) {
    var dwell = 0;
    columns.forEach(function (sourceId) {
      var cell = cellOf(row, sourceId);
      if (cell) { dwell += cell.dwell_minutes; }
    });
    return dwell;
  }
  // The tolerance in the field, or null while it holds no whole number of metres in range.
  function chosenTolerance() {
    var text = toleranceInput.value.trim();
    if (!/^\d{1,4}$/.test(text)) { return null; }
    var value = Number(text);
    return value <= TOLERANCE_MAX_M ? value : null;
  }
  // What the rows use now, named the same way in every state of the input.
  function toleranceInForceText() {
    return matrix.tolerance_m === runMatrix.tolerance_m
      ? "tolerance " + matrix.tolerance_m + " m (the run value; the report and CSV use it)"
      : "tolerance " + matrix.tolerance_m + " m (view choice; the report and CSV use " + runMatrix.tolerance_m + " m)";
  }
  // The run's rows at the run's tolerance (as the report and the CSV), else recomputed here.
  function applyTolerance() {
    var tolerance = chosenTolerance();
    if (tolerance === null) {
      // A refused entry must still say what the table in front of the examiner shows.
      toleranceNote.textContent = "Whole metres from 0 to " + TOLERANCE_MAX_M + " only; still showing " + toleranceInForceText() + ".";
      return;
    }
    if (tolerance === runMatrix.tolerance_m) {
      matrix = runMatrix;
      toleranceNote.textContent = toleranceInForceText();
    } else {
      matrix = G.presenceMatrixRows(runMatrix.sources, G.payload.analysis.per_source || {},
        (G.payload.analysis.case_places && G.payload.analysis.case_places.places) || [], tolerance, runMatrix.row_cap);
      toleranceNote.textContent = toleranceInForceText();
    }
    clearSelection();
    renderMatrix();
  }
  function visitCountText(count) { return count + (count === 1 ? " visit" : " visits"); }
  function visitSpanText(visit) {
    return G.localText(visit.first_local, visit.first_offset) + " – " + G.localText(visit.last_local, visit.last_offset);
  }

  // The centre of the largest free part of the map (left of, above or right of this window),
  // in container pixels: a place shown from the matrix must not end up under the matrix.
  function freeMapCentre() {
    var size = G.map.getSize();
    if (matrixWindow.hidden) { return L.point(size.x / 2, size.y / 2); }
    var mapBox = G.map.getContainer().getBoundingClientRect(), windowBox = matrixWindow.getBoundingClientRect();
    var freeLeft = Math.max(0, windowBox.left - mapBox.left), freeAbove = Math.max(0, windowBox.top - mapBox.top), freeRight = Math.max(0, mapBox.right - windowBox.right);
    var candidates = [
      { area: freeLeft * size.y, centre: L.point(freeLeft / 2, size.y / 2) },
      { area: freeAbove * size.x, centre: L.point(size.x / 2, freeAbove / 2) },
      { area: freeRight * size.y, centre: L.point(size.x - freeRight / 2, size.y / 2) }
    ];
    candidates.sort(function (a, b) { return b.area - a.area; });
    return candidates[0].centre;
  }
  function highlightPlace(row) {
    highlightLayer.clearLayers();
    L.circle([row.lat, row.lon], { radius: row.radius_m, color: G.inkColour, weight: 3, dashArray: "4 4", fillOpacity: 0.05, interactive: false }).addTo(highlightLayer);
    var zoom = Math.max(G.map.getZoom(), 16), size = G.map.getSize();
    var shift = L.point(size.x / 2, size.y / 2).subtract(freeMapCentre());
    G.map.setView(G.map.unproject(G.map.project([row.lat, row.lon], zoom).add(shift), zoom), zoom);
  }
  // Moves the time cursor to the first report of a visit; the cursor stays inside From/To.
  function cursorToVisit(visit, note) {
    var seconds = utcSeconds(visit.first_utc);
    G.setCursorSeconds(seconds);
    note.textContent = seconds < G.filters.fromSeconds || seconds > G.filters.toSeconds
      ? "This visit lies outside the From/To range, so the time cursor stopped at the range end. Reset the range to see it."
      : "Time cursor at " + G.localText(visit.first_local, visit.first_offset) + ".";
  }
  function clearSelection() {
    selectedKey = null;
    detail.textContent = "";
    highlightLayer.clearLayers();
  }
  function showDetail(row, cell) {
    detail.textContent = "";
    detail.appendChild(element("strong", row.label + " · " + G.sourceLabel(cell.source_id)));
    detail.appendChild(element("div", row.kind + ": " + KIND_MEASURES[row.kind] + "; radius " + G.formatDistance(row.radius_m), "matrix-muted"));
    var cursorNote = element("div", null, "matrix-muted");
    if (!cell.visit_count) {
      detail.appendChild(element("div", "No visit of this source was found at this place. This says nothing about where the device was."));
      return;
    }
    detail.appendChild(element("div", visitCountText(cell.visit_count) + ", total " + G.formatDuration(cell.dwell_minutes) +
      (cell.visit_count > VISITS_LISTED ? " (the first " + VISITS_LISTED + " are listed; presence_matrix_<stamp>.csv lists all)" : "")));
    var list = element("div", null, "matrix-visits");
    cell.visits.slice(0, VISITS_LISTED).forEach(function (visit) {
      var button = element("button", visitSpanText(visit) + " · " + G.formatDuration(visit.duration_minutes) + " · " + visit.report_count +
        (visit.report_count === 1 ? " report (" : " reports (") + G.recordNoun(cell.source_id, true) + " " + visit.first_line + "–" + visit.last_line + ")" +
        (visit.stay_id === null ? "" : " · stay " + visit.stay_id));
      button.type = "button";
      button.title = "Time cursor to the first report of this visit";
      button.addEventListener("click", function () { cursorToVisit(visit, cursorNote); });
      list.appendChild(button);
    });
    detail.appendChild(cursorNote);
    detail.appendChild(list);
    cursorToVisit(cell.visits[0], cursorNote);
  }
  // Flies to the place, marks its radius and sets the time cursor to the first visit.
  G.showMatrixCell = function (row, cell) {
    selectedKey = row.id + ":" + cell.source_id;
    highlightPlace(row);
    showDetail(row, cell);
    // The detail changes the room left for the table: keep the chosen cell in view.
    var chosen = tableHolder.querySelector("button.matrix-cell[data-cell=\"" + selectedKey + "\"]");
    if (chosen && chosen.scrollIntoView) { chosen.scrollIntoView({ block: "nearest", inline: "nearest" }); }
    Array.prototype.forEach.call(tableHolder.querySelectorAll("button.matrix-cell"), function (button) {
      button.classList.toggle("selected", button.getAttribute("data-cell") === selectedKey);
    });
  };

  function placeHeader(row, simultaneous) {
    var header = element("th", null, "matrix-place");
    header.scope = "row";
    var button = element("button", row.label, "matrix-place-button");
    button.type = "button";
    button.title = "Show the place on the map";
    button.addEventListener("click", function () {
      highlightPlace(row);
      var casePlace = row.kind === "case place" && (G.casePlaces || []).filter(function (place) { return place.id === row.reference_id; })[0];
      if (casePlace && G.showCasePlace) { G.showCasePlace(casePlace); }
    });
    header.appendChild(button);
    header.appendChild(element("span", row.kind, "matrix-tag"));
    if (simultaneous) {
      var tag = element("span", "simultaneous", "matrix-tag matrix-simultaneous");
      tag.title = "Visits of different shown sources overlap in time";
      header.appendChild(tag);
    }
    return header;
  }
  function cellNode(row, cell) {
    var node = element("td");
    var button = element("button", null, "matrix-cell" + (cell.visit_count ? "" : " empty"));
    button.type = "button";
    button.setAttribute("data-cell", row.id + ":" + cell.source_id);
    if (row.id + ":" + cell.source_id === selectedKey) { button.classList.add("selected"); }
    if (cell.visit_count) {
      button.appendChild(element("span", visitCountText(cell.visit_count) + " · " + G.formatDuration(cell.dwell_minutes)));
      button.appendChild(element("span", G.localText(cell.first_local, cell.first_offset) + " – " + G.localText(cell.last_local, cell.last_offset), "matrix-muted"));
      button.title = "First and last report of the visits. Click to show the place and move the time cursor to the first visit";
    } else {
      button.appendChild(element("span", "no visit"));
      button.title = "No visit of this source was found at this place";
    }
    button.addEventListener("click", function () { G.showMatrixCell(row, cell); });
    node.appendChild(button);
    return node;
  }
  function renderMatrix() {
    if (!matrix || matrixWindow.hidden) { return; }
    updateNote();
    var columns = shownColumns();
    // The detail of a source that is hidden meanwhile would keep showing its visits.
    if (selectedKey !== null && columns.indexOf(Number(selectedKey.split(":")[1])) < 0) { clearSelection(); }
    byId("matrix-shared-only-option").hidden = matrix.sources.length < 2;
    var rows = matrix.rows.filter(function (row) {
      var visiting = visitingSources(row, columns).length;
      return sharedOnlyBox.checked && matrix.sources.length >= 2 ? visiting >= 2 : (row.kind === "case place" || visiting >= 1);
    }).map(function (row) {
      return { row: row, simultaneous: simultaneousAmong(row, columns), dwell: shownDwell(row, columns) };
    });
    // Highlighted rows first, then the longest dwell over the shown sources, then matrix order.
    rows.sort(function (a, b) {
      return (b.simultaneous ? 1 : 0) - (a.simultaneous ? 1 : 0) || b.dwell - a.dwell || a.row.id - b.row.id;
    });
    byId("matrix-count").textContent = rows.length + " of " + matrix.rows.length + " listed places shown · " + columns.length + " of " + matrix.sources.length + " sources";
    tableHolder.textContent = "";
    if (!columns.length) {
      tableHolder.appendChild(element("p", "No source is shown: tick a source in the Sources window."));
      return;
    }
    var table = element("table");
    var head = element("tr");
    head.appendChild(element("th", "Place"));
    columns.forEach(function (sourceId) {
      var header = element("th");
      header.scope = "col";
      var swatch = element("span", null, "source-swatch");
      swatch.style.background = G.sourceColour(sourceId);
      header.appendChild(swatch);
      header.appendChild(element("span", " " + G.sourceName(sourceId)));
      header.title = G.sourceLabel(sourceId);
      head.appendChild(header);
    });
    table.appendChild(head);
    rows.forEach(function (entry) {
      var line = element("tr", null, entry.simultaneous ? "matrix-hit" : null);
      line.appendChild(placeHeader(entry.row, entry.simultaneous));
      columns.forEach(function (sourceId) { line.appendChild(cellNode(entry.row, cellOf(entry.row, sourceId))); });
      table.appendChild(line);
    });
    tableHolder.appendChild(table);
    if (!rows.length) { tableHolder.appendChild(element("p", "No place matches: no listed place has visits of two shown sources.")); }
    tableHolder.appendChild(note);
    // Cells scrolled into view (keyboard focus, cell click) stop below the sticky head.
    tableHolder.style.scrollPaddingTop = head.offsetHeight + "px";
  }

  function applyWindowVisibility() {
    matrixWindow.hidden = !viewBox.checked;
    if (!matrixWindow.hidden) {
      G.setWindowCollapsed(matrixWindow, false);
      renderMatrix();
    }
  }
  function updateNote() {
    // Only what is needed to read the table; the full rules are in the help.
    var text = "Case places count reports inside their radius; other places count stays. Don't compare the two.";
    if (matrix.rows_omitted) {
      text += " Showing " + matrix.rows.length + " of " + matrix.rows_total + " places; the CSV has all of them.";
    }
    note.textContent = text;
  }
  if (matrix) {
    toleranceInput.value = String(runMatrix.tolerance_m);
    toleranceInput.addEventListener("change", applyTolerance);
    byId("matrix-tolerance-reset").addEventListener("click", function () {
      toleranceInput.value = String(runMatrix.tolerance_m);
      applyTolerance();
    });
    toleranceNote.textContent = toleranceInForceText();
    viewBox.addEventListener("change", applyWindowVisibility);
    byId("matrix-close").addEventListener("click", function () {
      viewBox.checked = false;
      applyWindowVisibility();
    });
    sharedOnlyBox.addEventListener("change", renderMatrix);
    G.onSourcesChange(renderMatrix);
    G.onSourceColourChange(renderMatrix);
    G.map.on("click", function () { highlightLayer.clearLayers(); });
  }
})();
