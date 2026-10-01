# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Definitions and limits for the report, worded like the map help (template/map_help.js).

Placeholders in braces are filled from the effective analysis parameters with str.format.
When a definition changes here, change the matching sentence in map_help.js as well;
tests/test_report_html.py checks that the shared phrases stay in both.
"""

from __future__ import annotations

from geosnap.analysis.accuracy import RAYLEIGH_68_TO_95
from geosnap.analysis.encounters import JOINT_MOVEMENT_MIN_MINUTES
from geosnap.analysis.presence_matrix import PRESENCE_MATRIX_ROW_CAP
from geosnap.analysis.segments import CYCLING_MAX_KMH, STATIONARY_MAX_KMH, WALKING_MAX_KMH
from geosnap.analysis.speed_range import SPEED_RANGE_METHOD_TEXT, SPEED_RANGE_PREMISE_TEXT

# Shared with map_help.js word for word: which records carry no accuracy.
RECORDS_WITHOUT_ACCURACY = (
    "Records without an accuracy value (KML, GPX, and CSV or Google records that lack one)"
)

# Window verdict of a case place -> what it says, word for word also in map_help.js. A
# verdict describes the records of a device, never a person.
CASE_PLACE_VERDICT_TEXTS: dict[str, str] = {
    "present": "At least one record of this source lies inside the radius during the window.",
    "possibly present": (
        "No record of this source shows the device inside the radius during the window, but "
        "at least one could: its accuracy circle reaches the radius, it carries no accuracy "
        "value and lies within the assumed accuracy, or it comes from an excluded positioning "
        "method and lies inside the radius or within the radius plus the larger of its "
        "accuracy and the assumed accuracy."
    ),
    "elsewhere": (
        "This source has records during the window, and every one lies farther from the "
        "place than the radius plus its accuracy. This covers only the moments of the "
        "records: the longest span of the window without a record is given with the verdict."
    ),
    "no reports in window": (
        "This source has no record during the window: nothing can be said about where the "
        "device was. This is not absence."
    ),
}

# (term, definition) in the order the report lists them.
METHOD_DEFINITIONS: tuple[tuple[str, str], ...] = (
    (
        "Accepted record",
        "A record (text or CSV line, KML placemark, GPX point, GeoJSON feature, Google record) "
        "with latitude, longitude and timestamp that passed validation; records that matched "
        "the pattern but failed validation are listed with their reason in "
        "rejected_<stamp>.csv.",
    ),
    (
        "Undated record",
        "A record with a valid position but no timestamp (KML placemark or GPX point without "
        "a time, GeoJSON feature without a time property, CSV row whose mapped date cell, "
        "time-of-day cell or both are empty or whose mapping has no time column, Google "
        "frequent place). It is counted per source, listed in points_<stamp>.csv with empty "
        "time columns and drawn on the map "
        "as a hollow ring; every time-based analysis (stays, gaps, segments, encounters, "
        "presence matrix, crystal ball, timeline) uses dated records only. A time cell that "
        "holds unreadable text is a rejected record, not an undated one.",
    ),
    (
        "Distance",
        "Distances between records (steps, segments, gaps, speeds, the Speed tool) are "
        "geodesics on the WGS84 ellipsoid computed with GeographicLib (C. F. F. Karney, "
        "Algorithms for geodesics, J. Geodesy 87:43-55, 2013; accurate to about 15 nm). "
        "Proximity tests and encounter paths (stays, encounters with their joint-movement "
        "paths, case places, presence matrix, places) use the great circle on a sphere of "
        "radius 6 371 km, whose relative error of at most 0.56 % (under 0.6 m per 100 m) lies "
        "far below the position accuracy they compare against.",
    ),
    (
        "Accuracy level",
        "A reported accuracy is a confidence radius whose level the source defines, often "
        "68 % (one standard deviation), sometimes 95 %; the level is chosen per source in the "
        "project setup and recorded in metadata.json, and a source of unknown level is "
        "treated as 68 %. With analysis.accuracy_confidence = p95 (default) every use of an "
        "accuracy as an uncertainty (speed intervals, the stationary rule, speed ranges, case "
        "places, encounters) takes the radius at 95 %: a 68 % radius is multiplied by "
        f"{RAYLEIGH_68_TO_95:.4f} = sqrt(ln 0.05 / ln 0.32), the ratio of the 95 % to the "
        "68 % radius of a circular normal error (Rayleigh distribution). The factor assumes "
        "that the reported value is the radius of a circle holding the true position with "
        "68 % probability, as Android documents its accuracy; a value that is a standard "
        "deviation per axis would need a larger factor (2.45 = sqrt(-2 ln 0.05)). With "
        "reported, radii are used as the source gives them. Displays and exports show the reported "
        "value, and the accuracy limit of {max_accuracy_m} m compares the reported value. "
        "Larger radii weaken lower bounds and elsewhere verdicts and widen possible visits; "
        "a present verdict and same place rest on fixed distances. Speed intervals and "
        "speed range minimums are also given with the radii as reported, and a case place "
        "verdict is given as reported too when it differs.",
    ),
    (
        "Positioning method",
        "How a record's position was determined, when the source says so: gnss (satellite), "
        "wifi, cell (radio cell) or network; unknown otherwise. Records of a method listed "
        "in analysis.excluded_positioning_methods (default: cell, whose radius often "
        "describes a cell sector rather than a position) are left out of speeds, stays and "
        "encounters. They stay on the map, in the exports and in the case place check (as "
        "possible visits only), and every dated record still counts for gaps, since it shows that "
        "the device reported. "
        "The report gives the records per method and how many were left out.",
    ),
    (
        "Speed from the previous record",
        "Per record on the map: the distance, the elapsed time and the speed from the "
        "previous record of the same source, computed over the complete list before the map "
        "was thinned, so thinning cannot distort them. They cover exactly the records this "
        "analysis uses: a record with an accuracy above {max_accuracy_m} m or of an excluded "
        "positioning method is left out, carries no speed figures and shows no speed row, so the "
        "map and this report give "
        "the same numbers for the same pair of records.",
    ),
    (
        "Speed interval",
        "Every speed between two records comes with the interval its data allow: with the "
        "geodesic distance d, the elapsed time t, the uncertainty radii r1 and r2 (see "
        "Accuracy level) and the time resolution s of the source (the coarsest of 1 min, 1 s, 1 ms "
        "and 1 us of which every "
        "timestamp of the source is a whole multiple), the speed lies between "
        "max(0, d - r1 - r2) / (t + s) and (d + r1 + r2) / (t - s), without an upper bound "
        "when t is at most s, and there is no interval when a record reports no accuracy. "
        "Bounds are rounded outwards. The same interval with the radii as reported is given "
        "beside it. " + SPEED_RANGE_PREMISE_TEXT,
    ),
    (
        "Speed range (map)",
        "The Speed tool computes a range of one source between two records, optionally "
        "without records the examiner excluded with a reason; a recorded range is recomputed "
        "by GEOSnap from the map file and written to speed_ranges/ with its own hash chain. "
        + SPEED_RANGE_METHOD_TEXT,
    ),
    (
        "Duplicate",
        "Records of one source with the same latitude, longitude and accuracies, whatever "
        "their timestamps; every record stays in points_<stamp>.csv with the report count of "
        "its position, and the duplicate switch removes repeats from the map only.",
    ),
    (
        "Stay",
        "Places where the device lingered at least {stop_min_minutes} minutes within "
        "{stop_radius_m} m, computed at generation time. A silence of at least "
        "{gap_min_minutes} minutes between two analysed records ends a stay: a stay never "
        "spans a time without records.",
    ),
    (
        "Arrival and departure of a stay",
        "The first and last record of a stay are not the arrival and the departure. The "
        "analysed record before the stay bounds the arrival when it shows the device "
        "elsewhere: it reports an accuracy, lies farther from the stay centre than the stop "
        "radius plus its uncertainty radius, and the step from it to the first record of the "
        "stay takes time and stays below {implausible_speed_kmh} km/h. The device then "
        "arrived between that record and the first record of the stay; likewise it left "
        "between the last record of the stay and the record after it. Such a bound may span "
        "a silence. Otherwise that side is not bounded: when that record could lie at the "
        "place (within the stop radius plus its uncertainty radius), reports no accuracy, is "
        "a position jump or shares the instant of the stay record, or when there is no such "
        "record: the device may have been there already, or still. When the local time of the "
        "first record of the stay or of the record after it occurred twice (clocks going "
        "back), the arrival or departure bound lies later by that much. The stay's own times "
        "are those recorded; such a time may lie that much later as well.",
    ),
    (
        "Gap",
        "The time between consecutive dated records of one source at least "
        "{gap_min_minutes} minutes apart, i.e. no record in between, also none beyond the "
        "accuracy limit.",
    ),
    (
        "Movement class",
        "Each segment between consecutive positions is classed by its speed: stationary "
        "(within the combined accuracy of both points, taken as uncertainty radii, or below "
        f"{STATIONARY_MAX_KMH:g} km/h over less than {{gap_min_minutes}} minutes), "
        f"walking (below {WALKING_MAX_KMH:g} km/h), cycling (below {CYCLING_MAX_KMH:g} km/h), "
        "vehicle, implausible ({implausible_speed_kmh} km/h or faster; for two records at the "
        "same instant: when even the lowest speed of their interval reaches it), unknown "
        "(same instant otherwise, or an average below "
        f"{STATIONARY_MAX_KMH:g} km/h across a silence of at least {{gap_min_minutes}} "
        "minutes, which says nothing about standing still). The distance of a source counts "
        "the other classes only; implausible and unknown steps are given apart. Points with "
        "an accuracy above {max_accuracy_m} m or of an excluded positioning method are "
        "excluded from the analysis.",
    ),
    (
        "Last known position",
        "Per source the newest analysable position (accuracy within {max_accuracy_m} m, "
        "positioning method not excluded).",
    ),
    (
        "Encounter",
        "Places where two sources reported within {encounter_max_minutes} minutes and "
        "{encounter_max_distance_m} m of each other, or within the combined uncertainty radii "
        "of the two records if that is larger, computed at generation time. Every accepted "
        "record takes part, whatever its accuracy, except records of an excluded positioning "
        "method, so two records of ±1000 m each coincide up to 2000 m apart with the radii "
        "as reported, and up to about 3240 m when both are 68 % radii scaled to 95 %. An "
        "encounter whose records never came within {encounter_max_distance_m} m of each other "
        "coincided only through their accuracy: "
        "it is classed within accuracy, not same place, and a joint movement of that kind "
        "is marked (only within accuracy). The smallest time offset is the "
        "smallest time difference of two coinciding records, the closest pair offset the "
        "time difference of the two closest records (of equally close pairs, the one nearest "
        "in time): the records of an encounter can lie up to {encounter_max_minutes} minutes "
        "apart. Both are differences of the recorded times; when a local time of a record "
        "occurred twice (clocks going back), the encounter gives by how much it may lie "
        "later. The offsets are not widened by it, since the records coincide only under "
        "the recorded times.",
    ),
    (
        "Joint movement",
        f"An encounter of at least {JOINT_MOVEMENT_MIN_MINUTES} minutes in which the reports of "
        "both sources stayed within the encounter distance (or their combined accuracy) and "
        "time while both moved: the path through the midpoints of the coinciding records, and the "
        "participating records "
        "of each source by themselves, each cover at least {encounter_joint_movement_min_m} m. "
        "The path length is the length of the plausible steps: steps shorter than the "
        "encounter distance (or than the combined accuracy of the two records, if larger) "
        "count as scatter and add nothing, and a step without elapsed time or at "
        "{implausible_speed_kmh} km/h or faster is a position jump of the source data, or "
        "travel at that speed, such as a flight, and is excluded: its length is given as "
        "excluded length with the number of such steps, and the line on the map is broken "
        "there. The speed limit is that of the movement classes, but those run on the "
        "positions within the accuracy limit, encounters on every accepted record, so the two "
        "can differ. The displacement is the distance from the first to the last midpoint, "
        "jumps included. An encounter may span rest and movement; the class applies to the "
        "whole encounter. Every other encounter is classed same place, or within accuracy "
        "when its records never came within the encounter distance of each other. Joint "
        "movement describes the records of two devices: it does not say that they were in the same "
        "vehicle, nor who carried them.",
    ),
    (
        "Shared place",
        "Places where the stays of at least two sources lie together (stay centres within "
        "{stop_radius_m} m of the running place centre), computed at generation time, with "
        "every visit and whether visits overlapped in time.",
    ),
    (
        "Presence matrix",
        "Places as rows, sources as columns; a cell gives the visits of one source at one "
        "place: their number, the total dwell, the first and the last record. The two kinds of "
        "rows are measured differently: at a shared place or stay place (stay centres grouped "
        "within the matrix tolerance of {matrix_tolerance_m} m; the shared places of the map "
        "keep the stop radius of {stop_radius_m} m) a visit is a stay of at least "
        "{stop_min_minutes} minutes; at a case place a visit is a run of consecutive records "
        "inside the radius "
        "entered by the examiner (see Visit (case place)), so a single record is a visit of 0 "
        "minutes. Counts and dwell of the two kinds must not be compared with each other. "
        "Simultaneous means that visits of different sources overlap in time. A cell without "
        "a visit means that no visit of that source was found, not that the device was never "
        f"at the place. Listed are at most {PRESENCE_MATRIX_ROW_CAP} rows: every case place, "
        "then the shared places and the stay places with the longest dwell; "
        "presence_matrix_<stamp>.csv lists every place and every visit.",
    ),
    (
        "Case place",
        "A location from the case file entered by the examiner: label, position, radius, "
        "optionally a time window (bounds included), an address and a note. A position found "
        'by an address search is marked "geocoded, not verified by the examiner". Every '
        "accepted record of every source takes part in the check, whatever its accuracy; "
        "distances are great-circle distances to the centre of the place.",
    ),
    (
        "Visit (case place)",
        "Consecutive records of one source inside the radius of a case place (bound "
        "included); a record outside the radius or a silence of more than {gap_min_minutes} "
        "minutes ends the visit. A visit says that the device reported from there; its first "
        "and last record are not an arrival or a departure.",
    ),
    (
        "Possible visit",
        "A record outside the radius whose uncertainty radius reaches the radius "
        "(distance minus uncertainty radius within the radius; see Accuracy level), or a "
        "record inside the radius whose positioning method is excluded (see Positioning method): "
        "such a record never makes a visit or present, "
        'and when it is the only reason the verdict is possibly present with the basis "only '
        '<method> records inside". Outside the radius such a record counts as elsewhere only '
        "beyond the radius plus the larger of its accuracy and {max_accuracy_m} m; when only "
        'the assumed {max_accuracy_m} m bring it near, the basis is "<method> records near '
        'the radius; within the assumed {max_accuracy_m} m". Counted and listed apart from '
        "the visits, never merged with them.",
    ),
    (
        "Window verdict",
        "What the records of one source say about the time window of a case place: present "
        "= at least one record inside the radius; possibly present = none inside, but at "
        "least one that could be; elsewhere = records exist and every one lies farther away "
        "than the radius plus its accuracy (the closest is given); no reports in window = "
        "the source is silent, so nothing can be said about where the device was (this is "
        "not absence), and the nearest records before and after the window are given. A "
        "verdict describes the records of a device, never a person. A verdict covers only "
        "the moments of the records: between records nothing is known, so the first and last "
        "record of the window and the longest span of the window without a record (also "
        "before the first and after the last record) are given with it; GEOSnap sets no "
        "threshold for them. When every record inside the radius during the window has an "
        "accuracy circle larger than the radius (a record without an accuracy value counts "
        "with the assumed {max_accuracy_m} m), the verdict present says so and the best "
        "reported accuracy among these records is given; a visit lists the best accuracy of "
        "its records. When the radii as reported give another verdict or basis, it is given "
        "as the verdict as reported.",
    ),
    (
        "Records without accuracy at a case place",
        f"{RECORDS_WITHOUT_ACCURACY} are never a possible visit, except records of an "
        "excluded positioning method inside the radius, which are possible visits whatever "
        "their accuracy. Such a record counts as elsewhere only beyond the radius plus "
        "{max_accuracy_m} m (the accuracy limit of the "
        "analysis, taken as its assumed accuracy); a nearer one makes the verdict possibly "
        'present with the basis "accuracy not reported; within the assumed {max_accuracy_m} m". '
        "Inside the radius such a record counts with the same assumed accuracy: when no record "
        "inside the radius during the window has an accuracy within the radius, the verdict "
        'present carries the basis "inside the radius, but every such record has an accuracy '
        'circle larger than the radius or no accuracy value (assumed {max_accuracy_m} m)".',
    ),
    (
        "Accuracy not reported",
        f'{RECORDS_WITHOUT_ACCURACY} read "Accuracy: not reported"; their accuracy is stored '
        "as 0 m, so they pass the accuracy limit of the analysis. Each record is judged by "
        "itself: in a CSV or Google source the records with an accuracy keep their ± value.",
    ),
    (
        "Map thinning",
        "The map shows at most the configured point limit, shared between the sources; a "
        "thinned source keeps every n-th point (its stride). The CSV exports and the analysis "
        "use every accepted record.",
    ),
    (
        "Validation",
        "The analyses are checked against a synthetic reference dataset with known truth, "
        "run through the full processing in the test suite of the source code: two devices with "
        "stays, walking, cycling and driving legs of exact speed, a joint drive, a meeting, "
        "gaps, a position jump, cell fixes, records without accuracy, and stays next to an "
        "imprecise record and a position jump that must stay unbounded. Stays and their "
        "bounds, gaps, speeds and their intervals, encounters and case place verdicts are "
        "recovered within 1 m, 0.01 km/h and to the second.",
    ),
)

# (topic, limit) for the report section "Limits".
REPORT_LIMITS: tuple[tuple[str, str], ...] = (
    (
        "Accuracy",
        "The ± accuracy is taken from the source as reported; GEOSnap does not check it. The "
        "true position can lie outside the accuracy circle, also outside its 95 % radius. "
        "The accuracy level of a source is the examiner's choice in the project setup; the "
        "scaling from 68 % to 95 % assumes a circular normal error. "
        f"{RECORDS_WITHOUT_ACCURACY} carry no statement about the precision of their positions.",
    ),
    (
        "Device, not person",
        "The records place a device, not a person. Who carried the device at a given time "
        "must be established by other evidence.",
    ),
    (
        "Case places",
        "A verdict describes the records of a device, never a person, and only the records "
        "the source holds: a source can be silent because the device was off, had no "
        "reception or the export is incomplete. The radius is the examiner's choice; a "
        "geocoded position is the first answer of the address search and was not verified.",
    ),
    (
        "Joint movement and presence matrix",
        "Both describe the records of devices, never persons. A joint movement says that the "
        "records of two devices moved while they lay within the encounter distance and time "
        "of each other; records of two devices on the same road, train or bus look the same. "
        "The presence matrix counts only what the sources recorded: a device that was off, "
        "had no reception or reported too rarely for a stay leaves no visit.",
    ),
    (
        "Thinning and duplicates",
        "The map may show a thinned or de-duplicated selection of the points; the numbers in "
        "this report, the CSV exports and the analysis are based on every accepted record.",
    ),
    (
        "OpenStreetMap completeness",
        "OpenStreetMap data are incomplete: the absence of a place in the Overpass results is "
        "no proof that it does not exist. Nominatim addresses are the nearest OpenStreetMap "
        "object to a position at the time of the lookup, not a verified address.",
    ),
    (
        "Time sources",
        "Timestamps are taken from the records as written by the device or the export tool. "
        "GEOSnap converts them to UTC and to the display zone but cannot verify the device "
        "clock or the conversion made by the export tool. Times without an offset are "
        'counted per source in section 2 ("Times without offset") with their handling: '
        "assumed UTC (KML, GPX), read in the zone of the column mapping (CSV), rejected "
        "(Google).",
    ),
    (
        "Not recorded",
        "Views, filters, crystal ball estimates and live place checks made later in the map "
        "are not part of this report; search areas recorded from the map are kept in "
        "search_areas/ with their own hash chain.",
    ),
)
