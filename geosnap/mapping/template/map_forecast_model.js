// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  // Search-area forecast (crystal ball): pure functions over the rendered points and the
  // complete stays, gaps and segments of the analysis. No DOM, no Leaflet:
  // tests/forecast_model_check.js loads this file under node with a stub window.GEOSNAP.
  // Samples are counted in days, never in points.

  var G = window.GEOSNAP;
  var TIME_KERNEL_MINUTES = 45;         // half-width of the time-of-day window (kernel sigma)
  var OTHER_DAY_CLASS_WEIGHT = 0.3;     // weight of days of the other day class
  var SHRINKAGE_PSEUDO_DAYS = 3;        // pseudo-days of the shrinkage towards the next ladder level
  var DWELL_PRIOR_STAYS = 5;            // below this many stays the place's dwell survival mixes with the global one
  var MIN_DAYS = 3;                     // a ladder level needs this many contributing days to be the basis
  var PERCENT_MIN_DAYS = 5;             // below this only "x of n days" is shown
  var STRONG_MIN_DAYS = 14;
  var STRONG_MIN_EFFECTIVE_DAYS = 10;
  var RECENCY_HALF_LIFE_DAYS = 14;
  var RECENCY_MIN_SPAN_DAYS = 28;
  var RECENCY_FLOOR = 0.1;
  var MAX_WINDOW_STARTS = 2000;
  var MAX_WINDOW_POINTS = 200;          // cap of the inner reach loop per window
  var MAX_STRETCH = 4;
  var ROSE_FINE_MIN_DAYS = 30;          // 16 instead of 8 sectors from this many leaving days
  var ROSE_KERNEL_SECTOR_FRACTION = 0.25; // kernel sigma as a share of the sector width (11.25 deg at 8 sectors)
  var ROSE_SMOOTHING = 0.5;             // Dirichlet pseudo-days, spread evenly over the sectors
  var ROSE_NO_PREFERENCE_ENTROPY = 0.9; // H / H_max above this: no preferred direction
  var MAX_TRIP_SECONDS = 6 * 3600;      // corridors: longer trips are not drawn
  var SPARSE_TRIP_SPACING_SECONDS = 300; // corridors: median report spacing above this marks a sparse trip
  var MIN_DESTINATION_PLACES = 3;       // destination matching needs this many known places
  var MIN_TRIPS_FOR_TIME = 5;           // trip-duration likelihood from this many trips from the origin
  var TRIP_KERNEL_MIN_SECONDS = 60;     // lower bound of the trip-duration kernel bandwidth
  var TIME_LIKELIHOOD_FLOOR = 0.1;
  var OVERSHOOT_LIKELIHOOD = 0.2;       // travelled farther than 1.5 x the distance origin -> place
  var HEADING_CONCENTRATION = 2;        // kappa of the heading likelihood
  var LATERAL_DEGREES = 60;             // a place more than this off the heading is "off to the side"
  var ROUTINE_RECENT_DAYS = 7;          // routine change: the recent period
  var ROUTINE_MIN_RECENT_DAYS = 5;
  var ROUTINE_MIN_EARLIER_DAYS = 14;
  var ROUTINE_CHANGE_JSD = 0.15;        // mean Jensen-Shannon divergence above which the routine changed
  var ROUTINE_NIGHT_END_HOUR = 5;       // night = 00:00-05:00
  var ROUTINE_NIGHT_DROP = 0.3;         // drop of the night presence share at the main place
  var CONE_MAX_SECONDS = 1800;          // dead-reckoning cone only up to 30 min after the last report
  var CONE_MIN_CONTINUATION = 0.2;      // ... and while at least this share of earlier trips was still running
  var CONE_MIN_GLOBAL_TRIPS = 10;       // fallback to trips anywhere needs this many trips
  var CONE_RADIUS_FACTOR = 1.2;
  var END_POSITION_TOLERANCE_SECONDS = 600;  // window end position: nearest report within 10 min
  var MIN_DEPARTURES = 3;
  var MIN_COMPARABLE_GAPS = 3;
  var MIN_GAPS_FOR_PROFILE = 5;
  var GAP_PROFILE_MINUTES = 90;
  var MOTION_LOOKBACK_SECONDS = 600;
  var MIN_MOTION_SEGMENTS = 2;
  var MIN_VISITS_FOR_PRESENCE = 3;
  var CANDIDATE_LIMIT = 5;
  var DAY_SECONDS = 86400;
  var QUANTILES = { p50: 0.5, p80: 0.8, p95: 0.95 };
  var ELSEWHERE = 0;                    // place key of "elsewhere / moving"
  var MOVING_CLASSES = { walking: true, cycling: true, vehicle: true };

  // ---- geometry and statistics --------------------------------------------------------

  function haversineMetres(lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180;
    var dLat = (lat2 - lat1) * toRad;
    var dLon = (lon2 - lon1) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * 6371000 * Math.asin(Math.sqrt(a));
  }

  function bearingDegrees(lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180;
    var phi1 = lat1 * toRad, phi2 = lat2 * toRad, dLon = (lon2 - lon1) * toRad;
    var x = Math.sin(dLon) * Math.cos(phi2);
    var y = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLon);
    return (Math.atan2(x, y) * 180 / Math.PI + 360) % 360;
  }

  // Seconds since the epoch of an ISO stamp. The payload's fixed "YYYY-MM-DDTHH:MM:SS" form
  // (+00:00, Z or no offset = UTC) is parsed by hand with one Date.UTC per distinct date,
  // which is an order of magnitude faster than Date.parse over tens of thousands of points.
  var dayBaseSeconds = {};
  function twoDigits(text, index) { return (text.charCodeAt(index) - 48) * 10 + text.charCodeAt(index + 1) - 48; }
  function utcSeconds(isoText) {
    var tail = isoText.slice(19);
    if (isoText.charAt(10) !== "T" || (tail !== "" && tail !== "Z" && tail !== "+00:00")) { return Date.parse(isoText) / 1000; }
    var dateKey = isoText.slice(0, 10);
    var base = dayBaseSeconds[dateKey];
    if (base === undefined) {
      base = Date.UTC(Number(dateKey.slice(0, 4)), twoDigits(dateKey, 5) - 1, twoDigits(dateKey, 8)) / 1000;
      dayBaseSeconds[dateKey] = base;
    }
    return base + twoDigits(isoText, 11) * 3600 + twoDigits(isoText, 14) * 60 + twoDigits(isoText, 17);
  }

  // Local equirectangular projection around the last position (metres); used only to find
  // the farthest point of a window and to test proximity quickly, never for reported figures.
  var METRES_PER_DEGREE = 111320;
  function longitudeScale(originLat) { return METRES_PER_DEGREE * Math.cos(originLat * Math.PI / 180); }
  function projectPoints(points, originLat) {
    var lonScale = longitudeScale(originLat);
    points.forEach(function (point) {
      point.x = point.lon * lonScale;
      point.y = point.lat * METRES_PER_DEGREE;
    });
  }
  function planarDistance(a, b) {
    var dx = a.x - b.x, dy = a.y - b.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  // Linear-interpolation quantile of an unsorted sample.
  function quantile(sample, fraction) {
    var sorted = sample.slice().sort(function (a, b) { return a - b; });
    if (!sorted.length) { return null; }
    var position = (sorted.length - 1) * fraction;
    var lower = Math.floor(position);
    var upper = Math.min(lower + 1, sorted.length - 1);
    return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
  }

  // Weighted quantile: the smallest value at which the cumulative weight reaches the fraction.
  function weightedQuantile(entries, fraction) {
    var sorted = entries.slice().sort(function (a, b) { return a.value - b.value; });
    var total = 0;
    sorted.forEach(function (entry) { total += entry.weight; });
    if (!sorted.length || total <= 0) { return null; }
    var cumulative = 0;
    for (var i = 0; i < sorted.length; i++) {
      cumulative += sorted[i].weight;
      if (cumulative >= fraction * total - 1e-12) { return sorted[i].value; }
    }
    return sorted[sorted.length - 1].value;
  }

  // Quantile of minutes of day on the 24 h circle: rotate the sample so that its circular
  // mean sits at 12:00, take the (weighted) linear quantile, rotate back. Entries are minutes
  // or { value, weight }.
  function circularQuantile(entries, fraction) {
    var unweighted = entries.every(function (entry) { return typeof entry === "number"; });
    var weighted = entries.map(function (entry) {
      return typeof entry === "number" ? { value: entry, weight: 1 } : entry;
    });
    var mean = circularMean(weighted.map(function (entry) { return { bearing: entry.value / 4, weight: entry.weight }; }));
    if (mean === null) { return null; }
    var reference = mean.bearing * 4;
    var rotated = weighted.map(function (entry) {
      return { value: ((entry.value - reference + 720) % 1440 + 1440) % 1440, weight: entry.weight };
    });
    var value = unweighted
      ? quantile(rotated.map(function (entry) { return entry.value; }), fraction)
      : weightedQuantile(rotated, fraction);
    return value === null ? null : ((value + reference - 720) % 1440 + 1440) % 1440;
  }

  // Wilson 95 % score interval for a share observed over sampleSize (effective) days.
  function wilsonInterval(probability, sampleSize) {
    if (!(sampleSize > 0)) { return null; }
    var z = 1.96;
    var z2n = z * z / sampleSize;
    var centre = (probability + z2n / 2) / (1 + z2n);
    var halfWidth = z * Math.sqrt(probability * (1 - probability) / sampleSize + z2n / (4 * sampleSize)) / (1 + z2n);
    return { low: Math.max(0, centre - halfWidth), high: Math.min(1, centre + halfWidth) };
  }

  // Shrinkage of an observed share towards the share of the next lower ladder level.
  function shrink(observedShare, sampleDays, priorShare) {
    return (sampleDays * observedShare + SHRINKAGE_PSEUDO_DAYS * priorShare) / (sampleDays + SHRINKAGE_PSEUDO_DAYS);
  }

  function circularMean(bearingsWithWeights) {
    var sumX = 0, sumY = 0, sumWeight = 0;
    bearingsWithWeights.forEach(function (entry) {
      var angle = entry.bearing * Math.PI / 180;
      sumX += entry.weight * Math.sin(angle);
      sumY += entry.weight * Math.cos(angle);
      sumWeight += entry.weight;
    });
    if (sumWeight <= 0) { return null; }
    var concentration = Math.sqrt(sumX * sumX + sumY * sumY) / sumWeight;
    var bearing = (Math.atan2(sumX, sumY) * 180 / Math.PI + 360) % 360;
    return { bearing: bearing, concentration: concentration };
  }

  function sectorHalfAngle(concentration) {
    if (concentration <= 0) { return 90; }
    var degrees = Math.sqrt(-2 * Math.log(Math.min(concentration, 0.999999))) * 180 / Math.PI;
    return Math.min(90, Math.max(15, degrees));
  }

  // Rayleigh test of uniformity for a mean resultant length over n (effective) unit vectors;
  // p = exp(sqrt(1 + 4n + 4(n^2 - R_n^2)) - (1 + 2n)), Zar (1999) eq. 27.4, Berens (2009)
  // CircStat circ_rtest.
  function rayleighP(meanResultantLength, n) {
    if (!(n > 0)) { return null; }
    var resultant = meanResultantLength * n;
    var p = Math.exp(Math.sqrt(1 + 4 * n + 4 * (n * n - resultant * resultant)) - (1 + 2 * n));
    return Math.min(1, Math.max(0, p));
  }

  // Half width (degrees) of the 95 % confidence interval of the mean direction, Zar (1999)
  // eq. 26.24/26.25 as in CircStat circ_confmean; null where the interval is undefined (the
  // resultant is too short for the sample size), i.e. no direction can be established.
  var CHI_SQUARE_95_ONE_DF = 3.841458820694124;
  function confidenceHalfAngle(meanResultantLength, n) {
    if (!(n > 0) || !(meanResultantLength > 0)) { return null; }
    var resultant = meanResultantLength * n;
    var squared;
    if (meanResultantLength >= 0.9) {
      squared = n * n - (n * n - resultant * resultant) * Math.exp(CHI_SQUARE_95_ONE_DF / n);
    } else if (meanResultantLength > Math.sqrt(CHI_SQUARE_95_ONE_DF / (2 * n))) {
      squared = 2 * n * (2 * resultant * resultant - n * CHI_SQUARE_95_ONE_DF) / (4 * n - CHI_SQUARE_95_ONE_DF);
    } else {
      return null;
    }
    if (!(squared >= 0)) { return null; }
    return Math.acos(Math.min(1, Math.sqrt(squared) / resultant)) * 180 / Math.PI;
  }

  function compassName(bearing) {
    var names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    return names[Math.round(bearing / 45) % 8];
  }

  // ---- local time (all time-of-day arithmetic uses the payload's local stamps) ----

  function minuteOfDay(local) {
    return twoDigits(local, 11) * 60 + twoDigits(local, 14) + (local.length >= 19 ? twoDigits(local, 17) / 60 : 0);
  }
  function dateKeyOf(local) { return local.slice(0, 10); }
  function dayClassOf(dateKey) {
    var weekday = new Date(dateKey + "T00:00:00Z").getUTCDay();
    return weekday === 0 || weekday === 6 ? "weekend" : "weekday";
  }
  function circularMinutes(a, b) {
    var difference = Math.abs(a - b) % 1440;
    return Math.min(difference, 1440 - difference);
  }
  function pad2(n) { return n < 10 ? "0" + n : String(n); }
  function plural(count, noun) { return count + " " + noun + (count === 1 ? "" : "s"); }
  function formatMinute(minute) {
    var whole = ((Math.round(minute) % 1440) + 1440) % 1440;
    return pad2(Math.floor(whole / 60)) + ":" + pad2(whole % 60);
  }
  function timeKernel(deltaMinutes) { return Math.exp(-0.5 * Math.pow(deltaMinutes / TIME_KERNEL_MINUTES, 2)); }

  // Display-zone offset table [{t, offset (seconds)}], one entry per change; the first entry
  // holds before its instant too. Built from payload.zone_transitions ([utc seconds, offset
  // minutes, abbreviation]) when given, else from the offsets observed in the reports.
  function zoneTable(transitions) {
    return transitions.map(function (entry) { return { t: entry[0], offset: entry[1] * 60 }; });
  }
  function buildOffsetTable(points) {
    var table = [];
    points.forEach(function (point) {
      if (!table.length || table[table.length - 1].offset !== point.offset) {
        table.push({ t: point.t, offset: point.offset });
      }
    });
    return table;
  }
  function offsetAt(table, t) {
    var offset = table.length ? table[0].offset : 0;
    for (var i = 0; i < table.length && table[i].t <= t; i++) { offset = table[i].offset; }
    return offset;
  }
  function localStamp(table, t) {
    return new Date(Math.floor(t + offsetAt(table, t)) * 1000).toISOString().slice(0, 19);
  }
  // Wall clock -> UTC as in map_core.js (G.localToUtcSeconds): a repeated wall time gives
  // its earlier occurrence, a skipped one the next valid instant (the change itself).
  function localToUtc(table, dateKey, minute) {
    var naive = Date.parse(dateKey + "T00:00:00Z") / 1000 + minute * 60;
    if (!table.length) { return naive; }
    for (var i = 0; i < table.length; i++) {
      var candidate = naive - table[i].offset;
      if ((i === 0 || candidate >= table[i].t) && (i + 1 === table.length || candidate < table[i + 1].t)) { return candidate; }
      if (i + 1 < table.length && naive < table[i + 1].t + table[i + 1].offset && candidate >= table[i + 1].t) { return table[i + 1].t; }
    }
    return naive - table[table.length - 1].offset;
  }

  // ---- timeline lookups (stays and gaps are sorted by start and do not overlap) ----------

  function lastIndexAtOrBefore(values, t) {
    var low = 0, high = values.length - 1, found = -1;
    while (low <= high) {
      var mid = (low + high) >> 1;
      if (values[mid] <= t) { found = mid; low = mid + 1; } else { high = mid - 1; }
    }
    return found;
  }
  function intervalAt(entries, starts, t) {
    var index = lastIndexAtOrBefore(starts, t);
    return index >= 0 && entries[index].end >= t ? entries[index] : null;
  }
  // Pieces of the entries that intersect [t0, t1].
  function overlaps(entries, starts, t0, t1) {
    var pieces = [];
    for (var i = Math.max(0, lastIndexAtOrBefore(starts, t0)); i < entries.length && entries[i].start < t1; i++) {
      var start = Math.max(entries[i].start, t0), end = Math.min(entries[i].end, t1);
      if (end > start) { pieces.push({ start: start, end: end, entry: entries[i] }); }
    }
    return pieces;
  }

  // State of the device at an instant: a place key (inside a stay; the stay analysis keeps a
  // stay through a gap when the reports before and after lie within the stop radius),
  // "silent" (inside a gap outside any stay), ELSEWHERE (reports but no stay) or null
  // outside the recorded history.
  function stateAt(context, t) {
    if (t < context.firstT || t > context.last.t) { return null; }
    var stay = intervalAt(context.stays, context.stayStarts, t);
    if (stay) { return stay.placeKey; }
    var gap = intervalAt(context.gaps, context.gapStarts, t);
    return gap && t < gap.end ? "silent" : ELSEWHERE;
  }

  // Shares of [t0, t1] spent at each place, silent (gap outside stays) or moving; null when
  // the interval is not fully inside the recorded history.
  function coverage(context, t0, t1) {
    if (t0 < context.firstT || t1 > context.last.t || !(t1 > t0)) { return null; }
    var length = t1 - t0;
    var stayPieces = overlaps(context.stays, context.stayStarts, t0, t1);
    var places = {};
    var placed = 0;
    stayPieces.forEach(function (piece) {
      places[piece.entry.placeKey] = (places[piece.entry.placeKey] || 0) + (piece.end - piece.start) / length;
      placed += piece.end - piece.start;
    });
    var silent = 0;
    overlaps(context.gaps, context.gapStarts, t0, t1).forEach(function (gapPiece) {
      var seconds = gapPiece.end - gapPiece.start;
      stayPieces.forEach(function (piece) {
        seconds -= Math.max(0, Math.min(piece.end, gapPiece.end) - Math.max(piece.start, gapPiece.start));
      });
      silent += Math.max(0, seconds);
    });
    return { silent: silent / length, moving: Math.max(0, length - silent - placed) / length, places: places };
  }
  function shareOf(shares, key) { return key === ELSEWHERE ? shares.moving : (shares.places[key] || 0); }

  // ---- precomputation (once per page) ---------------------------------------------------

  // The reported radius times its factor to the confidence level of the analysis (asc).
  function uncertaintyRadius(radius, scale) {
    return (radius || 0) * (typeof scale === "number" ? scale : 1);
  }
  function parsePoints(rawPoints) {
    return rawPoints.map(function (point) {
      var t = utcSeconds(point.utc);
      var local = point.local || new Date(t * 1000).toISOString().slice(0, 19);
      return {
        lat: point.lat, lon: point.lon, t: t, accuracy: uncertaintyRadius(point.radius || point.accuracy_m, point.asc),
        local: local, minute: minuteOfDay(local), day: dateKeyOf(local),
        offset: typeof point.off === "number" ? point.off * 60 : utcSeconds(local + "Z") - t
      };
    }).sort(function (a, b) { return a.t - b.t; });
  }

  // Greedy clustering of the stays (longest first) into places; centre = duration-weighted mean.
  function clusterStays(stays, radiusMetres) {
    var places = [];
    stays.slice().sort(function (a, b) { return b.durationSeconds - a.durationSeconds; }).forEach(function (stay) {
      var place = null;
      for (var i = 0; i < places.length && place === null; i++) {
        if (haversineMetres(places[i].lat, places[i].lon, stay.lat, stay.lon) <= radiusMetres) { place = places[i]; }
      }
      if (place === null) {
        place = { key: places.length + 1, lat: stay.lat, lon: stay.lon, stays: [], totalSeconds: 0, weightSum: 0, address: stay.address };
        places.push(place);
      }
      var weight = Math.max(stay.durationSeconds, 60);
      place.lat = (place.lat * place.weightSum + stay.lat * weight) / (place.weightSum + weight);
      place.lon = (place.lon * place.weightSum + stay.lon * weight) / (place.weightSum + weight);
      place.weightSum += weight;
      place.totalSeconds += stay.durationSeconds;
      place.stays.push(stay);
      stay.placeKey = place.key;
    });
    return places;
  }

  function nearestPlaceKey(places, lat, lon, tolerance) {
    var bestKey = ELSEWHERE, bestDistance = tolerance;
    places.forEach(function (place) {
      // The great-circle distance is never shorter than its north-south part (> 111 000 m per
      // degree), so most places are ruled out without trigonometry; the outcome is unchanged.
      if (Math.abs(place.lat - lat) * 111000 > bestDistance) { return; }
      var distance = haversineMetres(place.lat, place.lon, lat, lon);
      if (distance <= bestDistance) { bestKey = place.key; bestDistance = distance; }
    });
    return bestKey;
  }

  // nearestPlaceKey for many lookups: only the places inside the latitude band of the tolerance
  // are measured (in their original order, so ties resolve as in nearestPlaceKey).
  function placeLocator(places) {
    var byLatitude = places.map(function (place, order) { return { place: place, order: order }; })
      .sort(function (a, b) { return a.place.lat - b.place.lat; });
    var latitudes = byLatitude.map(function (entry) { return entry.place.lat; });
    return function (lat, lon, tolerance) {
      var band = tolerance / 111000;
      var candidates = [];
      for (var i = lastIndexAtOrBefore(latitudes, lat - band) + 1; i < byLatitude.length && latitudes[i] <= lat + band; i++) { candidates.push(byLatitude[i]); }
      if (!candidates.length) { return ELSEWHERE; }
      candidates.sort(function (a, b) { return a.order - b.order; });
      return nearestPlaceKey(candidates.map(function (entry) { return entry.place; }), lat, lon, tolerance);
    };
  }

  function enumerateDays(firstDay, lastDay) {
    var days = [];
    for (var t = Date.parse(firstDay + "T00:00:00Z"); t <= Date.parse(lastDay + "T00:00:00Z"); t += DAY_SECONDS * 1000) {
      var key = new Date(t).toISOString().slice(0, 10);
      days.push({ key: key, dayClass: dayClassOf(key), index: days.length, hasData: false });
    }
    return days;
  }

  // input: { points: [{lat, lon, radius, asc, utc, local}] (rendered, analysable), last: analysis.last
  //          with asc, the factor from its reported to its uncertainty radius (default 1),
  //          stays: analysis.stays, gaps: analysis.gaps, segments: analysis.segments,
  //          stopRadiusM, stopMinMinutes, zoneTransitions: payload.zone_transitions (optional) }
  function prepare(input) {
    var points = parsePoints(input.points || []);
    var stopRadius = input.stopRadiusM;
    projectPoints(points, input.last.lat);
    var lastLocal = input.last.local || (points.length ? points[points.length - 1].local : new Date(utcSeconds(input.last.utc) * 1000).toISOString().slice(0, 19));
    var last = {
      lat: input.last.lat, lon: input.last.lon, t: utcSeconds(input.last.utc), accuracy: uncertaintyRadius(input.last.accuracy_m, input.last.asc),
      local: lastLocal, minute: minuteOfDay(lastLocal), day: dateKeyOf(lastLocal), offset: utcSeconds(lastLocal + "Z") - utcSeconds(input.last.utc)
    };
    projectPoints([last], input.last.lat);
    var stays = (input.stays || []).map(function (stay) {
      var arriveLocal = stay.arrive_local || null;
      var leaveLocal = stay.leave_local || null;
      return {
        lat: stay.lat, lon: stay.lon, start: utcSeconds(stay.arrive_utc), end: utcSeconds(stay.leave_utc),
        durationSeconds: stay.duration_minutes * 60, address: stay.address || null, arriveLocal: arriveLocal, leaveLocal: leaveLocal
      };
    }).sort(function (a, b) { return a.start - b.start; });
    var offsets = input.zoneTransitions && input.zoneTransitions.length ? zoneTable(input.zoneTransitions) : buildOffsetTable(points);
    stays.forEach(function (stay) {
      if (!stay.arriveLocal) { stay.arriveLocal = localStamp(offsets, stay.start); }
      if (!stay.leaveLocal) { stay.leaveLocal = localStamp(offsets, stay.end); }
      stay.arriveMinute = minuteOfDay(stay.arriveLocal);
      stay.leaveMinute = minuteOfDay(stay.leaveLocal);
      stay.arriveDay = dateKeyOf(stay.arriveLocal);
      stay.leaveDay = dateKeyOf(stay.leaveLocal);
    });
    var places = clusterStays(stays, stopRadius);
    // The stay in progress at the last report (right-censored) is not a completed stay.
    var currentStay = stays.length && stays[stays.length - 1].end >= last.t - 1 && stays[stays.length - 1].start <= last.t ? stays[stays.length - 1] : null;
    var gaps = (input.gaps || []).map(function (gap) {
      var startLocal = gap.start_local || localStamp(offsets, utcSeconds(gap.start_utc));
      return {
        start: utcSeconds(gap.start_utc), end: utcSeconds(gap.end_utc), durationSeconds: gap.duration_minutes * 60,
        startMinute: minuteOfDay(startLocal), startDay: dateKeyOf(startLocal), distance: gap.distance_m,
        fromLat: gap.from_lat, fromLon: gap.from_lon, toLat: gap.to_lat, toLon: gap.to_lon
      };
    }).sort(function (a, b) { return a.start - b.start; });
    var segments = (input.segments || []).map(function (segment) {
      return {
        start: utcSeconds(segment.start_utc), end: utcSeconds(segment.end_utc), distance: segment.distance_m,
        durationSeconds: segment.duration_seconds, bearing: segment.bearing_deg, movementClass: segment.movement_class
      };
    });
    var context = {
      points: points,
      pointTimes: points.map(function (point) { return point.t; }),
      last: last,
      firstT: points.length ? Math.min(points[0].t, stays.length ? stays[0].start : Infinity) : last.t,
      stopRadius: stopRadius,
      lonScale: longitudeScale(input.last.lat),
      stopMinSeconds: input.stopMinMinutes * 60,
      stays: stays,
      stayStarts: stays.map(function (stay) { return stay.start; }),
      currentStay: currentStay,
      gaps: gaps,
      gapStarts: gaps.map(function (gap) { return gap.start; }),
      segments: segments,
      places: places,
      placeByKey: {},
      offsets: offsets,
      days: enumerateDays(points.length ? points[0].day : last.day, last.day),
      dayByKey: {}
    };
    places.forEach(function (place) { context.placeByKey[place.key] = place; });
    context.lastPlaceKey = nearestPlaceKey(places, last.lat, last.lon, stopRadius + Math.min(last.accuracy, stopRadius));
    context.placesWithinAccuracy = places.filter(function (place) {
      return haversineMetres(place.lat, place.lon, last.lat, last.lon) <= last.accuracy;
    });
    context.days.forEach(function (day) { context.dayByKey[day.key] = day; });
    var locatePlace = placeLocator(places);
    points.forEach(function (point) {
      var stay = intervalAt(stays, context.stayStarts, point.t);
      point.placeKey = stay ? stay.placeKey : locatePlace(point.lat, point.lon, stopRadius + Math.min(point.accuracy, stopRadius));
      if (context.dayByKey[point.day]) { context.dayByKey[point.day].hasData = true; }
    });
    stays.forEach(function (stay) {
      [stay.arriveDay, stay.leaveDay].forEach(function (key) { if (context.dayByKey[key]) { context.dayByKey[key].hasData = true; } });
    });
    var weekendDays = 0, weekdayDays = 0;
    context.days.forEach(function (day) {
      if (!day.hasData) { return; }
      if (day.dayClass === "weekend") { weekendDays += 1; } else { weekdayDays += 1; }
    });
    context.dayClassesEnabled = weekendDays >= 2 && weekdayDays >= 3;
    context.dataSpanSeconds = points.length ? last.t - points[0].t : 0;
    context.recencyAvailable = context.dataSpanSeconds >= RECENCY_MIN_SPAN_DAYS * DAY_SECONDS;
    context.pointsHere = points.filter(function (point) { return planarDistance(point, last) <= stopRadius; }).length;
    return context;
  }

  // ---- day-based samples and the backoff ladder ---------------------------------------

  function dayWeight(context, day, weighting) {
    if (weighting !== "recent") { return 1; }
    var ageDays = context.days.length - 1 - day.index;
    return Math.max(RECENCY_FLOOR, Math.pow(0.5, ageDays / RECENCY_HALF_LIFE_DAYS));
  }

  // Analog days: the device was at the place of the last report around its time of day
  // (at that very minute, or for at least half of the ±45 min window, so that short visits
  // count too); the sample is the timeline state elapsed seconds later.
  function isAnalogAnchor(context, anchor) {
    var kernelSeconds = TIME_KERNEL_MINUTES * 60;
    var around = coverage(context, anchor - kernelSeconds, anchor + kernelSeconds);
    return around !== null && (stateAt(context, anchor) === context.lastPlaceKey || shareOf(around, context.lastPlaceKey) >= 0.5);
  }
  function analogSamples(context, elapsed, referenceClass, weighting) {
    var samples = [];
    context.days.forEach(function (day) {
      var anchor = localToUtc(context.offsets, day.key, context.last.minute);
      var target = anchor + elapsed;
      if (target > context.last.t || !isAnalogAnchor(context, anchor)) { return; }
      var state = stateAt(context, target);
      if (state === null) { return; }
      var atLocal = localStamp(context.offsets, target);
      var shares = {};
      if (state !== "silent") { shares[state] = 1; }
      samples.push({
        day: day, atLocal: atLocal, atSeconds: target, silent: state === "silent", shares: shares,
        classMatch: dayClassOf(dateKeyOf(atLocal)) === referenceClass, weight: dayWeight(context, day, weighting)
      });
    });
    return samples;
  }

  // Routine: shares of the ±45 min window around the reference time of day on every day.
  function routineSamples(context, referenceMinute, referenceClass, weighting) {
    var samples = [];
    var kernelSeconds = TIME_KERNEL_MINUTES * 60;
    context.days.forEach(function (day) {
      var anchor = localToUtc(context.offsets, day.key, referenceMinute);
      var around = coverage(context, anchor - kernelSeconds, anchor + kernelSeconds);
      if (!around) { return; }
      var silent = around.silent > 0.5;
      var shares = {};
      if (!silent) {
        var known = 1 - around.silent;
        Object.keys(around.places).forEach(function (key) { shares[key] = around.places[key] / known; });
        if (around.moving > 0) { shares[ELSEWHERE] = around.moving / known; }
      }
      samples.push({
        day: day, atLocal: localStamp(context.offsets, anchor), atSeconds: anchor, silent: silent, shares: shares,
        classMatch: day.dayClass === referenceClass, weight: dayWeight(context, day, weighting)
      });
    });
    return samples;
  }

  function aggregateLevel(level, samples, sameClassOnly, classesEnabled) {
    var rows = sameClassOnly ? samples.filter(function (sample) { return sample.classMatch; }) : samples;
    var silentDays = 0, days = 0, sumWeight = 0, sumSquares = 0;
    var shares = {}, counts = {};
    rows.forEach(function (sample) {
      var weight = sample.weight * (sample.classMatch || !classesEnabled ? 1 : OTHER_DAY_CLASS_WEIGHT);
      if (sample.silent) { silentDays += 1; return; }
      days += 1;
      sumWeight += weight;
      sumSquares += weight * weight;
      var dominant = null;
      Object.keys(sample.shares).forEach(function (key) {
        shares[key] = (shares[key] || 0) + weight * sample.shares[key];
        if (dominant === null || sample.shares[key] > sample.shares[dominant]) { dominant = key; }
      });
      if (dominant !== null) { counts[dominant] = (counts[dominant] || 0) + 1; }
    });
    if (sumWeight > 0) { Object.keys(shares).forEach(function (key) { shares[key] /= sumWeight; }); }
    return {
      level: level, days: days, effectiveDays: sumWeight > 0 ? sumWeight * sumWeight / sumSquares : 0,
      silentDays: silentDays, shares: shares, counts: counts, samples: rows
    };
  }

  // Ladder S5 -> S2 (bottom-up shrinkage); the basis is the highest level with >= 3 days.
  function buildLadder(context, elapsed, referenceMinute, referenceClass, weighting) {
    var analog = analogSamples(context, elapsed, referenceClass, weighting);
    var routine = routineSamples(context, referenceMinute, referenceClass, weighting);
    var classes = context.dayClassesEnabled;
    var levels = [aggregateLevel("S5", routine, false, classes)];
    if (classes) { levels.push(aggregateLevel("S4", routine, true, true)); }
    levels.push(aggregateLevel("S3", analog, false, classes));
    if (classes) { levels.push(aggregateLevel("S2", analog, true, true)); }
    var keys = {};
    keys[ELSEWHERE] = true;
    keys[context.lastPlaceKey] = true;
    levels.forEach(function (level) { Object.keys(level.shares).forEach(function (key) { keys[key] = true; }); });
    var prior = null;
    levels.forEach(function (level) {
      level.probabilities = {};
      Object.keys(keys).forEach(function (key) {
        var observed = level.shares[key] || 0;
        if (prior === null) {
          level.probabilities[key] = level.days > 0 ? observed : null;
        } else {
          level.probabilities[key] = level.days > 0 ? shrink(observed, level.days, prior[key]) : prior[key];
        }
      });
      if (level.days > 0 || prior !== null) { prior = level.probabilities; }
    });
    var basis = null;
    for (var i = levels.length - 1; i >= 0 && basis === null; i--) {
      if (levels[i].days >= MIN_DAYS) { basis = levels[i]; }
    }
    return { levels: levels, basis: basis, keys: Object.keys(keys).map(Number), analogDays: analog.length, routineDays: routine.length };
  }

  var LEVEL_LABELS = {
    S2: "days on which the device was here around the same time of day (same day class)",
    S3: "days on which the device was here around the same time of day (all days)",
    S4: "routine at the reference time of day (same day class)",
    S5: "routine at the reference time of day (all days)"
  };

  // ---- per-place descriptions ---------------------------------------------------------

  // Visits of a place without the stay in progress at the last report (right-censored).
  function completedVisits(context, place) {
    return place.stays.filter(function (stay) { return stay !== context.currentStay; });
  }

  function usualPresence(context, place, referenceClass) {
    var visits = completedVisits(context, place);
    var dayClass = null;
    if (context.dayClassesEnabled) {
      var sameClass = visits.filter(function (stay) { return dayClassOf(stay.arriveDay) === referenceClass; });
      if (sameClass.length >= MIN_VISITS_FOR_PRESENCE) { visits = sameClass; dayClass = referenceClass; }
    }
    if (visits.length < MIN_VISITS_FOR_PRESENCE) { return null; }
    return {
      arrive: formatMinute(circularQuantile(visits.map(function (stay) { return stay.arriveMinute; }), 0.5)),
      leave: formatMinute(circularQuantile(visits.map(function (stay) { return stay.leaveMinute; }), 0.5)),
      visits: visits.length,
      dayClass: dayClass
    };
  }

  function describePlace(context, key, referenceClass) {
    if (key === ELSEWHERE) { return { key: ELSEWHERE, lat: null, lon: null, address: null, visits: 0, distance: null, bearing: null, usualPresence: null, lastVisitLocal: null }; }
    var place = context.placeByKey[key];
    var lastVisit = null;
    place.stays.forEach(function (stay) {
      if (stay !== context.currentStay && (lastVisit === null || stay.end > lastVisit.end)) { lastVisit = stay; }
    });
    return {
      key: key, lat: place.lat, lon: place.lon, address: place.address, visits: completedVisits(context, place).length,
      totalSeconds: place.totalSeconds,
      distance: haversineMetres(context.last.lat, context.last.lon, place.lat, place.lon),
      bearing: bearingDegrees(context.last.lat, context.last.lon, place.lat, place.lon),
      usualPresence: usualPresence(context, place, referenceClass),
      lastVisitLocal: lastVisit ? lastVisit.leaveLocal : null
    };
  }

  // ---- rings and sector from windows ---------------------------------------------------

  // Farthest point within (t_start, t_start + windowSeconds]; the inner loop is capped at
  // MAX_WINDOW_POINTS evenly strided points (plus the window's last point).
  function reachWithin(points, pointTimes, startIndex, windowSeconds) {
    var start = points[startIndex];
    var endIndex = lastIndexAtOrBefore(pointTimes, start.t + windowSeconds);
    if (endIndex <= startIndex) { return null; }
    var count = endIndex - startIndex;
    var stride = Math.max(1, Math.ceil(count / MAX_WINDOW_POINTS));
    var farthest = null, farthestSquared = -1;
    for (var j = startIndex + stride; j <= endIndex; j += stride) {
      var dx = points[j].x - start.x, dy = points[j].y - start.y;
      var squared = dx * dx + dy * dy;
      if (squared > farthestSquared) { farthestSquared = squared; farthest = points[j]; }
    }
    if (count % stride !== 0) {
      var dxEnd = points[endIndex].x - start.x, dyEnd = points[endIndex].y - start.y;
      if (dxEnd * dxEnd + dyEnd * dyEnd > farthestSquared) { farthest = points[endIndex]; }
    }
    return { distance: haversineMetres(start.lat, start.lon, farthest.lat, farthest.lon), point: farthest };
  }
  // Windows from the chosen start points with per-day normalised weights
  // (w_d * k_day * k_acc / sum of k_acc of the day's starts).
  function windowSample(context, startIndices, windowSeconds, referenceClass, weighting) {
    var stride = Math.max(1, Math.ceil(startIndices.length / MAX_WINDOW_STARTS));
    var chosen = [];
    for (var k = 0; k < startIndices.length; k += stride) { chosen.push(startIndices[k]); }
    var windows = [];
    var accuracyByDay = {};
    chosen.forEach(function (index) {
      var point = context.points[index];
      var reach = reachWithin(context.points, context.pointTimes, index, windowSeconds);
      if (reach === null) { return; }
      var accuracyWeight = 1 / Math.max(1, point.accuracy / context.stopRadius);
      accuracyByDay[point.day] = (accuracyByDay[point.day] || 0) + accuracyWeight;
      windows.push({ start: point, reach: reach, accuracyWeight: accuracyWeight });
    });
    // Normalise per day over the usable windows only, so days with starts that have no
    // following point (before a gap) keep their full day weight.
    windows.forEach(function (entry) {
      var day = context.dayByKey[entry.start.day];
      var classWeight = !context.dayClassesEnabled || !day || day.dayClass === referenceClass ? 1 : OTHER_DAY_CLASS_WEIGHT;
      entry.weight = (day ? dayWeight(context, day, weighting) : 1) * classWeight * entry.accuracyWeight / accuracyByDay[entry.start.day];
    });
    return windows;
  }

  function distinctDays(context, indices) {
    var seen = {};
    indices.forEach(function (index) { seen[context.points[index].day] = true; });
    return Object.keys(seen).length;
  }

  function projected(context, lat, lon) {
    return { lat: lat, lon: lon, x: lon * context.lonScale, y: lat * METRES_PER_DEGREE };
  }

  // Farthest rendered point from the origin among the points in (t0, t1], inner loop capped at
  // MAX_WINDOW_POINTS evenly strided points plus the last one; null without points.
  function farthestPointBetween(context, origin, t0, t1) {
    var first = lastIndexAtOrBefore(context.pointTimes, t0) + 1;
    var lastIndex = lastIndexAtOrBefore(context.pointTimes, t1);
    if (lastIndex < first) { return null; }
    var stride = Math.max(1, Math.ceil((lastIndex - first + 1) / MAX_WINDOW_POINTS));
    var farthest = null, farthestSquared = -1;
    var consider = function (point) {
      var dx = point.x - origin.x, dy = point.y - origin.y;
      if (dx * dx + dy * dy > farthestSquared) { farthestSquared = dx * dx + dy * dy; farthest = point; }
    };
    for (var j = first; j <= lastIndex; j += stride) { consider(context.points[j]); }
    consider(context.points[lastIndex]);
    return farthest;
  }

  // Position at an instant for window ends: the centre of the stay in progress, else the
  // nearest rendered report within END_POSITION_TOLERANCE_SECONDS, else null.
  function positionAt(context, t) {
    var stay = intervalAt(context.stays, context.stayStarts, t);
    if (stay) { return { lat: stay.lat, lon: stay.lon }; }
    var before = lastIndexAtOrBefore(context.pointTimes, t);
    var best = null;
    [before, before + 1].forEach(function (index) {
      if (index < 0 || index >= context.points.length) { return; }
      var point = context.points[index];
      if (Math.abs(point.t - t) <= END_POSITION_TOLERANCE_SECONDS && (best === null || Math.abs(point.t - t) < Math.abs(best.t - t))) { best = point; }
    });
    return best ? { lat: best.lat, lon: best.lon } : null;
  }

  // Analog-day windows: one window per analog day (the S2/S3 criterion), starting at the
  // time of day of the last report at the centre of its place. Reach = farthest rendered point
  // or centre of a stay intersecting the window, so duplicate collapsing and the report
  // frequency do not change the sample; thinning only loses reach between stays.
  function analogWindows(context, windowSeconds, referenceClass, weighting) {
    var place = context.placeByKey[context.lastPlaceKey];
    var origin = projected(context, place.lat, place.lon);
    var windows = [];
    context.days.forEach(function (day) {
      var anchor = localToUtc(context.offsets, day.key, context.last.minute);
      var end = anchor + windowSeconds;
      if (end > context.last.t || !isAnalogAnchor(context, anchor)) { return; }
      var reach = { distance: 0, lat: origin.lat, lon: origin.lon };
      var point = farthestPointBetween(context, origin, anchor, end);
      if (point) { reach = { distance: haversineMetres(origin.lat, origin.lon, point.lat, point.lon), lat: point.lat, lon: point.lon }; }
      overlaps(context.stays, context.stayStarts, anchor, end).forEach(function (piece) {
        var distance = haversineMetres(origin.lat, origin.lon, piece.entry.lat, piece.entry.lon);
        if (distance > reach.distance) { reach = { distance: distance, lat: piece.entry.lat, lon: piece.entry.lon }; }
      });
      var classWeight = !context.dayClassesEnabled || day.dayClass === referenceClass ? 1 : OTHER_DAY_CLASS_WEIGHT;
      windows.push({ day: day, origin: origin, reach: reach, end: positionAt(context, end), weight: dayWeight(context, day, weighting) * classWeight });
    });
    return windows;
  }

  // Bearing of a window that left the stop radius: towards its end position when that lies
  // outside the radius, else towards its farthest position (out and back within the window).
  function windowBearing(entry, threshold) {
    var end = entry.end;
    var target = end && haversineMetres(entry.origin.lat, entry.origin.lon, end.lat, end.lon) > threshold ? end : entry.reach;
    return bearingDegrees(entry.origin.lat, entry.origin.lon, target.lat, target.lon);
  }

  // Windows that left the stop radius (reach scaled by the stretch), each with its bearing.
  function leavingWindows(windows, threshold, stretch) {
    var leaving = windows.filter(function (entry) { return entry.reach.distance * stretch > threshold; });
    leaving.forEach(function (entry) { entry.bearing = windowBearing(entry, threshold / stretch); });
    return leaving;
  }

  // Rings from the first window set with MIN_DAYS days: analog-day windows at the place of the
  // last report, else (away from known places) windows that start at reports near the last
  // position, else all windows. At a known place with too few analog days the windows near the
  // last position feed the tendency only: windows from all times of day at one place understate
  // the reach at this time of day, and the global windows cover the true later position more
  // reliably. Returns the window sets for the tendency, or null when no window length can be
  // justified.
  function estimateRings(context, elapsed, referenceClass, weighting, result) {
    var span = context.dataSpanSeconds;
    var windowSeconds = elapsed;
    var stretch = 1;
    if (span > 0 && elapsed > span / 2) {
      windowSeconds = span / 2;
      stretch = elapsed / windowSeconds;
      result.extrapolated = true;
    }
    result.windowSeconds = windowSeconds;
    if (stretch > MAX_STRETCH) {
      result.radiiNote = "the elapsed time is more than twice the recorded history, so no radius can be justified";
      return null;
    }
    var last = context.last;
    var windowSets = { windowSeconds: windowSeconds, stretch: stretch, analog: null, near: null };
    var windows = null, tier = null, conditionedDays = 0, basisNote = null;
    if (context.lastPlaceKey !== ELSEWHERE) {
      var analog = analogWindows(context, windowSeconds, referenceClass, weighting);
      conditionedDays = analog.length;
      if (analog.length >= MIN_DAYS) {
        windowSets.analog = analog;
        windows = analog;
        tier = "T1";
        basisNote = analog.length + " days on which the device was here around " + formatMinute(last.minute) + " (one window per day; reach from reports and stays)";
      }
    }
    if (windows === null) {
      var completeBefore = last.t - windowSeconds;
      var near = [];
      context.points.forEach(function (point, index) {
        if (point.t <= completeBefore && planarDistance(point, last) <= context.stopRadius + last.accuracy) { near.push(index); }
      });
      windowSets.near = pointWindows(context, near, windowSeconds, referenceClass, weighting);
      var nearDays = distinctDays(context, near);
      if (context.lastPlaceKey === ELSEWHERE) { conditionedDays = nearDays; }
      if (nearDays >= MIN_DAYS && context.lastPlaceKey === ELSEWHERE) {
        windows = windowSets.near;
        tier = "T2";
        conditionedDays = nearDays;
        basisNote = nearDays + " days on which the device was near the last position";
      }
    }
    if (windows === null) {
      var all = [];
      context.points.forEach(function (point, index) { if (point.t <= last.t - windowSeconds) { all.push(index); } });
      windows = pointWindows(context, all, windowSeconds, referenceClass, weighting);
      result.radiiBasis = { kind: "global", tier: null, days: distinctDays(context, all), note: "all recorded windows of this length, weighted per day; only " + conditionedDays + " comparable days, fewer than " + MIN_DAYS };
    } else {
      result.radiiBasis = { kind: "conditioned", tier: tier, days: conditionedDays, note: basisNote };
    }
    result.windowCount = windows.length;
    if (windows.length < MIN_DAYS) {
      result.radiiNote = "too few complete windows of this length in the recorded history (" + windows.length + ")";
      return windowSets;
    }
    var entries = windows.map(function (entry) { return { value: entry.reach.distance, weight: entry.weight }; });
    result.radii = {};
    Object.keys(QUANTILES).forEach(function (key) {
      result.radii[key] = weightedQuantile(entries, QUANTILES[key]) * stretch + last.accuracy;
    });
    // With few windows the 95 % quantile is the farthest reach observed, which a new window
    // exceeds with probability of about 1 / (n + 1).
    var farthest = Math.max.apply(null, entries.map(function (entry) { return entry.value; }));
    result.rings = { windowCount: windows.length, r95IsMaximumObserved: weightedQuantile(entries, QUANTILES.p95) >= farthest };
    if (tier === "T1") {
      var leaving = leavingWindows(windows, context.stopRadius + last.accuracy, stretch);
      result.rose = buildRose(windows, leaving, stretch, last.accuracy);
    }
    return windowSets;
  }

  // ---- tendency direction from the last position -----------------------------------------

  // Statistics over one unit vector per day (weight = the model's day weight, no distance
  // weight): mean direction, mean resultant length, effective n = (sum w)^2 / sum w^2 (times
  // overlapFactor for windows longer than a day), Rayleigh p, 95 % confidence interval of the
  // mean, and a second trigonometric moment check for two opposite directions (the doubled
  // angles concentrate more than the angles themselves; Mardia & Jupp 2000, section 2.3).
  function directionStatistics(dayBearings, overlapFactor) {
    var sumWeight = 0, sumSquares = 0;
    dayBearings.forEach(function (entry) { sumWeight += entry.weight; sumSquares += entry.weight * entry.weight; });
    if (!(sumWeight > 0)) { return null; }
    var nEffective = sumWeight * sumWeight / sumSquares * overlapFactor;
    var mean = circularMean(dayBearings);
    var doubled = circularMean(dayBearings.map(function (entry) { return { bearing: (2 * entry.bearing) % 360, weight: entry.weight }; }));
    var halfAngle = confidenceHalfAngle(mean.concentration, nEffective);
    var p = rayleighP(mean.concentration, nEffective);
    var doubledP = rayleighP(doubled.concentration, nEffective);
    var axis = doubled.bearing / 2;
    return {
      nEffective: nEffective, resultantLength: mean.concentration, bearing: mean.bearing, confidenceHalfAngle: halfAngle, rayleighP: p,
      oppositeDirections: dayBearings.length >= MIN_DAYS && doubledP < 0.05 && doubled.concentration > mean.concentration ? [axis, axis + 180] : null
    };
  }

  // T2 day vectors: per day the mean bearing of its leaving windows; the day weight is the sum
  // of its window weights (the model's day weight, as windowSample normalises per day).
  function nearDayBearings(windows, threshold, stretch) {
    var days = {}, order = [];
    windows.forEach(function (entry) {
      var key = entry.start.day;
      if (!days[key]) { days[key] = { weight: 0, leaving: [] }; order.push(key); }
      days[key].weight += entry.weight;
    });
    leavingWindows(windows, threshold, stretch).forEach(function (entry) {
      days[entry.start.day].leaving.push({ bearing: entry.bearing, weight: entry.weight });
    });
    var bearings = [];
    order.forEach(function (key) {
      var mean = circularMean(days[key].leaving);
      if (mean !== null) { bearings.push({ bearing: mean.bearing, weight: days[key].weight }); }
    });
    return { bearings: bearings, total: order.length };
  }

  // T4 spatial prior: bearing to every other known place (beyond the stop radius of the last
  // position), weighted by the calendar days with a stay there.
  function knownPlaceBearings(context) {
    var last = context.last;
    var bearings = [];
    context.places.forEach(function (place) {
      if (haversineMetres(last.lat, last.lon, place.lat, place.lon) <= context.stopRadius) { return; }
      var visited = {};
      place.stays.forEach(function (stay) {
        var first = context.dayByKey[stay.arriveDay], lastDay = context.dayByKey[stay.leaveDay];
        if (!first || !lastDay) { visited[stay.arriveDay] = true; return; }
        for (var i = first.index; i <= lastDay.index; i++) { visited[context.days[i].key] = true; }
      });
      var days = Object.keys(visited).length;
      if (days > 0) { bearings.push({ bearing: bearingDegrees(last.lat, last.lon, place.lat, place.lon), weight: days }); }
    });
    return bearings;
  }

  var TENDENCY_BASIS = {
    T1: "departures from this place on comparable days",
    T2: "windows starting near the last position",
    T4: "known places seen from the last position"
  };

  // Tendency from the last position: the first tier with at least one day that gives a
  // bearing. T1 analog-day windows at the place of the last report, else T2 windows starting
  // near the last position, else T4 known places. When the device stayed at its place on
  // every analog day, T1 gives no bearing; that fact is kept (stayedDays) and T4 follows.
  // The heading of the latest movement (T3) is kept apart and never averaged in.
  function estimateTendency(context, windowSets, elapsed, motion) {
    var tendency = {
      tier: null, basis: null, days: 0, nEffective: 0, resultantLength: 0, bearing: null, confidenceHalfAngle: null,
      rayleighP: null, evidence: "sparse", oppositeDirections: null, leftDays: null, dayBearings: [], stayedDays: null,
      motion: motion && motion.heading && motion.speedKmh > 0 && elapsed <= CONE_MAX_SECONDS
        ? { bearing: motion.heading.bearing, ageMinutes: elapsed / 60, speedKmh: motion.speedKmh } : null
    };
    var threshold = context.stopRadius + context.last.accuracy;
    var overlapFactor = 1;
    if (windowSets) {
      overlapFactor = Math.min(1, DAY_SECONDS / windowSets.windowSeconds);
      if (windowSets.analog) {
        var leaving = leavingWindows(windowSets.analog, threshold, windowSets.stretch).map(function (entry) {
          return { bearing: entry.bearing, weight: entry.weight };
        });
        if (leaving.length) {
          tendency.tier = "T1";
          tendency.dayBearings = leaving;
          tendency.leftDays = { left: leaving.length, total: windowSets.analog.length };
        } else {
          tendency.stayedDays = windowSets.analog.length;
        }
      } else if (windowSets.near) {
        var near = nearDayBearings(windowSets.near, threshold, windowSets.stretch);
        if (near.bearings.length) {
          tendency.tier = "T2";
          tendency.dayBearings = near.bearings;
          tendency.leftDays = { left: near.bearings.length, total: near.total };
        }
      }
    }
    if (tendency.tier === null) {
      var places = knownPlaceBearings(context);
      if (!places.length) { return tendency; }
      tendency.tier = "T4";
      tendency.dayBearings = places;
      overlapFactor = 1;
    }
    tendency.basis = TENDENCY_BASIS[tendency.tier];
    tendency.days = tendency.tier === "T4"
      ? tendency.dayBearings.reduce(function (sum, entry) { return sum + entry.weight; }, 0)
      : tendency.dayBearings.length;
    var statistics = directionStatistics(tendency.dayBearings, overlapFactor);
    if (statistics === null) { return tendency; }
    Object.keys(statistics).forEach(function (key) { tendency[key] = statistics[key]; });
    if (tendency.days < MIN_DAYS) {
      tendency.evidence = "sparse";
    } else if (statistics.rayleighP < 0.05 && statistics.nEffective >= PERCENT_MIN_DAYS && statistics.confidenceHalfAngle !== null) {
      tendency.evidence = "established";
    } else {
      tendency.evidence = "weak";
    }
    return tendency;
  }

  // Map sector: the window-based tendency with its confidence interval as half angle; none
  // where the interval is undefined.
  function sectorFromTendency(tendency) {
    if (!tendency || (tendency.tier !== "T1" && tendency.tier !== "T2") || tendency.confidenceHalfAngle === null) { return null; }
    return {
      bearing: tendency.bearing, compass: compassName(tendency.bearing), concentration: tendency.resultantLength,
      strength: tendency.evidence === "established" ? "dominant" : "weak", halfAngle: tendency.confidenceHalfAngle,
      basis: tendency.basis, sampleSize: tendency.days
    };
  }

  // Direction rose with anisotropic reach over the analog windows that left the
  // place. P(s) = (sum_i w_i K(theta_i, s) + alpha / S) / (sum_i w_i + alpha), K a circular
  // Gaussian normalised over the sectors; reach80(s) = weighted 80 % quantile of the reaches with
  // w_i K as weight, only for sectors that hold at least MIN_DAYS leaving days.
  function buildRose(windows, leaving, stretch, accuracy) {
    if (leaving.length < MIN_DAYS) { return null; }
    var count = leaving.length >= ROSE_FINE_MIN_DAYS ? 16 : 8;
    var width = 360 / count;
    var sigma = width * ROSE_KERNEL_SECTOR_FRACTION;
    var kernel = function (bearing, centre) {
      var difference = Math.abs(bearing - centre) % 360;
      difference = Math.min(difference, 360 - difference);
      return Math.exp(-0.5 * Math.pow(difference / sigma, 2));
    };
    var sectors = [];
    for (var s = 0; s < count; s++) { sectors.push({ bearing: s * width, probability: 0, reach80: null, days: 0, reachEntries: [] }); }
    var weightSum = 0;
    leaving.forEach(function (entry) {
      var kernels = sectors.map(function (sector) { return kernel(entry.bearing, sector.bearing); });
      var kernelSum = kernels.reduce(function (sum, value) { return sum + value; }, 0);
      sectors.forEach(function (sector, index) {
        sector.probability += entry.weight * kernels[index] / kernelSum;
        sector.reachEntries.push({ value: entry.reach.distance, weight: entry.weight * kernels[index] });
      });
      sectors[Math.round(entry.bearing / width) % count].days += 1;
      weightSum += entry.weight;
    });
    var entropy = 0;
    sectors.forEach(function (sector) {
      sector.probability = (sector.probability + ROSE_SMOOTHING / count) / (weightSum + ROSE_SMOOTHING);
      if (sector.probability > 0) { entropy -= sector.probability * Math.log(sector.probability); }
      if (sector.days >= MIN_DAYS) { sector.reach80 = weightedQuantile(sector.reachEntries, 0.8) * stretch + accuracy; }
      delete sector.reachEntries;
    });
    var mean = circularMean(leaving.map(function (entry) { return { bearing: entry.bearing, weight: entry.weight }; }));
    var entropyRatio = entropy / Math.log(count);
    return {
      sectors: sectors, leavingDays: leaving.length, analogDays: windows.length, entropyRatio: entropyRatio,
      preferred: entropyRatio <= ROSE_NO_PREFERENCE_ENTROPY, percentAvailable: leaving.length >= PERCENT_MIN_DAYS,
      meanBearing: mean.bearing, concentration: mean.concentration,
      basis: leaving.length + " of " + windows.length + " comparable days left the stop radius within the window"
    };
  }

  // Point-start windows in the shape of the analog windows.
  function pointWindows(context, startIndices, windowSeconds, referenceClass, weighting) {
    return windowSample(context, startIndices, windowSeconds, referenceClass, weighting).map(function (entry) {
      return { day: context.dayByKey[entry.start.day], origin: entry.start, start: entry.start, weight: entry.weight,
        end: positionAt(context, entry.start.t + windowSeconds),
        reach: { distance: entry.reach.distance, lat: entry.reach.point.lat, lon: entry.reach.point.lon } };
    });
  }

  // ---- corridors: earlier trips from the place of the last report --------------------------

  // Trip = (leave of a completed stay here, arrival of the next stay); polyline = rendered
  // reports strictly between. Trips longer than min(6 h, 95 % trip duration) are excluded, a
  // trip with a gap inside is dashed; a destination needs MIN_DAYS drawable trips. Independent
  // of the reference time, so computed once per context.
  function buildCorridors(context) {
    if (context.corridors) { return context.corridors; }
    var corridors = [];
    context.corridors = corridors;
    if (context.lastPlaceKey === ELSEWHERE) { return corridors; }
    var stays = context.stays;
    var durations = [];
    for (var k = 0; k + 1 < stays.length; k++) { durations.push(stays[k + 1].start - stays[k].end); }
    var longestTrip = Math.min(MAX_TRIP_SECONDS, durations.length ? quantile(durations, 0.95) : MAX_TRIP_SECONDS);
    var bundles = {}, departures = 0;
    for (var i = 0; i + 1 < stays.length; i++) {
      var stay = stays[i], next = stays[i + 1];
      if (stay.placeKey !== context.lastPlaceKey || stay === context.currentStay) { continue; }
      departures += 1;
      var bundle = bundles[next.placeKey];
      if (!bundle) {
        var place = context.placeByKey[next.placeKey];
        bundle = bundles[next.placeKey] = { destinationKey: next.placeKey, address: place.address, lat: place.lat, lon: place.lon,
          departures: 0, share: 0, trips: [], dashedTrips: [], excluded: 0, sparseTrips: 0 };
      }
      bundle.departures += 1;
      var first = lastIndexAtOrBefore(context.pointTimes, stay.end) + 1;
      var lastIndex = lastIndexAtOrBefore(context.pointTimes, next.start - 1);
      if (next.start - stay.end > longestTrip || lastIndex - first + 1 < 2) { bundle.excluded += 1; continue; }
      var stride = Math.max(1, Math.ceil((lastIndex - first + 1) / MAX_WINDOW_POINTS));
      var polyline = [], spacings = [];
      for (var j = first; j <= lastIndex; j++) {
        if (j > first) { spacings.push(context.points[j].t - context.points[j - 1].t); }
        if ((j - first) % stride === 0 || j === lastIndex) { polyline.push([context.points[j].lat, context.points[j].lon]); }
      }
      if (quantile(spacings, 0.5) > SPARSE_TRIP_SPACING_SECONDS) { bundle.sparseTrips += 1; }
      var interrupted = overlaps(context.gaps, context.gapStarts, stay.end, next.start).length > 0;
      (interrupted ? bundle.dashedTrips : bundle.trips).push(polyline);
    }
    Object.keys(bundles).forEach(function (key) {
      var bundle = bundles[key];
      bundle.share = bundle.departures / departures;
      if (bundle.trips.length + bundle.dashedTrips.length >= MIN_DAYS) { corridors.push(bundle); }
    });
    corridors.sort(function (a, b) { return b.departures - a.departures || a.destinationKey - b.destinationKey; });
    return corridors;
  }

  // ---- transitions, dwell, silence, motion ----------------------------------------------

  function leaveKernel(leaveMinute, lastMinute, referenceMinute, elapsed) {
    if (elapsed >= DAY_SECONDS) { return 1; }
    var span = ((referenceMinute - lastMinute) % 1440 + 1440) % 1440;
    var offset = ((leaveMinute - lastMinute) % 1440 + 1440) % 1440;
    if (offset <= span) { return 1; }
    return timeKernel(Math.min(circularMinutes(leaveMinute, lastMinute), circularMinutes(leaveMinute, referenceMinute)));
  }

  function estimateTransitions(context, elapsed, referenceMinute, weighting) {
    if (context.lastPlaceKey === ELSEWHERE) { return null; }
    var departures = [];
    for (var i = 0; i + 1 < context.stays.length; i++) {
      var stay = context.stays[i];
      if (stay.placeKey !== context.lastPlaceKey || stay === context.currentStay) { continue; }
      var day = context.dayByKey[stay.leaveDay];
      departures.push({
        toKey: context.stays[i + 1].placeKey, leaveMinute: stay.leaveMinute,
        weight: (day ? dayWeight(context, day, weighting) : 1) * leaveKernel(stay.leaveMinute, context.last.minute, referenceMinute, elapsed)
      });
    }
    if (departures.length < MIN_DEPARTURES) { return null; }
    var totalStaySeconds = 0;
    context.places.forEach(function (place) { totalStaySeconds += place.totalSeconds; });
    var weightSum = 0, counts = {}, weights = {};
    departures.forEach(function (departure) {
      weightSum += departure.weight;
      counts[departure.toKey] = (counts[departure.toKey] || 0) + 1;
      weights[departure.toKey] = (weights[departure.toKey] || 0) + departure.weight;
    });
    var rows = context.places.map(function (place) {
      var popularity = totalStaySeconds > 0 ? place.totalSeconds / totalStaySeconds : 1 / context.places.length;
      return { key: place.key, address: place.address, lat: place.lat, lon: place.lon, count: counts[place.key] || 0,
        probability: ((weights[place.key] || 0) + popularity) / (weightSum + 1) };
    }).sort(function (a, b) { return b.probability - a.probability; });
    // Departure window: weighted quartiles of the leave times, only when enough departure
    // weight falls in or near the span from the time of day of the last report to the reference.
    var leaveMinutes = departures.map(function (departure) { return { value: departure.leaveMinute, weight: departure.weight }; });
    var distribution = {};
    rows.forEach(function (row) { distribution[row.key] = row.probability; });
    return {
      departures: departures.length,
      distribution: distribution,
      rows: rows.filter(function (row) { return row.count > 0; }).slice(0, 3),
      leaveWindow: weightSum >= 1
        ? { from: formatMinute(circularQuantile(leaveMinutes, 0.25)), to: formatMinute(circularQuantile(leaveMinutes, 0.75)) }
        : null
    };
  }

  function dwellArrivalSeconds(context) {
    if (context.currentStay) { return context.currentStay.start; }
    var points = context.points, last = context.last;
    var tolerance = context.stopRadius + Math.min(last.accuracy, context.stopRadius);
    var index = points.length - 1;
    while (index > 0 && haversineMetres(points[index - 1].lat, points[index - 1].lon, last.lat, last.lon) <= tolerance) { index -= 1; }
    return points.length ? points[index].t : last.t;
  }

  function survivalShare(stays, dwellSeconds, elapsed) {
    var atLeast = stays.filter(function (stay) { return stay.durationSeconds >= dwellSeconds; });
    var outlasting = atLeast.filter(function (stay) { return stay.durationSeconds >= dwellSeconds + elapsed; });
    return { sampleSize: atLeast.length, probability: atLeast.length ? outlasting.length / atLeast.length : null };
  }

  // Leave hazard: share of the completed visits here that covered the time of day of the
  // last report (one visit per covered day) and lasted at least the elapsed time beyond it;
  // weights w_d * k_day, only the same day class when it holds enough visits. Null below
  // DWELL_PRIOR_STAYS covering visits.
  function leaveHazard(context, completedHere, elapsed, weighting) {
    if (!context.hazardAnchors) {
      context.hazardAnchors = context.days.map(function (day) { return localToUtc(context.offsets, day.key, context.last.minute); });
    }
    var anchors = context.hazardAnchors;
    var lastClass = dayClassOf(context.last.day);
    var visits = [];
    completedHere.forEach(function (stay) {
      for (var i = Math.max(0, lastIndexAtOrBefore(anchors, stay.start - 1) + 1); i < anchors.length && anchors[i] <= stay.end; i++) {
        var day = context.days[i];
        visits.push({ survived: stay.end >= anchors[i] + elapsed, sameClass: day.dayClass === lastClass, weight: dayWeight(context, day, weighting) });
      }
    });
    var dayClass = null;
    if (context.dayClassesEnabled) {
      var sameClass = visits.filter(function (visit) { return visit.sameClass; });
      if (sameClass.length >= DWELL_PRIOR_STAYS) { visits = sameClass; dayClass = lastClass; }
    }
    if (visits.length < DWELL_PRIOR_STAYS) { return null; }
    var survivedWeight = 0, weightSum = 0;
    visits.forEach(function (visit) {
      var weight = visit.weight * (visit.sameClass || !context.dayClassesEnabled ? 1 : OTHER_DAY_CLASS_WEIGHT);
      weightSum += weight;
      if (visit.survived) { survivedWeight += weight; }
    });
    return { probability: survivedWeight / weightSum, visits: visits.length, dayClass: dayClass };
  }

  function estimateDwell(context, elapsed, result, weighting) {
    result.dwellSeconds = context.last.t - dwellArrivalSeconds(context);
    result.dwellIsStay = result.dwellSeconds >= context.stopMinSeconds;
    if (!result.dwellIsStay) { return; }
    var completed = context.stays.filter(function (stay) { return stay !== context.currentStay; });
    var here = completed.filter(function (stay) { return stay.placeKey === context.lastPlaceKey && context.lastPlaceKey !== ELSEWHERE; });
    var elsewhere = completed.filter(function (stay) { return here.indexOf(stay) < 0; });
    var placeShare = survivalShare(here, result.dwellSeconds, elapsed);
    var globalShare = survivalShare(elsewhere, result.dwellSeconds, elapsed);
    var hazard = leaveHazard(context, here, elapsed, weighting);
    if (hazard !== null) {
      result.dwell = {
        probability: hazard.probability, method: "leave-hazard", coveringVisits: hazard.visits, dayClass: hazard.dayClass,
        basis: plural(hazard.visits, "visit") + " (completed stays here covering " + formatMinute(context.last.minute) +
          (hazard.dayClass ? " on " + hazard.dayClass + "s" : "") + ") that lasted at least the elapsed time",
        placeSampleSize: hazard.visits, globalSampleSize: globalShare.sampleSize
      };
      return;
    }
    var probability = null, basis;
    if (placeShare.sampleSize >= DWELL_PRIOR_STAYS) {
      probability = placeShare.probability;
      basis = plural(placeShare.sampleSize, "completed stay") + " here of at least the current dwell time";
    } else if (globalShare.probability !== null) {
      var placeProbability = placeShare.probability === null ? 0 : placeShare.probability;
      probability = (placeShare.sampleSize * placeProbability + DWELL_PRIOR_STAYS * globalShare.probability) / (placeShare.sampleSize + DWELL_PRIOR_STAYS);
      basis = plural(placeShare.sampleSize, "completed stay") + " here mixed with " + plural(globalShare.sampleSize, "stay") + " at other places of at least the current dwell time (the other places count as " + DWELL_PRIOR_STAYS + " stays)";
    } else {
      basis = "no completed stay lasted at least as long as the current one";
    }
    result.dwell = { probability: probability, method: "survival", basis: basis, placeSampleSize: placeShare.sampleSize, globalSampleSize: globalShare.sampleSize };
  }

  // Silence routine: days on which a silence began here within GAP_PROFILE_MINUTES of the
  // time of day of the last report, out of the days the device was here at that time.
  function silenceRoutine(context, startedHere, tolerance) {
    var last = context.last;
    if (context.lastPlaceKey === ELSEWHERE) { return null; }
    var nearTime = startedHere.filter(function (gap) { return gap.start < last.t && circularMinutes(gap.startMinute, last.minute) <= GAP_PROFILE_MINUTES; });
    var gapDays = {};
    nearTime.forEach(function (gap) { gapDays[gap.startDay] = true; });
    var days = Object.keys(gapDays).length;
    if (days < MIN_COMPARABLE_GAPS) { return null; }
    var ofDays = 0;
    context.days.forEach(function (day) {
      var anchor = localToUtc(context.offsets, day.key, last.minute);
      if (gapDays[day.key] || (anchor < last.t && stateAt(context, anchor) === context.lastPlaceKey)) { ofDays += 1; }
    });
    var minutes = nearTime.map(function (gap) { return gap.startMinute; });
    return {
      days: days, ofDays: ofDays, from: formatMinute(circularQuantile(minutes, 0.1)), to: formatMinute(circularQuantile(minutes, 0.9)),
      endedHere: nearTime.filter(function (gap) { return gap.distance <= tolerance; }).length
    };
  }

  function estimateSilence(context, elapsed) {
    var last = context.last;
    var tolerance = context.stopRadius + last.accuracy;
    var gaps = context.gaps;
    if (!gaps.length) { return null; }
    var startedHere = gaps.filter(function (gap) { return haversineMetres(gap.fromLat, gap.fromLon, last.lat, last.lon) <= tolerance; });
    var visitsHere = context.lastPlaceKey === ELSEWHERE ? 0 : completedVisits(context, context.placeByKey[context.lastPlaceKey]).length;
    var silence = {
      gapsHere: startedHere.length,
      gapsPerVisitHere: visitsHere ? startedHere.length / visitsHere : null,
      gapsPerStayAnywhere: context.stays.length ? gaps.length / context.stays.length : null,
      timeProfileShare: gaps.length >= MIN_GAPS_FOR_PROFILE
        ? gaps.filter(function (gap) { return circularMinutes(gap.startMinute, last.minute) <= GAP_PROFILE_MINUTES; }).length / gaps.length : null,
      comparable: null
    };
    silence.routine = silenceRoutine(context, startedHere, tolerance);
    silence.routineSentence = silence.routine === null ? null
      : "Here, a silence began on " + silence.routine.days + " of " + silence.routine.ofDays + " days between " + silence.routine.from + " and " +
        silence.routine.to + " and ended here " + plural(silence.routine.endedHere, "time") + ".";
    var longEnough = function (gap) { return gap.durationSeconds >= elapsed; };
    var comparable = startedHere.filter(longEnough);
    var anyStart = false;
    if (comparable.length < MIN_COMPARABLE_GAPS) { comparable = gaps.filter(longEnough); anyStart = true; }
    if (comparable.length < MIN_COMPARABLE_GAPS) { return silence; }
    var weighted = comparable.map(function (gap) {
      return { gap: gap, weight: Math.max(RECENCY_FLOOR, timeKernel(circularMinutes(gap.startMinute, last.minute))) };
    });
    var weightSum = 0, sameWeight = 0, endPlaces = {};
    weighted.forEach(function (entry) {
      weightSum += entry.weight;
      if (entry.gap.distance <= tolerance) { sameWeight += entry.weight; }
      var endKey = nearestPlaceKey(context.places, entry.gap.toLat, entry.gap.toLon, 2 * context.stopRadius);
      endPlaces[endKey] = (endPlaces[endKey] || 0) + entry.weight;
    });
    var remaining = weighted.map(function (entry) { return { value: entry.gap.durationSeconds - elapsed, weight: entry.weight }; });
    silence.comparable = {
      count: comparable.length,
      anyStart: anyStart,
      sameEndShare: sameWeight / weightSum,
      endPlaces: Object.keys(endPlaces).map(function (key) {
        return { key: Number(key), share: endPlaces[key] / weightSum, address: Number(key) === ELSEWHERE ? null : context.placeByKey[key].address };
      }).sort(function (a, b) { return b.share - a.share; }),
      remainingSeconds: { p50: weightedQuantile(remaining, 0.5), p80: weightedQuantile(remaining, 0.8) }
    };
    return silence;
  }

  function lastCompletedStay(context) {
    var last = context.last;
    return context.stays.filter(function (stay) { return stay !== context.currentStay && stay.end <= last.t; }).pop() || null;
  }

  // Departures from a place: next place key and trip duration per completed stay there.
  function departuresFrom(context, placeKey) {
    var departures = [];
    for (var i = 0; i + 1 < context.stays.length; i++) {
      var stay = context.stays[i];
      if (stay.placeKey === placeKey && stay !== context.currentStay) {
        departures.push({ toKey: context.stays[i + 1].placeKey, seconds: context.stays[i + 1].start - stay.end });
      }
    }
    return departures;
  }

  // Destination matching, only while the motion line is active:
  // posterior(b) ~ prior(b) * exp(kappa cos(heading - bearing(L, b))) * L_dist(b) * L_time(b)
  // over the known places b other than the origin, scaled by the share of departures that
  // ended at a known place.
  function matchDestinations(context, motion) {
    var last = context.last;
    var originStay = lastCompletedStay(context);
    if (!motion || !motion.heading || !(motion.speedKmh > 0) || !originStay) { return null; }
    if (last.accuracy > context.stopRadius || context.places.length < MIN_DESTINATION_PLACES) { return null; }
    var origin = context.placeByKey[originStay.placeKey];
    var speed = motion.speedKmh / 3.6;
    var departures = departuresFrom(context, origin.key);
    var allTrips = [];
    for (var i = 0; i + 1 < context.stays.length; i++) { allTrips.push(context.stays[i + 1].start - context.stays[i].end); }
    var candidates = context.places.filter(function (place) { return place.key !== origin.key; });
    var totalStaySeconds = 0;
    context.places.forEach(function (place) { totalStaySeconds += place.totalSeconds; });
    var prior = {}, priorBasis;
    if (departures.length >= MIN_DEPARTURES) {
      var counts = {};
      departures.forEach(function (departure) { counts[departure.toKey] = (counts[departure.toKey] || 0) + 1; });
      candidates.forEach(function (place) {
        prior[place.key] = ((counts[place.key] || 0) + (totalStaySeconds > 0 ? place.totalSeconds / totalStaySeconds : 0)) / (departures.length + 1);
      });
      priorBasis = plural(departures.length, "earlier departure") + " from the origin";
    } else {
      var reach = allTrips.length ? speed * quantile(allTrips, 0.95) : Infinity;
      var reachable = candidates.filter(function (place) { return haversineMetres(origin.lat, origin.lon, place.lat, place.lon) <= reach; });
      if (!reachable.length) { reachable = candidates; }
      reachable.forEach(function (place) { prior[place.key] = 1 / reachable.length; });
      priorBasis = "uniform over the places within reach (fewer than " + MIN_DEPARTURES + " departures from the origin)";
    }
    var tripSeconds = departures.map(function (departure) { return departure.seconds; });
    var timeLikelihood = function () { return 1; };
    var timeBasis = "trip durations not used (fewer than " + MIN_TRIPS_FOR_TIME + " trips from the origin)";
    if (tripSeconds.length >= MIN_TRIPS_FOR_TIME) {
      var meanSeconds = tripSeconds.reduce(function (sum, value) { return sum + value; }, 0) / tripSeconds.length;
      var spread = Math.sqrt(tripSeconds.reduce(function (sum, value) { return sum + Math.pow(value - meanSeconds, 2); }, 0) / tripSeconds.length);
      var bandwidth = Math.max(TRIP_KERNEL_MIN_SECONDS, 1.06 * spread * Math.pow(tripSeconds.length, -0.2));
      var density = function (x) {
        return tripSeconds.reduce(function (sum, value) { return sum + Math.exp(-0.5 * Math.pow((x - value) / bandwidth, 2)); }, 0);
      };
      var peak = Math.max.apply(null, tripSeconds.map(density));
      timeLikelihood = function (x) { return Math.max(TIME_LIKELIHOOD_FLOOR, density(x) / peak); };
      timeBasis = plural(tripSeconds.length, "earlier trip") + " from the origin";
    }
    var travelledSeconds = last.t - originStay.end;
    var travelledMetres = haversineMetres(origin.lat, origin.lon, last.lat, last.lon);
    var heading = motion.heading.bearing;
    var rows = [], scoreSum = 0;
    candidates.forEach(function (place) {
      if (!prior[place.key]) { return; }
      var distanceAhead = haversineMetres(last.lat, last.lon, place.lat, place.lon);
      var bearing = bearingDegrees(last.lat, last.lon, place.lat, place.lon);
      var turn = Math.abs(heading - bearing) % 360;
      turn = Math.min(turn, 360 - turn);
      var distanceFactor = travelledMetres <= 1.5 * haversineMetres(origin.lat, origin.lon, place.lat, place.lon) + context.stopRadius ? 1 : OVERSHOOT_LIKELIHOOD;
      var score = prior[place.key] * Math.exp(HEADING_CONCENTRATION * Math.cos(turn * Math.PI / 180)) * distanceFactor *
        timeLikelihood(travelledSeconds + distanceAhead / speed);
      scoreSum += score;
      rows.push({ key: place.key, address: place.address, lat: place.lat, lon: place.lon, score: score, distanceAhead: distanceAhead,
        minutesAhead: distanceAhead / speed / 60, bearing: bearing, lateral: turn > LATERAL_DEGREES });
    });
    if (!(scoreSum > 0)) { return null; }
    var unknown = departures.filter(function (departure) { return context.placeByKey[departure.toKey].stays.length < 2; }).length;
    var unknownShare = departures.length ? unknown / departures.length : 0;
    rows.forEach(function (row) { row.probability = (1 - unknownShare) * row.score / scoreSum; delete row.score; });
    rows = rows.filter(function (row) { return row.probability >= 0.01; })
      .sort(function (a, b) { return b.probability - a.probability || a.key - b.key; }).slice(0, CANDIDATE_LIMIT);
    return {
      origin: { key: origin.key, address: origin.address, lat: origin.lat, lon: origin.lon },
      rows: rows, unknownShare: unknownShare, priorBasis: priorBasis, timeBasis: timeBasis,
      travelledSeconds: travelledSeconds, travelledMetres: travelledMetres
    };
  }

  function estimateMotion(context) {
    var last = context.last;
    var recent = context.segments.filter(function (segment) {
      return segment.end <= last.t + 1 && segment.end > last.t - MOTION_LOOKBACK_SECONDS && MOVING_CLASSES[segment.movementClass];
    });
    if (recent.length < MIN_MOTION_SEGMENTS) { return null; }
    var distance = 0, seconds = 0, byClass = {};
    recent.forEach(function (segment) {
      distance += segment.distance;
      seconds += segment.durationSeconds;
      byClass[segment.movementClass] = (byClass[segment.movementClass] || 0) + segment.distance;
    });
    var dominantClass = Object.keys(byClass).sort(function (a, b) { return byClass[b] - byClass[a]; })[0];
    var heading = circularMean(recent.map(function (segment) { return { bearing: segment.bearing, weight: segment.distance }; }));
    // While travelling the last position is no place; the trips that matter start at the
    // place of the last completed stay.
    var origin = lastCompletedStay(context);
    var trips = [], tripsFromOrigin = [];
    for (var i = 0; i + 1 < context.stays.length; i++) {
      var trip = context.stays[i + 1].start - context.stays[i].end;
      trips.push(trip);
      if (origin && context.stays[i].placeKey === origin.placeKey) { tripsFromOrigin.push(trip); }
    }
    var typicalTrip = null;
    if (tripsFromOrigin.length >= MIN_DAYS) {
      typicalTrip = { seconds: quantile(tripsFromOrigin, 0.8), basis: tripsFromOrigin.length + " earlier trips from the last stay's place" };
    } else if (trips.length >= MIN_DAYS) {
      typicalTrip = { seconds: quantile(trips, 0.8), basis: trips.length + " trips between stays anywhere" };
    }
    return {
      speedKmh: seconds > 0 ? distance / seconds * 3.6 : null,
      movementClass: dominantClass,
      heading: heading ? { bearing: heading.bearing, compass: compassName(heading.bearing), concentration: heading.concentration } : null,
      segments: recent.length,
      typicalTrip: typicalTrip
    };
  }

  // ---- dead-reckoning cone -------------------------------------------------------------

  // Only while the motion line is active, up to CONE_MAX_SECONDS and while the share of
  // earlier trips (from the origin, else anywhere) still running is at least CONE_MIN_CONTINUATION.
  function deadReckoningCone(context, motion, elapsed) {
    if (!motion || !(motion.speedKmh > 0) || elapsed > CONE_MAX_SECONDS) { return null; }
    var last = context.last;
    var originStay = lastCompletedStay(context);
    if (!originStay) { return null; }
    var trips = departuresFrom(context, originStay.placeKey).map(function (departure) { return departure.seconds; });
    var tripBasis = plural(trips.length, "earlier trip") + " from the last stay's place";
    if (trips.length < MIN_TRIPS_FOR_TIME) {
      trips = [];
      for (var i = 0; i + 1 < context.stays.length; i++) { trips.push(context.stays[i + 1].start - context.stays[i].end); }
      tripBasis = plural(trips.length, "trip") + " between stays anywhere";
      if (trips.length < CONE_MIN_GLOBAL_TRIPS) { return null; }
    }
    var travelled = last.t - originStay.end;
    var runningAt = function (seconds) { return trips.filter(function (trip) { return trip >= seconds; }).length; };
    var runningNow = runningAt(travelled);
    if (!runningNow) { return null; }
    var continuation = runningAt(travelled + elapsed) / runningNow;
    if (continuation < CONE_MIN_CONTINUATION) { return null; }
    // Segments shorter than twice the accuracy sum (both ends taken as the last accuracy) carry no bearing.
    var recent = context.segments.filter(function (segment) {
      return segment.end <= last.t + 1 && segment.end > last.t - MOTION_LOOKBACK_SECONDS && MOVING_CLASSES[segment.movementClass] &&
        segment.distance >= 4 * last.accuracy;
    });
    var axis = circularMean(recent.map(function (segment) { return { bearing: segment.bearing, weight: segment.distance }; }));
    if (axis === null) { return null; }
    var initialHalfAngle = sectorHalfAngle(axis.concentration);
    return {
      bearing: axis.bearing, compass: compassName(axis.bearing),
      halfAngle: initialHalfAngle + (90 - initialHalfAngle) * Math.min(1, elapsed / quantile(trips, 0.8)),
      radius: motion.speedKmh / 3.6 * elapsed * CONE_RADIUS_FACTOR, continuation: continuation, speedKmh: motion.speedKmh,
      basis: "extrapolation of the last movement: " + Math.round(100 * continuation) + " % of the " + tripBasis + " that lasted this long lasted at least the elapsed time longer"
    };
  }

  // ---- routine change --------------------------------------------------------------------

  function jensenShannon(first, second) {
    var keys = {};
    Object.keys(first).concat(Object.keys(second)).forEach(function (key) { keys[key] = true; });
    var divergence = 0;
    Object.keys(keys).forEach(function (key) {
      var p = first[key] || 0, q = second[key] || 0, m = (p + q) / 2;
      if (p > 0) { divergence += 0.5 * p * Math.log(p / m) / Math.LN2; }
      if (q > 0) { divergence += 0.5 * q * Math.log(q / m) / Math.LN2; }
    });
    return divergence;
  }

  function placeName(context, key) {
    var place = context.placeByKey[key];
    return place.address && place.address.display_name ? place.address.display_name : "known place " + key;
  }

  // Last ROUTINE_RECENT_DAYS calendar days against the days before: per day class and hour of
  // day a distribution over {places, moving, silent}; Jensen-Shannon divergence (base 2) averaged
  // over the bins with data on both sides, weighted by the recent days per bin (so two weekend
  // days do not weigh as much as five weekdays). Independent of the reference, computed once.
  function detectRoutineChange(context) {
    if (context.routineChange !== undefined) { return context.routineChange; }
    context.routineChange = null;
    var recentFrom = context.days.length - ROUTINE_RECENT_DAYS;
    var bins = { recent: {}, earlier: {} }, daysWithData = { recent: 0, earlier: 0 };
    var night = { recent: {}, earlier: {} }, nightHours = { recent: 0, earlier: 0 };
    context.days.forEach(function (day) {
      var side = day.index >= recentFrom ? "recent" : "earlier";
      var dayClass = context.dayClassesEnabled ? day.dayClass : "all";
      var hasData = false;
      for (var hour = 0; hour < 24; hour++) {
        var shares = coverage(context, localToUtc(context.offsets, day.key, hour * 60), localToUtc(context.offsets, day.key, (hour + 1) * 60));
        if (!shares) { continue; }
        hasData = true;
        var binKey = dayClass + "|" + hour;
        var bin = bins[side][binKey] = bins[side][binKey] || { days: 0, shares: {} };
        bin.days += 1;
        var add = function (key, share) { if (share > 0) { bin.shares[key] = (bin.shares[key] || 0) + share; } };
        Object.keys(shares.places).forEach(function (key) { add(key, shares.places[key]); });
        add(String(ELSEWHERE), shares.moving);
        add("silent", shares.silent);
        if (hour < ROUTINE_NIGHT_END_HOUR) {
          nightHours[side] += 1;
          Object.keys(shares.places).forEach(function (key) { night[side][key] = (night[side][key] || 0) + shares.places[key]; });
        }
      }
      if (hasData) { daysWithData[side] += 1; }
    });
    if (daysWithData.earlier < ROUTINE_MIN_EARLIER_DAYS || daysWithData.recent < ROUTINE_MIN_RECENT_DAYS) { return null; }
    var divergenceSum = 0, compared = 0;
    Object.keys(bins.recent).forEach(function (binKey) {
      var recent = bins.recent[binKey], earlier = bins.earlier[binKey];
      if (!earlier) { return; }
      var normalise = function (bin) {
        var distribution = {};
        Object.keys(bin.shares).forEach(function (key) { distribution[key] = bin.shares[key] / bin.days; });
        return distribution;
      };
      divergenceSum += recent.days * jensenShannon(normalise(recent), normalise(earlier));
      compared += recent.days;
    });
    if (!compared) { return null; }
    var jsd = divergenceSum / compared;
    if (jsd <= ROUTINE_CHANGE_JSD) { return null; }
    var indicators = [];
    var recentStart = localToUtc(context.offsets, context.days[recentFrom].key, 0);
    context.places.forEach(function (place) {
      if (place.stays.length >= MIN_DAYS && place.stays.every(function (stay) { return stay.start >= recentStart; })) {
        indicators.push({ kind: "new-place", key: place.key, address: place.address, stays: place.stays.length,
          text: "a new place, " + placeName(context, place.key) + ", with " + place.stays.length + " stays only in the last " + ROUTINE_RECENT_DAYS + " days" });
      }
    });
    var mainPlace = context.places.slice().sort(function (a, b) { return b.totalSeconds - a.totalSeconds; })[0];
    if (mainPlace && nightHours.recent && nightHours.earlier) {
      var earlierShare = (night.earlier[mainPlace.key] || 0) / nightHours.earlier;
      var recentShare = (night.recent[mainPlace.key] || 0) / nightHours.recent;
      if (earlierShare - recentShare > ROUTINE_NIGHT_DROP) {
        indicators.push({ kind: "night-presence-drop", key: mainPlace.key, address: mainPlace.address, earlier: earlierShare, recent: recentShare,
          text: "night presence (00-05) at " + placeName(context, mainPlace.key) + " fell from " + Math.round(100 * earlierShare) + " % to " + Math.round(100 * recentShare) + " %" });
      }
    }
    context.routineChange = {
      kind: "routine-change", jsd: jsd, indicators: indicators, recentDays: daysWithData.recent, earlierDays: daysWithData.earlier,
      text: "Possible routine change: the last " + ROUTINE_RECENT_DAYS + " days differ from the " + daysWithData.earlier +
        " days before (mean Jensen-Shannon divergence " + jsd.toFixed(2) + " per hour of day)" +
        (indicators.length ? "; " + indicators.map(function (indicator) { return indicator.text; }).join("; ") : "") + "." +
        (context.recencyAvailable ? " Try \"recent days count more\"." : "")
    };
    return context.routineChange;
  }

  // ---- ranking, confidence, warnings ----------------------------------------------------

  function buildRanking(context, ladder, referenceClass, elapsed) {
    var basis = ladder.basis;
    var percentAvailable = basis.days >= PERCENT_MIN_DAYS;
    var rows = ladder.keys.map(function (key) {
      var probability = basis.probabilities[key];
      var place = describePlace(context, key, referenceClass);
      place.kind = key === ELSEWHERE ? "elsewhere" : (key === context.lastPlaceKey ? "here" : "place");
      place.probability = probability === null ? 0 : probability;
      place.days = basis.counts[key] || 0;
      place.ofDays = basis.days;
      place.interval = percentAvailable ? wilsonInterval(place.probability, basis.effectiveDays) : null;
      return place;
    }).filter(function (row) {
      return row.kind !== "place" || row.days > 0 || row.probability >= 0.01;
    }).sort(function (a, b) { return b.probability - a.probability; });
    var placeRows = 0;
    rows = rows.filter(function (row) {
      if (row.kind !== "place") { return true; }
      placeRows += 1;
      return placeRows <= CANDIDATE_LIMIT;
    });
    var confidence;
    if (basis.days < PERCENT_MIN_DAYS || basis.level === "S4" || basis.level === "S5") {
      confidence = { level: "weak", reason: basis.days < PERCENT_MIN_DAYS ? "fewer than " + PERCENT_MIN_DAYS + " comparable days" : "routine fallback (" + basis.level + ")" };
    } else if (basis.days >= STRONG_MIN_DAYS && basis.effectiveDays >= STRONG_MIN_EFFECTIVE_DAYS) {
      confidence = { level: "strong", reason: basis.days + " comparable days" };
    } else {
      confidence = { level: "medium", reason: basis.days + " comparable days" };
    }
    // Levels below the basis (earlier in the bottom-up list) contributed through shrinkage.
    var smoothedWith = ladder.levels.slice(0, ladder.levels.indexOf(basis)).filter(function (level) { return level.days > 0; })
      .map(function (level) { return level.days + " days at " + level.level; });
    return {
      rows: rows,
      percentAvailable: percentAvailable,
      confidence: confidence,
      ladder: {
        level: basis.level, label: LEVEL_LABELS[basis.level], days: basis.days, effectiveDays: basis.effectiveDays,
        silentDays: basis.silentDays, smoothedWith: smoothedWith, dayClass: context.dayClassesEnabled ? referenceClass : null,
        levels: ladder.levels.map(function (level) {
          return { level: level.level, days: level.days, effectiveDays: level.effectiveDays, silentDays: level.silentDays, shares: level.shares, probabilities: level.probabilities };
        }).reverse(),
        elapsedSeconds: elapsed
      },
      dayList: basis.samples.map(function (sample) {
        var dominant = null;
        Object.keys(sample.shares).forEach(function (key) { if (dominant === null || sample.shares[key] > sample.shares[dominant]) { dominant = key; } });
        return {
          date: sample.day.key, dayClass: sample.day.dayClass, atLocal: sample.atLocal, atSeconds: sample.atSeconds,
          stateKey: sample.silent ? "silent" : Number(dominant), classMatch: sample.classMatch,
          address: sample.silent || Number(dominant) === ELSEWHERE ? null : context.placeByKey[Number(dominant)].address
        };
      }).sort(function (a, b) { return a.date < b.date ? 1 : -1; })
    };
  }

  function collectWarnings(context, result, referenceOffset) {
    var warnings = [];
    if (result.extrapolated) { warnings.push("The elapsed time is more than half the recorded history: the rings are a linear extrapolation."); }
    if (referenceOffset !== null && referenceOffset !== context.last.offset) {
      warnings.push("Clock change between the last report and the reference time: times of day are compared in the display zone.");
    }
    if (context.last.accuracy > 2 * context.stopRadius) {
      warnings.push("The last position is imprecise (uncertainty " + Math.round(context.last.accuracy) + " m, over twice the stop radius)" +
        (context.placesWithinAccuracy.length > 1 ? ": " + context.placesWithinAccuracy.length + " known places lie within it." : "."));
    }
    if (!context.dayClassesEnabled && context.days.length >= MIN_DAYS) {
      warnings.push("Weekdays and weekends are not separated: fewer than 2 weekend days or 3 weekdays with data.");
    }
    if (result.routineChange) { warnings.push(result.routineChange.text); }
    return warnings;
  }

  // input: prepare() fields plus { referenceUtcSeconds, referenceLocal ("YYYY-MM-DDTHH:MM[:SS]",
  //        optional), dayWeighting ("equal" | "recent"), context (from prepare(), optional) }
  function estimate(input) {
    var context = input.context || prepare(input);
    var last = context.last;
    var elapsed = input.referenceUtcSeconds - last.t;
    var result = {
      elapsedSeconds: elapsed,
      analysedPoints: context.points.length,
      dataSpanSeconds: context.dataSpanSeconds,
      dayCount: context.days.length,
      lastPlace: context.lastPlaceKey === ELSEWHERE ? null : describePlace(context, context.lastPlaceKey, null),
      windowSeconds: null,
      windowCount: 0,
      extrapolated: false,
      radii: null,
      radiiNote: null,
      radiiBasis: null,
      rings: null,
      direction: null,
      tendency: null,
      rose: null,
      corridors: [],
      dwellSeconds: 0,
      dwellIsStay: false,
      dwell: null,
      pointsHere: context.pointsHere,
      visitsHere: context.lastPlaceKey === ELSEWHERE ? 0 : completedVisits(context, context.placeByKey[context.lastPlaceKey]).length,
      ranking: null,
      rankingNote: null,
      percentAvailable: false,
      confidence: null,
      ladder: null,
      dayList: [],
      transitions: null,
      silence: null,
      motion: null,
      destinations: null,
      cone: null,
      recencyAvailable: context.recencyAvailable,
      dayWeighting: "equal",
      dayClassesEnabled: context.dayClassesEnabled,
      warnings: [],
      routineChange: null,
      candidates: []
    };
    if (!(elapsed > 0)) {
      result.radiiNote = "reference time is not after the last known position";
      result.rankingNote = result.radiiNote;
      return result;
    }
    if (context.points.length < 2) {
      result.radiiNote = "fewer than two analysable points";
      result.rankingNote = result.radiiNote;
      return result;
    }
    var referenceLocal = input.referenceLocal ? (input.referenceLocal.length === 16 ? input.referenceLocal + ":00" : input.referenceLocal) : localStamp(context.offsets, input.referenceUtcSeconds);
    var referenceOffset = input.referenceLocal ? utcSeconds(referenceLocal + "Z") - input.referenceUtcSeconds : null;
    var referenceMinute = minuteOfDay(referenceLocal);
    var referenceClass = dayClassOf(dateKeyOf(referenceLocal));
    var weighting = input.dayWeighting === "recent" && context.recencyAvailable ? "recent" : "equal";
    result.dayWeighting = weighting;
    result.referenceLocal = referenceLocal;

    var ladder = buildLadder(context, elapsed, referenceMinute, referenceClass, weighting);
    if (ladder.basis === null) {
      result.rankingNote = "fewer than " + MIN_DAYS + " comparable days at any level (" + ladder.analogDays + " analog days, " + ladder.routineDays + " days with a routine window)";
    } else {
      var ranking = buildRanking(context, ladder, referenceClass, elapsed);
      result.ranking = ranking.rows;
      result.percentAvailable = ranking.percentAvailable;
      result.confidence = ranking.confidence;
      result.ladder = ranking.ladder;
      result.dayList = ranking.dayList;
      result.candidates = ranking.rows.filter(function (row) { return row.kind === "place"; });
    }
    var windowSets = estimateRings(context, elapsed, referenceClass, weighting, result);
    estimateDwell(context, elapsed, result, weighting);
    result.transitions = estimateTransitions(context, elapsed, referenceMinute, weighting);
    result.corridors = buildCorridors(context);
    result.silence = estimateSilence(context, elapsed);
    result.motion = result.dwellIsStay ? null : estimateMotion(context);
    result.destinations = matchDestinations(context, result.motion);
    result.cone = deadReckoningCone(context, result.motion, elapsed);
    result.tendency = estimateTendency(context, windowSets, elapsed, result.motion);
    result.direction = sectorFromTendency(result.tendency);
    result.routineChange = detectRoutineChange(context);
    result.warnings = collectWarnings(context, result, referenceOffset);
    return result;
  }

  G.forecast = {
    estimate: estimate,
    prepare: prepare,
    quantile: quantile,
    weightedQuantile: weightedQuantile,
    circularQuantile: circularQuantile,
    windowSample: windowSample,
    wilsonInterval: wilsonInterval,
    shrink: shrink,
    circularMean: circularMean,
    rayleighP: rayleighP,
    confidenceHalfAngle: confidenceHalfAngle,
    coverage: coverage,
    sectorHalfAngle: sectorHalfAngle,
    compassName: compassName,
    zoneTable: zoneTable,
    localStamp: localStamp,
    localToUtc: localToUtc,
    haversineMetres: haversineMetres,
    bearingDegrees: bearingDegrees,
    ELSEWHERE: ELSEWHERE
  };
})();
