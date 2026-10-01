// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  var payload = JSON.parse(document.getElementById("geosnap-data").textContent);
  var providerByKey = {};
  (payload.tile_providers || []).forEach(function (provider) { providerByKey[provider.key] = provider; });

  function initialBaseMapKey() {
    if (payload.tile_source === "online" && providerByKey[payload.tile_provider]) { return payload.tile_provider; }
    if (payload.tile_source === "local" && payload.tiles_url) { return "local"; }
    return "none";
  }

  var map = L.map("map", { preferCanvas: true, maxZoom: 20 });
  map.attributionControl.setPrefix(false);
  L.control.scale({ imperial: false }).addTo(map);
  var baseLayer = null;

  var pointLayer = L.layerGroup();
  var circleLayer = L.layerGroup();
  var routeLayer = L.layerGroup();
  var hullLayer = L.layerGroup();
  var graticuleLayer = L.layerGroup();
  var heatLayer = L.heatLayer([], { radius: 25, blur: 15, maxZoom: 17 });
  // leaflet-heat keeps a pending redraw frame after removal; cancel it so it never draws on a
  // detached layer.
  heatLayer.on("remove", function () {
    if (heatLayer._frame) {
      L.Util.cancelAnimFrame(heatLayer._frame);
      heatLayer._frame = null;
    }
  });

  // Points of all sources in one chronological order (ties: source order), so every per-source
  // list taken from it is chronological too. Order, comparison and durations use UTC seconds.
  payload.points.forEach(function (point) { point.utcSeconds = Date.parse(point.utc) / 1000; });
  var allPoints = payload.points.slice().sort(function (a, b) {
    return a.utcSeconds - b.utcSeconds || a.s - b.s;
  });
  var pointBySourceLine = {};
  allPoints.forEach(function (point) { pointBySourceLine[point.s + ":" + point.n] = point; });

  var sources = payload.sources || [];
  var sourceById = {};
  var sourceVisible = {};
  var sourceColours = {};
  sources.forEach(function (source) {
    sourceById[source.id] = source;
    sourceVisible[source.id] = true;
    sourceColours[source.id] = source.colour;
  });

  var GEOSNAP = {
    payload: payload,
    map: map,
    usesTiles: false,
    baseMapKey: "none",
    allPoints: allPoints,
    sources: sources,
    sourceById: sourceById,
    lastVisibleBySource: {},
    sourceListeners: [],
    colourListeners: [],
    // from/to: wall clock "YYYY-MM-DDTHH:MM:SS" as entered; fromSeconds/toSeconds and
    // cursorSeconds: the UTC instants every comparison uses.
    filters: { from: "", to: "", fromSeconds: 0, toSeconds: 0, maxAccuracy: null, cursorSeconds: null },
    overlays: [],
    renderListeners: [],
    legendEntries: [],
    colourByTime: false,
    lastVisible: [],
    started: false
  };
  window.GEOSNAP = GEOSNAP;

  // ---- display zone: wall clock <-> UTC only through payload.zone_transitions ----------
  // Entries [utc seconds, offset minutes east of UTC, abbreviation]; the first also holds
  // before its instant. The browser's own zone is never consulted.
  var zoneTransitions = payload.zone_transitions && payload.zone_transitions.length ? payload.zone_transitions : [[0, 0, "UTC"]];
  function wallAsUtcSeconds(wall) {
    return Date.parse(wall.slice(0, 16) + (wall.length >= 19 ? wall.slice(16, 19) : ":00") + "Z") / 1000;
  }
  function pad2(n) { return n < 10 ? "0" + n : String(n); }
  GEOSNAP.offsetText = function (offsetMinutes) {
    var magnitude = Math.abs(offsetMinutes);
    return (offsetMinutes < 0 ? "-" : "+") + pad2(Math.floor(magnitude / 60)) + ":" + pad2(magnitude % 60);
  };
  // {local: "YYYY-MM-DDTHH:MM:SS", offset: minutes, abbreviation} of a UTC instant.
  GEOSNAP.utcToLocal = function (seconds) {
    var index = 0;
    while (index + 1 < zoneTransitions.length && zoneTransitions[index + 1][0] <= seconds) { index += 1; }
    var entry = zoneTransitions[index];
    return { local: new Date((Math.floor(seconds) + entry[1] * 60) * 1000).toISOString().slice(0, 19), offset: entry[1], abbreviation: entry[2] };
  };
  // Every UTC instant whose wall clock reads `wall`: none in a skipped hour, two in a repeated one.
  function occurrencesOf(wall) {
    var naive = wallAsUtcSeconds(wall);
    var found = [];
    zoneTransitions.forEach(function (entry, index) {
      var candidate = naive - entry[1] * 60;
      var next = zoneTransitions[index + 1];
      if ((index === 0 || candidate >= entry[0]) && (!next || candidate < next[0])) { found.push(candidate); }
    });
    return found;
  }
  // A repeated wall time gives its earlier occurrence (later with preferLater); a skipped
  // one gives the next valid instant, the clock change itself.
  GEOSNAP.localToUtcSeconds = function (wall, preferLater) {
    var found = occurrencesOf(wall);
    if (found.length) { return preferLater ? found[found.length - 1] : found[0]; }
    var naive = wallAsUtcSeconds(wall);
    for (var i = 1; i < zoneTransitions.length; i++) {
      if (naive < zoneTransitions[i][0] + zoneTransitions[i][1] * 60) { return zoneTransitions[i][0]; }
    }
    return naive - zoneTransitions[zoneTransitions.length - 1][1] * 60;
  };
  GEOSNAP.isAmbiguousLocal = function (wall) { return occurrencesOf(wall).length > 1; };
  // Display text of a wall time; the offset is added only where the wall time is ambiguous.
  GEOSNAP.localText = function (wall, offsetMinutes) {
    var text = wall.replace("T", " ");
    return typeof offsetMinutes === "number" && GEOSNAP.isAmbiguousLocal(wall) ? text + " " + GEOSNAP.offsetText(offsetMinutes) : text;
  };
  GEOSNAP.localTextAt = function (seconds) {
    var local = GEOSNAP.utcToLocal(seconds);
    return GEOSNAP.localText(local.local, local.offset);
  };
  // ISO wall time with offset, as in the CSV files ("2026-10-25T02:30:00+02:00").
  GEOSNAP.localIso = function (wall, offsetMinutes) {
    return typeof offsetMinutes === "number" ? wall + GEOSNAP.offsetText(offsetMinutes) : wall;
  };

  // ---- sources: visibility and colour are view choices in this browser, not recorded ----
  GEOSNAP.pointAt = function (sourceId, line) { return pointBySourceLine[sourceId + ":" + line] || null; };
  GEOSNAP.sourceAnalysis = function (sourceId) {
    var analysis = payload.analysis;
    return analysis && analysis.per_source ? analysis.per_source[String(sourceId)] || null : null;
  };
  GEOSNAP.sourceColour = function (sourceId) { return sourceColours[sourceId] || "#1d5c8f"; };
  GEOSNAP.sourceName = function (sourceId) {
    var source = sourceById[sourceId];
    return source ? source.label : "source " + sourceId;
  };
  GEOSNAP.sourceLabel = function (sourceId) {
    var source = sourceById[sourceId];
    return source ? source.icon + " " + source.label : "source " + sourceId;
  };
  // What a record number counts in this source ("line", "placemark", "GPX point", "record"),
  // named once per format in Python (record_noun) and carried in the payload.
  GEOSNAP.recordNoun = function (sourceId, plural) {
    var source = sourceById[sourceId];
    if (!source) { return plural ? "records" : "record"; }
    return (plural ? source.record_noun_plural : source.record_noun) || (plural ? "records" : "record");
  };
  // Whether a source's records carry an accuracy: "all", "some" or "none" (accuracy_reporting,
  // computed in Python; a payload without it has only the source-wide accuracy_known). Single
  // records are judged by their own accuracy_known / acc_known, never by this.
  GEOSNAP.accuracyReporting = function (source) {
    if (source.accuracy_reporting === "all" || source.accuracy_reporting === "some" || source.accuracy_reporting === "none") {
      return source.accuracy_reporting;
    }
    return source.accuracy_known === false ? "none" : "all";
  };
  GEOSNAP.accuracyReportingNote = function (source) {
    var reporting = GEOSNAP.accuracyReporting(source);
    return reporting === "none" ? "accuracy not reported" : reporting === "some" ? "accuracy partly reported" : "";
  };
  GEOSNAP.recordNounTitle = function (sourceId) {
    var noun = GEOSNAP.recordNoun(sourceId);
    return noun.charAt(0).toUpperCase() + noun.slice(1);
  };
  GEOSNAP.isSourceVisible = function (sourceId) { return !!sourceVisible[sourceId]; };
  GEOSNAP.visibleSources = function () {
    return sources.filter(function (source) { return sourceVisible[source.id]; });
  };
  GEOSNAP.onSourcesChange = function (listener) { GEOSNAP.sourceListeners.push(listener); };
  function sourcesChanged() {
    var visibleSources = GEOSNAP.visibleSources();
    GEOSNAP.capabilities.refresh();
    GEOSNAP.sourceListeners.forEach(function (listener) { listener(visibleSources); });
    if (GEOSNAP.started) { GEOSNAP.render(); }
  }
  GEOSNAP.setSourcesVisible = function (sourceIds, visible) {
    sourceIds.forEach(function (sourceId) { sourceVisible[sourceId] = !!visible; });
    sourcesChanged();
  };
  GEOSNAP.setSourceColour = function (sourceId, colour) {
    sourceColours[sourceId] = colour;
    Array.prototype.forEach.call(document.querySelectorAll('[data-source-swatch="' + Number(sourceId) + '"]'), function (swatch) {
      swatch.style.background = colour;
    });
    // A colour change only restyles: no recompute of the crystal ball or the briefing.
    GEOSNAP.colourListeners.forEach(function (listener) { listener(sourceId, colour); });
    if (GEOSNAP.started) { GEOSNAP.render(); }
  };
  GEOSNAP.onSourceColourChange = function (listener) { GEOSNAP.colourListeners.push(listener); };
  // Point totals of the shown sources, and how many sources are hidden, for the status line.
  var pointTotalBySource = {};
  allPoints.forEach(function (point) { pointTotalBySource[point.s] = (pointTotalBySource[point.s] || 0) + 1; });
  function shownTotalText() {
    var shown = GEOSNAP.visibleSources();
    var total = shown.reduce(function (sum, source) { return sum + (pointTotalBySource[source.id] || 0); }, 0);
    var hidden = sources.length - shown.length;
    return total + " points" + (hidden ? " (" + hidden + (hidden === 1 ? " source" : " sources") + " hidden)" : "");
  }
  // Chronological points per source id; callers iterate G.visibleSources() for the order.
  GEOSNAP.groupBySource = function (points) {
    var groups = {};
    points.forEach(function (point) { (groups[point.s] = groups[point.s] || []).push(point); });
    return groups;
  };
  // Records without a timestamp: one list, each record naming its source in
  // `s`, thinned per source exactly like the dated points.
  var undatedBySource = {};
  (payload.undated_points || []).forEach(function (record) {
    (undatedBySource[record.s] = undatedBySource[record.s] || []).push(record);
  });
  GEOSNAP.undatedPointsOf = function (sourceId) { return undatedBySource[sourceId] || []; };
  // Undated records of a source, drawn or not: the payload counts every one in
  // undated_total, while undatedPointsOf holds the thinned selection this map draws.
  GEOSNAP.undatedTotalOf = function (source) {
    return typeof source.undated_total === "number" ? source.undated_total : GEOSNAP.undatedPointsOf(source.id).length;
  };
  // "161" or "3000 (500 on this map)", worded like the dated records.
  GEOSNAP.undatedCountText = function (source) {
    var total = GEOSNAP.undatedTotalOf(source);
    var drawn = GEOSNAP.undatedPointsOf(source.id).length;
    return total + (drawn !== total ? " (" + drawn + " on this map)" : "");
  };
  // Undated records of every source, drawn or not: decides whether the legend names them.
  GEOSNAP.undatedTotal = function () {
    return sources.reduce(function (sum, source) { return sum + GEOSNAP.undatedTotalOf(source); }, 0);
  };
  GEOSNAP.lastVisibleUndated = [];
  // Every drawn position of the last render (dated and undated): what the view must cover.
  GEOSNAP.lastVisiblePositions = [];
  // A dated point and an undated record both carry lat/lon.
  function positionOf(record) { return [record.lat, record.lon]; }
  GEOSNAP.routeLayerShown = function () { return map.hasLayer(routeLayer); };

  // ---- capabilities: what the shown records allow ---------------------------------------
  // Python writes payload.capabilities over every source; the browser recomputes the same
  // numbers for the shown sources, so hiding a source updates the gating. FEATURE_REQUIREMENTS
  // names per feature the counts and their least values; the first unmet count gives the
  // reason. A feature marked perSource is judged per source (the crystal ball) and is
  // available when any shown source qualifies.
  var FEATURE_REQUIREMENTS = {
    time: { needs: { dated_points: 2, dated_share: 0.5 } },
    route: { needs: { sources_with_route: 1 } },
    last_known: { needs: { dated_points: 1 } },
    stays: { needs: { sources_with_route: 1, stays: 1 } },
    gaps: { needs: { sources_with_route: 1, gaps: 1 } },
    crystal: { needs: { dated_points: 20, days_with_data: 3 }, perSource: true },
    matrix: { needs: { matrix_rows: 1 } },
    encounters: { needs: { encounters: 1 } },
    shared_places: { needs: { shared_places: 1 } },
    case_places: { needs: { case_places: 1 } },
    route_shown: { needs: { route_shown: 1 } }
  };
  function countText(count, singular, plural) { return count + " " + (count === 1 ? singular : plural); }
  function datedOfTotalText(values) {
    var total = values.dated_points + values.undated_points;
    return values.dated_points + " of " + total + (values.dated_points === 1 ? " has" : " have") + " one";
  }
  var CAPABILITY_REASONS = {
    dated_points: function (values, needed) {
      var wanted = needed === 1 ? "one record with a timestamp"
        : (needed === 2 ? "two" : String(needed)) + " records with timestamps";
      return "needs at least " + wanted + ": " + datedOfTotalText(values);
    },
    dated_share: function (values) { return "needs timestamps on at least half of the records: " + datedOfTotalText(values); },
    sources_with_route: function () { return "needs a shown source with two or more records with timestamps"; },
    stays: function () { return "no stays in the analysis of the shown sources"; },
    gaps: function () { return "no gaps in the analysis of the shown sources"; },
    matrix_rows: function () { return "no matrix rows: the shown sources have no stays and there are no case places"; },
    encounters: function () { return "no encounters between the shown sources"; },
    shared_places: function () { return "no shared places among the shown sources"; },
    case_places: function () { return "no case places in this project"; },
    route_shown: function () { return "needs the Route layer switched on for a shown source"; }
  };
  var CRYSTAL_NEEDS = FEATURE_REQUIREMENTS.crystal.needs;
  function crystalReason(values) {
    return "needs " + CRYSTAL_NEEDS.dated_points + " records with timestamps on " + CRYSTAL_NEEDS.days_with_data + " days: " +
      values.dated_points + " on " + countText(values.days_with_data, "day", "days");
  }
  var CRYSTAL_NONE_REASON = "no shown source has " + CRYSTAL_NEEDS.dated_points + " records with timestamps on " + CRYSTAL_NEEDS.days_with_data + " days";
  function sourceFacts(source) {
    // Totals over every record of the source (dated_records, undated_total, data_days), not
    // only the rendered ones, so the browser's numbers match payload.capabilities. A payload
    // without these fields falls back to the rendered points, which undercount a thinned source.
    var days = {};
    if (source.data_days) {
      source.data_days.forEach(function (day) { days[day] = true; });
    } else {
      GEOSNAP.allPoints.forEach(function (point) { if (point.s === source.id) { days[point.local.slice(0, 10)] = true; } });
    }
    var dated = typeof source.dated_records === "number" ? source.dated_records : (source.points_total || 0);
    var undated = typeof source.undated_total === "number" ? source.undated_total : GEOSNAP.undatedPointsOf(source.id).length;
    return { dated: dated, undated: undated, days: days };
  }
  function capabilityValues(shownSources) {
    var analysis = GEOSNAP.payload.analysis || {};
    var perSource = analysis.per_source || {};
    var shown = {};
    var days = {};
    var values = { dated_points: 0, undated_points: 0, dated_share: 0, days_with_data: 0, sources: shownSources.length, sources_with_route: 0,
      encounters: 0, shared_places: 0, matrix_rows: 0, case_places: 0, stays: 0, gaps: 0, segments: 0, route_shown: 0 };
    shownSources.forEach(function (source) {
      shown[source.id] = true;
      var facts = sourceFacts(source);
      values.dated_points += facts.dated;
      values.undated_points += facts.undated;
      if (facts.dated >= 2) { values.sources_with_route += 1; }
      Object.keys(facts.days).forEach(function (day) { days[day] = true; });
      var sourceAnalysis = perSource[String(source.id)];
      if (sourceAnalysis) {
        values.stays += (sourceAnalysis.stays || []).length;
        values.gaps += (sourceAnalysis.gaps || []).length;
        values.segments += (sourceAnalysis.segments || []).length;
      }
    });
    values.days_with_data = Object.keys(days).length;
    var total = values.dated_points + values.undated_points;
    values.dated_share = total ? values.dated_points / total : 0;
    values.encounters = (analysis.encounters || []).filter(function (encounter) { return shown[encounter.source_a] && shown[encounter.source_b]; }).length;
    values.shared_places = (analysis.shared_places || []).filter(function (place) {
      return place.sources.filter(function (sourceId) { return shown[sourceId]; }).length >= 2;
    }).length;
    // Without a shown source the matrix has no column and lists nothing, case places included.
    values.matrix_rows = shownSources.length
      ? ((analysis.presence_matrix && analysis.presence_matrix.rows) || []).filter(function (row) {
        return row.kind === "case place" || (row.cells || []).some(function (cell) { return shown[cell.source_id] && cell.visit_count > 0; });
      }).length
      : 0;
    values.case_places = analysis.case_places ? (analysis.case_places.places || []).length : 0;
    values.route_shown = GEOSNAP.routeLayerShown() ? values.sources_with_route : 0;
    return values;
  }
  function verdictOf(feature, values) {
    var requirement = FEATURE_REQUIREMENTS[feature];
    var needs = requirement.needs;
    var unmet = Object.keys(needs).filter(function (name) { return !(values[name] >= needs[name]); });
    if (!unmet.length) { return { ok: true, reason: "" }; }
    return { ok: false, reason: requirement.perSource ? crystalReason(values) : CAPABILITY_REASONS[unmet[0]](values, needs[unmet[0]]) };
  }
  var capabilityListeners = [];
  GEOSNAP.capabilities = {
    // Over the shown sources, recomputed on every change; payload.capabilities holds the
    // same numbers over every source and is what the report and the briefing cite.
    values: null,
    // {ok, reason} for a feature; with sourceId the judgement of that one source.
    allows: function (feature, sourceId) {
      var requirement = FEATURE_REQUIREMENTS[feature];
      if (!requirement) { return { ok: true, reason: "" }; }
      if (sourceId !== undefined) {
        var source = GEOSNAP.visibleSources().filter(function (candidate) { return candidate.id === sourceId; })[0];
        return verdictOf(feature, capabilityValues(source ? [source] : []));
      }
      if (requirement.perSource) {
        var qualifies = GEOSNAP.visibleSources().some(function (candidate) { return verdictOf(feature, capabilityValues([candidate])).ok; });
        return qualifies ? { ok: true, reason: "" } : { ok: false, reason: CRYSTAL_NONE_REASON };
      }
      return verdictOf(feature, GEOSNAP.capabilities.values || capabilityValues(GEOSNAP.visibleSources()));
    },
    onChange: function (listener) { capabilityListeners.push(listener); },
    refresh: function () {
      GEOSNAP.capabilities.values = capabilityValues(GEOSNAP.visibleSources());
      capabilityListeners.forEach(function (listener) { listener(GEOSNAP.capabilities.values); });
    }
  };
  // ---- end of capabilities -----------------------------------------------------------------

  // Leaflet writes layer control names and string tooltips as HTML: source labels are escaped
  // there or handed over as elements.
  GEOSNAP.escapeHtml = function (text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  };
  // The design token --ink of map.css, read once: Leaflet and SVG attributes need the value.
  GEOSNAP.inkColour = (window.getComputedStyle(document.documentElement).getPropertyValue("--ink") || "").trim() || "#1c2733";
  // A longitude moved by whole turns next to a reference, so that shapes across the
  // antimeridian are drawn together instead of around the world.
  GEOSNAP.longitudeNear = function (longitude, reference) {
    return longitude + 360 * Math.round((reference - longitude) / 360);
  };
  // " on this map (1 of every N reports)" for a thinned source, "" otherwise: a statement about
  // reports that only sees the drawn ones must say so.
  GEOSNAP.thinnedMapNote = function (sourceId) {
    var source = sourceById[sourceId];
    return source && source.thinning_stride > 1 ? " on this map (1 of every " + source.thinning_stride + " reports)" : "";
  };
  GEOSNAP.anyThinnedSource = function () {
    return sources.some(function (source) { return source.thinning_stride > 1; });
  };
  // Creates an element; text is always set as textContent (null/undefined: none).
  GEOSNAP.element = function (tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = text; }
    if (className) { node.className = className; }
    return node;
  };
  GEOSNAP.textElement = function (text, className) {
    var node = document.createElement("span");
    node.textContent = text;
    if (className) { node.className = className; }
    return node;
  };

  // ---- base map: a built-in provider ("online"), the local tile set, or nothing ----
  function baseMapOptions(key) {
    if (key === "local" && payload.tiles_url) {
      return { url: payload.tiles_url, maxZoom: payload.tiles_max_zoom, attribution: payload.tiles_attribution, subdomains: [] };
    }
    var provider = providerByKey[key];
    if (!provider) { return null; }
    return { url: provider.url, maxZoom: provider.max_zoom, attribution: provider.attribution, subdomains: provider.subdomains };
  }

  GEOSNAP.setBaseMap = function (key) {
    var options = baseMapOptions(key);
    if (baseLayer) { map.removeLayer(baseLayer); baseLayer = null; }
    GEOSNAP.baseMapKey = options ? key : "none";
    GEOSNAP.usesTiles = !!options;
    document.getElementById("map").classList.toggle("no-basemap", !options);
    map.setMaxZoom(options ? options.maxZoom : 20);
    if (options) {
      baseLayer = L.tileLayer(options.url, {
        maxZoom: options.maxZoom,
        attribution: options.attribution,
        subdomains: options.subdomains,
        crossOrigin: "anonymous"
      }).addTo(map);
    }
    var select = document.getElementById("map-style");
    if (select.value !== GEOSNAP.baseMapKey) { select.value = GEOSNAP.baseMapKey; }
  };

  (function buildMapStyleSelect() {
    var select = document.getElementById("map-style");
    // title: the full name on hover where the menu shows the short one.
    function addOption(value, label, title) {
      var option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      if (title) { option.title = title; }
      select.appendChild(option);
    }
    (payload.tile_providers || []).forEach(function (provider) { addOption(provider.key, provider.short_label || provider.label, provider.label); });
    if (payload.tile_source === "local" && payload.tiles_url) { addOption("local", "Local tiles"); }
    addOption("none", "No base map");
    select.addEventListener("change", function () { GEOSNAP.setBaseMap(select.value); });
  })();
  GEOSNAP.setBaseMap(initialBaseMapKey());

  // options: group (one of LAYER_GROUPS), dependsOn (name of the layer this
  // one needs switched on), requires (feature of FEATURE_REQUIREMENTS), tip (control tooltip).
  GEOSNAP.registerOverlay = function (name, layer, visibleByDefault, options) {
    options = options || {};
    GEOSNAP.overlays.push({ name: name, layer: layer, visible: !!visibleByDefault, group: options.group || "Records",
      dependsOn: options.dependsOn || null, requires: options.requires || null, tip: options.tip || "", blocked: false, wanted: false, input: null, label: null });
    return layer;
  };

  GEOSNAP.onRender = function (listener) { GEOSNAP.renderListeners.push(listener); };

  // Overlays that are switched off are not rebuilt on every render (the time cursor renders
  // several times per second); they catch up when the layer control switches them on.
  GEOSNAP.deferredRender = function (layer, build) {
    var pending = null;
    function catchUp() {
      if (pending === null) { return; }
      build(pending.visible, pending.latLngs);
      pending = null;
    }
    GEOSNAP.onRender(function (visible, latLngs) {
      pending = { visible: visible, latLngs: latLngs };
      if (map.hasLayer(layer)) { catchUp(); }
    });
    map.on("overlayadd", function (event) { if (event.layer === layer) { catchUp(); } });
  };

  // Date + hour + minute inputs inside `container` (label from data-label). Values are
  // "YYYY-MM-DDTHH:MM" in the display zone; the control never holds an incomplete state.
  GEOSNAP.dateTimeControl = function (container) {
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    function select(count, tip) {
      var node = document.createElement("select");
      GEOSNAP.controlTip(node, tip);
      for (var i = 0; i < count; i++) {
        var option = document.createElement("option");
        option.value = pad(i);
        option.textContent = pad(i);
        node.appendChild(option);
      }
      return node;
    }
    var label = document.createElement("span");
    label.textContent = container.dataset.label;
    var date = document.createElement("input");
    date.type = "date";
    GEOSNAP.controlTip(date, "Date");
    var hour = select(24, "Hour (00-23)");
    var separator = document.createElement("span");
    separator.textContent = ":";
    var minute = select(60, "Minute");
    [label, date, hour, separator, minute].forEach(function (node) { container.appendChild(node); });
    var listeners = [];
    var control = {
      get: function () {
        return date.value ? date.value + "T" + hour.value + ":" + minute.value : null;
      },
      set: function (local) {
        date.value = local.slice(0, 10);
        hour.value = local.slice(11, 13);
        minute.value = local.slice(14, 16);
      },
      onChange: function (listener) { listeners.push(listener); }
    };
    [date, hour, minute].forEach(function (input) {
      input.addEventListener("change", function () {
        var local = control.get();
        if (local === null) { return; }
        listeners.forEach(function (listener) { listener(local); });
      });
    });
    return control;
  };

  // ---- control tooltips: one delayed box for every control ------------------------------
  // CONTROL_TOOLTIP_DELAY_MS is the one place to change the delay (MANUAL.md, "Control tooltips").
  // Every element with data-tip gets the box after the delay on hover and on keyboard focus; a
  // native title met on the way is moved into data-tip, so the delay is the same everywhere.
  // Leaflet's point tooltips (hover on markers inside the map pane) are not touched.
  var CONTROL_TOOLTIP_DELAY_MS = 600;
  GEOSNAP.CONTROL_TOOLTIP_DELAY_MS = CONTROL_TOOLTIP_DELAY_MS;
  var TIP_REASON_PREFIX = "Unavailable: ";
  var tipBox = GEOSNAP.element("div", null, "control-tip");
  tipBox.id = "control-tip";
  tipBox.setAttribute("role", "tooltip");
  tipBox.hidden = true;
  document.body.appendChild(tipBox);
  var tipHost = null;
  var tipTimer = null;
  GEOSNAP.controlTip = function (element, text) {
    element.setAttribute("data-tip", text);
    element.removeAttribute("title");
    return element;
  };
  function tipHostOf(target) {
    if (!target || !target.closest) { return null; }
    var host = target.closest("[data-tip], [title]");
    if (!host || host.tagName === "OPTION" || host.closest(".leaflet-map-pane")) { return null; }
    if (!host.hasAttribute("data-tip")) { GEOSNAP.controlTip(host, host.getAttribute("title")); }
    return host;
  }
  function controlTipText(host) {
    var text = host.getAttribute("data-tip") || "";
    var reason = host.getAttribute("data-tip-reason");
    return reason ? (text ? text + "\n" : "") + TIP_REASON_PREFIX + reason : text;
  }
  function hideControlTip() {
    if (tipTimer !== null) { clearTimeout(tipTimer); tipTimer = null; }
    if (tipHost) { tipHost.removeAttribute("aria-describedby"); }
    tipHost = null;
    tipBox.hidden = true;
  }
  function showControlTip(host) {
    var text = controlTipText(host);
    if (!text) { return; }
    tipBox.textContent = text;
    tipBox.hidden = false;
    host.setAttribute("aria-describedby", tipBox.id);
    var anchor = host.getBoundingClientRect();
    var box = tipBox.getBoundingClientRect();
    var left = Math.max(8, Math.min(anchor.left, window.innerWidth - box.width - 8));
    var top = anchor.bottom + 6;
    if (top + box.height > window.innerHeight - 8) { top = Math.max(8, anchor.top - box.height - 6); }
    tipBox.style.left = left + "px";
    tipBox.style.top = top + "px";
  }
  function scheduleControlTip(host) {
    if (host === tipHost) { return; }
    hideControlTip();
    tipHost = host;
    tipTimer = setTimeout(function () {
      tipTimer = null;
      if (tipHost === host) { showControlTip(host); }
    }, CONTROL_TOOLTIP_DELAY_MS);
  }
  document.addEventListener("mouseover", function (event) {
    var host = tipHostOf(event.target);
    if (host) { scheduleControlTip(host); } else if (tipHost && !tipHost.contains(event.target)) { hideControlTip(); }
  });
  document.addEventListener("mouseout", function (event) {
    if (tipHost && !tipHost.contains(event.relatedTarget)) { hideControlTip(); }
  });
  document.addEventListener("focusin", function (event) {
    var host = tipHostOf(event.target);
    if (host) { scheduleControlTip(host); }
  });
  document.addEventListener("focusout", function (event) {
    if (tipHost && tipHost.contains(event.target)) { hideControlTip(); }
  });
  document.addEventListener("mousedown", hideControlTip, true);
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") { hideControlTip(); } }, true);
  window.addEventListener("scroll", hideControlTip, true);

  // ---- gating: an unavailable control is disabled, greyed and explains why ----
  // The element keeps its place. A container (a label, the transport group, a date-time
  // control) gates every form control inside it; the reason joins the control tooltip of the
  // element or of the nearest data-tip ancestor.
  var gatedControls = [];
  function formControlsOf(element) {
    if (/^(INPUT|SELECT|BUTTON|TEXTAREA)$/.test(element.tagName)) { return [element]; }
    return Array.prototype.slice.call(element.querySelectorAll("input, select, button"));
  }
  function setTipReason(element, reason) {
    var host = element.closest("[data-tip]") || element;
    if (reason) { host.setAttribute("data-tip-reason", reason); } else { host.removeAttribute("data-tip-reason"); }
  }
  function applyGate(entry) {
    var verdict = GEOSNAP.capabilities.allows(entry.feature);
    formControlsOf(entry.element).forEach(function (control) { control.disabled = !verdict.ok; });
    entry.element.classList.toggle("gated", !verdict.ok);
    setTipReason(entry.element, verdict.ok ? "" : verdict.reason);
    var changed = entry.ok !== verdict.ok;
    entry.ok = verdict.ok;
    if (changed && entry.onChange) { entry.onChange(verdict.ok); }
  }
  // onChange(ok) is called when the availability flips, first at registration.
  GEOSNAP.gateControl = function (element, feature, onChange) {
    var entry = { element: element, feature: feature, onChange: onChange || null, ok: null };
    gatedControls.push(entry);
    applyGate(entry);
    return entry;
  };
  // A module that enables its own controls again (the live check when it finishes) asks the
  // gate afterwards, so a gated control is never left clickable with its reason still showing.
  GEOSNAP.refreshGate = function (element) {
    gatedControls.forEach(function (entry) { if (entry.element === element) { entry.ok = null; applyGate(entry); } });
  };
  GEOSNAP.capabilities.onChange(function () { gatedControls.forEach(applyGate); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-feature]"), function (element) {
    GEOSNAP.gateControl(element, element.getAttribute("data-feature"));
  });

  // ---- collapse: every floating window and map control folds to its title bar ----------
  // One chevron button per title bar; the state is a view choice and is not kept.
  var SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  function collapseButtonOf(windowElement) {
    return windowElement.querySelector(":scope > .window-title > .collapse-button, :scope > .control-title > .collapse-button");
  }
  GEOSNAP.setWindowCollapsed = function (windowElement, collapsed) {
    windowElement.classList.toggle("collapsed", !!collapsed);
    var button = collapseButtonOf(windowElement);
    if (!button) { return; }
    var label = collapsed ? "Expand" : "Collapse";
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    button.setAttribute("aria-label", label);
    GEOSNAP.controlTip(button, label);
    if (!collapsed && windowElement.classList.contains("moved")) { keepWindowInside(windowElement); }
    // Focus inside the folded content would be lost: it moves to the button.
    if (collapsed && windowElement.contains(document.activeElement) && !button.contains(document.activeElement)) { button.focus(); }
  };
  GEOSNAP.addCollapseButton = function (windowElement, titleElement) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "collapse-button";
    var icon = document.createElementNS(SVG_NAMESPACE, "svg");
    icon.setAttribute("viewBox", "0 0 16 16");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    var chevron = document.createElementNS(SVG_NAMESPACE, "path");
    chevron.setAttribute("d", "M3.5 10l4.5-4.5 4.5 4.5");
    icon.appendChild(chevron);
    button.appendChild(icon);
    // Before the close button, if any, so close stays in the corner.
    titleElement.insertBefore(button, titleElement.querySelector(".window-close"));
    button.addEventListener("click", function (event) {
      // The title bar itself may react to clicks (crystal ball): the button acts alone.
      event.stopPropagation();
      GEOSNAP.setWindowCollapsed(windowElement, !windowElement.classList.contains("collapsed"));
    });
    GEOSNAP.setWindowCollapsed(windowElement, windowElement.classList.contains("collapsed"));
    return button;
  };
  // ---- movable windows: drag a window by its title bar, kept inside the map area ----------
  // A press becomes a drag only after DRAG_START_DISTANCE_PX, so a plain click on the title
  // still reaches its own handlers; buttons and fields in the title never start a drag. The
  // position is a view choice for this page and is not kept.
  var DRAG_START_DISTANCE_PX = 4;
  var DRAG_IGNORED_TARGETS = "button, input, select, textarea, a, label";
  function bringWindowToFront(windowElement) {
    Array.prototype.forEach.call(document.querySelectorAll(".floating-window.front"), function (other) {
      if (other !== windowElement) { other.classList.remove("front"); }
    });
    windowElement.classList.add("front");
  }
  // Keeps a moved window completely inside its container (the map area).
  function keepWindowInside(windowElement) {
    if (!windowElement.classList.contains("moved") || windowElement.hidden) { return; }
    var area = windowElement.offsetParent;
    if (!area) { return; }
    var maximumLeft = Math.max(0, area.clientWidth - windowElement.offsetWidth);
    var maximumTop = Math.max(0, area.clientHeight - windowElement.offsetHeight);
    windowElement.style.left = Math.min(Math.max(0, windowElement.offsetLeft), maximumLeft) + "px";
    windowElement.style.top = Math.min(Math.max(0, windowElement.offsetTop), maximumTop) + "px";
  }
  GEOSNAP.keepWindowInside = keepWindowInside;
  function makeWindowMovable(windowElement, titleElement) {
    var press = null;
    titleElement.classList.add("window-handle");
    titleElement.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || event.target.closest(DRAG_IGNORED_TARGETS)) { return; }
      bringWindowToFront(windowElement);
      press = { x: event.clientX, y: event.clientY, left: 0, top: 0, dragging: false, pointer: event.pointerId };
      // Captured at once: a quick drag leaves the title bar before the first move arrives.
      titleElement.setPointerCapture(event.pointerId);
    });
    titleElement.addEventListener("pointermove", function (event) {
      if (press === null || event.pointerId !== press.pointer) { return; }
      var dx = event.clientX - press.x, dy = event.clientY - press.y;
      if (!press.dragging) {
        if (Math.abs(dx) + Math.abs(dy) < DRAG_START_DISTANCE_PX) { return; }
        press.dragging = true;
        // Where the window really is on screen: a centring transform does not show in
        // offsetLeft, so the start is taken from the rendered box.
        var box = windowElement.getBoundingClientRect();
        var area = windowElement.offsetParent.getBoundingClientRect();
        press.left = box.left - area.left - windowElement.offsetParent.clientLeft;
        press.top = box.top - area.top - windowElement.offsetParent.clientTop;
        // From here the window is placed by its top left corner, whatever its default anchor.
        windowElement.classList.add("moved", "dragging");
        windowElement.style.right = "auto";
        windowElement.style.bottom = "auto";
        windowElement.style.transform = "none";
      }
      windowElement.style.left = (press.left + dx) + "px";
      windowElement.style.top = (press.top + dy) + "px";
      keepWindowInside(windowElement);
      event.preventDefault();
    });
    function endPress(event) {
      if (press === null || event.pointerId !== press.pointer) { return; }
      if (press.dragging) {
        windowElement.classList.remove("dragging");
        // The click that ends a drag is not a click on the title; a drag that ends without a
        // click must not swallow the next real one.
        var swallow = function (clickEvent) { clickEvent.stopPropagation(); };
        titleElement.addEventListener("click", swallow, true);
        setTimeout(function () { titleElement.removeEventListener("click", swallow, true); }, 0);
      }
      press = null;
    }
    titleElement.addEventListener("pointerup", endPress);
    titleElement.addEventListener("pointercancel", endPress);
  }
  Array.prototype.forEach.call(document.querySelectorAll(".floating-window > .window-title"), function (titleElement) {
    GEOSNAP.addCollapseButton(titleElement.parentElement, titleElement);
    makeWindowMovable(titleElement.parentElement, titleElement);
  });
  window.addEventListener("resize", function () {
    Array.prototype.forEach.call(document.querySelectorAll(".floating-window.moved"), keepWindowInside);
  });

  // swatch: {colour, shape: "dot"|"line"|"dashed"|"text", text?, note?}
  GEOSNAP.addLegendEntry = function (label, swatch) {
    GEOSNAP.legendEntries.push({ label: label, swatch: swatch || {} });
  };

  GEOSNAP.formatDuration = function (minutes) {
    if (minutes === null || minutes === undefined) { return "-"; }
    if (minutes < 0) { return "-" + GEOSNAP.formatDuration(-minutes); }
    var total = Math.round(minutes);
    var days = Math.floor(total / 1440);
    var hours = Math.floor((total % 1440) / 60);
    var mins = total % 60;
    var parts = [];
    if (days) { parts.push(days + " d"); }
    if (hours || days) { parts.push(hours + " h"); }
    parts.push(mins + " min");
    return parts.join(" ");
  };

  GEOSNAP.formatDistance = function (metres) {
    if (metres === null || metres === undefined) { return "-"; }
    return metres >= 1000 ? (metres / 1000).toFixed(2) + " km" : Math.round(metres) + " m";
  };

  GEOSNAP.formatSpeed = function (kmh) {
    return kmh === null || kmh === undefined ? "-" : kmh.toFixed(1) + " km/h";
  };

  // Ring and fill in the source colour; "colour by time" fills from oldest (blue) to newest
  // (red) over the shown span (fraction 0..1, null without a span) and keeps the source ring.
  GEOSNAP.pointColour = function (point, fraction) {
    var colour = GEOSNAP.sourceColour(point.s);
    if (!GEOSNAP.colourByTime || fraction === null) { return { stroke: colour, fill: colour }; }
    return { stroke: colour, fill: "hsl(" + (220 - 220 * fraction) + ", 80%, 55%)" };
  };

  // rows: [[label, value]]; values are written as text, null/undefined as "-". A row with
  // the label alone ([heading]) is a group heading.
  GEOSNAP.rowsElement = function (rows) {
    var container = document.createElement("div");
    container.className = "geosnap-tooltip";
    rows.forEach(function (row) {
      var line = document.createElement("div");
      var label = document.createElement("span");
      var value = document.createElement("span");
      if (row.length === 1) {
        line.className = "group";
        label.className = "label group";
        label.textContent = row[0];
        value.textContent = "";
        line.appendChild(label);
        line.appendChild(value);
        container.appendChild(line);
        return;
      }
      label.className = "label";
      label.textContent = row[0] ? row[0] + ": " : "  ";
      if (row[1] && typeof row[1] === "object" && row[1].nodeType === 1) { value.appendChild(row[1]); }
      else { value.textContent = row[1] === null || row[1] === undefined ? "-" : String(row[1]); }
      line.appendChild(label);
      line.appendChild(value);
      container.appendChild(line);
    });
    return container;
  };

  // ---- row tooltips: readable, reachable, scrollable ------------------------------------
  // A row tooltip opens at the pointer and stays where it is, so the pointer can move onto it:
  // it closes TOOLTIP_LEAVE_GRACE_MS after the pointer left both the layer and the tooltip.
  // Long values wrap (map.css .geosnap-tooltip) and a tall tooltip scrolls with the wheel
  // without zooming the map. Only one row tooltip is open at a time.
  var TOOLTIP_LEAVE_GRACE_MS = 400;
  var TOOLTIP_EDGE_MARGIN_PX = 4;
  var heldTooltipLayer = null;
  var tooltipCloseTimer = null;
  function cancelTooltipClose() {
    if (tooltipCloseTimer !== null) { clearTimeout(tooltipCloseTimer); tooltipCloseTimer = null; }
  }
  function scheduleTooltipClose(layer) {
    cancelTooltipClose();
    tooltipCloseTimer = setTimeout(function () { tooltipCloseTimer = null; layer.closeTooltip(); }, TOOLTIP_LEAVE_GRACE_MS);
  }
  // A tooltip beside the pointer is centred on it vertically; near the top or bottom edge of
  // the map it is shifted inside, so no row is cut off.
  function keepTooltipInsideMap(tooltipElement) {
    tooltipElement.style.marginTop = "";
    var frame = map.getContainer().getBoundingClientRect();
    // Never taller than the map: a short map (timeline open, small window) scrolls sooner.
    var rows = tooltipElement.querySelector(".geosnap-tooltip");
    if (rows) {
      rows.style.maxHeight = "min(60vh, 420px, " + Math.max(60, Math.floor(frame.height - 2 * TOOLTIP_EDGE_MARGIN_PX - 12)) + "px)";
    }
    var box = tooltipElement.getBoundingClientRect();
    var shift = 0;
    if (box.bottom > frame.bottom - TOOLTIP_EDGE_MARGIN_PX) { shift = frame.bottom - TOOLTIP_EDGE_MARGIN_PX - box.bottom; }
    if (box.top + shift < frame.top + TOOLTIP_EDGE_MARGIN_PX) { shift = frame.top + TOOLTIP_EDGE_MARGIN_PX - box.top; }
    if (shift) { tooltipElement.style.marginTop = Math.round(shift) + "px"; }
  }
  L.Layer.include({
    bindRowsTooltip: function (build) {
      var layer = this;
      layer.bindTooltip(build, { sticky: true, className: "geosnap-rows-tip" });
      // Leaflet closes a tooltip on mouseout and moves a sticky one with the pointer
      // (Layer._initTooltipInteractions, Leaflet 1.9.4); both would keep the pointer from
      // ever reaching it.
      layer.off({ mouseout: layer.closeTooltip, mousemove: layer._moveTooltip }, layer);
      layer.on("mouseover", function () {
        cancelTooltipClose();
        if (heldTooltipLayer !== null && heldTooltipLayer !== layer) { heldTooltipLayer.closeTooltip(); }
      });
      layer.on("mouseout", function () { scheduleTooltipClose(layer); });
      layer.on("tooltipopen", function (event) {
        // Leaflet's own mouseover handler opens this tooltip before ours runs, so the one
        // still held from the previous layer is closed here.
        if (heldTooltipLayer !== null && heldTooltipLayer !== layer) { heldTooltipLayer.closeTooltip(); }
        heldTooltipLayer = layer;
        var tooltipElement = event.tooltip.getElement();
        if (!tooltipElement.getAttribute("data-held")) {
          tooltipElement.setAttribute("data-held", "true");
          L.DomEvent.disableScrollPropagation(tooltipElement);
          L.DomEvent.disableClickPropagation(tooltipElement);
          L.DomEvent.on(tooltipElement, "mouseenter", cancelTooltipClose);
          L.DomEvent.on(tooltipElement, "mouseleave", function () { scheduleTooltipClose(layer); });
        }
        keepTooltipInsideMap(tooltipElement);
      });
      layer.on("tooltipclose", function () { if (heldTooltipLayer === layer) { heldTooltipLayer = null; } });
      return layer;
    }
  });

  // ---- context actions: a click popup with "Check surroundings" ----
  // G.checkSurroundings is defined later (map_online.js); popups are built on click. A target
  // takes its own extent (accuracy, stop radius, ring) but at least the embedded query radius.
  var DEFAULT_CHECK_RADIUS = (payload.online && payload.online.overpass_radius_m) || 250;
  var embeddedPlaceTotal = 0;
  Object.keys(payload.places || {}).forEach(function (category) { embeddedPlaceTotal += payload.places[category].length; });
  GEOSNAP.embeddedPlaceTotal = embeddedPlaceTotal;
  GEOSNAP.surroundingsAvailable = embeddedPlaceTotal > 0 || !!(payload.online && payload.online.enabled && payload.online.overpass_endpoint);
  GEOSNAP.checkTarget = function (label, lat, lon, extentMetres) {
    return { label: label, lat: lat, lon: lon, radius: Math.max(DEFAULT_CHECK_RADIUS, Math.ceil(extentMetres || 0)) };
  };
  // The wording of the accuracy levels and the factor from a 68 % to a 95 % radius, as the
  // analysis states them; a map without them falls back to the same values.
  var analysisParameters = (payload.analysis && payload.analysis.parameters) || {};
  GEOSNAP.accuracyLevelTexts = analysisParameters.accuracy_level_texts || { "68": "68 %", "95": "95 %", "unknown": "unknown (treated as 68 %)" };
  GEOSNAP.accuracyScaleFactor = typeof analysisParameters.accuracy_scale_factor === "number"
    ? analysisParameters.accuracy_scale_factor : Math.sqrt(Math.log(0.05) / Math.log(0.32));
  // The factor from a source's reported radii to its uncertainty radii (asc, one per
  // source), 1 when its records carry none.
  var accuracyScaleBySource = null;
  GEOSNAP.accuracyScaleOf = function (sourceId) {
    if (accuracyScaleBySource === null) {
      accuracyScaleBySource = {};
      GEOSNAP.allPoints.forEach(function (point) {
        if (typeof point.asc === "number" && !(point.s in accuracyScaleBySource)) { accuracyScaleBySource[point.s] = point.asc; }
      });
    }
    return accuracyScaleBySource[sourceId] || 1;
  };
  // targets: [{label, lat, lon, radius, action?, profile?}]; action is the button text.
  GEOSNAP.contextActionsElement = function (title, targets) {
    var box = document.createElement("div");
    box.className = "context-actions";
    box.appendChild(GEOSNAP.textElement(title, "context-title"));
    targets.forEach(function (target) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "context-action";
      button.textContent = (target.action || "Check surroundings") + " · " + GEOSNAP.formatDistance(target.radius);
      button.addEventListener("click", function () {
        map.closePopup();
        GEOSNAP.checkSurroundings(target);
      });
      box.appendChild(button);
    });
    return box;
  };
  // describe(): {title, targets}; nothing is bound when neither embedded places nor a live
  // service exist, so no button promises an answer that cannot come.
  GEOSNAP.bindContextActions = function (layer, describe) {
    if (!GEOSNAP.surroundingsAvailable) { return layer; }
    return layer.bindPopup(function () {
      var description = describe();
      return GEOSNAP.contextActionsElement(description.title, description.targets);
    }, { className: "context-popup", maxWidth: 320 });
  };

  // The original record is cut only beyond this length, and then says so with its full length.
  var TOOLTIP_RECORD_LENGTH = 1000;
  function originalRecordText(text) {
    return text.length > TOOLTIP_RECORD_LENGTH
      ? text.slice(0, TOOLTIP_RECORD_LENGTH) + " … (" + text.length + " characters in total)"
      : text;
  }
  var NOTE_PART_SEPARATOR = " · ";
  var TOOL_NOTE_MARK = "[GEOSnap] ";
  var NOTE_LABEL_MAX_CHARS = 40;
  var RECORD_ROWS_HEADING = "From the record";
  // Labels of the rows GEOSnap writes itself; a record field of the same name (any case) is
  // shown as prose under the record heading, never as a labelled row.
  var FIXED_TOOLTIP_LABELS = ["geosnap", "source", "name", "description", "original", "latitude", "longitude",
    "accuracy", "utc", "local", "note", "reports at this position", "first report (utc)", "last report (utc)",
    "saved place", "coordinates", RECORD_ROWS_HEADING.toLowerCase()];
  // The parts of a record's description (joined by " · ") as tooltip rows: {tool, record}.
  // tool: GEOSnap's own remarks ("[GEOSnap] …") as rows labelled "GEOSnap". record: a
  // "name: value" part (CSV column without a role, KML ExtendedData field, GeoJSON property,
  // GPX hdop) as a label/value row when its label has 1..NOTE_LABEL_MAX_CHARS characters, the
  // rest holds no further ": " and the label is none of the fixed labels or reservedLabels
  // (the record noun); everything else is prose, labelled "Description" on its first row.
  // Text only, never markup.
  GEOSNAP.noteRows = function (note, reservedLabels) {
    var rows = { tool: [], record: [] };
    var reserved = FIXED_TOOLTIP_LABELS.concat((reservedLabels || []).map(function (label) { return String(label).toLowerCase(); }));
    var proseSeen = false;
    note.split(NOTE_PART_SEPARATOR).forEach(function (part) {
      if (part.indexOf(TOOL_NOTE_MARK) === 0) { rows.tool.push(["GEOSnap", part.slice(TOOL_NOTE_MARK.length)]); return; }
      var split = part.indexOf(": ");
      var label = split > 0 ? part.slice(0, split) : "";
      if (label && split <= NOTE_LABEL_MAX_CHARS && part.indexOf(": ", split + 2) === -1 && reserved.indexOf(label.toLowerCase()) === -1) {
        rows.record.push([label, part.slice(split + 2)]);
        return;
      }
      rows.record.push([proseSeen ? "" : "Description", part]);
      proseSeen = true;
    });
    return rows;
  };
  // The fixed rows of a tooltip, then the heading and the rows that come from the record.
  GEOSNAP.withRecordRows = function (fixedRows, recordRows) {
    return recordRows.length ? fixedRows.concat([[RECORD_ROWS_HEADING]], recordRows) : fixedRows;
  };
  // A record within the accuracy limit whose positioning method the run left out of the
  // analysis (analysis.excluded_positioning_methods); it stays on the map.
  GEOSNAP.leftOutByMethod = function (point) {
    var parameters = (payload.analysis && payload.analysis.parameters) || {};
    var methods = parameters.excluded_positioning_methods || [];
    if (typeof point.pm !== "string" || methods.indexOf(point.pm) < 0) { return false; }
    return !(typeof parameters.max_accuracy_m === "number" && point.radius > parameters.max_accuracy_m);
  };
  GEOSNAP.tooltipElement = function (point) {
    var source = sourceById[point.s];
    var record = originalRecordText(point.line);
    var rows = [["Source", GEOSNAP.sourceLabel(point.s)]];
    if (point.lbl) { rows.push(["Name", point.lbl]); }
    var noteRows = point.note ? GEOSNAP.noteRows(point.note, [GEOSNAP.recordNounTitle(point.s)]) : { tool: [], record: [] };
    rows = rows.concat(noteRows.tool);
    rows.push(["Original", record]);
    if (point.acc_known === false) {
      rows.push(["Latitude", String(point.lat)], ["Longitude", String(point.lon)], ["Accuracy", "not reported"]);
    } else {
      rows.push(["Latitude", point.lat + " ± " + point.lat_acc + " m"], ["Longitude", point.lon + " ± " + point.lon_acc + " m"]);
      // The reported radius and, when the analysis scales it, the radius at 95 %.
      if (typeof point.asc === "number" && point.asc !== 1) { rows.push(["Accuracy", "± " + point.radius + " m (95 %: " + Math.round(point.radius * point.asc) + " m)"]); }
    }
    if (point.pm) { rows.push(["Positioning", point.pm]); }
    rows.push(
      ["UTC", point.utc],
      ["Local", GEOSNAP.localText(point.local, point.off) + " " + point.zone],
      [GEOSNAP.recordNounTitle(point.s), String(point.n)],
      ["Reports at this position", String(point.dup)]
    );
    if (point.dup > 1) {
      rows.push(["First report (UTC)", point.first]);
      rows.push(["Last report (UTC)", point.last]);
    }
    // Speed from the previous record with its interval, when the payload
    // carries the figures.
    if (GEOSNAP.speedRows) { rows = rows.concat(GEOSNAP.speedRows(point)); }
    if (payload.analysis && payload.analysis.parameters && point.radius > payload.analysis.parameters.max_accuracy_m) {
      rows.push(["Note", "excluded from analysis (accuracy)"]);
    } else if (GEOSNAP.leftOutByMethod(point)) {
      rows.push(["Note", "left out of speed, stays and encounters: " + point.pm]);
    }
    return GEOSNAP.rowsElement(GEOSNAP.withRecordRows(rows, noteRows.record));
  };

  // A record without a timestamp: the same rows minus every time row, and
  // the row that says why they are missing. It carries no sequence number: those count the
  // dated records of a source.
  GEOSNAP.undatedTooltipElement = function (record) {
    var text = record.line || "";
    var original = originalRecordText(text);
    var rows = [["Source", GEOSNAP.sourceLabel(record.s)]];
    if (record.lbl) { rows.push(["Name", record.lbl]); }
    var noteRows = record.note ? GEOSNAP.noteRows(record.note, [GEOSNAP.recordNounTitle(record.s)]) : { tool: [], record: [] };
    rows = rows.concat(noteRows.tool);
    rows.push(["Original", original]);
    if (record.acc_known === false) {
      rows.push(["Latitude", String(record.lat)], ["Longitude", String(record.lon)], ["Accuracy", "not reported"]);
    } else {
      rows.push(["Latitude", record.lat + " ± " + record.acc + " m"], ["Longitude", record.lon + " ± " + record.acc + " m"]);
    }
    rows.push(["Timestamp", "none in the record"], [GEOSNAP.recordNounTitle(record.s), String(record.n)]);
    return GEOSNAP.rowsElement(GEOSNAP.withRecordRows(rows, noteRows.record));
  };
  // The undated records the Points layer draws: those of the shown sources that pass the
  // accuracy filter. The time filter and the time cursor cannot apply to a record without
  // a time, so they never hide one.
  GEOSNAP.filteredUndatedPoints = function () {
    var maxAccuracy = GEOSNAP.filters.maxAccuracy;
    var records = [];
    GEOSNAP.visibleSources().forEach(function (source) {
      GEOSNAP.undatedPointsOf(source.id).forEach(function (record) {
        if (maxAccuracy !== null && record.acc_known !== false && record.acc > maxAccuracy) { return; }
        records.push(record);
      });
    });
    return records;
  };

  GEOSNAP.filteredPoints = function () {
    var filters = GEOSNAP.filters;
    return GEOSNAP.allPoints.filter(function (point) {
      if (!sourceVisible[point.s]) { return false; }
      if (point.utcSeconds < filters.fromSeconds || point.utcSeconds > filters.toSeconds) { return false; }
      if (filters.maxAccuracy !== null && point.radius > filters.maxAccuracy) { return false; }
      if (filters.cursorSeconds !== null && point.utcSeconds > filters.cursorSeconds) { return false; }
      return true;
    });
  };

  function convexHull(latLngs) {
    var xy = latLngs.map(function (pair) { return [pair[1], pair[0]]; });
    xy.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    if (xy.length < 3) { return []; }
    function cross(o, a, b) {
      return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    }
    var lower = [];
    xy.forEach(function (p) {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) { lower.pop(); }
      lower.push(p);
    });
    var upper = [];
    for (var i = xy.length - 1; i >= 0; i--) {
      var q = xy[i];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) { upper.pop(); }
      upper.push(q);
    }
    lower.pop();
    upper.pop();
    return lower.concat(upper).map(function (p) { return [p[1], p[0]]; });
  }

  GEOSNAP.render = function () {
    var visible = GEOSNAP.filteredPoints();
    GEOSNAP.lastVisible = visible;
    GEOSNAP.lastVisibleBySource = GEOSNAP.groupBySource(visible);
    pointLayer.clearLayers();
    circleLayer.clearLayers();
    routeLayer.clearLayers();
    hullLayer.clearLayers();
    var latLngs = [];
    var firstSeconds = visible.length ? visible[0].utcSeconds : 0;
    var spanSeconds = GEOSNAP.colourByTime && visible.length > 1 ? visible[visible.length - 1].utcSeconds - firstSeconds : 0;
    var indexInSource = {};
    visible.forEach(function (point) {
      var position = [point.lat, point.lon];
      var fraction = spanSeconds > 0 ? (point.utcSeconds - firstSeconds) / spanSeconds : null;
      var colours = GEOSNAP.pointColour(point, fraction);
      // The index within the point's own source: neighbours never cross sources.
      var index = indexInSource[point.s] = indexInSource[point.s] === undefined ? 0 : indexInSource[point.s] + 1;
      latLngs.push(position);
      var marker = L.circleMarker(position, { radius: 5, color: colours.stroke, fillColor: colours.fill, fillOpacity: 0.85, weight: GEOSNAP.colourByTime ? 2 : 1, bubblingMouseEvents: false })
        .bindRowsTooltip(function () { return GEOSNAP.tooltipElement(point); })
        .addTo(pointLayer);
      marker.on("click", function () { map.fire("geosnap:pointclick", { point: point, index: index }); });
    });
    // Records without a timestamp: a hollow ring in the source colour, no time fill.
    var undated = GEOSNAP.filteredUndatedPoints();
    GEOSNAP.lastVisibleUndated = undated;
    undated.forEach(function (record) {
      var ring = L.circleMarker([record.lat, record.lon], { radius: 6, color: GEOSNAP.sourceColour(record.s), fillOpacity: 0, weight: 2, bubblingMouseEvents: false })
        .bindRowsTooltip(function () { return GEOSNAP.undatedTooltipElement(record); })
        .addTo(pointLayer);
      ring.on("click", function () { map.fire("geosnap:undatedclick", { record: record }); });
    });
    // The positions the map must have in view: the records without a timestamp are drawn,
    // so they belong in the bounds even when no dated record is shown.
    GEOSNAP.lastVisiblePositions = latLngs.concat(undated.map(function (record) { return [record.lat, record.lon]; }));
    var status = visible.length + " of " + shownTotalText() + " shown";
    if (undated.length) {
      status += " \u00b7 " + undated.length + (undated.length === 1 ? " record" : " records") + " without timestamp";
    }
    if (GEOSNAP.filters.fromSeconds > GEOSNAP.filters.toSeconds) { status = "From is after To: nothing to show"; }
    document.getElementById("status").textContent = status;
    // The listeners (heatmap, convex hull, route, arrows, classes) get the dated points
    // alone: every one of them reads the time order, which an undated record has not.
    GEOSNAP.renderListeners.forEach(function (listener) { listener(visible, latLngs); });
    return latLngs;
  };

  // One shared popup for point clicks instead of a popup object per point (renders run often).
  map.on("geosnap:pointclick", function (event) {
    if (!GEOSNAP.surroundingsAvailable || GEOSNAP.speedToolActive) { return; }
    var point = event.point;
    var label = "Point · " + GEOSNAP.sourceName(point.s) + " · " + GEOSNAP.recordNoun(point.s) + " " + point.n;
    var target = GEOSNAP.checkTarget(label, point.lat, point.lon, point.acc_known === false ? 0 : point.radius * (typeof point.asc === "number" ? point.asc : 1));
    L.popup({ className: "context-popup", maxWidth: 320 })
      .setLatLng([point.lat, point.lon])
      .setContent(GEOSNAP.contextActionsElement(label + " · " + GEOSNAP.localText(point.local, point.off), [target]))
      .openOn(map);
  });

  map.on("geosnap:undatedclick", function (event) {
    if (!GEOSNAP.surroundingsAvailable || GEOSNAP.speedToolActive) { return; }
    var record = event.record;
    var label = "Record without timestamp · " + GEOSNAP.sourceName(record.s) + " · " + GEOSNAP.recordNoun(record.s) + " " + record.n;
    var target = GEOSNAP.checkTarget(label, record.lat, record.lon, record.acc_known === false ? 0 : record.acc);
    L.popup({ className: "context-popup", maxWidth: 320 })
      .setLatLng([record.lat, record.lon])
      .setContent(GEOSNAP.contextActionsElement(label, [target]))
      .openOn(map);
  });

  GEOSNAP.deferredRender(circleLayer, function (visible) {
    visible.forEach(function (point) {
      if (point.acc_known === false) { return; }
      L.circle([point.lat, point.lon], { radius: point.radius, color: "#e07b00", weight: 1, fillOpacity: 0.12 })
        .bindRowsTooltip(function () { return GEOSNAP.tooltipElement(point); })
        .addTo(circleLayer);
    });
    GEOSNAP.lastVisibleUndated.forEach(function (record) {
      if (record.acc_known === false) { return; }
      L.circle([record.lat, record.lon], { radius: record.acc, color: "#e07b00", weight: 1, fillOpacity: 0.12 })
        .bindRowsTooltip(function () { return GEOSNAP.undatedTooltipElement(record); })
        .addTo(circleLayer);
    });
  });
  function trackOf(points) { return points.map(function (point) { return [point.lat, point.lon]; }); }
  // One route per source: devices of different people never form one track.
  GEOSNAP.deferredRender(routeLayer, function (visible) {
    var groups = GEOSNAP.groupBySource(visible);
    GEOSNAP.visibleSources().forEach(function (source) {
      var track = trackOf(groups[source.id] || []);
      if (track.length < 2) { return; }
      var colour = GEOSNAP.sourceColour(source.id);
      var name = GEOSNAP.sourceLabel(source.id);
      L.polyline(track, { color: colour, weight: 3, opacity: 0.8 }).bindTooltip(GEOSNAP.textElement("Route · " + name), { sticky: true }).addTo(routeLayer);
      L.circleMarker(track[0], { radius: 7, color: colour, weight: 3, fillColor: "#0a8a0a", fillOpacity: 1 })
        .bindTooltip(GEOSNAP.textElement("Start · " + name)).addTo(routeLayer);
      L.circleMarker(track[track.length - 1], { radius: 7, color: colour, weight: 3, fillColor: "#c01818", fillOpacity: 1 })
        .bindTooltip(GEOSNAP.textElement("End · " + name)).addTo(routeLayer);
    });
  });
  GEOSNAP.deferredRender(heatLayer, function (visible, latLngs) {
    heatLayer.setLatLngs(latLngs.map(function (pair) { return [pair[0], pair[1], 1]; }));
  });
  GEOSNAP.deferredRender(hullLayer, function (visible) {
    var groups = GEOSNAP.groupBySource(visible);
    GEOSNAP.visibleSources().forEach(function (source) {
      var hull = convexHull(trackOf(groups[source.id] || []));
      if (hull.length >= 3) {
        L.polygon(hull, { color: GEOSNAP.sourceColour(source.id), weight: 2, fillOpacity: 0.08 }).addTo(hullLayer);
      }
    });
  });

  // One line naming every filter in force, for the print header and the export metadata.
  GEOSNAP.filterDescription = function () {
    var filters = GEOSNAP.filters;
    var parts = ["shown " + GEOSNAP.lastVisible.length + " of " + shownTotalText()];
    if (filters.from !== GEOSNAP.dataRange.from || filters.to !== GEOSNAP.dataRange.to) {
      parts.push("time " + filters.from.replace("T", " ") + " to " + filters.to.replace("T", " "));
    }
    if (filters.maxAccuracy !== null) {
      // A record without an accuracy value cannot be judged by the slider and stays shown.
      var keptWithout = GEOSNAP.lastVisible.some(function (point) { return point.acc_known === false; }) ||
        (GEOSNAP.lastVisibleUndated || []).some(function (record) { return record.acc_known === false; });
      parts.push("accuracy up to " + filters.maxAccuracy + " m" + (keptWithout ? " (records without accuracy kept)" : ""));
    }
    if (filters.cursorSeconds !== null) { parts.push("time cursor at " + GEOSNAP.localTextAt(filters.cursorSeconds)); }
    var shownSources = GEOSNAP.visibleSources();
    if (shownSources.length < sources.length) {
      parts.push("sources: " + (shownSources.length ? shownSources.map(function (source) { return source.label; }).join(", ") : "none"));
    }
    var crystal = GEOSNAP.crystalBallDescription ? GEOSNAP.crystalBallDescription() : null;
    if (crystal) { parts.push(crystal); }
    return parts.join(" · ");
  };

  function graticuleStep(zoom) {
    if (zoom >= 15) { return 0.01; }
    if (zoom >= 12) { return 0.05; }
    if (zoom >= 10) { return 0.1; }
    if (zoom >= 8) { return 0.5; }
    if (zoom >= 6) { return 1; }
    if (zoom >= 4) { return 5; }
    return 10;
  }

  function drawGraticule() {
    graticuleLayer.clearLayers();
    if (!map.hasLayer(graticuleLayer)) { return; }
    var bounds = map.getBounds();
    var step = graticuleStep(map.getZoom());
    var south = Math.floor(bounds.getSouth() / step) * step;
    var north = Math.ceil(bounds.getNorth() / step) * step;
    var west = Math.floor(bounds.getWest() / step) * step;
    var east = Math.ceil(bounds.getEast() / step) * step;
    var style = { color: "#999999", weight: 1, opacity: 0.6, interactive: false };
    var drawn = 0;
    for (var lat = south; lat <= north && drawn < 200; lat += step, drawn++) {
      L.polyline([[lat, west], [lat, east]], style)
        .bindTooltip(lat.toFixed(4), { permanent: true, direction: "right", className: "graticule-label" })
        .addTo(graticuleLayer);
    }
    for (var lon = west; lon <= east && drawn < 400; lon += step, drawn++) {
      L.polyline([[south, lon], [north, lon]], style)
        .bindTooltip(lon.toFixed(4), { permanent: true, direction: "top", className: "graticule-label" })
        .addTo(graticuleLayer);
    }
  }

  function buildLegend() {
    var LegendControl = L.Control.extend({
      onAdd: function () {
        var legendContainer = L.DomUtil.create("div", "geosnap-legend");
        var container = legendContainer;
        L.DomEvent.disableClickPropagation(container);
        var titleRow = GEOSNAP.element("div", null, "control-title");
        titleRow.appendChild(GEOSNAP.element("strong", "Legend"));
        container.appendChild(titleRow);
        GEOSNAP.addCollapseButton(legendContainer, titleRow);
        GEOSNAP.legendEntries.forEach(function (entry) {
          var row = document.createElement("div");
          row.className = "entry";
          var swatch = document.createElement("span");
          swatch.className = "swatch" + (entry.swatch.shape === "line" ? " line" : entry.swatch.shape === "dashed" ? " dashed" : "");
          if (entry.swatch.shape === "source") {
            // Colour follows the Sources window (G.setSourceColour updates it in place).
            swatch.style.background = GEOSNAP.sourceColour(entry.swatch.sourceId);
            swatch.setAttribute("data-source-swatch", String(Number(entry.swatch.sourceId)));
            row.appendChild(swatch);
            swatch = GEOSNAP.textElement(entry.swatch.text || "", "legend-icon");
          } else if (entry.swatch.shape === "text") {
            swatch.className = "";
            swatch.textContent = entry.swatch.text || "";
            swatch.style.fontWeight = "700";
          } else if (entry.swatch.shape === "dashed") {
            swatch.style.borderTopColor = entry.swatch.colour;
          } else if (entry.swatch.shape === "ring") {
            // A record without a timestamp: the marker is a ring, so the swatch is one too.
            swatch.style.background = "transparent";
            swatch.style.border = "2px solid " + entry.swatch.colour;
          } else {
            swatch.style.background = entry.swatch.colour;
          }
          var label = document.createElement("span");
          label.textContent = entry.label;
          row.appendChild(swatch);
          row.appendChild(label);
          if (entry.swatch.note) {
            var note = document.createElement("span");
            note.className = "note";
            note.textContent = " " + entry.swatch.note;
            row.appendChild(note);
          }
          container.appendChild(row);
        });
        return container;
      }
    });
    var legend = new LegendControl({ position: "bottomleft" }).addTo(map);
    return legend;
  }

  sources.forEach(function (source) {
    GEOSNAP.addLegendEntry(source.label, { shape: "source", sourceId: source.id, text: source.icon, note: GEOSNAP.accuracyReportingNote(source) });
  });
  GEOSNAP.addLegendEntry("Points in the source colour", { colour: "#9e9e9e", note: "colour by time: fill blue = oldest, red = newest, ring = source" });
  if (GEOSNAP.undatedTotal() > 0) {
    GEOSNAP.addLegendEntry("Record without timestamp (hollow ring)", { colour: "#9e9e9e", shape: "ring", note: "in the Points layer, no sequence number, left out of time-based analysis" });
  }
  GEOSNAP.addLegendEntry("Accuracy circle (± metres; none where not reported)", { colour: "rgba(224,123,0,0.3)" });
  GEOSNAP.addLegendEntry("Route per source (chronological, source colour)", { colour: "#5b6875", shape: "line" });
  GEOSNAP.addLegendEntry("Start of shown range", { colour: "#0a8a0a" });
  GEOSNAP.addLegendEntry("End of shown range", { colour: "#c01818" });
  GEOSNAP.addLegendEntry("Convex hull per source", { colour: "rgba(122,31,162,0.4)" });
  GEOSNAP.addLegendEntry("Base map style", { shape: "text", text: "▦", note: "view choice, not recorded" });

  GEOSNAP.registerOverlay("Points", pointLayer, true, { group: "Records", tip: "Every shown record as a dot; without a timestamp as a hollow ring" });
  GEOSNAP.registerOverlay("Accuracy circles", circleLayer, false, { group: "Records", tip: "One circle per record with its reported accuracy" });
  GEOSNAP.registerOverlay("Route", routeLayer, false, { group: "Movement", requires: "route", tip: "Records of each source joined in time order" });
  GEOSNAP.registerOverlay("Heatmap", heatLayer, false, { group: "Records", tip: "Density of the shown records with a timestamp (visual aid)" });
  GEOSNAP.registerOverlay("Convex hull", hullLayer, false, { group: "Records", tip: "Smallest convex area around each source's records with a timestamp" });
  GEOSNAP.registerOverlay("Graticule", graticuleLayer, true, { group: "Map", tip: "Latitude and longitude grid with labels" });

  document.getElementById("project-name").textContent = payload.project;
  function baseMapDescription() {
    if (payload.tile_source === "online") {
      var provider = providerByKey[payload.tile_provider];
      return "online base map: " + (provider ? provider.label : payload.tile_provider) +
        " (tile requests reveal the viewed area to the provider; the style is a view choice, not recorded)";
    }
    return payload.tile_source === "local" ? "local tiles" : "no base map (offline)";
  }
  var metaParts = [
    "generated " + payload.generated_at,
    "times shown in " + payload.display_zone,
    baseMapDescription()
  ];
  if (sources.length > 1) { metaParts.push(sources.length + " sources"); }
  if (payload.thinning_stride > 1) {
    metaParts.push("rendered " + payload.rendered_points + " of " + payload.total_points + " points (stride " + payload.thinning_stride + ")");
  }
  document.getElementById("meta").textContent = metaParts.join(" · ");

  var cursorBox = document.getElementById("cursor-position");
  map.on("mousemove", function (event) {
    cursorBox.textContent = event.latlng.lat.toFixed(5) + ", " + event.latlng.lng.toFixed(5);
  });
  map.on("moveend zoomend overlayadd overlayremove", drawGraticule);

  // ---- time filter: prefilled with the data span, applied on every change ----
  var points = GEOSNAP.allPoints;
  GEOSNAP.dataRange = {
    from: points.length ? points[0].local.slice(0, 16) + ":00" : "0001-01-01T00:00:00",
    to: points.length ? points[points.length - 1].local.slice(0, 16) + ":59" : "9999-12-31T23:59:59"
  };
  // From takes the earlier occurrence of a repeated wall time and To the later one, so the
  // range covers both; a wall time in a skipped hour means the next valid instant.
  var TO_BOUND_LAST_SECOND_FRACTION = 0.999999;
  function setTimeRange(from, to) {
    GEOSNAP.filters.from = from;
    GEOSNAP.filters.to = to;
    GEOSNAP.filters.fromSeconds = GEOSNAP.localToUtcSeconds(from, false);
    // To includes its whole last second: records keep microseconds, so a record at
    // hh:mm:59.6 lies inside a range ending at hh:mm.
    GEOSNAP.filters.toSeconds = GEOSNAP.localToUtcSeconds(to, true) + TO_BOUND_LAST_SECOND_FRACTION;
  }
  setTimeRange(GEOSNAP.dataRange.from, GEOSNAP.dataRange.to);
  var filterFrom = GEOSNAP.dateTimeControl(document.getElementById("filter-from"));
  var filterTo = GEOSNAP.dateTimeControl(document.getElementById("filter-to"));
  GEOSNAP.timeFilterListeners = [];
  GEOSNAP.onTimeFilterChange = function (listener) { GEOSNAP.timeFilterListeners.push(listener); };
  function applyTimeFilter() {
    var fromLocal = filterFrom.get();
    var toLocal = filterTo.get();
    // A cleared date field leaves the previous filter in force until it is complete again.
    if (fromLocal === null || toLocal === null) { return; }
    setTimeRange(fromLocal + ":00", toLocal + ":59");
    GEOSNAP.timeFilterListeners.forEach(function (listener) { listener(GEOSNAP.filters.from, GEOSNAP.filters.to); });
    GEOSNAP.render();
  }
  GEOSNAP.resetTimeFilter = function () {
    filterFrom.set(GEOSNAP.dataRange.from);
    filterTo.set(GEOSNAP.dataRange.to);
    GEOSNAP.filters.cursorSeconds = null;
    applyTimeFilter();
  };
  filterFrom.set(GEOSNAP.dataRange.from);
  filterTo.set(GEOSNAP.dataRange.to);
  filterFrom.onChange(applyTimeFilter);
  filterTo.onChange(applyTimeFilter);
  document.getElementById("filter-reset").addEventListener("click", GEOSNAP.resetTimeFilter);
  document.getElementById("colour-by-time").addEventListener("change", function (event) {
    GEOSNAP.colourByTime = event.target.checked;
    GEOSNAP.render();
  });

  GEOSNAP.setControlVisible = function (control, visible) {
    if (!control) { return; }
    control.getContainer().style.display = visible ? "" : "none";
  };

  function bindViewToggle(checkboxId, controlName) {
    var checkbox = document.getElementById(checkboxId);
    function apply() { GEOSNAP.setControlVisible(GEOSNAP[controlName], checkbox.checked); }
    checkbox.addEventListener("change", apply);
    apply();
  }

  // ---- layer control: groups, dependencies and gating ----------------------------------
  // Leaflet's L.control.layers cannot group; this control keeps its look and class names.
  // A dependent layer can only be on while its parent is; a layer whose feature is unavailable
  // is off. In both cases the wish is remembered and restored when the block lifts.
  var LAYER_GROUPS = ["Records", "Movement", "Between sources", "Case", "Map"];
  var LAYER_ORDER = {
    "Records": ["Points", "Accuracy circles", "Sequence numbers", "Heatmap", "Convex hull"],
    "Movement": ["Route", "Direction arrows", "Movement classes", "Stays", "Gaps", "Last known position"],
    "Between sources": ["Encounters", "Shared places"],
    "Case": ["Case places"],
    "Map": ["Graticule"]
  };
  var overlayByLayerId = {};
  function overlayNamed(name) {
    return GEOSNAP.overlays.filter(function (entry) { return entry.name === name; })[0] || null;
  }
  function overlaysOfGroup(groupName) {
    var order = LAYER_ORDER[groupName] || [];
    function rank(entry) {
      var index = order.indexOf(entry.name);
      return index < 0 ? order.length : index;
    }
    return GEOSNAP.overlays.filter(function (entry) { return entry.group === groupName; })
      .map(function (entry, index) { return { entry: entry, index: index }; })
      .sort(function (a, b) { return rank(a.entry) - rank(b.entry) || a.index - b.index; })
      .map(function (item) { return item.entry; });
  }
  function setOverlayShown(entry, shown) {
    if (shown && !map.hasLayer(entry.layer)) {
      map.addLayer(entry.layer);
      map.fire("overlayadd", { layer: entry.layer, name: entry.name });
    } else if (!shown && map.hasLayer(entry.layer)) {
      map.removeLayer(entry.layer);
      map.fire("overlayremove", { layer: entry.layer, name: entry.name });
    }
  }
  // Why an overlay cannot be on right now: its feature is unavailable or its parent is off.
  function overlayBlockReason(entry) {
    if (entry.requires) {
      var verdict = GEOSNAP.capabilities.allows(entry.requires);
      if (!verdict.ok) { return verdict.reason; }
    }
    if (entry.dependsOn) {
      var parent = overlayNamed(entry.dependsOn);
      if (!parent || !map.hasLayer(parent.layer)) { return "needs the " + entry.dependsOn + " layer switched on"; }
    }
    return null;
  }
  function syncOverlayRow(entry) {
    var reason = overlayBlockReason(entry);
    if (reason) {
      if (!entry.blocked) { entry.wanted = map.hasLayer(entry.layer); entry.blocked = true; }
      setOverlayShown(entry, false);
    } else if (entry.blocked) {
      entry.blocked = false;
      setOverlayShown(entry, entry.wanted);
    }
    if (!entry.input) { return; }
    entry.input.checked = map.hasLayer(entry.layer);
    entry.input.disabled = !!reason;
    entry.label.classList.toggle("gated", !!reason);
    if (reason) { entry.label.setAttribute("data-tip-reason", reason); } else { entry.label.removeAttribute("data-tip-reason"); }
  }
  // Parents first, so a dependent sees its parent's final state.
  function syncOverlays() {
    GEOSNAP.overlays.forEach(function (entry) { if (!entry.dependsOn) { syncOverlayRow(entry); } });
    GEOSNAP.overlays.forEach(function (entry) { if (entry.dependsOn) { syncOverlayRow(entry); } });
  }
  function overlayRow(entry) {
    var label = GEOSNAP.element("label", null, "layer-row" + (entry.dependsOn ? " layer-dependent" : ""));
    var input = document.createElement("input");
    input.type = "checkbox";
    input.className = "leaflet-control-layers-selector";
    input.checked = map.hasLayer(entry.layer);
    input.addEventListener("change", function () {
      setOverlayShown(entry, input.checked);
      // A parent toggled: dependents follow; the Route decides the route check as well.
      GEOSNAP.capabilities.refresh();
    });
    label.appendChild(input);
    label.appendChild(GEOSNAP.element("span", " " + entry.name));
    if (entry.tip) { GEOSNAP.controlTip(label, entry.tip); }
    entry.input = input;
    entry.label = label;
    return label;
  }
  // The list scrolls inside the map instead of running past its bottom edge: the map is
  // short whenever the timeline or the briefing takes its room.
  var layersList = null;
  var LAYERS_LIST_MARGIN = 90;  // title bar, the control's own margins and the attribution
  function fitLayersList() {
    if (!layersList) { return; }
    layersList.style.maxHeight = Math.max(80, map.getSize().y - LAYERS_LIST_MARGIN) + "px";
  }
  map.on("resize", fitLayersList);

  var LayersControl = L.Control.extend({
    onAdd: function () {
      var container = L.DomUtil.create("div", "leaflet-control-layers leaflet-control-layers-expanded geosnap-layers");
      container.setAttribute("aria-label", "Layers");
      L.DomEvent.disableClickPropagation(container);
      L.DomEvent.disableScrollPropagation(container);
      var titleRow = GEOSNAP.element("div", null, "control-title");
      titleRow.appendChild(GEOSNAP.element("strong", "Layers"));
      container.appendChild(titleRow);
      GEOSNAP.addCollapseButton(container, titleRow);
      var list = GEOSNAP.element("div", null, "leaflet-control-layers-list");
      layersList = list;
      var overlays = GEOSNAP.element("div", null, "leaflet-control-layers-overlays");
      LAYER_GROUPS.forEach(function (groupName) {
        var entries = overlaysOfGroup(groupName);
        if (!entries.length) { return; }
        var group = GEOSNAP.element("div", null, "layer-group");
        group.setAttribute("role", "group");
        group.setAttribute("aria-label", groupName);
        group.appendChild(GEOSNAP.element("div", groupName, "layer-group-title"));
        entries.forEach(function (entry) { group.appendChild(overlayRow(entry)); });
        overlays.appendChild(group);
      });
      list.appendChild(overlays);
      container.appendChild(list);
      fitLayersList();
      return container;
    }
  });
  // Layers added or removed by other modules (a case place shown from the timeline) keep
  // the checkboxes true; markers added to a shown group are not overlays and are skipped.
  map.on("layeradd layerremove", function (event) {
    var entry = overlayByLayerId[L.stamp(event.layer)];
    if (entry && entry.input) { entry.input.checked = map.hasLayer(entry.layer); }
  });
  GEOSNAP.capabilities.onChange(syncOverlays);

  GEOSNAP.start = function () {
    if (GEOSNAP.started) { return; }
    // The view is set before anything is drawn: Leaflet refuses to place a marker on a map
    // without one, and the first render happens as soon as the overlays go on. The bounds
    // come from the data, and the records without a timestamp are drawn, so they belong in
    // them even when no dated record is shown.
    var initialPositions = GEOSNAP.filteredPoints()
      .map(positionOf)
      .concat(GEOSNAP.filteredUndatedPoints().map(positionOf));
    if (initialPositions.length > 0) {
      map.fitBounds(L.latLngBounds(initialPositions), { padding: [30, 30], maxZoom: 16 });
    } else {
      map.setView([0, 0], 2);
    }
    GEOSNAP.started = true;
    GEOSNAP.overlays.forEach(function (entry) {
      overlayByLayerId[L.stamp(entry.layer)] = entry;
      if (entry.visible) { entry.layer.addTo(map); }
    });
    GEOSNAP.layersControl = new LayersControl({ position: "topright" }).addTo(map);
    GEOSNAP.capabilities.refresh();
    GEOSNAP.legendControl = buildLegend();
    bindViewToggle("view-legend", "legendControl");
    bindViewToggle("view-layers", "layersControl");
    GEOSNAP.render();
    if (initialPositions.length === 0) {
      document.getElementById("status").textContent = "No records to display";
    }
    drawGraticule();
  };
})();
