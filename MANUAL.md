# GEOSnap manual

The full reference for GEOSnap: every input format, every analysis and its rules, the
map tools, the report, the verification and the settings. For an overview, the build
instructions and the license, see [README.md](README.md).

GEOSnap extracts geolocation records from text files, KML, KMZ, GPX, CSV, GeoJSON and
Google location exports, documents every step for later review and exports an
interactive HTML map. It runs in the terminal.

## Input

Place `.txt`, `.log`, `.kml`, `.kmz`, `.gpx`, `.csv`, `.json` or `.geojson` files in the `input/` directory next to
`GEOSnap.exe` (or `GEOSnap.py` when running from source); the directory is
created on first start.
Press R on the file selection screen to pick up files added while GEOSnap is running.
Lines with a location record look like this; other text is ignored:

```
51.43612 ± 19.83 meters | 6.90872 ± 19.83 meters | 2026-09-02 06:15:11 +0000 UTC
```

The time is read by its UTC offset. A zone name that contradicts the offset
(`+0200 UTC`) or the date (`+0100 CEST` in January) rejects the line, with the
reason; a name with several meanings (`PDT`, `EST`) is read by the offset alone.
A line may hold several records; each is read on its own and carries the line number.
Leftover text on a line that still holds both marks of a record (`±` and `|`) is
taken as a record in another layout (fractional seconds, `m`, no zone name) and is
rejected ("record does not match the expected layout …") rather than skipped. Positions within 0.001°
of 0/0 (null island) are rejected.

`original_line`, kept for every record in `points_<stamp>.csv`,
`rejected_<stamp>.csv` and the tooltip, is the raw line for TXT and CSV
sources (cut at 2000 characters). For KML, KMZ, GPX, GeoJSON and Google JSON it
is the record **re-serialised** by GEOSnap (the placemark or track point as one
line of XML with collapsed whitespace, the JSON object in compact form): byte-exact
comparison has to be made against the source file named and hashed in `metadata.json`.

Source files are only ever read. UTF-8 (with or without BOM), UTF-16 and the
fallback encoding from `config.toml` are supported. The encoding is decided
from the first chunk of the file (the first 1 MiB by default,
`read_chunk_bytes`); UTF-16 is only detected when the file starts with a BOM.

Reading and hashing use constant memory, however large the file. Every
accepted record, though, stays in memory until the export, and the GPX/KML
export briefly needs about 2.2 KB per record at its peak: 100,000 records were
measured at roughly 280 MB, so 600,000 records need about 1.3 GB. A file
without line breaks is a single line and is held in memory as a whole. Split
extremely dense files (very many records, or no line breaks at all) into
smaller parts before processing them.

The accepted formats and how each is recognised. Key **H** on the file selection
screen shows the same list and adds, per format, what is read, what is required, what
is refused and a minimal example. Both are generated from the same module,
`geosnap/tui/format_help.py`.

| Suffix | Format | Recognised by |
|--------|--------|---------------|
| `.txt`, `.log` | `text` | the suffix |
| `.kml` | `kml` | a .kml file that is not a ZIP archive |
| `.kmz`, `.kml` | `kmz` | a .kmz file, or a .kml file that starts with the ZIP signature |
| `.gpx` | `gpx` | the suffix |
| `.csv` | `csv` | the suffix |
| `.geojson`, `.json` | `geojson` | "type": "FeatureCollection" or "type": "Feature" in the first 64 KB |
| `.json` | `google-records` | {"locations": [ at the start of the file |
| `.json` | `google-timeline` | {"semanticSegments", "rawSignals" or "userLocationProfile" |
| `.json` | `google-semantic` | {"timelineObjects" at the start of the file |

A `.json` file that is neither a Google export nor GeoJSON is refused with a
reason that names both families; GEOSnap never guesses a layout.

#### What key H shows

The same text as the help screen: one entry per format with what is read, what a file
must have and what is refused. The sections below give the full rules; a minimal
example per format is on the screen.

**`.txt`, `.log` — format `text`**

- **Read:** Every record with a latitude, a longitude and a time, also several in one
  line; other text counts as unrelated. The two ± values are the accuracy in metres; the
  whole line is kept as original_line.
- **Required:** A time with its UTC offset in every record. The line number is the
  record number.
- **Refused:** Coordinates out of range or not finite, null island (within 0.001° of
  0/0), a zone name contradicting the offset (+0200 UTC) or the date (+0100 CEST in
  January), non-existent local times, times before 1990 or after 2099, and record-like
  text (± and |) in another layout. A zone abbreviation with several meanings (PDT, EST)
  is read by the offset alone.

**`.kml` — format `kml`**

- **Read:** Placemarks in any Document/Folder nesting: a Point with TimeStamp/when or
  TimeSpan/begin becomes a record, name and description become its label and note, and
  ExtendedData follows in the note as 'name: value' parts. gx:Track yields one record
  per when/coord pair, a MultiGeometry one record per Point. A placemark without a time
  is read without a timestamp.
- **Required:** The root element must be kml, and a placemark needs a position: a Point,
  a gx:Track with as many gx:coord as when elements, or both. The placemark number is
  the record number.
- **Refused:** A DOCTYPE declaration and malformed XML refuse the whole file (no entity
  expansion). Per placemark: an empty when/begin, coordinates that are missing,
  unparsable, out of range or at null island, unparsable times, times before 1990 or
  after 2099, a TimeSpan with an end but no begin, a MultiGeometry with several Points
  but no time, and tracks whose when and coord counts differ. KML reports no accuracy in
  metres.

**`.kmz`, `.kml` — format `kmz`**

- **Read:** The root-level doc.kml if present, else the first .kml entry in archive
  order, decompressed straight into the KML reader; then every KML rule above. Nothing
  is unpacked to disk, so entry names with absolute or parent paths cannot write
  anywhere.
- **Required:** A ZIP archive holding at least one .kml entry.
- **Refused:** As KML, plus an archive without a .kml entry. Further .kml entries are
  not read; their number and first names are given in a warning. Images and other
  entries are ignored.

**`.gpx` — format `gpx`**

- **Read:** wpt, rtept and trkpt elements in document order, GPX 1.0 or 1.1: name
  becomes the label, desc and cmt the note, hdop stays in the note as a dilution factor,
  and fix (2d, 3d, dgps or pps) marks the position as satellite-based and stays in the
  note. A point without a time is read without a timestamp.
- **Required:** The root element must be gpx in the GPX 1.0 or 1.1 namespace, and a
  point needs lat and lon. The position among the points is the record number.
- **Refused:** A DOCTYPE declaration, malformed XML and a root element in another
  namespace refuse the whole file. Per point: an empty time element, missing or
  unparsable lat/lon, values out of range, null island, times before 1990 or after 2099.
  GPX has no accuracy in metres, so records carry none.

**`.csv` — format `csv`**

- **Read:** The columns you map in the Columns… dialog: latitude and longitude or one
  position column, a date/timestamp and a time of day, the end of a time span, the
  accuracy in metres, a label, a note and the positioning method (GNSS, Wi-Fi, cell or
  network; other values leave it unknown). Every column without a role, and the method
  column, goes into the note as 'header: value'. Of several method columns, the one
  naming known methods is used; if they disagree, you confirm the choice.
- **Required:** A header row (the first non-blank line) and a usable column mapping:
  coordinates are required, a timestamp is optional. Without a time column every row is
  read without a timestamp, and so is a row with any mapped time cell that is empty (the
  date, the time of day or both). Times without an offset need a time zone, and the
  delimiter, the time format and the day/month order are part of the mapping. A radius
  or range column used as accuracy needs your confirmation: a cell sector radius is not
  an accuracy.
- **Refused:** The run cannot start while a CSV source has no usable mapping. Per row: a
  cell that is in no single coordinate notation, a time that is there but unreadable
  (missing is not wrong), a date whose day/month order was never chosen, a date cell
  that contradicts the time-of-day cell, a quoted field that does not close within 50
  lines, a field above 16 MiB and times before 1990 or after 2099. An empty accuracy
  means 'not reported', not a refusal.

**`.geojson`, `.json` — format `geojson`**

- **Read:** Features one by one (RFC 7946): a Point becomes one record, a MultiPoint one
  record per position. Timestamp, accuracy, name and positioning method come from the
  properties by the same synonyms as the CSV mapping; every other property, the method
  included, goes into the note as 'name: value'. A property named radius is taken as
  accuracy and the note says so, since it may be a cell sector radius. A feature without
  a time candidate is read without a timestamp.
- **Required:** A FeatureCollection with a features array, or a single Feature.
  Positions are [longitude, latitude] in WGS 84 by definition of the format. The
  feature's position in the collection is the record number.
- **Refused:** The whole file: not UTF-8, malformed JSON, a top-level value that is no
  object, a type other than FeatureCollection or Feature, a missing features array,
  content after the closing brace, a single feature above 16 MiB. Per feature: a member
  name that occurs twice, several timestamp or accuracy properties, an end time without
  a start time, a date beside a time property that is no plain time name, coordinates
  that are not two numbers, out of range, not finite or at null island, times before
  1990 or after 2099, and times without an offset while the display zone is 'local'.

**`.json` — format `google-records`**

- **Read:** The location history of a Google Takeout Records.json, streamed record by
  record, so files of several GB need no more memory: latitudeE7/longitudeE7, timestamp
  or timestampMs, accuracy in metres, with source and deviceTag in the note. A source of
  GPS, WIFI or CELL also gives the positioning method.
- **Required:** A locations array, and every time must carry an offset. E7 values above
  1,800,000,000 are corrected by 2^32, a documented export quirk.
- **Refused:** The whole file: exactly one comma is expected between records and nothing
  but the closing brace after the array (records read before the error are documented
  but not used). Per record: a member name that occurs twice, a time without an offset,
  times before 1990 or after 2099, coordinates out of range or at null island.

**`.json` — format `google-timeline`**

- **Read:** An on-device Timeline export (2024 and later), read as a whole: timelinePath
  points, rawSignals positions with accuracyMeters and source (GPS, WIFI or CELL gives
  the positioning method), visits and activities, and the frequent places as records
  without a timestamp.
- **Required:** One of the three top-level members, and every time must carry an offset.
- **Refused:** As for Records.json, per entry; files above 256 MiB. Visits and
  activities are Google's own inference: their start and end points say so in the note
  and are not GEOSnap stays by themselves.

**`.json` — format `google-semantic`**

- **Read:** A Takeout Semantic Location History, read as a whole: placeVisit entries,
  activitySegment start and end points, and simplifiedRawPath points with
  accuracyMeters.
- **Required:** A timelineObjects array, and every time must carry an offset.
- **Refused:** As for Records.json, per entry; files above 256 MiB. Visits and
  activities are Google's inference.

### KML input

A `.kml` file (any case of the suffix; for `.kmz` see [KMZ input](#kmz-input)) is read as
KML, streamed placemark by placemark with the same constant-memory hashing as
text files. Placemarks may sit in any Document/Folder nesting; the KML
namespace is optional.

- The root element must be `kml`. An empty `when`/`begin` is rejected
  ("empty timestamp") rather than taken as a record without a timestamp. Elements outside
  placemarks (styles, folders) are released from memory as soon as they are read.
- A placemark with a `Point` and a `TimeStamp/when` (or `TimeSpan/begin`)
  becomes a record: its `name` and `description` (HTML removed, paragraphs
  joined with " · ", at most 500 characters) become label and note, and the
  placemark number takes the place of the line number. Timestamps are ISO 8601
  with `Z`, an offset (at most ±14:00, minutes 00/15/30/45), or no zone (read as
  UTC, warned once per file).
- A `MultiGeometry` with several `Point`s yields one record per point, all with the
  placemark's time, name, description and number; the note says
  `[GEOSnap] MultiGeometry point 2 of 3`. Several points without a time are
  rejected ("MultiGeometry with n Points without a timestamp"): a placemark yields
  at most one record without a timestamp. A `TimeSpan` with an `end` but no
  `begin` is rejected ("TimeSpan with an end but no begin") and never read as a
  record without a timestamp.
- An `ExtendedData` block follows the description in the note as
  `name: value` parts: `Data name="…"/value` and
  `SchemaData/SimpleData name="…"` (untyped `Data` without a `value`, unnamed
  and empty fields are skipped). The same bounds as for CSV columns without a
  role apply (name 40, value 100, whole list 500 characters, then
  `… (+N more fields; full placemark in the source file, placemark n)`). The
  fields of a track placemark go to every point of the track; records without a
  timestamp keep them too.
- `gx:Track` (and the KML 2.3 `Track`) yields one record per `when`/`coord`
  pair; a placemark holding both a track and a `Point` yields both.
- A placemark with a `Point` but no time is a **record without a timestamp**
  (for example a favourite, an address or a Wi-Fi network): a normal record
  that just lacks the time (see "Records without a timestamp").
- `LineString`s (they have no times), empty tracks and placemarks without geometry
  count as unrelated and are logged.
- Rejected, with the reason in `rejected_<stamp>.csv`: missing or unparsable
  coordinates (only ASCII numbers), latitude/longitude out of range,
  non-finite values, coordinates within 0.001° of 0/0 ("null island"),
  unparsable timestamps, and tracks whose `when` and `coord` counts differ.
- KML reports **no accuracy**: such records carry `accuracy_known = false`
  and ± 0, and exports and the summary say "accuracy not reported". The same
  holds for GPX and for Google visits, activities and timeline paths, and for
  CSV rows without an accuracy value. Records without a timestamp can come from KML,
  GPX, GeoJSON and CSV, and from Google frequent places.
- A file with a `DOCTYPE` declaration is refused before any placemark is
  read (no entity expansion), and so is malformed XML; the source is then marked
  `failed`. The encoding comes from a byte-order mark or the XML declaration
  (`fallback_encoding` does not apply to KML).
- Memory: like text input, every accepted record, dated or not, stays in memory
  until the export.

### KMZ input

A `.kmz` file (any case of the suffix), or a `.kml` file that starts with the ZIP
signature, is read as format `kmz`: a ZIP archive whose KML entry is decompressed
as a stream straight into the KML reader. **Nothing is unpacked to disk**, so entry
names with absolute or parent paths cannot write anywhere; all KML rules above
(DOCTYPE refusal included) apply unchanged, and records are counted as placemarks.

- **Entry read.** The root-level `doc.kml` if present, else the first `.kml` entry
  in archive order. Further `.kml` entries are not read; their number and the
  first 20 names are given in a warning (log, summary, `metadata.json`). Images
  and other entries are ignored. Entry names appear quoted (control characters
  escaped) in every message.
- **Hashes.** The source SHA-256 (summary, report, `metadata.json`, `--verify`) is
  that of the `.kmz` file itself. It is computed over the complete file before the
  entry is read, so it covers the whole file even if the source fails later.
  `metadata.json` also records `kmz_entry`: entry `name`, `sha256` of the decompressed KML
  bytes, `bytes_read`, `read_completely`, `other_kml_entries` (first 20) and
  `other_kml_entry_count`; the report shows
  the entry name and hash in the source table. The entry hash equals the SHA-256 of
  the same KML saved as a plain file. The size shown for the source is that of the
  archive; the progress line counts decompressed bytes of the entry.
- **Refused** (source `failed`, reason in the summary): a file that is not a ZIP
  archive, an archive without a `.kml` entry, an encrypted KML entry, a compression
  method Python cannot read, a decompressed size above 4 GiB, a compression ratio
  above 200:1 once more than 64 MiB were produced (zip bomb guard, checked while
  streaming against the entry's compressed size), and a damaged archive (CRC-32 or
  decompression error). If streaming fails, the counts for the placemarks read so
  far are kept; the CRC-32 is only known at the end of the entry, and the chunk that
  reveals the error is not evaluated.

### GPX input

A `.gpx` file (GPX 1.0 or 1.1) is streamed like KML: same pull parser, same
`DOCTYPE` refusal, same hashing of every byte read. `wpt`, `rtept` and `trkpt`
elements are read in document order; their position among them is the record
number.

- With a `time`: a record; `name` becomes the label, `desc` and `cmt` the note.
  Times without a zone are read as UTC (warned once per file).
- Without a `time`: a record without a timestamp (like a KML placemark without
  one). An empty `time` element is rejected ("empty timestamp").
- The root element must be `gpx`, in the GPX 1.0 or 1.1 namespace or without
  one. A `gpx` root in another namespace refuses the whole file (none of its
  points would be read). Only `wpt`/`rtept`/`trkpt` in the GPX 1.0 or 1.1
  namespace, or without a namespace, count; elements inside `<extensions>` and in
  other namespaces are ignored.
- GPX has **no accuracy in metres**: records carry `accuracy_known = false`.
  `hdop` is a dilution factor, not a distance, so it is only kept in the note
  (`hdop: 1.4`).
- `fix` gives the positioning method (see [Analysis](#analysis)): `2d`, `3d`,
  `dgps` and `pps` are satellite fixes (`gnss`); `none` or any other value leaves
  the method unknown. The value stays in the note (`fix: 3d`); `none` adds
  `[GEOSnap] GPX fix: none (no position fix)`.
- Rejected with the reason: missing `lat`/`lon`, unparsable or out-of-range
  coordinates, null island, unparsable times.

### CSV input

A `.csv` file needs a **column mapping**. The setup screen shows it under the
source row, and the **Columns…** dialog sets it. The run cannot start while a
CSV source has no usable mapping; the feedback line names the source and the
problem. The mapping is written to `metadata.json` per source (`csv_mapping`,
with `csv_zone_origin` saying where its zone came from).

#### Columns dialog

The dialog opens **by itself** when the setup screen appears, for the first
CSV whose suggested mapping has a problem or rests on an assumption (a zone
taken from the host system or `config.toml`, an assumed position order, a weakly
supported day/month order). OK confirms it and moves on to the next such CSV;
Escape or Cancel leaves the row unusable until Columns… is used. A CSV that is
recognised cleanly (for instance ISO times with offset) opens no dialog. Columns…
reopens the dialog at any time.

From top to bottom:

- **Preview** of the first rows (up to five; four are in view on a 24-line
  console). A column chosen for a role shows the role in its heading
  (`Breitengrad ↦ latitude`). The arrow keys select a line, which is shown in full
  below the table (the table shortens cells).
- **Latitude, Longitude** or one **Position** column; the **Position order**
  field appears only when a position column is chosen.
- **Date / timestamp** and **Time of day**: the date and the time may sit in
  two columns and are combined; a column holding both goes into Date /
  timestamp alone. **Time span (optional)** takes the end of each record when
  the file has end columns (**End date / timestamp**, **End time of day**).
- **Time format** (see the list below), then **Time zone** for times without
  offset, with a note saying where the zone came from (and "not used" when every
  sampled time carries its own offset: such a file needs no zone confirmation and
  opens no dialog by itself). Type part of a zone name,
  a country or a region (`berl`, `germany`, `new_y`): the list under the field
  filters as you type, Down opens the whole list, Enter or a click picks, Escape
  closes. The value is always an IANA name; anything else is refused as
  "unknown time zone".
- **Day/month with '.', '/', '-'**: one field per separator that the sampled
  dates actually use, only under the Automatic format.
- **Delimiter** with the column count each candidate gives
  (`semicolon (6 columns, detected)`), then **Accuracy (m)**, **Label**, **Note**
  and **Positioning method**.
- **Why the suggestion chose these columns**: one line per role, taken from the
  header text and the sampled cells ("header 'Breitengrad' names the latitude",
  "the 7 sampled cells are timestamps"), and pairs that were left unused
  ("Ost and Nord look like projected coordinates without a zone; not used").
- Docked at the bottom: the **check** of the preview rows (per line the parsed
  record or the rejection reason; at most four rows high, scrollable) with every
  assumption that OK confirms ("Assumed, confirmed by OK: …"), and the buttons.

On small consoles the body scrolls. At 80×24 the latitude and longitude fields
are visible without scrolling, and OK and Cancel are always visible. Tab moves
through the fields from top to bottom at any window height, then to the check area,
OK and Cancel.

- The first non-blank row is the header; blank lines before it are skipped
  (counted as unrelated). Delimiter by `csv.Sniffer` among `,` `;` tab `|`,
  changeable in the dialog; encoding as for text files (BOM, UTF-8,
  `fallback_encoding`), decided on the first 64 KB shown in the dialog and
  used for the whole file (a later undecodable byte becomes U+FFFD and is
  counted in `decode_replacements`; the codec never changes halfway through).
- A column name that occurs more than once (or an empty one) is shown and
  recorded as "name (column n)"; a mapping naming such a column without the
  number is refused.
- Rows are parsed strictly. A quoted field must close within 50 lines, and a
  field may hold at most 16 MiB. A row that breaks either rule, or has other
  quoting errors, is rejected on its own, and reading continues with the next
  line. (GEOSnap raises the csv module's field size limit for this.)
- Zone names must be IANA names (`Europe/Berlin`, `UTC`; case is corrected);
  `localtime` is not accepted.
- Roles: latitude and longitude, or one position column, are required; the
  **timestamp is optional**. A separate time-of-day column, an end date and an
  end time of a span (`timestamp_end`, `time_of_day_end`), accuracy in metres,
  label, note and the positioning method are optional too. An empty accuracy
  means "not reported" for that row.
- The **positioning method** column is read per row: `gps`, `gnss`, `satellite`,
  `sat`, `glonass`, `galileo`, `2d`, `3d`, `dgps` and `pps` read as `gnss`;
  `wifi` and `wlan` as `wifi`; `cell`, `cellid`, `gsm`, `umts`, `lte`, `5g`,
  `tower` and `cellular` as `cell`; `network` as `network` (case, spaces, `-` and
  `_` ignored, so `Cell-ID` is `cell`). Only these exact words count: `fused`,
  `passive` or `GPS (fused)` leave the method unknown. Unlike the other roles, this
  column also stays in the note as `header: value`, so the raw text is always
  visible. Of several columns whose header names a method (`provider` next to
  `source`), the one whose sampled cells name most known methods is suggested
  (the first one on a tie). If another of them names a different method in a
  sampled row, that is an assumption that OK confirms ("columns 'provider' and
  'source' name different positioning methods; 'provider' is used"), and every
  row in which they differ keeps the mapped column's method with the note
  `[GEOSnap] positioning methods disagree: gnss (provider), cell (source)`.
- **No time column at all**: every row is read as a record **without a
  timestamp** (see "Records without a timestamp" below). The dialog lists this
  among the assumptions that OK confirms ("no time column: records are read without
  timestamps; time-based features will be unavailable"), the check shows each
  preview row as `51.1, 7.2, no timestamp`, and the source row says
  `time=none (records without timestamps)`. Without a timestamp column the dialog
  does not ask for a time format or time zone, and the other time roles are
  refused (`column 'Uhrzeit' (time_of_day) needs a timestamp column`).
- A mapped timestamp cell that is **empty** gives a record without a timestamp
  and the note `[GEOSnap] no timestamp in the record`; a cell with text that
  cannot be read is still **rejected** with its reason. Missing is not
  wrong.
- Date and time in two columns are joined per row. If **either** cell is empty,
  the record has no timestamp: both cells empty, an empty date next to a filled time
  or a filled date next to an empty time. The position is real and missing is not
  wrong, so the row becomes a record without a timestamp instead of a rejection.
  (The **end** of a span is handled differently: a half-filled end keeps the record,
  which has its start, and the note names what was missing:
  `[GEOSnap] end time refused: time of day missing`.) A date
  cell that already carries a time must agree with the time cell, otherwise
  the row is rejected as a contradiction. When the two agree (a date cell
  `21.09.2026 14:03` next to `14:03`, or a date cell with its own offset whose
  wall-clock time in the mapping's zone equals the time cell), the row is read
  from the date cell. The time cell may read `9:30`,
  `09:30:00`, `2:03 PM`, `14:03 Uhr` or, hours and minutes joined by a dot,
  `9.30` / `09.30 Uhr` (the older German written form; two parts only, since
  `09.30.15` could be a date with a two-digit year).
- The end of a span is not a separate point: the record keeps the start as
  its time and the note receives `[GEOSnap] end: <local time with offset>`
  (UTC without a zone), `[GEOSnap] end before start` when it precedes the
  start (the row is kept and left to the examiner) or `[GEOSnap] end time refused:
  <reason>` when the end cells cannot be read, and `[GEOSnap] end time
  ambiguous, earlier occurrence used` for an end in the repeated autumn hour;
  empty end cells add nothing.
  Without an end date column an end time is read on the date of the start.
- Coordinates are read **per cell**, so one column may mix notations (see the
  table below). Nothing is guessed: either a cell is read in exactly one notation
  or the row is rejected with the reason.
- Every non-empty cell of a column **without a role** goes into the record's
  description as `header: value`, joined by ` · ` (cells beyond the header as
  `(column n)`). It appears in the map tooltip and in `record_note` of
  `points_<stamp>.csv`. Bounds: a value is cut at 100 characters, a header at
  40, and only whole parts are kept within 500 characters; when some do not
  fit, the list ends with `… (+N more columns; full row in the source file,
  line n)`, so a file with hundreds of columns cannot bloat the map. The
  row itself is kept in `original_line`, cut at 2000 characters; beyond that,
  the source file (hashed in `metadata.json`) is the reference.
- Descriptions and names: what GEOSnap itself writes starts with `[GEOSnap]`
  and comes first (`[GEOSnap] coordinates read as …`, `[GEOSnap] ambiguous
  local time …`). Text from the file can never pass itself off as such: `[GEOSnap]`
  in a cell becomes `(GEOSnap)`, a field or column **named** `GEOSnap` (any case) is
  shown as `(GEOSnap)` too, the dot of the part separator ` · ` becomes `-`,
  and control and format characters (Unicode categories Cc and Cf: NUL, ESC,
  bidi overrides, zero-width characters, BOM) are removed from names and
  descriptions of **all** formats (CSV, KML, GPX, Google) by one shared rule.
  `original_line` keeps the text as it was.
- **Assumptions need confirmation.** A suggested mapping that takes something
  for granted is not applied automatically: the setup screen refuses to start
  ("confirm in Columns…: …") until the dialog has been closed with OK; the dialog
  lists the assumption ("Assumed, confirmed by OK: …"). Read without the dialog,
  such a file is refused. These count as assumptions: the order of a position column
  that neither its header (`lat, lon`, `lonlat`, `Position [lon lat]`, `x,y`, …) nor
  hemisphere letters or self-ordered notations in every sampled row establish;
  `x`/`y` columns (`X coordinate`, `Koordinate Y`, …) taken as longitude/latitude
  (a convention, not proof); a date and a time-of-day column found by their
  content alone; the zone UTC taken from a header that says "UTC"; a day/month
  order supported by fewer than 5 % of the sampled rows that need one; an
  accuracy column whose header contains "radius", "range" or "reichweite"
  ("column 'Radius' used as accuracy: confirm it is a location accuracy, not a
  cell sector radius"), since the radius of a radio cell sector is not an accuracy;
  method columns that disagree (see the positioning method above).
- Time formats: **Automatic** (per row, see below), ISO 8601 (with or without
  offset), Unix seconds, Unix milliseconds, `DD.MM.YYYY HH:MM[:SS]`,
  `MM/DD/YYYY HH:MM[:SS] [AM|PM]`, `YYYY-MM-DD HH:MM[:SS]`, Excel serial date.
  With a fixed format, every row in another form is rejected with the reason.
- A time without offset is read in the chosen zone, and never silently. The
  suggested zone is, in this order: UTC when one of the time headers (timestamp,
  time of day, end date, end time) names UTC as a word (`Zeit (UTC)`,
  `Uhrzeit (UTC)`); the fixed zone of an offset a header names (`Zeit (UTC+2)`
  → `Etc/GMT-2`, `GMT-5` → `Etc/GMT+5`; the IANA `Etc/GMT` names carry the
  inverted sign; an offset with minutes such as `UTC-5:30` has no fixed zone, so
  nothing is suggested, and never the default); otherwise the display zone of
  `config.toml` if it names one; otherwise the zone of the **host system** (see
  Configuration); otherwise none, and one has to be picked. A zone taken from a
  header is an assumption that OK confirms ("the cells carry no offset that proves
  it"), and a zone chosen against a header that names an offset is listed as
  well ("the header 'Zeit (UTC+2)' names the offset UTC+2; the chosen zone
  Europe/Berlin has another offset on some dates"). A zone from `config.toml` or the
  host is an assumption that the dialog lists and OK confirms: the device that
  recorded the data may well have used another zone. `csv_zone_origin` in `metadata.json` records
  `examiner`, `display_setting`, `host_system` or `header` per source. A wall
  time that occurs twice (autumn clock change) is read as the earlier one and
  the record's note says "[GEOSnap] ambiguous local time, earlier occurrence
  used"; one that does not exist (spring) is rejected.
- Columns are also found by the **words of the header**, confirmed by the
  rows of the first 64 KB (at most 500). A pair of headers naming the axes
  (`x`/`y`, `Ost`/`Nord`, `East`/`North`, `Easting`/`Northing`,
  `Rechtswert`/`Hochwert`, `GPS Lon`/`GPS Lat`; paired by their other words,
  `X coordinate` with `Y coordinate`) is suggested as longitude/latitude only
  when every sampled cell reads as degrees on that axis. A pair whose values
  all lie far beyond 180 is reported as **projected coordinates (UTM or
  Gauß-Krüger metres, zone not stated)** and never suggested; when a file
  holds both, the degree pair wins and the metre pair goes into the note like
  any other column. A header with a date word (`Datum`, `date`) or a time word
  (`Uhrzeit`, `Zeit`, `time`, `timestamp`, …) together with a start or end qualifier
  (`Anfang`, `Start`, `Beginn`, `von`, `from`; `Ende`, `End`, `bis`, `to`;
  also compounds such as `Startzeit`, `Enddatum`) gets its role from the
  cells: dates without a time become the timestamp (or end date), times of day
  the time of day (or end time), and full timestamps the timestamp. A header
  written in NFD (macOS) matches just like one in NFC. Without any such header, two
  columns whose cells are all dates and all times of day are suggested for
  confirmation; two columns whose hemisphere letters name their axis, or one
  column in which every sampled cell reads as a position, are found by content
  as before. A header marks scaled integers only when
  `E7`/`E6` is set off from a latitude/longitude name: `latitudeE7`, `lat_e7`,
  `lat E7`, `Latitude (E7)`, `Breite [E6]`; `lat_phone7`, `Breite6`, `late7`
  or `lat_e7_backup` do not. In such a column a non-zero integer below 0.001°
  (`51` under E7) is refused as "looks like plain degrees"; the source row
  line in the setup screen shows `(E7 integers)`.
- The suggested mapping comes from header synonyms in English and German
  (lat/latitude/Breite/Breitengrad, lon/lng/longitude/Länge/Längengrad,
  time/timestamp/date/Date/Time/Datum/Zeit, accuracy/horizontal
  accuracy/Genauigkeit/Radius, name/label/Bezeichnung, note/description/Notiz,
  position/location/Koordinaten, source/provider/method/positioning/positioning
  method/fix/fix type/location source/Ortungsart/Quelle) and from the header
  words above. A positioning method column is suggested only when at least one
  sampled cell names a known method (`source` often names the file or app
  instead). Whether CSV exports of other applications are recognised this
  way is an **untested assumption**: no sample files were available. Check the
  mapping in the dialog: every suggested role, the time format, the day/month order,
  the zone and the delimiter (with the column count per candidate) come with
  the reason they were chosen, built from the header text and the sample
  counts ("header 'Y coordinate' names the y axis and all 161 sampled cells
  read as latitudes").
- The record number is the line on which the row starts (header = line 1). Empty
  rows count as unrelated; rows with too few fields, unparsable values, invalid
  accuracy, out-of-range coordinates or unreadable times are rejected with the
  reason.

#### Coordinate notations

| Notation | Examples |
|---|---|
| Decimal degrees | `51.4361`, `51,4361`, `51.4361°`, `+51.4361`, `-122.4194` |
| with hemisphere letter (N S E W, German O = east), in front or behind; a sign **or** a letter, never both | `N51.4361`, `51.4361 N`, `6.9087° O`, `W 122.4194` |
| Degrees and decimal minutes | `51° 26.166' N`, `N 51 26.166`, `51°26,166′N`, `51d26.166m` |
| Degrees, minutes, seconds | `51°26'09.9"N`, `51 26 09.9 N`, `51° 26′ 9.96″ N`, `51:26:09.9 N`, `51d26m09.9s`, `51°26'09.9''N` |
| Position cell: two angles, separated by `,` `;` `/` or space | `51.4361, 6.9087`, `51,4361; 6,9087`, `51,4361 6,9087`, `N51°26.166' E006°54.522'`, `51°26'09.9"N 6°54'31.3"E`, `(51.4361, 6.9087)` |
| WKT point (longitude first by definition) | `POINT(6.9087 51.4361)` |
| geo URI (RFC 5870, WGS84 only) | `geo:51.4361,6.9087`, `geo:51.4361,6.9087;u=35` (the uncertainty is not taken as accuracy) |
| Map URL naming a marked position | `…maps?q=51.4361,6.9087`, `…?api=1&query=51.4361,6.9087`, `…openstreetmap.org/?mlat=51.4361&mlon=6.9087` |
| UTM | `32U 354641 5700397`, `UTM 32 N 354641 5700397`, `32 South 354641mE 3700612mN` |
| MGRS, 100 km to 1 m squares | `32ULC5464100397`, `32U LC 54641 00397`, `32ULC5400` |
| E7/E6 integer, only under a header ending in `E7`/`E6` | `514361000` under `latitudeE7` |

Understood as the same sign: `°` `º` `˚` (and `d`), `'` `′` `’`, `"` `″` `”`
and `''`, any Unicode space (no-break, thin), the minus sign `−`. A sign stands
directly before its number. Minutes and seconds have at most two integer digits.

Order in a position cell: the column has **one order** (`position_order`,
"Position order" in the dialog: latitude, longitude or longitude, latitude),
suggested as longitude first when the raw header names the longitude first
(`lonlat`, `lon/lat`, `Position [lon lat]`, `x,y`, `Länge/Breite`). Hemisphere
letters name their axes and must agree with the column's order; a row whose
letters say otherwise (`E6.9087 N51.4361` in a latitude-first column) is
refused rather than reinterpreted. Plain pairs read longitude first are marked in the
description (`decimal degrees, longitude first`). WKT, geo URIs, map URLs, UTM
and MGRS carry their own order.

The letter S and seconds: `S` is south only where it cannot be the unit of
seconds. A capital `S` is read after a space (`51 26 09.9 S`) or after the
closing seconds mark (`51°26'09.9"S`); glued to the digits of open seconds
(`51 26 09.9S`) it is refused. A small `s` is south only after a space, and
with seconds only after their closing mark as well (`51°26'09.9" s`);
`51° 26' 09.9s`, `51 26 09.9 s`, `51°26'09.9"s` and `33.8688s` are refused
("seconds or south"). With unit letters (`51d26m09.9s`) the `s` is the unit; a
final capital `S` there is refused unless a sign or hemisphere letter settles it.

Refused, each with its reason in `rejected_<stamp>.csv`: minutes or seconds of
60 or more; a fraction on anything but the last part (`51.5° 26'`); a sign
inside the angle (`51° -26'`) or apart from its number (`- 5`,
`51.4361 - 6.9087`); sign and letter together (`-51.4 N`, `S-51.5`, `+51.4 N`);
a letter of the other axis (`E 6.9` in the latitude column); values beyond
±90 / ±180; NaN, infinity, exponents and texts that look like one (`1E -5`),
underscores, full-width digits; `O` glued to a digit (`6O` could be sixty;
`6 O`, `6° O` and `O6` are read); a position with more than one reading
(`1,2,3`, `6.9 E 51.4`, `51.4361 N 6 54 31`); in a position cell, degrees and
minutes without unit signs or letters (`51 26 6 54`, `5 1.4361, 6.9087`: a
stray space would pass as minutes; in a latitude or longitude cell of its own
`51 26 09.9` is read); a number with grouped thousands (`7.800,2`, `1,100.5`:
never 7.8° N 2° E); one number with a decimal comma in a position cell
(`51,43`); map URLs that only carry the centre of a view
(`/@51.4361,6.9087,15z`, `#map=15/…`), because the view is not where a marker
was, and URLs with a second position (`ll=`, `center=`, `saddr=`, `daddr=`, …);
geo URIs with another `crs`; WKT with Z, SRID or other geometry; integers like
`514361000` under a plain header (never scaled by magnitude); polar MGRS/UPS;
UTM zones 32X, 34X, 36X; an MGRS square that does not occur in its latitude
band. A comma between two digits is a decimal comma as soon as the text
separates anywhere else (`51,43 6,90` = 51.43, 6.90); alone it may separate
(`51.4361,6.9087`). Positions within 0.001° of 0/0 are rejected as before.
No parser can detect a plain pair written longitude first in a column
declared latitude first, UTM eastings and northings swapped near the equator,
or a band letter that fits another valid reading. That is why the examiner
confirms the column order.

UTM letter: a latitude band (C–X) gives the hemisphere and must fit the
northing. `N` is north under both readings. `S` is refused when the northing
fits band S (32–40° N, half a degree of margin), because hemisphere S would put
the same numbers south of the equator; otherwise only the hemisphere reading
is possible. `North`/`South`/`Nord`/`Süd` are always a hemisphere. Eastings
must lie in 100 000–900 000 m; the datum is taken as WGS84 (ETRS89 differs by
less than a metre). Not read: zone-prefixed eastings (`32354641`), other
datums, Gauß-Krüger.

Precision: degrees/minutes/seconds are converted exactly to double precision
(`d + m/60 + s/3600`; round trip below 1e-9°). UTM and MGRS use the Krüger
series in Karney's form; against PROJ 9.8.1 (pyproj 3.8.0) and GeoTrans (mgrs
1.5.4) the largest difference over 3700 reference vectors (worldwide, zone and
band edges, Norway 31V/32V, Svalbard, both hemispheres, 79–80° S, 83–84° N) is
0.00006 mm; GEOSnap itself needs neither library. An MGRS reference names a square;
by the standard its coordinates are the square's **south-west corner** (digits are
truncated), and that corner is taken as the position. The description says so (`MGRS (1 km square, south-west corner, WGS84
assumed)`); the accuracy stays "not reported", because the device did not
report one.

Traceability: the row as read stays in `original_line` of `points_<stamp>.csv`
and `rejected_<stamp>.csv` and in the tooltip. Every coordinate that was not a
plain decimal number adds `[GEOSnap] coordinates read as <notation> from <cell
text>` to the description (`record_note`); plain decimals, which make up most of a
large file, add nothing. The mapping (roles, time format, day/month order, zone) is in
`metadata.json` and the report.

#### Timestamps

**Automatic** reads each cell on its own, only in forms with a single reading:

- year first: `2026-09-21T14:03:11Z`, `2026-09-21 14:03:11,250 +02:00`,
  `2026/09/21 14:03`, `20260921T140311Z`;
- month names, English or German, optional weekday: `21 Sep 2026 14:03:11`,
  `21. September 2026 14:03`, `Sep 21, 2026 2:03:11 PM`,
  `Mon, 21 Sep 2026 14:03:11 +0200`;
- Unix time as digits, the unit by magnitude: seconds, milliseconds or
  microseconds that fall between 1990 and 2100; other numbers are refused
  (pick a Unix format explicitly);
- 12-hour clock with AM/PM, `Uhr` after the time, fractions with point or comma;
- zone: `Z`, `+02:00`, `+0200`, `+02`, `UTC+2`, `GMT+2`, or one of UTC, GMT, WET,
  WEST, CET/MEZ, CEST/MESZ, EET, EEST. Other abbreviations (EST, CST, IST, BST, …)
  have more than one meaning and are refused. `+0200 CEST` is read by its
  offset, and a listed abbreviation must agree with that offset. An abbreviation must
  also fit the date: CET/MEZ/CEST/MESZ are checked against the rules of
  Europe/Berlin, WET/WEST against Europe/Lisbon, EET/EEST against
  Europe/Helsinki (`2026-07-01 14:00 CET` is refused: "write an offset"; in the
  hour repeated in autumn the abbreviation tells which occurrence is meant; a wall
  time skipped in spring is refused). Offsets: at most ±14:00, minutes 00/15/30/45;
- a leading weekday must match the date (`Tue, 21 Sep 2026` is refused);
- day and month as numbers (`21.09.2026`, `03/04/2026`, `3-4-2026`): **one order
  per file and separator mark** (`date_order_dot`, `date_order_slash`,
  `date_order_dash`: day first or month first; "Day/month with '.' / '/' /
  '-'" in the dialog). Dotted dates say nothing about slashed ones: a file with
  `21.09.2026` and `09/05/2026` gets day first for `.` and no order for `/`.
  An order is suggested only if, in the sampled rows with that separator, a
  number above 12 appears in one position and never in the other. Without such
  evidence, or with contradicting evidence, nothing is assumed: the dialog
  refuses OK until the order is chosen, and without the dialog such rows are
  rejected ("day/month order not chosen for dates written with '/'"). Evidence
  from fewer than 5 % of the rows that need an order (one odd `13/01/2026`
  among many `09/0x/2026`) is suggested but has to be confirmed. A row that
  does not fit the order (`04/13/2026` with day first) is rejected and never
  re-read the other way round.

Refused: a date without time of day (midnight is never assumed), two-digit
years, anything left over after the time. Every reader (text, KML, GPX, CSV in
every time format, GeoJSON, Google) refuses a time outside 1990-01-01 to
2099-12-31 ("timestamp outside the plausible range 1990-01-01 to 2099-12-31 (a
default value such as 0001-01-01 or 1970-01-01?)"). For date and time in two columns,
map the date to Timestamp and the time to **Time of day** (suggested for Datum/Uhrzeit,
date/time, Datum Beginn/Uhrzeit Beginn when the cells agree; see the rules for
joined cells above). Excel serial dates (1900 system, rounded to
the millisecond, zone needed) are read only when that format is chosen; it is
suggested when the header says "Excel". The 1904 system cannot be told apart
and is not read. Naive times follow the rule above: the mapped zone, the
ambiguous-hour note, and rejection of times that do not exist.

### Google input

A `.json` file is recognised by the start of its content (first 64 KB).
Only layouts whose field names could be confirmed from public documentation
are supported (sources in `geosnap/extraction/google_reader.py`):

| Format | Recognised by | Records |
|--------|---------------|---------|
| `google-records` (Takeout `Records.json`) | `{"locations": [` | `latitudeE7`/`longitudeE7`, `timestamp` or `timestampMs`, `accuracy` (m); `source` and `deviceTag` in the note |
| `google-timeline` (on-device Timeline export, 2024+) | `{"semanticSegments"`, `"rawSignals"` or `"userLocationProfile"` | `timelinePath` points; `rawSignals` positions with `accuracyMeters`; visits and activities; frequent places as records without a timestamp |
| `google-semantic` (Takeout Semantic Location History) | `{"timelineObjects"` | `placeVisit`, `activitySegment` (start/end), `simplifiedRawPath` points with `accuracyMeters` |

- `Records.json` is streamed record by record (files of several GB); the
  other two are read as a whole and refused if larger than 256 MiB.
- E7 values above 1,800,000,000 in `Records.json` are corrected by 2^32 (a
  documented export quirk).
- Visits and activities are Google's **inference**: their start and end
  points say so in the note ("Google visit (provider inference)") and are
  not GEOSnap stays by themselves. Named fields of a record use the same
  `name: value` form as every other format, one tooltip row each: `source: WIFI`,
  `deviceTag: 123`, `placeId: …`, `probability: …`, `activityType: …`,
  `distance: 3500 m`, `address: …`, `placeConfidence: …`.
- `source` gives the positioning method: `GPS` is `gnss`, `WIFI` `wifi`, `CELL`
  `cell` (any case); every other value leaves the method unknown. The raw value
  stays in the note (`source: WIFI`).
- Times must carry an offset; a time without one is rejected, not assumed to be UTC.
- `Records.json` is parsed strictly: exactly one comma between records and
  nothing but the closing `}` after the array; otherwise the source fails
  (points read before the error are documented but not used).
- A member name that occurs twice in one object (JSON would keep the last value
  silently) rejects the record ("member name occurs twice in one object: 'latitudeE7'");
  in the Timeline and Semantic files it rejects the whole entry (segment, raw signal,
  frequent place, timeline object), and outside any entry the source fails.
- With "Remove duplicates", a visit's end point (same position as its start)
  is collapsed on the **map** only; the analysis keeps both points.
- Other JSON (including the iOS Timeline export, which is a top-level array) is
  refused in the setup; if run anyway, the source fails with a reason that
  names both recognised families (Google exports and GeoJSON). A `.json` file
  that is not a Google export but holds `"type": "FeatureCollection"` or
  `"type": "Feature"` in its first 64 KB is read as GeoJSON (see below).

### GeoJSON input

A `.geojson` file (any case of the suffix), or a `.json` file recognised as
above, is read as format `geojson` following RFC 7946: a `FeatureCollection`
streamed **feature by feature** with the same sliding-buffer technique as
`Records.json` (constant memory beyond the records kept, SHA-256 while reading,
strict UTF-8, a byte-order mark skipped), or a single `Feature`. Members before
and after `features` (`name`, `crs`, `bbox`) are read and ignored: positions are
`[longitude, latitude[, altitude]]` in WGS 84 by definition of the format.

- **Geometry.** A `Point` becomes one record; a `MultiPoint` one record per
  position, each with the same properties and the note part
  `[GEOSnap] MultiPoint 2 of 5`; the `Point`/`MultiPoint` members of a
  `GeometryCollection` are handled the same way. `LineString`, `Polygon`, the other geometries
  and `null` count as unrelated (logged once per geometry type). A position
  that is not two numbers, out of range, non-finite or at null island rejects
  the feature.
- **Timestamp.** From `properties`, by the same header synonyms and words as
  the CSV mapping (case-insensitive, Unicode NFC): `time`, `timestamp`, `date`,
  `datetime`, `zeit`, `zeitstempel`, `zeitpunkt`, `datum`, …, and names
  containing a date or time word (`StartTime`, `Anfangsdatum`). Values are read
  per cell like the CSV time format "automatic" (ISO 8601, month names, Unix
  seconds/milliseconds; day-and-month numbers such as `03/04/2026` are refused
  because no order was chosen). A property holding only a time of day is joined
  with the one date property (`Datum` + `Uhrzeit`) only when its name is a plain
  time name (`time`, `Uhrzeit`, `start_time`; `elapsed_time` is not a clock time,
  and `time_utc`/`local_time` are not joined: a bare date beside one is rejected
  with its name, "date without a joinable time of day: 'time_utc' is not a plain time name").
  Properties qualified as the end (`end_time`) never give the time; they stay in
  the note, as the CSV end columns do. A feature with only an end time is rejected
  ("end time without a start time: …"), just as a KML TimeSpan without a begin is.
  Of several date candidates the one qualified as start (`start_time` next to
  `end_time`) wins; otherwise the feature is rejected with their names (`several
  timestamp properties, none chosen: 'timestamp', 'datetime'`) instead of a guess.
  A feature without any time property is a **record without a timestamp**, like a
  KML placemark without one. The properties not used for the time stay in the note.
- **Times without an offset** need a zone. GEOSnap uses the display zone of
  the run (`timezone.display` in `config.toml`) if it names one; with
  `display = "local"` such features are rejected with the CSV wording
  (`time without offset needs a time zone`). `metadata.json` records the
  handling under `naive_timestamps` (`read in Europe/Berlin (display zone)` or
  `rejected (display zone is local, no zone to read them in)`). There is no
  per-source zone dialog for GeoJSON in this version.
- **Accuracy** in metres from the one property matching the accuracy synonyms
  (`accuracy`, `horizontalaccuracy`, `genauigkeit`, `radius`, `accuracym`;
  numbers or numeric strings); two such properties reject the feature, and none
  means "not reported". A property named `radius` is taken as accuracy and the note
  says `[GEOSnap] radius taken as accuracy`: a cell sector radius then makes the
  record wide rather than falsely exact, and the examiner sees where the value came
  from (unlike CSV, there is no mapping dialog to confirm it).
  **Name** from the one property matching the label synonyms (`name`, `title`,
  `label`, `bezeichnung`, `titel`); with two, both stay in the note.
- **Positioning method** from the properties matching the CSV positioning method
  synonyms (`source`, `provider`, `method`, `fix`, …), read with the same words;
  properties naming different methods leave it unknown and the note names them
  (`[GEOSnap] positioning methods disagree: gnss (provider), cell (source)`).
  These properties stay in the note as well.
- **Note.** Every other property as `name: value` (`null` and empty values
  skipped, booleans and numbers as JSON text), nested objects flattened with
  dot paths (`device.os.name: Q`, at most 8 levels), arrays as compact JSON
  text (`tags: ["a","b"]`), with the CSV bounds (name 40, value 100, whole
  list 500 characters, then `… (+N more properties; full feature in the source
  file, feature n)`). A number the decoder could not take as written is replaced
  by a label, never printed as `Infinity`: an integer of more than 40 digits as
  `<number too long>`, an overflowing float (`1e999`) or `NaN` as
  `<number out of range>`; as a timestamp or accuracy such a value rejects the
  feature with the same wording. A member or property name that occurs twice
  in one object of a feature (`geometry` twice, `n` twice) rejects the feature
  ("member name occurs twice in one object: 'n'"), because JSON decoding would
  silently keep the last value.
- **Record number** is the feature's position in the collection (1 for a
  single `Feature`); `original_line` is the feature's own text **as written**
  in the file with the whitespace outside strings removed (key order, repeated
  keys and number spellings such as `1.50` or `1e999` kept; cut at 2000
  characters). A single-`Feature` document is assembled from the text of its
  members in file order.
- **Refused** (source `failed`, reason in the summary, the bytes read hashed):
  not UTF-8, malformed JSON (the features read before the error are
  documented), a top-level value that is not an object, an object whose `type` is
  neither `FeatureCollection` nor `Feature`, a `FeatureCollection` without a
  `features` array, a top-level member that occurs twice, content after the
  closing brace, a single feature larger
  than 16 MiB. Rejected per feature, with the reason: not an object, a `type`
  other than `Feature`, `properties` that is not an object, and breaches of the
  coordinate and time rules above.

### Sources

A project can combine several files, for example the devices of one person or
of several people. Mark each file with Space on the file selection screen and
press Enter; with nothing marked, Enter (or the file number) opens only the
highlighted file. The setup screen then shows one row per source in list order:
file name and format (text, kml, kmz, gpx, csv, geojson, google-records,
google-timeline or google-semantic), for CSV a line with the column mapping and the **Columns…**
button, a **name** (defaults to the file name
without extension; 1-32 characters, unique within the project), a **type**
(smartphone, computer, laptop, tablet, watch, vehicle, other) and a **colour**:
a name from the eight-colour palette (blue, orange, green, pink, yellow, light blue,
purple, brown; the n-th source gets the n-th colour by default) or a hex value such
as `#ff8800` or `#f80`, with a swatch showing the effective colour.
The name, type and colour identify the source in the map, the exports and the
metadata.

Under the name, type and colour of each source is an **Accuracy level** list:
`unknown (treated as 68 %)` (the default), `68 %` or `95 %`. A reported accuracy
is a confidence radius whose level is defined by the source: often 68 % (one
standard deviation), sometimes 95 %. GEOSnap cannot read the level from the file,
so the examiner sets it when the documentation of the device or export states it.
The level is recorded per source as `accuracy_level` (`unknown`, `68` or `95`) in
`metadata.json` and `analysis.json` and shown in the report; the summary screen
names it in its Analysis section when at least one source has a known level. How the level is used is described under
[Analysis](#analysis). Below the sources, the run settings show the effect in one
line, for example `Accuracy: 95 % (unknown levels treated as 68 %) · cell records
left out of speed, stays and encounters`.

Above the sources, the setup screen has two optional fields, **Case
reference** and **Examiner** (up to 80 printable characters each, trimmed;
both may stay empty). They are written to `metadata.json` (`case`) and the report.

## Output

Every run creates `output/YYYY/MM/YYYYMMDD_HHMMSS_<project>/` with:

| File | Content |
|------|---------|
| `GEOSnap.log` | Complete, non-rotating log of this run (UTC timestamps) |
| `points_<stamp>.csv` | Every accepted record of all sources: first the dated ones in chronological order (ties: source, then line), then the records **without a timestamp** in source and record order, with UTC and local time, source line number, report count per position, `source_id`, `source_label`, the record's own name and description (`record_label`, `record_note`; filled by KML, GPX, CSV with those columns, GeoJSON and Google), `accuracy_known` (per record), `positioning_method` (`gnss`, `wifi`, `cell`, `network`, empty when unknown) and, as last column, the original line. The accuracy columns hold the radius as reported. The columns are the same for both kinds: a record without a timestamp leaves `timestamp_utc`, `timestamp_local`, `local_timezone` and `duplicate_count` empty |
| `rejected_<stamp>.csv` | Records (text or CSV lines, KML placemarks, GPX points, Google records) that matched the record pattern but failed validation, with `source_id`, `source_label` and reason |
| `map_<stamp>.html` | Self-contained Leaflet map: points, accuracy circles, route, heatmap, convex hull, time filter, analysis layers, briefing, time cursor |
| `analysis.json` | Forensic analysis as `{"sources": [...], "per_source": {"1": ...}, "encounters": [...], "shared_places": [...], "case_places": {...} or null, "presence_matrix": {...} or null, "parameters": {...}}`; each `sources` entry carries its `accuracy_level`; each `per_source` entry holds that source's first/last position, `positioning_methods` (dated records per method, `unknown` for none), `points_excluded_positioning_method` and `points_excluded_positioning_method_after_last`, stays (with `arrived_after_*` and `left_before_*`, null when not bounded, and `arrived_by_*`, the upper arrival bound), gaps, segments (with `speed_low_kmh` and `speed_high_kmh`, and `speed_low_reported_kmh` and `speed_high_reported_kmh` with the radii as reported), key figures (`totals`, with `segments_left_out` and `distance_left_out_m`) and addresses with query time; encounters carry `min_time_offset_seconds`, `closest_pair_offset_seconds`, `coincided_within_accuracy_only` and `time_ambiguity_seconds`, case place window verdicts `verdict_as_reported` and `basis_as_reported` (null when equal to the verdict); `parameters` holds every `[analysis]` setting, `accuracy_confidence` and `excluded_positioning_methods` included |
| `stays_<stamp>.csv` | One stay per row, first columns `source_id`, `source_label`; last columns `arrived_after_utc`, `arrived_after_local`, `arrived_by_utc`, `arrived_by_local`, `left_before_utc`, `left_before_local` (`arrived_after_*` and `left_before_*` empty when that side is not bounded, see [Analysis](#analysis)) |
| `gaps_<stamp>.csv` | One gap per row, first columns `source_id`, `source_label` |
| `segments_<stamp>.csv` | One segment per row, first columns `source_id`, `source_label` (then from/to line number, times, geodesic distance, duration, km/h, the speed interval `speed_low_kmh`/`speed_high_kmh` and the same interval with the radii as reported `speed_low_reported_kmh`/`speed_high_reported_kmh` (rounded outwards to 0.1 km/h; empty without an interval or an upper bound, see [Analysis](#analysis)), bearing, movement class) |
| `encounters_<stamp>.csv` | One encounter per row: both sources, start/end, duration, centre, closest distance, hits and first/last line per source, then `movement` (`joint movement`, `same place` or `within accuracy`), `path_length_m`, `displacement_m`, `path_excluded_m`, `implausible_steps`, `min_time_offset_seconds`, `closest_pair_offset_seconds`, `coincided_within_accuracy_only` (`true` or `false`) and `time_ambiguity_seconds` (only with two or more sources) |
| `shared_places_<stamp>.csv` | One visit per row: shared place, its sources, whether visits overlap in time, the visiting source, stay and arrive/leave times (only with two or more sources) |
| `case_places_<stamp>.csv` | Position check of the case places (only with case places): one row per place × source × visit, every possible visit, every record without accuracy near the radius, the closest approach and the window verdict, with `verdict_as_reported` and `basis_as_reported` filled when the radii as reported give another verdict or basis; UTC and local time with offset; see [Case places and position check](#case-places-and-position-check) |
| `presence_matrix_<stamp>.csv` | Presence matrix in long form (only with two or more sources or with case places): one row per place × source × visit, and one row with empty visit columns for a source without a visit at the place; every place, not capped; UTC and local time with offset; see [Presence matrix and joint movement](#presence-matrix-and-joint-movement) |
| `route_<stamp>.gpx` | GPX 1.1, one track per source (named after the source), waypoints for each source's last position and stays, and one waypoint per encounter; every track point carries the record's name in `<name>` and, after the accuracy and record reference, its description in `<desc>` marked as record text: `±12.00 m; line 12; record note: Confidence: high · Speed: 12 km/h` |
| `route_<stamp>.kml` | KML 2.2, one route and point set per source in the source colour, stays, last positions and an Encounters folder; every point's `<description>` holds the time and accuracy, then the record's name and description as `; record name: …; record note: …` (XML-escaped). Everything after `record name:`/`record note:` is text of the record, never GEOSnap's |
| `overpass_<stamp>.json` | Raw Overpass response for the embedded places search (only when Overpass ran) |
| `nominatim_<stamp>.json` | Raw Nominatim responses: `responses` for the reverse-geocoded addresses, `searches` for the address searches of case places entered without coordinates (only when Nominatim ran) |
| `metadata.json` | `case` (case reference, examiner); `case_places` (the places as entered and, when loaded from a file, its name and SHA-256); `sources`: one entry per source (id, label, kind, colour, file name, path, size at read time, SHA-256 and what it covers (`sha256_scope`), `kmz_entry` (KMZ only, else null: the archive entry read and the SHA-256 of its decompressed bytes), encoding, format, `csv_mapping` (CSV only, else null), `naive_timestamps` (count and handling of times without offset), `accuracy_reporting` (`all`, `some` or `none` of the records carry an accuracy), `accuracy_level` (as chosen in the setup), status `read`/`partial`/`skipped`/`failed`, error, map thinning stride, counts including rendered map points); `warnings` (for example a failed source); total counts, effective settings (`settings`, with `accuracy_confidence` and `excluded_positioning_methods`), analysis summary per source (with `positioning_methods` and `points_excluded_positioning_method`) with encounter and shared place counts, online summary (with online services off, `browser_endpoint` and `fallback_endpoint` are null), `notes`, output hashes (every file written before `metadata.json`; not `GEOSnap.log`, `metadata.json` itself, the report or the manifest, which are written later), status |
| `report_<stamp>.html` | Run report for handing over the results: case and run, sources with hashes, method and every effective setting, results per source and across sources, case places, presence matrix, online services, limits, and every project file with size and SHA-256; see [Report and verification](#report-and-verification) |
| `MANIFEST.sha256` | Written last: SHA-256 of every file in the project directory (including the report, except itself, `search_areas/`, `speed_ranges/` and `reopen.log`) in `sha256sum` format |
| `reopen.log` | Only after the finished project was reopened (key P): what was verified and served, appended per reopening; outside the manifest |
| `search_areas/` | Only after "Record search area" in the map's crystal ball: `search_area_<n>_<stamp>.json`, `.gpx`, `.kml`, `.html` per recording, the hash chain `records.jsonl` and its lock file `records.lock`; outside the manifest, see [Search area handover](#search-area-handover) |
| `speed_ranges/` | Only after "Record calculation" in the map's Speed tool: `speed_range_<n>_<stamp>.json` and `.html` per recording, the hash chain `records.jsonl` and `records.lock`; outside the manifest, see [Speed range records](#speed-range-records) |

`output/GEOSnap.log` is the rotating application log.

## Report and verification

At the end of every run GEOSnap writes `report_<stamp>.html` and then
`MANIFEST.sha256`, in this order, after `metadata.json` and after the project log
has been closed:

- **Report.** A self-contained HTML page without scripts or network access, laid
  out for A4 (open it in a browser and print to PDF). Sections: 1 case and run
  (case reference, examiner, project, UTC and local times to the second, local
  as `2026-10-25 02:30:00 +02:00`, version, platform),
  2 sources (file, size, SHA-256 and what it covers, encoding, format, status,
  counters named after the format's records, e.g. "GPX points read", times
  without offset with their handling, which records report an accuracy, the
  accuracy level and whether its radii were scaled to 95 % ("68 %; radii scaled to
  95 % for the analysis (factor 1.6215)"), time span, and for CSV the column
  mapping), 3 method and parameters (definitions worded like the map help, every
  effective setting, and the **Validation** line naming the reference dataset), 4 results per source,
  5 across sources, 6 case places (per place and source: window verdict with its
  meaning, records in the window, closest approach, visits, possible visits),
  7 presence matrix (places × sources, at most 200 rows, with the note that stay rows
  and case place rows are measured differently), 8 online services (endpoints,
  requests, response hashes, errors), 9 limits, 10 files with size and SHA-256 and
  how to verify them. Section 4 gives, per source, the dated records per
  positioning method and how many were left out because of their method, the distance
  over plausible steps, the implausible or unknown steps left out of it and the
  highest plausible speed (see Analysis), and lists the stays with when the device
  arrived and left ("between … and …" or "not bounded"). Section 5 lists every
  encounter with its class (joint movement, same place or within accuracy), path
  length, displacement, smallest time offset and closest pair offset. Section 6
  adds "Verdict with the accuracy as reported" where the radii as reported give
  another verdict or basis.
- **Manifest.** `MANIFEST.sha256` lists every file of the project directory with
  its SHA-256 in `sha256sum` format (`<hash>  <relative path>`, forward slashes,
  sorted, LF line ends), except the manifest itself, `search_areas/` and
  `speed_ranges/` (search areas and speed ranges recorded later from the map;
  each carries its own hash chain in `records.jsonl`) and `reopen.log` (appended to whenever the
  finished project is reopened, see [Reopening a finished project](#reopening-a-finished-project)).
- **Manifest hash.** The SHA-256 of `MANIFEST.sha256` is shown on the summary
  screen together with the verify command and recorded in the application log
  `output/GEOSnap.log`. Write it down in the case file: it anchors all other
  hashes.

The hashes are spread over several places because a file cannot contain its own hash.
`metadata.json` lists the hashes of the files written before it; the report lists
every file except itself and the manifest; the manifest lists every file
including the report and `metadata.json`; the manifest hash is kept outside the
project directory.

To verify a project directory (also a copy, for example after handing it over):

```
GEOSnap.exe --verify <project directory>
```

From source: `python GEOSnap.py --verify <project directory>`.

The check prints one line per file (`OK`, `MISMATCH`, `MISSING`, `EXTRA` for a file
not in the manifest). Symbolic links (and Windows junctions) are never followed: a
link is reported as `EXTRA` (`link, not followed`), and so are empty directories and
special files. Next comes the state of each record hash chain, `search_areas/`
(search areas) and `speed_ranges/` (speed ranges): each line of `records.jsonl`
carries the SHA-256 of the previous line without its line end (the first line 64
zeros) and the SHA-256 of each recorded file; the check prints the record count and
the SHA-256 of the last line. Finally, for source files that still exist at the paths
recorded in `metadata.json`, it reports whether they are unchanged (`NOT AVAILABLE`
when a file is gone, which is not a failure). The sources are checked only when
`metadata.json` itself matches the manifest, since otherwise its recorded paths
cannot be trusted; a recorded network
or device path (`\\host\share`, `//host/share`, `\\.\`, `\\?\`) is not opened and reads
`NOT CHECKED`. Control and format characters in names are printed escaped. The report ends
with one line per chain, `COMPARE WITH THE CASE FILE: search_areas/ 2 record(s), last
line SHA-256 …`. An intact chain does not prove that no record was removed or that
the chain was not rewritten as a whole, so compare the count and the hash with the
values noted in the case file. Exit code 0 means everything matches, 1 at least one
deviation, 2 a call error (no such directory, no manifest). `GEOSnap.exe --version` prints the version; without
arguments the terminal application starts. Without GEOSnap, the manifest can be
checked inside the project directory with `sha256sum -c MANIFEST.sha256`
(GNU coreutils); on Windows, `certutil -hashfile <file> SHA256` prints a file's
SHA-256 to compare with its line in the manifest. The report, the search area
sheet and the speed range sheet print the verify command of the program that wrote them
(`GEOSnap.exe --verify "<directory>"` from the executable,
`python GEOSnap.py --verify "<directory>"` from source).

The check is strict by design: any file in the project directory that the
manifest does not list is reported as `EXTRA` and counts as a deviation. This
includes files that Windows or Office create by themselves, such as `Thumbs.db`,
`desktop.ini` or Excel lock files (`~$points_….csv`) left while a CSV is open in
Excel. Close the programs and remove such files from a copy before checking it,
or note them as explained deviations.

CSV cells contain unmodified text from the source file. Open the CSV files in
Excel via Data → From Text/CSV with all columns as Text; do not double-click
them, because a source line beginning with `=`, `+`, `-` or `@` would otherwise
be evaluated as a formula. This applies to every CSV GEOSnap produces, in the
project directory and exported from the map. `metadata.json` repeats this note
under `notes`.

## Map

By default the map shows online tiles from OpenStreetMap Germany (FOSSGIS,
`tile.openstreetmap.de`): opening `map_<stamp>.html` in a browser makes it fetch
tiles from that provider, which reveals the viewer's IP address and the viewed
area. The **Map style** list in the toolbar switches between the built-in
providers (OpenStreetMap Germany, OpenTopoMap, Esri World Imagery / World Topo)
or no base map. The switch is a view choice made in the browser and is not
recorded; the provider used when the map was generated is written to `metadata.json`. The map's Content
Security Policy allows exactly the tile origins of these providers and nothing
else; each provider's required attribution is shown on the map.

| Key | Provider | Attribution (as shown on the map, plain text) | Usage policy note |
|-----|----------|-------------------------------------------------|--------------------|
| `osm-de` | OpenStreetMap Germany (FOSSGIS) | © OpenStreetMap contributors | Operated by FOSSGIS e.V. under the OpenStreetMap tile usage policy; serves tiles to pages opened from disk. |
| `opentopomap` | OpenTopoMap (topography) | Map data: © OpenStreetMap contributors, SRTM \| Map style: © OpenTopoMap (CC-BY-SA) | Volunteer-run service; heavy or automated use is discouraged. |
| `esri-imagery` | Esri World Imagery (aerial) | Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community | Esri World Imagery under the Esri terms of use; attribution required. |
| `esri-topo` | Esri World Topo | Tiles © Esri — Esri, HERE, Garmin, FAO, NOAA, USGS | Esri World Topographic Map under the Esri terms of use; attribution required. |

OpenStreetMap Standard (`tile.openstreetmap.org`) and the CARTO basemaps are
not offered: the standard server refuses browser requests without a Referer
header, and a `file://` page never sends one, so it shows an "Access blocked"
placeholder for maps opened from disk. Only providers that work without an API
key or Referer are included. `tile_source = "osm"` from earlier versions still
works as an alias for `tile_source = "online"` with `tile_provider = "osm-de"`
and logs a warning.

Set `tile_source = "none"` in `config.toml` for a fully offline map with no base
map at all. To use your own legally obtained offline tile set, set
`tile_source = "local"` and place the tiles as `tiles/{z}/{x}/{y}.png` next to
the executable. `tile_provider` picks the provider the map starts with.

When the map opens, only **Points**, **Graticule** and **Last known position**
are on; every other layer (accuracy circles, route, heatmap, convex hull,
movement classes, arrows, sequence numbers, stays, gaps) can be switched on
in the layer control. The control (top right) groups them:
**Records**, **Movement** (per source), **Between sources**, **Case** and
**Map**. Direction arrows and movement classes depend on the route: they can
only be on while **Route** is on. Switching Route off switches them off, and
their previous state returns with Route. Hover over a point to see the original line,
both timestamps and the number of reports at that position. The From/To fields
(date, hour, minute in the display zone, prefilled with the first and last
report) filter by local time; every change takes effect immediately.

Point tooltips have the same rows for every format, in two groups. First come
GEOSnap's own rows: Source, Name (the record's own name), the rows labelled
**GEOSnap** (a coordinate conversion, an ambiguous local time, a MultiPoint
index: the `[GEOSnap]` parts of the note), Original (the record; shortened only
beyond 1000 characters, then marked `… (N characters in total)`),
position with accuracy, UTC and local time, the record number and the reports
at that position. Then, under the heading **From the record**, comes what the
file says about the record: its description (`record_note` in `points_<stamp>.csv`)
split at ` · ` into parts. A part of the form `name: value` whose name has at
most 40 characters and whose value holds no further `: ` becomes its own row
labelled with the name (CSV columns without a role, KML `ExtendedData` fields,
GeoJSON properties, GPX `hdop: 1.4`); prose (a KML description paragraph, a GPX
comment) keeps the **Description** label on its first row. A record field
named like one of GEOSnap's rows (`UTC`, `Source`, `Name`, `Latitude`, the
record noun, …, any case) is shown as prose `UTC: 1999-01-01` under the record
heading, never as a labelled row, so the two groups cannot be confused; a field
called `GEOSnap` is bracketed as `(GEOSnap)` by the readers. A paragraph of the
record's own description that happens to begin with `Word: ` is shown as a row
labelled `Word` under "From the record". Such a label is part of the record's
text, not a field name of the export; the Original row and the source file show
what the record actually holds. Text records (`.txt`/`.log`) carry no name or
description, so they have no record group. Records without a timestamp
use the same two groups. Everything is written as text, never as markup.

The accuracy row gives the reported value and, where the analysis scales it, the
uncertainty radius it uses (`± 20 m, 95 %: 32 m`); a **Positioning** row names the
positioning method. The tooltips of stays say when the device arrived and left
("arrived between X and Y", "left between A and B", or "not bounded"), and those
of encounters give the smallest time offset and the closest pair offset; the
timeline tooltips carry the same. The map's Help window describes every tooltip
row in detail.

A tooltip has a maximum width (at most 520 px, less in a narrow window):
labels and values wrap, text without spaces (hashes, URLs) breaks anywhere, and a
tooltip taller than 60 % of the window, 420 px or the map area scrolls vertically.
Near the top or bottom edge it is moved inside the map. It opens at the pointer and
stays put when the pointer leaves the marker, so the pointer can move onto it; the mouse
wheel then scrolls the tooltip without zooming the map. It closes about 400 ms after
the pointer has left both the marker and the tooltip, and its text can be selected
and copied. Only one such tooltip is open at a time. The tooltips of stays, segments,
encounters, case places and last known positions behave the same way.

Times and daylight saving: every order, comparison and duration is based on UTC.
Local times appear as wall-clock time in the display zone; where a wall-clock
time occurs twice (the hour repeated when clocks go back), lists and tooltips
add the offset, e.g. `02:30 +02:00` and `02:30 +01:00`. In From/To, From means
the earlier and To the later occurrence of a repeated time, so both are
covered; a time in the hour skipped when clocks go forward means the next valid
time (02:30 on 29 March 2026 in Europe/Berlin means 03:00). The map converts
between wall-clock time and UTC only with the zone's clock changes recorded when
the map was generated (`zone_transitions` in the map payload: from two days before the
first report to one year after generation), never with the zone of the
viewing computer. In `analysis.json` every `*_local` field (and `local` of
`first`/`last`) has a sibling `*_offset` (`offset`) in minutes east of UTC; the
CSV files write local times as ISO 8601 with offset
(`2026-10-25T02:30:00+02:00`).

When more points exist than `point_limit`, the map shows a deterministic subset
and says so; the CSV is always complete. Each source is thinned separately: it
keeps its first and last point and gets a share of the limit in proportion to its
number of points, so a small source is never crowded out by a large one.
`metadata.json` records the stride per source and the largest stride overall.

## Records without a timestamp

A position without a time is a normal record that just lacks the time. Such a
record comes from a KML placemark or a GPX point without a time, a GeoJSON
feature without a time property, a Google frequent place, a CSV row whose
mapped time cell is empty, or every row of a CSV mapped **without a time
column**. Text in a time field that cannot be read is a different case: that row
is **rejected** with its reason (missing is not wrong).

How these records are handled:

- **Counted** as `undated_records` in `metadata.json`, both per source and in the
  run's totals, and as "Records without timestamp" in the report's **Records**
  section and in the run summary
  ("Records without timestamp: n"). The report's method text defines the term
  ("Undated record").
- **Exported** in `points_<stamp>.csv` with the same columns as the dated
  records and the time columns empty (`timestamp_utc`, `timestamp_local`,
  `local_timezone`) as well as `duplicate_count`; reports per position are
  counted over dated records only.
- **Drawn** on the map in the **Points** layer as a hollow ring in the source
  colour (no time fill), thinned per source by the same rule as the dated
  points. The tooltip has no UTC, Local or duplicate rows and carries
  `Timestamp: none in the record`. The record gets no sequence number, because
  sequence numbers count the dated records of a source. The legend names them ("Record
  without timestamp (hollow ring)"). Clicking one offers Check surroundings
  like any other position.
- **Not** part of anything time-based: stays, gaps, segments, encounters,
  shared places, the presence matrix, the crystal ball, the timeline, the time
  filter and the time cursor all use dated records only. The time filter
  and the cursor therefore never hide a record without a timestamp, but the
  accuracy filter does apply to one that reports an accuracy.
- **Included** in the map's own CSV, GeoJSON and GPX exports: the CSV row has
  empty time cells, the GeoJSON feature `timestamp_utc: null`, and the GPX
  export writes them as `<wpt>` waypoints (a track point without a time says
  nothing about the order of a track).

The ring's tooltip carries everything known about such a record, and the Points
layer shows them by default.

### Capabilities and what they gate

The map payload carries `capabilities`, computed from the run's own facts:

| Key | Meaning |
|-----|---------|
| `dated_points`, `undated_points` | Accepted records with and without a timestamp |
| `dated_share` | `dated_points` divided by all accepted records (0 to 1) |
| `days_with_data` | Distinct local dates (display zone) with dated records |
| `sources` | Sources in the project |
| `sources_with_route` | Sources with at least two dated records |
| `encounters`, `matrix_rows`, `case_places`, `stays`, `gaps`, `segments` | Results the analysis found |

Per source the payload adds `dated_records`, `undated_total`,
`undated_rendered`, `undated_thinning_stride` and `data_days`, so the browser
recomputes the same numbers for the sources that are shown: hiding a source
updates what the map offers. A control the numbers do not support is disabled
and greyed out, keeps its place and explains why in its tooltip; it never
disappears, so the examiner can see what exists. Gating is a view decision and never changes
recorded data.

Every dated record in the map payload also describes how it relates to the
previous analysed record of its source: `spd` (km/h, null for the first), `dpm`
(geodesic metres), `dts` (seconds), `cum` (path metres from the first record), `seq`
(its position in the source's list of analysed records), `spd_lo` and `spd_hi` (the
speed interval in km/h, null without one or without an upper bound), `cmb` (the
cumulative minimum path in metres: each step shortened by the accuracy radii of its
two records; see the Speed tool under [Map tools](#map-tools)) and `cls` (the movement class of the step).
`spd_lo_rep`, `spd_hi_rep` and `cmb_rep` are the same figures with the radii as
reported, `asc` is the factor from the reported radius to the uncertainty radius
the bounds use (see [Analysis](#analysis)) and `pm` the positioning method (null
when unknown).
Each source entry carries `time_resolution_s`, the time resolution its intervals
assume. These figures are computed in Python over the **complete, unthinned** list,
so thinning the map cannot distort a speed, and a range can be summed exactly even
between two points that are far apart on the map.

The speed figures cover exactly the records the analysis uses: a record whose
accuracy exceeds `max_accuracy_m`, or whose positioning method is listed in
`excluded_positioning_methods`, is excluded from the analysis, so it carries no
figures at all (all these fields are empty) and its tooltip shows no speed row
(a record beyond the accuracy limit says "excluded from analysis (accuracy)"). The map and the report
therefore give the same distance, duration and speed for the same pair of
records, to the last digit. Records of a `gx:Track` placemark share one record
number, so the figures are matched to each point directly, never by that number.

## Analysis

Every run analyses the accepted **dated** points of each source separately, in
chronological order, with the parameters from `[analysis]` in `config.toml`
(records without a timestamp are not included, see the section above):

| Key | Meaning |
|-----|---------|
| `stop_radius_m` | Points within this radius of the running centre form a stay |
| `stop_min_minutes` | Minimum duration for a candidate to become a stay |
| `gap_min_minutes` | Time without any record that counts as a gap; a silence this long also ends a stay, and a slow step across it is `unknown` |
| `max_accuracy_m` | Points with a larger ± value stay on the map but are excluded from stays and segments (gaps count every dated record); compares the radius as reported |
| `implausible_speed_kmh` | Segments at or above this speed are flagged `implausible` (for two records at the same instant: when even the lowest speed of their interval reaches it) |
| `encounter_max_minutes` | Two sources' records at most this far apart in time can form an encounter; hits closer than this in time merge into one encounter (default 10, 1 to 1440) |
| `encounter_max_distance_m` | Two sources' records at most this far apart (or within their combined ± values, if larger) can form an encounter (default 100, 1 to 50000) |
| `encounter_joint_movement_min_m` | An encounter of at least 5 minutes whose records moved at least this far is classed as a joint movement, otherwise as same place or within accuracy (default 500, 100 to 100000) |
| `matrix_tolerance_m` | Stay centres within this distance form one row of the presence matrix (default 50, 0 to 3000); the shared places keep `stop_radius_m`, and the map's Matrix window can try other values |
| `accuracy_confidence` | `p95` (default): accuracy radii of sources at 68 % or of unknown level are scaled to 95 % wherever an accuracy is used as an uncertainty; `reported`: radii are used as the source gives them |
| `excluded_positioning_methods` | Positioning methods left out of speeds, stays and encounters (default `["cell"]`; any of `gnss`, `wifi`, `cell`, `network`; `[]` keeps all) |

Points excluded by `max_accuracy_m` are marked "excluded from analysis
(accuracy)" in the map tooltip; the map still shows them.

- **Accuracy level.** A reported accuracy is a confidence radius whose level is
  defined by the source: often 68 % (one standard deviation), sometimes 95 %. The level
  is chosen per source in the project setup (see [Sources](#sources)); a source of
  unknown level is treated as 68 %. With `accuracy_confidence = "p95"` (default)
  every use of an accuracy as an uncertainty takes the radius at 95 %: a 68 %
  radius is multiplied by 1.6215 = sqrt(ln 0.05 / ln 0.32), the ratio of the 95 % to the 68 % radius of a
  circular normal position error (Rayleigh distribution; the scaling assumes such
  an error). The factor assumes that the reported value is the radius of a circle
  holding the true position with 68 % probability, as Android documents its
  accuracy; a value that is a standard deviation per axis would need a larger
  factor (2.45 = sqrt(−2 ln 0.05)). A 95 % radius, and every radius with `accuracy_confidence =
  "reported"`, keeps the factor 1. The result is the **uncertainty radius**. It
  is used by the speed intervals and the stationary rule of the segments, the
  minimums of the Speed tool and the speed range records, the case place check
  (possible visits, the margin of `elsewhere`, the basis of `present`), the
  widening of encounters, the thresholds of the crystal ball and the extent of
  Check surroundings from a point or a last known position. Everywhere else the
  **reported** radius is used unchanged: the accuracy shown in tooltips (with
  the uncertainty radius beside it), accuracy circles, the accuracy filter, the exports of the records, the stays' mean accuracy and the
  `max_accuracy_m` limit. A record without an accuracy value stays without one
  (± 0, "not reported").
- **Direction of caution.** Larger radii weaken lower bounds (the lower speed
  bound, the minimum average speed) and `elsewhere` verdicts, and widen claims of
  possibility (`within accuracy`, `possibly present`, possible visits), which are
  labelled as such. Claims resting on fixed distances do not change: `present`
  (a record inside the radius) and `same place` (records within
  `encounter_max_distance_m`).
- **As reported.** Where numbers are shown, the figures with the radii as
  reported appear next to the primary ones: the speed interval of a segment
  (`speed_low_reported_kmh`, `speed_high_reported_kmh` in `segments_<stamp>.csv`
  and `analysis.json`, `spd_lo_rep`, `spd_hi_rep` in the map payload), and the
  minimum and stepwise minimum average speed in the Speed tool and in speed range records
  (`minimum_average_reported_kmh`, `stepwise_minimum_average_reported_kmh`). A
  case place verdict is given as reported only when it differs
  (`verdict_as_reported`, `basis_as_reported`).
- **Positioning method.** How a record's position was determined, if the source
  says so: `gnss` (satellite), `wifi`, `cell` (radio cell) or `network`; otherwise
  unknown. It comes from a CSV column mapped as positioning method, Google's
  `source`, a GeoJSON property of the same names and GPX `fix` (see the input
  sections); text and KML records carry none. A value that names no known method
  leaves it unknown and stays in the note. Records of a method listed in
  `excluded_positioning_methods` (default `cell`, whose radius often describes a
  cell sector rather than a position) are left out of speeds, segments, stays, the
  first and last known position and encounters. They stay on the map, in the
  exports and in the case place check (as possible visits, never `present`), and
  every dated record still counts for gaps, since it shows that the device was
  reporting. The method is shown in the point tooltip and the `positioning_method`
  column of `points_<stamp>.csv`; the report and the map's Briefing give the dated
  records per method and how many were left out. Each dated record is counted
  once: a record beyond `max_accuracy_m` counts as excluded for accuracy regardless
  of its method, so analysed, excluded for accuracy and excluded for positioning
  method add up to the total (`points_analysed`, `points_excluded_accuracy`,
  `points_excluded_positioning_method` in `analysis.json`). Records of an excluded
  method newer than the last known position are mentioned next to it ("Newer records
  left out" in the report and the Briefing,
  `points_excluded_positioning_method_after_last`).
- **Stays** are found by sequential clustering: a candidate grows while each
  next point stays within `stop_radius_m` of the running mean of the points
  seen so far; a point outside ends the candidate and starts a new one.
  Candidates reaching `stop_min_minutes` become stays. A silence of at least
  `gap_min_minutes` between two analysed points also ends the candidate: a stay
  never spans a period without records, since nothing is known about where the
  device was during it. Two points at the same place six hours apart with nothing in
  between are therefore two stays (or none), not one six-hour stay.
- **Arrival and departure of a stay.** The first and last record of a stay are
  not the arrival and the departure. The analysed record before (after) the stay
  bounds the arrival (departure) only if it shows the device was really elsewhere:
  it reports an accuracy, lies farther from the stay centre than `stop_radius_m`
  plus its uncertainty radius, does not share the instant of the stay's first (last)
  record, and the step between the two stays below `implausible_speed_kmh`. The
  device then arrived between that record (`arrived_after_utc`) and the first
  record of the stay (`arrived_by_utc`), or left between the last record of the
  stay and that record (`left_before_utc`). Such a bound may span a silence: a
  distant, plausible record before or after a long silence still shows the device
  was elsewhere. If the local time of the first record of the stay, or of the record
  after it, occurred twice (clocks going back, read as the earlier occurrence), the
  arrival (`arrived_by_utc`) or departure bound is moved later by that ambiguity;
  otherwise `arrived_by_utc` equals `arrive_utc`. The record before needs no
  widening, since its true instant can only be later. When there is no such
  record, or it could lie at the place (within `stop_radius_m` plus its
  uncertainty radius), reports no accuracy, is a position jump or shares the
  instant, that side is **not bounded**: the device may already have been there,
  or may still have been there. The bounds are shown as
  "arrived between X and Y" / "left between A and B" in the report, the GPX and
  KML stay descriptions and the map (the timeline adds the date when a bound falls
  on another day than the stay edge); the stays CSV and `analysis.json` carry the
  bounds (`arrived_after_*`, `arrived_by_*`, `left_before_*`; `arrived_after_*`
  and `left_before_*` empty or null when not bounded).
- **Gaps** are two consecutive dated records of one source that lie
  `gap_min_minutes` or more apart, with no record in between. They take every dated
  record into account, including those beyond `max_accuracy_m`: such a record shows
  that the device was reporting.
- **Segments** connect consecutive analysed points and are classified as an
  estimate: `stationary` (within the combined uncertainty radii of its two points, or
  below 1 km/h over less than `gap_min_minutes`), `walking` (< 7 km/h), `cycling`
  (< 25), `vehicle` (25 km/h up to the implausible threshold), `implausible`
  (>= `implausible_speed_kmh`, logged as a warning; bad data or a second
  device) and `unknown`. Two records at the same instant are `implausible` when
  even the lowest speed of their interval (below) reaches the threshold, else
  `unknown`. A step slower than 1 km/h across a silence of at least
  `gap_min_minutes` and longer than the combined uncertainty radii is `unknown`,
  because a slow average says nothing about standing still. The threshold check comes
  first, so a segment within the combined uncertainty radii is `stationary` only when it
  is not already implausible.
- **Distances.** Steps, segments, gaps, speeds, the Speed tool and the map's
  neighbour box use the geodesic on the WGS84 ellipsoid computed with
  GeographicLib (C. F. F. Karney, "Algorithms for geodesics", J. Geodesy
  87:43–55, 2013; accurate to about 15 nm); the segment bearing is the geodesic
  initial azimuth. Proximity tests (stays, encounters with their paths, case
  places, presence matrix, places, crystal ball) still use the great circle on a
  sphere of radius 6 371 km: its relative error of at most 0.56 % (under 0.6 m per
  100 m) is far smaller than the position accuracy they compare against. The report's
  method section says which figure uses which formula.
- **Time resolution** per source: the coarsest of 1 min, 1 s, 1 ms and 1 µs of
  which every dated timestamp of the source is a whole multiple. A finer clock
  whose values all happen to be whole seconds counts as 1 s: the interval becomes
  wider, never wrong. The Speed tool shows it.
- **Speed interval.** Every step comes with the speeds its data allow. With the
  geodesic distance d, the elapsed time t, the uncertainty radii r1 and r2 of the
  two records (the larger of latitude and longitude accuracy, scaled by the
  accuracy level, see above) and the time
  resolution s: at least max(0, d − r1 − r2) / (t + s), at most
  (d + r1 + r2) / (t − s), and no upper bound when t ≤ s. There is no interval
  when either record reports no accuracy. The bounds are rounded outwards to
  0.1 km/h (the lower down, the upper up), so rounding never tightens them. This
  assumes that every true position lies within its uncertainty circle and every true
  instant within the time resolution; a reported accuracy is a confidence radius
  defined by the source (often about 68 %), not a guaranteed bound, and even its
  95 % radius can be exceeded. The
  movement classes use the estimate d / t, apart from the same-instant rule above.
  The interval is in `segments_<stamp>.csv` and the `segments` of `analysis.json`
  (`speed_low_kmh`, `speed_high_kmh`), in the segment and point tooltips and in the
  neighbour box; the same interval with the radii as reported is given next to it
  (`speed_low_reported_kmh`, `speed_high_reported_kmh`).
- **Report totals** per source count only the plausible steps: "Distance over
  plausible steps" sums every segment that is neither `implausible` nor
  `unknown`; "Left out: implausible or unknown steps" gives their number and
  length (`segments_left_out`, `distance_left_out_m` in `analysis.json`), and
  "Highest plausible speed" is the fastest counted segment.
- The **last known position** is the analysed point with the newest
  timestamp; the **first known position** the oldest.

With two or more sources, two cross-source analyses follow (see Multiple
sources):

- **Encounters**: for each pair of sources, records a and b coincide when
  they are at most `encounter_max_minutes` apart and their distance is at most
  `max(encounter_max_distance_m, ua + ub)`, where ua and ub are the uncertainty radii
  of the two records. Coinciding records are merged into one encounter as long as
  each next hit starts at most `encounter_max_minutes` after the previous one ended. Each encounter lists
  both sources, start and end, duration, the mean position of the involved
  records, the closest distance, the number of records per source and their
  first and last source lines, the **smallest time offset** (the smallest time
  difference of two coinciding records, `min_time_offset_seconds`) and the
  **closest pair offset** (the time difference of the two closest records; of
  equally close pairs the one nearest in time, `closest_pair_offset_seconds`):
  the records of an encounter can lie up to `encounter_max_minutes` apart, and
  the offsets show how far apart they actually were. The map and the GPX/KML descriptions give
  both offsets rounded down to whole seconds, longer ones in minutes, hours and
  days ("smallest time offset 40 s (closest pair 2 min 5 s)"); the CSV,
  `analysis.json` and the report keep the precise values.
  Both are differences of the recorded times and are not widened. If the local
  time of a record occurred twice (clocks going back), `time_ambiguity_seconds`
  gives the largest amount by which it may lie later (0 if none) and the texts
  say so, since under the other reading the records may not coincide at all.
  All records take part, including those excluded by `max_accuracy_m`, but not
  those of an excluded positioning method; with the default `p95`, two records of
  ± 1000 m at 68 % therefore coincide up to about 3240 m apart
  (2 × 1000 m × 1.6215); a record without a reported accuracy (KML) counts with
  ± 0. An encounter whose records never came within `encounter_max_distance_m`
  of each other coincided only through their accuracy and is classed
  `within accuracy`, not `same place`; a joint movement of
  that kind is marked `joint movement (only within accuracy)` in the report, the
  GPX and KML descriptions and the map (`coincided_within_accuracy_only` in
  `encounters_<stamp>.csv` and `analysis.json`). The offsets are in
  `encounters_<stamp>.csv`, `analysis.json`, the report, the GPX and KML encounter
  descriptions and the map.
- **Shared places**: stay centres of different sources within
  `stop_radius_m` of a running place centre form a shared place. Each lists
  its sources, every visit (source, stay, arrival, departure) and whether
  visits of different sources overlap in time.
- **Case places**: visits, possible visits, closest approach and a window
  verdict per case place and source; see
  [Case places and position check](#case-places-and-position-check).

Results are written to `analysis.json` and to the per-run CSV/GPX/KML files
listed under Output, and are embedded in the map for the briefing panel.

Known limitations:

- The map knows the display zone's clock changes only from two days before the
  first report to one year after generation; a crystal-ball reference time
  outside that range uses the nearest known offset.
- The first Nominatim failure stops further address lookups for that run.

## Multiple sources

A project with several sources reads them one after another; each keeps its
own SHA-256, encoding, counters and status in `metadata.json`. Cancelling
stops the source currently being read (status `partial`, no hash) and skips the
rest (`skipped`). A source that cannot be read (file missing or unreadable; KML or
GPX with a DOCTYPE, a wrong root element, malformed or empty XML; malformed or
unrecognised JSON; a CSV without a usable column mapping) is marked `failed`
with the reason; the run continues with the other sources, still ends as
`completed`, and lists the failure under `warnings` in `metadata.json` and in
the summary. A source that fails partway through (KML, GPX, CSV, Google) keeps its
counters, the bytes read and the SHA-256 of those bytes (`sha256_scope`:
"bytes read before the failure"); a source refused before its records are read
(unrecognised JSON, unusable CSV mapping) is still hashed over the whole file.
Its rejected records stay in `rejected_<stamp>.csv`, its points are not used
and it is not included in the total counts. A run in which no source could be read
fails.

Counters: `accepted` counts **points**, `invalid` counts **rejected
records**. One record can yield several points (a KML track, a Google visit or
activity gives a start and an end point), so `accepted` can exceed the number
of records. Per source, `metadata.json` also lists `naive_timestamps`: how many
timestamps had no offset and how the format treats them (GPX/KML: assumed UTC;
CSV: read in the mapping's zone; Google: rejected).

| Analysed per source | Analysed across sources |
|---------------------|-------------------------|
| First/last position, stays, gaps, segments, key figures, addresses | Encounters (same place at the same time, per source pair) |
| Duplicates (the same position, accuracy and positioning method count only within one source) | Shared places (stays of different sources at one location) |
| | Joint movement (class of an encounter) and the presence matrix, see [Presence matrix and joint movement](#presence-matrix-and-joint-movement) |

Points of different sources never form one track: every per-source result
belongs to exactly one device, and only encounters and shared places link
sources. Both cross-source results exist only with two or more sources, as do
`encounters_<stamp>.csv` and `shared_places_<stamp>.csv`. The thresholds are
`encounter_max_minutes` and `encounter_max_distance_m` under `[analysis]`
(see Analysis). Record intervals vary widely between devices, so widen them
for sparse sources. Online lookups (Nominatim addresses, Overpass anchors)
take the last position and the longest stays of each source in turn, within
the same limits as for one source.

KML sources carry no accuracy: their points are exported with
`accuracy_known = false` and ± 0. Placemarks without a timestamp become records
without a timestamp (counted in `metadata.json`, drawn in the Points layer as
hollow rings) and are not part of the timeline or the analysis.

## Case places and position check

A **case place** is a location from the case file that you want to check the sources
against: a label, latitude and longitude, a radius in metres (default 100, 10–5000),
optionally a time window, an address and a note. Enter them in the project setup with
**Case places…**: type a place and press Add, select a row (Enter or click) to change or
remove it, or choose a CSV file from the `input` directory and press **Load file**.

The file is UTF-8, separated by commas or semicolons, with numbers as plain ASCII
decimals with a decimal point (`51.45561`, `-16.8`; no exponent, underscore or other
digits, in the dialog as well). Its header is exactly

```text
label,latitude,longitude,radius_m,from_local,to_local,address,note
Kiosk,51.45561,7.01156,150,2026-07-01 22:00,2026-07-02 01:00,,late opening
Flat,,,,,,"Kennedyplatz 1, Essen",
```

A file with an unknown column, a bad number, a radius outside 10–5000, a window whose
start lies after its end, a duplicate label or an unusable time is refused as a whole,
with the row numbers (header = row 1); nothing is loaded from it. Its name and SHA-256
are recorded in `metadata.json` together with the places as entered; a row changed in
the dialog afterwards no longer refers to its file row. The limit is 200 case places.

**Window times** are wall-clock times of the display zone (`timezone.display`), written
`YYYY-MM-DD HH:MM[:SS]`; both bounds are included. A time that occurs twice when clocks
go back (Europe/Berlin: 02:00–02:59 on the last Sunday of October) is refused until you
add its offset (`2026-10-25 02:30 +02:00` for the first, `+01:00` for the second
occurrence). A time that does not exist when clocks go forward (last Sunday of March,
02:00–02:59) is refused, and so is an offset that contradicts the zone. Every output
gives the window in UTC and as local time with its offset. If the display zone is
changed after the case places were entered, their window times are re-read in the
new zone; a time that does not fit the new zone blocks the start.

**Address without coordinates.** Such a place is searched once at Nominatim
(`online.nominatim_search_endpoint`) when online services and Nominatim are on; the
setup screen says so before the run. The first result is taken as it is, recorded with
its display name and coordinates and marked **"geocoded, not verified by the
examiner"** in `analysis.json`, the CSV, the report and the map; check it before relying
on it. When offline, without a result, after a failed search or after cancelling, the
place stays **"not located"**: it is documented, named in a warning and not checked.

**The check** runs for every located case place and every source that was read, over
every accepted record (regardless of its accuracy or positioning method, map thinning
and the duplicate switch), with great-circle distances to the centre of the
place. Accuracies are judged by their uncertainty radius (see
[Analysis](#analysis)):

| Result | Meaning |
|--------|---------|
| Visit | Consecutive records inside the radius (bound included), except records of an excluded positioning method. A record outside the radius, or a silence of more than `analysis.gap_min_minutes`, ends the visit. First and last record are not an arrival or a departure |
| Possible visit | A record outside the radius whose accuracy circle, taken as uncertainty radius, reaches it (distance − uncertainty radius ≤ radius); a record of an excluded positioning method inside the radius, or outside it within the radius plus the larger of `analysis.max_accuracy_m` and its uncertainty radius. Counted and listed apart, never merged with visits |
| Closest approach | The nearest record overall and inside the window: distance, time, accuracy, record number |
| Window verdict | Exactly one per source, only with a window: `present` (at least one record inside the radius, not counting records of an excluded positioning method), `possibly present` (none inside, but at least one that could be), `elsewhere` (records exist and every one lies farther away than the radius plus its uncertainty radius), `no reports in window` (the source is silent) |

A verdict covers **only the moments of the records**: between records nothing is
known. Every verdict for a window that holds records therefore comes with the first and
last record in the window and the longest span of the window without any record from that
source (also counted from the window start to the first record and from the last record
to the window end), in minutes. GEOSnap sets no threshold for them: an `elsewhere` based
on a single record in a 12-hour window is shown as exactly that, and you judge it. When every
record inside the radius during the window has an uncertainty radius larger than the
radius, `present` carries the basis "inside the radius, but every such record has an
accuracy circle larger than the radius", and the best (smallest) reported accuracy among
these records is given. A record inside the radius without an accuracy value is
counted with the assumed accuracy (`analysis.max_accuracy_m`, see below): when no record inside
the radius during the window has an accuracy within the radius and at least one has
none, the basis reads "inside the radius, but every such record has an accuracy circle
larger than the radius or no accuracy value (assumed 200 m)"; every visit lists the best reported accuracy of its records
(CSV column `accuracy_m` of a visit row, `best_inside_accuracy_m` of a verdict row).

A verdict describes **the records of a device, never a person**. `no reports in window`
means that nothing can be said about where the device was; it does not mean absence.
Instead, the nearest records before and after the window are given with their distance.
Records without an accuracy value (KML, GPX, and CSV or Google records that lack one)
are never a possible visit, except records of an excluded positioning method inside the
radius, which are possible visits whatever their accuracy. Such a record counts as
`elsewhere` only beyond the radius plus `analysis.max_accuracy_m` (the accuracy limit of the analysis, taken as its assumed
accuracy), and a nearer one makes the verdict `possibly present` with the basis
"accuracy not reported; within the assumed 200 m" (the number is `max_accuracy_m`, not
scaled). The radius is your choice and is stated in every output.

Records of a positioning method in `analysis.excluded_positioning_methods` (default
`cell`, whose radius often describes a cell sector rather than a position) take part in
the check but are **never present**. Inside the radius they are possible visits and make
the verdict `possibly present` with the basis "only cell records inside". Outside it they
count as clear of the radius only beyond the radius plus the larger of
`analysis.max_accuracy_m` and their uncertainty radius, and a record that is near only
because of this assumed margin gives the basis "cell records near the radius; within the assumed
200 m". In `case_places_<stamp>.csv` such a possible visit carries the detail "cell
record inside the radius", or "cell record near the radius" when it is near only
because of the assumed margin.

**Verdict as reported.** When the radii as reported (factor 1) give another verdict or
basis, for example `elsewhere` instead of `possibly present` for a record whose 68 %
circle misses the radius but whose 95 % circle reaches it, that verdict is given next to
the primary one: "Verdict with the accuracy as reported" in the report,
`verdict_as_reported` and `basis_as_reported` in `analysis.json` and
`case_places_<stamp>.csv`. When both agree, nothing more is shown.

Coordinates in `case_places_<stamp>.csv` and `presence_matrix_<stamp>.csv` have 6
decimals (about 0.1 m), like the stay, encounter and shared place CSVs; the coordinates
exactly as entered are in `metadata.json` and `analysis.json`.

Outputs: the `case_places` block of `analysis.json` (with `gap_threshold_minutes` and
`unknown_accuracy_margin_m`; possible visits are listed up to 100 per place and source,
the counts are complete), `case_places_<stamp>.csv` (complete), the report section
"Case places", the map layer **Case places** (on by default: label and dashed radius
circle; click for the verdict, closest approach, visits and possible visits per shown
source) and one row per case place in the timeline (window as a bracket, visits as bars
in the source colour; the timeline is on at start when case places exist). The check
took 0.2 s for the 13 140 records of a real KML export and 50 places, and about 6 s for one
million synthetic records and 50 places (measured 2026-09-21 on the development
machine).

## Presence matrix and joint movement

**Joint movement.** Every encounter has a class. `joint movement` means that the records
of both sources stayed within the encounter distance (or their combined accuracy) and
time while both moved. It does not say that the devices were in the same vehicle, nor
who carried them; records of two
devices on the same road, train or bus look the same. The rule:

- the encounter lasts at least 5 minutes, and
- the path through the midpoints of the coinciding records is at least
  `encounter_joint_movement_min_m` long (default 500 m), and
- the participating records of each source on their own cover that length too. Without
  this condition, a coarse record of a passing device (± 800 m) that coincides with a
  resting device would move the midpoints although only one device moved.

Every other encounter is `same place`, or `within accuracy` when its records never came
within `encounter_max_distance_m` of each other and coincided only through their combined
accuracy. An encounter may span rest and movement; the class applies to the whole
encounter.

`path_length_m` is the length of the plausible steps of the path, not the distance covered:

- **Scatter adds nothing.** A record becomes a path vertex only when it lies farther from
  the previous vertex than the encounter distance (`encounter_max_distance_m`, or the
  combined ± of the two records, if larger). Two devices resting side by side for a day
  have a path of a few metres, not of kilometres.
- **Position jumps add nothing.** A step from vertex to vertex counts only if time has
  passed (measured from the last record at the previous vertex) and its speed stays below
  `implausible_speed_kmh` (default 250), the limit of the movement classes. Any other step
  is a position jump in the source data (or travel at that speed, such as a flight) and
  is excluded: the path continues from the new position, the step's length goes to
  `path_excluded_m`, `implausible_steps` counts such steps, and the line on the map is
  broken there. Several records of one instant are taken nearest first, so a second
  position reported for the same instant is a jump, not a route there. The rule applies to
  the midpoint path and to each source's own path, so a jump can never create a joint
  movement. In the real KML export used for testing, a third of all consecutive steps are
  such jumps.
- **Remaining inconsistency.** Segments and movement classes use the positions within
  `max_accuracy_m`, encounters use every accepted record; a step can therefore be
  implausible in one and absent from the other, or the reverse. A single outlier that
  leaves and returns at a plausible speed still adds the distance out and back.

`displacement_m` is the distance from the first to the last midpoint, jumps included (small
for a round trip).

Outputs: `movement`, `path_length_m`, `displacement_m`, `path_excluded_m` and
`implausible_steps` in `analysis.json` (plus `path_parts`, the midpoint path as lines broken
at the jumps and thinned to at most 200 vertices in all, for joint movements), in
`encounters_<stamp>.csv`, the report, the briefing, the encounter tooltip and the timeline
(dashed connector); GPX and KML name the class in the encounter description. The map draws
a joint movement as a line through the midpoints in the colours of both sources instead of
the 🤝 badge; clicking it shows both sources' reports, as for every encounter.

**Presence matrix.** With two or more sources, or with at least one case place, GEOSnap
builds a matrix: places as rows, sources as columns (sources that were read; a failed or
skipped source has no column). A cell gives the visits of one source at one place: their
number, the total dwell, the first and the last record, and the visits themselves. Rows:

1. the shared places (stays of at least two sources together),
2. the located case places in the order entered,
3. the stay places of one source only,

with groups 1 and 3 each sorted by total dwell, longest first.

Stay centres within `analysis.matrix_tolerance_m` (0 to 3000 m, default 50) of a running
place centre form one row; the **Shared places** layer of the map and `shared_places_<stamp>.csv`
keep `stop_radius_m`, so the two can group differently. The project setup shows
the tolerance as **Matrix tolerance (m)**, where it can be changed for one run without
touching `config.toml`; the value used is in `analysis.json` (`presence_matrix.tolerance_m`,
`parameters.matrix_tolerance_m`), in `metadata.json` and in the report.

The two kinds of rows are measured differently. At a shared place or stay place a visit is
a **stay** (at least `stop_min_minutes` within `stop_radius_m`). At a case place a visit is
a **run of consecutive records inside the radius you entered** (see Case places), so a
single record is a visit of 0 minutes and a jittery source produces several short visits.
Do not compare counts or dwell between the two kinds. A row is `simultaneous` when visits of
different sources overlap in time (bounds included). A cell without a visit means that no
visit of that source was found; it does not mean that the device was never there. A cell
describes the records of a device, never a person.

`analysis.json` (`presence_matrix`: `row_cap`, `rows_total`, `rows_omitted`, `sources`,
`rows`), the report section "Presence matrix" and the map list at most 200 rows: every
case place, then the shared places and the stay places with the longest dwell.
`presence_matrix_<stamp>.csv` always lists every place and every visit. Without a matrix
the key `presence_matrix` is null and no CSV is written.

In the map, **View → Matrix** opens the table. Click a cell: the map flies to the place,
marks its radius, moves the time cursor to the first record of the first visit (the cursor
stays inside the From/To range; the window says so when the visit lies outside) and lists
the visits below the table (the table scrolls by itself, its head and the detail stay in
view; the place is shown in the free part of the map, not under the window); click a
visit to move the cursor there. **shared only** keeps the places with
visits of at least two shown sources. A source hidden in the Sources window loses its
column, `simultaneous` is then judged among the shown sources, and places without a visit
of a shown source are left out (case places stay). The **Tolerance (m)** field regroups the
stay centres in the browser with the same algorithm as the run (0 to 3000 m; case places
keep the radius you entered, and **Reset** returns to the run value); the window then reads
"tolerance 120 m (view choice; the report and CSV use 50 m)". Rows whose visits of two or
more shown sources overlap in time are **highlighted** and listed first, then every row by
its total dwell over the shown sources.

## Online services

`[online] enabled` is the master switch: when set to `false`, it turns map tiles
from `online` into `none` and skips both Overpass and Nominatim, whatever
their own settings say. `metadata.json` records the effective state.

| Service | Sends | When |
|---------|-------|------|
| Map tiles (`tile_source = "online"`) | Viewer's IP address and the viewed map area | Each time the map is opened or panned in a browser; the chosen provider receives them |
| Overpass (embedded) | The analysed area (last position and stay centres of every source) | Once, when the map is generated |
| Overpass (live "Check surroundings" in the map) | The checked centre and radius, or for "Check along the shown route" up to 100 vertices of one source's shown route and the corridor width | Whenever a check cannot be answered from the embedded places |
| Nominatim | The last position and stay centres of every source, one request at a time | Once per lookup, when the map is generated |
| Nominatim (address search) | The address text of every case place entered without coordinates, one request per place | Once per such place, when the project is processed; never without such a place |

Each provider has its own usage policy and attribution (shown in the map and
in Help → Map styles).

The live check in the map is sent by the browser to `online.overpass_browser_endpoint`
(empty = `overpass_endpoint`). `overpass-api.de` requires a Referer header: browser
requests without one get HTTP 406 without CORS headers, which the browser reports as
"Failed to fetch"; with a Referer it answers normally (verified 2026-09-18). A map
opened from disk (`file://`) never sends a Referer. The OpenStreetMap standard tiles
also require one.

Alternatively, press **O** on the summary screen to open the map through a local
server: GEOSnap serves the map at a secret address
`http://127.0.0.1:<port>/<random token>/map_<stamp>.html`, so the browser sends
`http://127.0.0.1:<port>/` as Referer and live checks against `overpass-api.de` work.
The server listens only on 127.0.0.1 (reachable from this computer only), delivers
only that one map file and only at that address (every other path, including the
project's log, metadata and exports, gets 404; GET/HEAD only, apart from two POST
routes: `records/search-area` records a search area, see
[Search area handover](#search-area-handover), and `records/speed-range` a range of the
Speed tool, see [Speed range records](#speed-range-records); requests whose Host is
not `127.0.0.1:<port>` or `localhost:<port>` are refused). It runs as long as GEOSnap
runs and stops when GEOSnap exits; each request is logged in the application log. The
random token keeps other users of the same computer (for example a shared Windows
terminal server) from guessing the address; treat the address like the map itself.
The double-clicked map file still works for everything else, offline included; only
live checks against endpoints that require a Referer fail there, and the map says so
in its status line. Local tiles (`tile_source = "local"`) lie outside the project
directory and are not served, so the served map has no base map; open the file from
disk to see them. Instances that accepted these requests in the same
test (open CORS, no Referer requirement) include `https://overpass.kumi.systems/api/interpreter`
(operated together with `overpass.private.coffee`); it was overloaded at the time of the
test (HTTP 504). Setting it sends every live-checked area to that operator, so the choice
is left to you. The embedded query at generation time always uses `overpass_endpoint`.

A map opened from disk falls back once: when the endpoint above refuses a live check
(HTTP 406, or a network/CORS error, as `overpass-api.de` gives without a Referer), the
same query is sent exactly once to `online.overpass_fallback_endpoint` (default
`https://overpass.private.coffee/api/interpreter`, a public instance listed on the
OpenStreetMap wiki, operated by Private.coffee; it answered such requests with CORS open
on 2026-09-18, under load after about 36 s). The checked area then goes to that
operator. The status line names the server that answered, e.g. "answered by
overpass.private.coffee (fallback: overpass-api.de refuses maps opened from disk)", and
the surroundings export records it as `endpoint`. If the fallback fails too, it is not
tried again, and a map served by GEOSnap (key O) always uses the primary endpoint. An
empty value means no fallback. Both origins are listed in the map's `connect-src`; `metadata.json` records
it as `online.overpass.fallback_endpoint`.

Nominatim is only ever called from Python (never from the browser), with 1.1 s
between requests and at most `nominatim_max_lookups` reverse lookups in total, as
the OpenStreetMap/Nominatim usage policy requires. Address searches for case places
(`nominatim_search_endpoint`) share that pacing, run first and do not count against
`nominatim_max_lookups`; a failed search stops all further Nominatim requests of the
run. Both services require the
OpenStreetMap attribution shown on the map. Raw responses are saved as
`overpass_<stamp>.json` and `nominatim_<stamp>.json`; every request is logged
with endpoint, timing, response size and SHA-256.

If a service fails (network, HTTP error, timeout, malformed response), the run
completes without that content, the failure is logged as a warning, and
`metadata.json` records the error under `online`.

### Place catalogue

The embedded and the live Overpass search use one shared catalogue of 31 place
categories (`geosnap/online/place_catalogue.py`), grouped as follows; the map
payload carries the catalogue, so the browser keeps no copy of its own:

- Safety / authorities: Police; Fire / ambulance station; Prison / courthouse;
  Surveillance camera
- Health: Hospital / clinic; Pharmacy / drugstore; Social facility / care home
- Education / children: Kindergarten / childcare; School / college / university;
  Playground; Sports facility
- Transport: Railway station / halt / subway entrance; Public transport stop /
  bus station / taxi rank; Railway line / level crossing; Traffic signals; Fuel
  station / motorway services; Parking; Airport / harbour / ferry
- Retail / services: Bank / ATM; Supermarket / convenience / kiosk
- Food / lodging: Restaurant / cafe / fast food; Bar / pub / nightlife / liquor
  store; Hotel / hostel / guest house; Camp site / hut / shelter
- Community: Place of worship; Cemetery
- Leisure / outdoor: Park / green space / nature reserve; Forest / wood / scrub
- Water / nature: Water / river / bathing site
- Infrastructure / hazard: Bridge / viaduct; Cliff / quarry / cave / derelict site

`overpass_categories` in `config.toml` selects the embedded categories by key
(the full key list is in the comments there). The default leaves out eight
categories that are rarely relevant: Prison / courthouse,
Sports facility, Parking, Airport / harbour / ferry, Restaurant / cafe / fast
food, Bar / pub / nightlife / liquor store, Place of worship and Cemetery.
A `config.toml` with the key `railway` still loads: the key is expanded to both
`railway_station` and `railway_line` with a logged warning. Kindergartens are not
part of `school`; they have their own category `kindergarten`.

Each category has a density class that caps the search radius: sparse
categories are queried up to 20 000 m, medium ones up to 5 000 m, dense ones up
to 2 000 m. A category whose cap is below `overpass_radius_m` is skipped; the
run then logs a warning and `metadata.json` lists it under
`online.overpass.skipped_categories`. A `remark` in the Overpass answer (for
example a timeout) is logged as a warning and recorded under
`online.overpass.remark`. The query asks for at most 2 000 elements. Overpass
silently cuts a larger result, so when an answer reaches that limit the run logs
a warning and records `online.overpass.truncated = true`. A hit whose centre lies
more than three times the radius from every anchor is a large feature (forest,
river, ...) that only touches the search area; it carries `far_centre = true` in
the map.

## Browsers

The map is one self-contained HTML file and needs a current browser. Checked on
2026-09-18 with Chromium (the engine of Google Chrome, Microsoft Edge and Brave) and
Mozilla Firefox, both opened from disk and served by GEOSnap (key O on the summary
screen): every tool loads and runs without script errors.

- **Microsoft Edge, Google Chrome, Chromium, Brave, Firefox**: supported. Brave's
  shields may block the online map tiles or the live place check; allow them for the
  map if needed.
- **Internet Explorer**: not supported. Microsoft retired it in June 2022, and Windows 11
  no longer starts it by itself. It lacks features the map relies on (CSS custom
  properties, `fetch`, `Promise`, date and colour inputs, `<details>`). Opened there, the
  map shows a short notice naming the supported browsers instead of a broken page; the
  project files (CSV, GPX, KML, JSON) stay usable.
- **Live place checks** use `overpass-api.de` only when the map is served by GEOSnap
  (key O), because that server requires a Referer header, which a file opened from disk
  never sends; from disk the check goes once to `online.overpass_fallback_endpoint`
  instead (see Online services). Everything else works the same from disk. A busy server
  (HTTP 429/504) gets one retry after 8 seconds.

### Screen sizes and windows

The map fits the browser window instead of growing past it. Measured in Chromium and
Firefox at 1024 × 768, 1280 × 720, 1366 × 768, 1920 × 1080, 2560 × 1440 and 390 × 844:

- The toolbar wraps; no control is ever cut off, covered or wider than its own box. The map
  area and the timeline share the remaining height; the map area keeps at least
  220 px and the timeline plot scrolls inside its panel, so nothing is pushed below the
  window edge.
- Every window (Places, Sources, Crystal ball, Matrix, Speed, Help, the briefing panel)
  stays inside the map area: its width and height are bounded by the map, and its content
  scrolls inside. Each has a chevron that folds it to its title bar.
- The layer control and the legend scroll inside the map; the legend stays a narrow column
  instead of spreading over the map.
- No screen width produces a horizontal page scrollbar. Below 700 px the briefing panel
  moves below the map, the windows anchored at the left edge use the full width, and the
  toolbar labels wrap. A phone-sized window is usable but plain: every control is
  reachable and the page scrolls vertically.
- Text is 13 px up to about 1900 px window width and grows to 17 px at 2560 px, so the
  toolbar, the windows and the legend stay readable on a large display.

Two limitations are by design rather than defects. On a 1366 × 768 screen with the
timeline open, the map area is about 290 px high, so the Matrix window together with the Sources and
the Help window leaves little of the map visible; fold or close one of them. At phone
width a window covers most of the map while it is open.

## Map tools

- **Legend** (bottom left, View → Legend) explains every layer's symbol and colour,
  including the movement classes and the time colour gradient.
- **Movement classes**: route segments coloured by class (grey stationary,
  green walking, blue cycling, orange vehicle, red dashed implausible), with
  direction arrows at segment midpoints and chronological sequence numbers.
- **Colour by time**: toggle points between a uniform colour and a gradient
  from oldest (blue) to newest (red).
- **Click a point** to highlight the previous and next point of the same source
  that the map shows (filters and thinning decide which those are; the box says
  "Previous shown" and "Next shown"), with the geodesic distance, the time
  difference, the speed and its interval to both. Redrawing the map or changing the
  shown sources clears it.
- **Accuracy filter**: a slider that hides points (and dependent layers) with
  a larger ± than the chosen value on the map only; the underlying analysis
  is unaffected and stays as documented. Records without an accuracy value always
  pass; when such records are shown, the filter description says so ("accuracy up
  to 50 m (records without accuracy kept)").
- **Time filter** (From/To): date, hour (00-23) and minute, prefilled with the
  data span so that no field is ever empty; To includes the whole minute,
  including fractions of its last second. Reset restores the full span and
  switches the time cursor off. When the time controls become unavailable (for
  example because the dated source is hidden), the cursor is switched off,
  playback stops and the range is reset, so no hidden filter remains active. Stays, gaps,
  the last known position and places are computed at generation time and do
  not follow the filter.
- **Time cursor and playback**: a slider over the From/To range with five
  transport buttons (previous point, play, pause, stop, next point). Moving the
  slider, playing or stepping switches the cursor on: points up to the cursor
  time are shown, the current position (the last report at or before the
  cursor) is marked by a pulsing orange dot labelled with the cursor time and
  the age of that report, and the last n minutes of track are highlighted
  (default 60). Play advances by the chosen data time per real second (1 min/s
  to 1 d/s) and stops at the end of the range; pause keeps the cursor where it
  is; stop switches it off; the step buttons jump to the previous or next
  report inside the range; Follow keeps the current position in view. For a
  thinned source the cursor and timeline texts say "on this map (1 of every N
  reports)": a report missing from the map may exist in the complete data.
- **Timeline** (View → Timeline; on at start with two or more sources or with case places): a panel
  under the map with one time axis over the From/To range and one lane per
  shown source ("All sources") or for one chosen source. Stays are bars in the
  source colour (labelled with the place where there is room), a thin line
  connects the reports, gaps are hatched, encounters are vertical
  connectors between two lanes (only with "All sources"; dashed for a joint
  movement, whose tooltip gives the path length and displacement) and an outline marks
  the time two sources stayed at a shared place together. The time cursor is a
  vertical line: a click sets it, the arrow keys move it report by report
  (Home/End; Escape switches it off); hovering or focusing shows every lane's state
  at that time. Positions use UTC; labels are wall-clock times of the display zone. The
  timeline follows the time range and the shown sources, not the accuracy filter
  or the cursor. It is always printed (after the map), and the CSV export writes its
  stays, report spans, gaps, encounters (with their class), overlapping visits and, with
  case places, their windows and visits (`case_place_window`, `case_place_visit`) as
  `…_timeline_derived.csv`. The time axis stays at the top of the panel and the
  blocks below it scroll vertically, each under its own heading ("Sources",
  "Case places"), so six sources stay readable at 1366 × 768; the arrow keys up
  and down and Page up/down scroll them from the keyboard.
- **Matrix** (View → Matrix; the tick keeps its place and is disabled with a
  reason when the shown sources have no stays and there are no case places):
  the presence matrix, see
  [Presence matrix and joint movement](#presence-matrix-and-joint-movement). Not printed.
- **Unavailable controls (gating)**: whether a control has what it needs is
  computed from the records, not assumed. The time filter, the time cursor and its transport
  buttons, the timeline, the sequence numbers, "colour by time" and the Speed
  tool need at least two records with a timestamp **and** a timestamp on at
  least half of the records; route, movement classes, arrows, stays and gaps
  need a shown source with two or more timed records (and a stay or a gap of
  its own), while the **last known position** needs only one timed record,
  because it is the first thing an examiner looks for; the crystal ball needs 20 timed records on
  3 days for at least one shown source; the Matrix option needs a matrix row;
  encounters, shared places and case places need one of their own. A control
  that lacks this basis is **disabled and greyed out but keeps its place**, and its
  tooltip gives the reason ("needs timestamps on at least half of the records:
  3 of 40 have one"). The counts are recomputed for the **shown** sources, so
  hiding a source in the Sources window can switch a control off and showing it
  again brings it back. `payload.capabilities` carries the same counts over all
  sources for the run; gating never changes recorded data.
- **Control tooltips**: every button, checkbox, list, slider and layer row
  has a small explanatory box that appears after a delay on hover and on
  keyboard focus (Escape closes it); a gated control adds its reason. The
  delay is set by a single constant, `CONTROL_TOOLTIP_DELAY_MS` in
  `geosnap/mapping/template/map_core.js` (600 ms). The tooltips of the points on the
  map are Leaflet's own and appear immediately, unaffected by this delay.
- **Speed tool** (toolbar button "Speed", order Crystal · Speed · Briefing ·
  Help): click two records of one source, in either order; the range runs from
  the earlier to the later in the recorded order. The range covers **every analysed
  record** of that source between the two ends, whatever the time filter, the accuracy
  slider or the cursor show, so its figures do not change with the view. The panel gives both record numbers with their
  local times, the records in the range, the elapsed time with the time resolution
  of the source (see Analysis) and, as the headline figure, the **minimum average speed**: the
  geodesic straight line between the two ends, shortened by their two uncertainty
  radii (see [Analysis](#analysis)), divided by the elapsed time plus the time
  resolution. It is a lower bound that depends on the two end records only: the true
  path is at least as long as the straight line between the true end positions. The
  **stepwise minimum** sums the steps between consecutive records, each shortened in
  the same way (a record without an accuracy adds nothing), and is given separately
  with the number of records it relies on: it holds only if every one of them lies
  within its circle, which becomes
  unlikely for many records (with 68 % circles, 10 records all inside: about 2 %).
  Both assume that every true position lies within its uncertainty
  circle and every true instant within the time resolution; a reported accuracy is
  a confidence radius defined by the source (often about 68 %), not a guaranteed
  bound. Both minimums are also given with the radii as reported. Next come the
  straight line and the **path over the records** with their average speeds (the path is an estimate, not a bound: position noise
  lengthens it, sparse records shorten it), the slowest and fastest step and the
  implausible steps. Path, minimum, record count and elapsed time come from the
  figures the payload carries per record (`cum`, `cmb`, `seq`), computed over the
  complete list. On a thinned source the records between the ends are not all on
  the map: path and minimum still cover every record, the step figures only the
  drawn ones, and the panel says so. A click on another source, on an earlier
  record, on a record without a timestamp or on one the run's accuracy limit or
  its positioning method excluded from the analysis is refused with the reason
  (such a record carries no distance, time or speed, and an estimate must not be shown as a measurement);
  where such a record lies **inside** a range, the panel says how many records lie
  outside the analysis. The order is the recorded one: a range follows the
  record's position in the source's full list, never the file line, which a CSV
  may sort in any order and which every record of a KML `gx:Track` placemark shares.
  Clear, leaving the mode, or changing the shown sources or the time filter
  discards the range.

  **Excluding records.** "Exclude records" switches a mode in which a click on a
  record strictly between the two ends excludes it (a red cross; a second click
  or Restore takes it back); a click on an end or outside the range is refused
  with the reason. Every excluded record is listed with a reason to pick
  (position outlier, implausible jump, duplicate position, other) and an optional
  note, and the figures are shown side by side: without the excluded records and
  with all records. On a thinned source records cannot be excluded, since the
  records between the ends are not all present; the panel gives the reason.
  "Record calculation" is enabled only when the map is served by GEOSnap (key O)
  and every exclusion has a reason; see [Speed range records](#speed-range-records).
  The map file opened from disk explains that recording needs the map opened from
  GEOSnap.

  Every point tooltip also carries "Speed from previous record" with a
  small gauge in the colour of the movement class: below walking speed
  (7 km/h) the value is given in m/s with one decimal, above it in km/h with one
  decimal, followed by the step it is based on ("38 m in 12 s") and the speed
  interval (bounds rounded outwards), or "none" when a record reports no accuracy.
- **Collapse**: every window (Places, Sources, Matrix, Crystal ball, Help, Neighbours),
  the legend and the layer control have a chevron button in their title bar
  that folds them to the title bar and back. The button is keyboard accessible;
  the folded state is not kept after reloading. Starting a check from the crystal ball folds it and brings
  the Places window to the front.
- **Map style** lists the providers by short name ("OpenStreetMap Germany",
  "OpenTopoMap", "Esri Imagery", "Esri Topo"); the full name is the option's
  tooltip and stays in the TUI, metadata and help.
- **Crystal ball** (toolbar button "Crystal"): an estimated search area around the
  last known position for a reference time (default: now, editable). From the
  device's own history it derives rings inside which the device stayed in 50, 80
  and 95 % of recorded windows of the same length, the share of comparable stays that lasted that
  long, the direction of earlier departures from that location, and known
  places within reach. It is a probability estimate, not evidence: a notice has to
  be acknowledged before use, only "Record search area" writes to the project
  (see [Search area handover](#search-area-handover)), and the window states the
  sample sizes and flags extrapolation. The "Directions" block follows the
  headline and the ranking (without a rose: a grey, empty compass with the reason
  and the condition), and a line under the rings explains them ("Of N comparable
  windows of Δt, 50 % stayed within …"; when two radii agree to the metre it says
  why). When the records allow it, it also draws a direction rose (wedge length = 80 % reach per
  direction, shade = share of days; "Left the place: N 8 of 12 days…") and, in the
  window's "Directions" block, a schematic chart of it (wedge area = share of
  leaving days, hover for days, share and 80 % reach, a "Table" view), offers
  "Routes from here" (earlier trips bundled by destination, "Work · 9 of 12
  departures", off by default), shows matching destinations and a dashed
  extrapolation cone while the device was moving, warns about a routine change
  and gives the silence routine sentence. A weak basis (routine fallback or
  fewer than 5 comparable days) names no "most likely" place: the last known
  position stays the primary search point. With embedded places it names the
  nearest hazard within r95 and offers "Check hazards in r95". See "Crystal
  ball model" below.
- **Briefing panel**: last known position, tables of stays and gaps (click a row
  to centre the map on it), key figures and provenance (project, source hash,
  generation time, map mode, online services).
- **Print**: a header with project name, source hash, generation time, map
  mode, the display time zone and the filters in force is added above the
  printed page; tiles print only if already loaded.
- **Exports** (PNG, CSV, GeoJSON, GPX) cover the currently filtered view; PNG,
  GeoJSON and GPX name the filters in force (time range, accuracy limit, time
  cursor). Every export file name carries `_derived` and is **not** part of the
  recorded evidence: it is neither hashed nor logged. Exported CSVs need the
  same text-import handling as the ones in the project directory.
- **View**: toggles in the toolbar show or hide the legend (off at start), the
  grouped layer control, the Places window, the Sources window, the timeline and
  the Matrix window without changing which layers are on.
- **Windows** (Help, Crystal ball, Speed, Matrix, Places, Sources, Neighbours):
  drag a window by its title bar to move it; it stays inside the map and the
  window you grab comes to the front. The title bar stays at the top while the
  window scrolls, so fold and close are always within reach. Positions are kept until
  the page is closed. The layer control and the legend stay in their corners.
- **Places window** (View → Places, bottom right; the Sources window sits top
  left): the 31 catalogue categories as grouped checkboxes with their symbol
  (All / None per group) control which embedded places are shown and which
  categories "Check surroundings" uses; the window lists the embedded counts
  per category. Markers show the category emoji in the group colour. Places
  are not in the layer control.
- **Check surroundings** (Places window): the popup of a point, stay, last
  known position, encounter, record without a timestamp, crystal ball candidate
  or ring label offers "Check surroundings", which takes that centre and its extent
  (for a point or a last known position its uncertainty radius; at least the embedded
  query radius); "Check a point on the map…" uses a click on the map. Profiles
  preselect categories (Hazards, Shelter and help, Onward travel, Recorded traces,
  Custom). The result list is sorted by distance (symbol, category, name, distance, direction, origin) with the
  nearest hit and count per category at the top; a click centres the map. A check
  is answered offline from the embedded places ("answered from embedded data")
  when the circle lies inside a circle queried at generation (the map stores the
  centres of those circles as chosen at generation,
  `payload.online.overpass_anchors`; they count only when places were embedded)
  and all chosen categories were queried then. Otherwise Overpass is queried
  live from the browser (density rule, time limit, output limit, remarks), and
  embedded and live hits are merged by OSM type and id. A busy server (HTTP 504
  or 429) is asked once more after 8 s, and a refused request from a map opened
  from disk points to key O (see Online services). "Check along the shown
  route" checks a corridor (default 100 m, at most 500 m) along one source's
  shown route, simplified to at most 100 vertices for the Overpass linestring
  `around` filter; the list can be sorted by the time the route passed. CSV and
  GeoJSON exports carry `_derived`, the centre, radius, profile, query time,
  endpoint and "not recorded". Live results are only a view, not evidence, and
  OpenStreetMap data are incomplete.
- **Help**: a window explaining every layer of the layer control (with the
  parameters actually used for this map), the place categories and their OSM
  features, embedded versus live places, the filters, time cursor, exports,
  print and map styles.

## Crystal ball model

The estimate answers "where is the device most likely at the reference time r,
given its last report at time t and its own history?". Everything is derived
from this device's records; samples are counted in days, never in points.

- **Presence timeline** built from the stays and gaps of the analysis (complete
  even when the map thinned or de-duplicated its points). Stays are clustered into
  places by `stop_radius_m`; the place of the last report is its start point.
- **Comparable days**: days on which the device was at the same place around
  the same time of day as the last report; the state d = r − t later on each
  of them (still here, a known place, elsewhere/moving, silent) gives the
  ranking as "x of n days" with a percentage and a 95 % interval once five or
  more days contribute. Weekdays and weekends are treated separately when the
  history has at least three weekdays and two weekend days. If fewer than three such
  days exist, the estimate falls back to the routine at the reference time of
  day (any place), and finally to all days. Every level is smoothed with the
  next (three pseudo-days), and the level used is named under "Why?" together
  with the list of days; clicking a day moves the time cursor to it.
- **Confidence badge**: weak (fewer than five contributing days or fallback to
  the time-of-day routine), medium (five to thirteen days), strong (fourteen
  days or more with at least ten effective days).
- **Search rings** for "elsewhere": the farthest distance reached within d,
  measured from the rendered points at the start place around the same time of
  day, weighted per day so that a day with many records does not dominate; 50 / 80 /
  95 % quantiles plus the uncertainty radius of the last report. Without three
  comparable days the rings come from all windows of that length. When d exceeds
  half of the history, they are stretched linearly and flagged; beyond twice the
  history no radius is given. The sector shows the distance-weighted mean bearing of
  those windows that left the place.
- **Transitions and dwell**: where the device went next after earlier stays at
  this place (once there are three departures), and how long stays here usually lasted
  compared with the current one (place-specific, mixed with all stays when
  fewer than five exist here).
- **Silence**: whether gaps typically start here or at this time of day, where
  earlier gaps of at least the elapsed length ended, and when they usually
  ended. This assumes the device stayed put while silent, which the window
  states.
- **Motion line**: speed, movement class and heading of the last ten minutes
  when the device was moving at its last report.
- **Warnings** appear only when they apply: extrapolated rings, thinned or
  de-duplicated points (rings only), a clock change between t and r, an
  imprecise last position, an undeterminable day class. "Recent days count
  more" (half-life 14 days) is offered only for histories of 28 days or more.

The reference time is entered in the display zone and converted with the
clock changes recorded at generation (`zone_transitions`; "local" is the zone
of the generating computer, taken by its IANA name). In a repeated hour it means the
earlier occurrence unless only the later one lies after the last report. Elapsed time
and windows are based on UTC, times of day on the wall clock. "Now" uses the
computer's clock. All thresholds (45-minute window, three pseudo-days, badge limits)
are documented assumptions; the estimate is written to the project only through
"Record search area". Validation basis: a backtest on public GPS logger tracks of
49 people (GeoLife; mostly Beijing, 2007–2012, no accuracy values, logger often off)
and on one sparse phone export; it led to no change of any threshold. Phone location histories were not available for validation.

## Search area handover

"Record search area" in the crystal ball window saves the current estimate for the
chosen source in the project directory, so that it can be passed on and checked
later. It is enabled only when the map was opened from GEOSnap (summary screen,
key O) in the same GEOSnap session: the page is then served by GEOSnap's own loopback
server at `http://127.0.0.1:<port>/<secret token>/…`. A map opened from disk shows the
button disabled with the explanation; "Download (not recorded)" saves the same
record as a `_derived` JSON file, which is neither recorded nor hashed.

- **Transfer.** The browser sends a search area record (JSON, at most 2 MB) to
  `POST /<token>/records/search-area` on the same server. The server accepts it
  only with the secret token, a loopback `Host`, an `Origin` equal to its own
  origin (`http://127.0.0.1:<port>` or `http://localhost:<port>`),
  `Content-Type: application/json` and a `Content-Length`; otherwise it answers
  404, 400, 403, 415, 411 or 413 with a short reason. The map's Content Security
  Policy allows `connect-src 'self'` for this request.
- **Record.** `record_type` `geosnap-search-area`, `record_version` 1,
  `created_utc`, `display_zone`; `source` (id, label, file name, SHA-256);
  `reference` and `last_position.time` (wall clock, offset in minutes, UTC; they
  must agree); `last_position` (latitude, longitude, accuracy, place and the
  optional `uncertainty_m`, the accuracy scaled by the source's accuracy level:
  null exactly when the accuracy is, and between the accuracy and 1.6215 times it);
  `rings` (r50/r80/r95 in metres, basis, basis kind, days, windows, window length,
  extrapolated) or `rings_note`; `rose` (sectors with bearing, days, share,
  80 % reach); `cone`; `candidates` (rank, place or "here", label, address,
  position, days, share, 95 % interval, distance, bearing); `elsewhere_share`;
  `destinations`; `hazards` (nearest embedded hazard within r95); `headline`;
  `confidence`; `warnings`; `model` (name, application version, parameters,
  among them `accuracy_confidence`, `accuracy_scale` and
  `excluded_positioning_methods`). The search area sheet gives the uncertainty
  radius next to the accuracy ("95 %: …") when the two differ.
  Python validates it strictly: no missing or unknown keys, exact types, finite
  numbers, latitude/longitude ranges, bounded strings and lists, no control
  characters, r50 ≤ r80 ≤ r95, reference after the last report.
- **Files.** GEOSnap renders every file itself from the validated values (never
  markup from the browser) into `search_areas/search_area_<n>_<stamp>`:
  `.json` (the record plus generation metadata: record number, recording time,
  application, case reference and examiner from `metadata.json`), `.gpx` (GPX 1.1:
  waypoints for the last position and the candidates, each ring as a closed track
  of 64 vertices), `.kml` (KML 2.2: ring polygons with styles, placemarks) and
  `.html`, the search area sheet: one A4 page without scripts ("Search area – estimate,
  not evidence"; case reference, examiner, source with SHA-256, last position in
  decimal degrees and degrees/minutes/seconds, last report and reference time local
  with offset and UTC, elapsed time, rings with basis and n and the ring
  explanation, a to-scale sketch with rings, rose wedges, cone, numbered
  candidates, north arrow and scale bar, candidates table, nearest hazards,
  warnings, confidence, model and parameters, SHA-256 of the sibling files).
  Print it from the browser to PDF.
- **Hash chain.** Each recording appends one line to `search_areas/records.jsonl`
  (UTF-8, LF): record number, recording time UTC and local, source id, label and
  SHA-256, reference time UTC, `files` (file name → SHA-256) and
  `previous_line_sha256` (SHA-256 of the previous line without its line end, 64
  zeros for the first line). Recordings are serialised, also across GEOSnap
  processes, by the lock file `search_areas/records.lock` (neither evidence nor a
  record file): a recording waits at most 10 s for it and is otherwise refused ("in use by
  another process; nothing was recorded, try again"). The application log
  `output/GEOSnap.log` names every recording with its file hashes.
  `GEOSnap.exe --verify <project directory>` checks the chain and every file
  (`MISMATCH`, `MISSING`, `EXTRA` for a file no record lists). A recording never
  changes the manifest, which excludes `search_areas/`.
- **Chain anchor.** The chain cannot reveal that its last lines were removed
  together with their files, because the shortened chain is still intact. The
  server's answer to every recording therefore carries `last_line_sha256`, the SHA-256 of
  the new last `records.jsonl` line (without its line end); the map shows it in
  the recording confirmation and the application log records it. Keep the hash
  of the last records.jsonl line from the recording confirmation in the case
  file; later, the SHA-256 of the last line must match it. `--verify` prints the
  record count and this hash for every chain and repeats them at the end of its
  report ("COMPARE WITH THE CASE FILE"): an intact chain alone does not prove that
  no recording was removed.
- **Display zone.** The record's local times must carry the offset of the
  project's display zone (`settings.display_timezone` in `metadata.json`) at their
  UTC instants; otherwise the record is refused. With `display = "local"` only
  the agreement of wall clock, offset and UTC is checked: "local" is the system
  zone of the computer, and `metadata.json` records no zone name to compare
  with. The recording time (`recorded_local`) is written in the display zone
  with its offset, like the record's own times. Distances (rings, reaches,
  accuracy) may be up to 20 040 km, half the Earth's circumference, so
  long-distance data (for example a Google history across continents) can be
  recorded.
- **Errors.** When a recording cannot be written, the answer gives the reason
  without paths or tracebacks, for example that `records.jsonl` does not end with
  a line feed (it was changed outside GEOSnap; run `--verify`).
- **Served map only.** Recording works only from a map that GEOSnap serves: key O
  on the summary screen, or, in any later session, key O on the projects screen
  (see [Reopening a finished project](#reopening-a-finished-project)). A later
  recording continues the same `records.jsonl` hash chain. The map file opened
  from disk can download search areas but not record them.

## Speed range records

"Record calculation" in the map's Speed tool saves a range of one source, with the
records the examiner excluded, in the project directory. Like "Record search area", it
works only when the map is served by GEOSnap (key O on the summary or the projects
screen) and every excluded record has a reason.

- **Transfer.** The browser sends the source id, the two ends (sequence number,
  record number, UTC), the excluded records with reason and note, and the figures it
  shows to `POST /<token>/records/speed-range`, under the same checks as the search
  area route (token, loopback `Host`, own `Origin`, JSON, `Content-Length`, at most
  2 MB).
- **Recomputation.** GEOSnap reads the payload of the map file it serves, looks up
  the named records, recomputes every figure in Python
  (`geosnap/analysis/speed_range.py`, the same method as the browser, with the
  uncertainty factor `asc` each record carries in the payload; a map whose records
  carry no `asc` or `cmb_rep` used the radii as reported, and the browser then need
  not send figures as reported). It refuses the recording when a record does not
  match or a figure the browser showed differs by more than floating-point noise ("the figures shown in the browser differ from GEOSnap's …; nothing was
  recorded"). The recorded figures are GEOSnap's own.
- **Files.** `speed_ranges/speed_range_<n>_<stamp>.json` (record type
  `geosnap-speed-range`, version 1: recording time, application, case reference and
  examiner, project, the map file's name and SHA-256, the request, the figures, the
  method and the premise text; the figures include `minimum_average_reported_kmh`
  and `stepwise_minimum_average_reported_kmh`, the minimums with the radii as
  reported) and `speed_ranges/speed_range_<n>_<stamp>.html`, a printable sheet
  (figures with the minimums as reported next to them, excluded records with their
  reasons, method and premise, the JSON file's SHA-256 and the verify command).
- **Hash chain.** Each recording appends one line to `speed_ranges/records.jsonl`
  (record number, recording time, source, both end times, number of excluded
  records, `files` with their SHA-256, `previous_line_sha256`), exactly like
  `search_areas/`, serialised by `speed_ranges/records.lock`. The answer, the map and
  the application log give the SHA-256 of the new last line; write it down in the
  case file. `--verify` checks this chain as well; `speed_ranges/` is outside the manifest.

## Reopening a finished project

Key **P** on the file selection screen lists the project directories below `output/`
(`output/<year>/<month>/<stamp>_<name>`), newest first, with the start time from
`metadata.json` and the number of recorded search areas. R refreshes, Esc returns.

- **Enter** runs the same check as `--verify` in the background and shows its
  lines. A project without `MANIFEST.sha256` (made by an older version, or a run that
  did not finish) can be opened; the screen says that it cannot be verified.
- **O** serves the map of the project opened with Enter through the local server
  and opens the browser, exactly like the summary screen. With verification
  deviations, O still works: the status line starts with the deviation count and
  a warning is logged. The examiner decides; GEOSnap does not hide evidence.
- **Log.** Reopening, the verification result and the serving of the map are
  appended to `reopen.log` in the project directory (and to the application log).
  The run's own `GEOSnap.log` is listed in the manifest and is therefore never
  written to again: appending to it would make every later `--verify` report a
  mismatch. `reopen.log` is outside the manifest (`--verify` names it as such), so
  it is not tamper-evident; the manifest hash noted in the case file remains the
  anchor. `--verify` of an older GEOSnap version may report `reopen.log` as `EXTRA`.
- **Limits.** Only real directories at exactly that depth are listed. Symbolic
  links are not followed (a directory whose resolved path differs from its listed
  path is skipped, which is meant to cover Windows junctions but has not yet been
  tested on Windows), and only the project's own `map_<stamp>.html` (a regular file inside
  the project directory) is served. A project whose `search_areas/` is a link is
  not served, and a recording refuses a linked `search_areas/` or a
  `records.jsonl` that is not a regular file; `reopen.log` is written only when it
  is a regular file. Nothing is re-analysed or edited. The verification itself
  cannot be cancelled; Esc and R wait until it has finished.

## Keys

| Screen | Keys |
|--------|------|
| File selection | Arrow keys, Space marks/unmarks a file, Enter opens the marked files (or the highlighted one), file number opens that file (toggles its mark while files are marked), R refreshes the list (marks are kept), P lists the finished projects, H explains the accepted file formats (in the footer from 64 columns on, the key works at every width), PgUp/PgDn scroll the screen, Esc to quit |
| Input formats (key H) | PgUp/PgDn and the arrow keys scroll the list of accepted formats (per format: what is read, what is required, what is refused, a minimal example), Esc back to the file selection |
| Projects | Arrow keys, Enter verifies the highlighted project, O serves the verified project's map and opens the browser, R refreshes the list, PgUp/PgDn scroll the screen (the verification report starts at the top of the window after Enter), Esc back to the file selection |
| Project setup | Type the project name, Tab through the optional Case reference and Examiner fields, the duplicate switch (Space toggles), each source's name, type (Space opens the menu, Enter picks), colour (type a name or hex value; Down or a typed prefix opens the name list, Enter or a click picks, Esc closes it) and accuracy level (same keys as the type), Case places… (Enter or a click opens the case places dialog), for a CSV source Columns… (Enter or a click opens the column mapping dialog, which also opens on its own for a CSV with a problem or an assumption; its body scrolls on small consoles, OK and Cancel stay visible, Esc cancels; in its preview table the arrow keys choose a line, which is shown in full below the table; in its Time zone field typing filters the zone list, Down opens it, Enter or a click picks), and the Display time zone field (same keys as the zone field; empty keeps `config.toml`), PgUp/PgDn scroll the screen (the arrow keys too, where the focused field does not use them), Enter to start, Esc back. A refusal and the resulting project directory are shown in a line docked above the footer |
| Case places dialog | Opened with Case places… in the project setup (Enter or a click). Type a place into the fields and press Add; Enter or a click on a table row loads it into the fields, Update stores the change, Remove deletes the row; Load file replaces the rows by those of the chosen CSV file; the highlighted row is shown in full below the table (arrow keys while the table has the focus); Tab moves between fields and buttons; the body scrolls on small consoles while the message line and the buttons stay visible; OK keeps the places, Cancel or Esc discards the changes |
| Processing | Esc asks to cancel; partial results are kept; Ctrl+Q asks to cancel and quit; the application closes once the cancelled status is written; PgUp/PgDn scroll the screen |
| Cancel dialog | Y confirms, N or Esc keeps running (also after Ctrl+Q: Y cancels and quits) |
| Every screen | G opens the author's page (https://github.com/ot2i7ba) in the browser; the header shows it as [g]. While a text field or an open list has the focus, G is typed there instead |
| Summary | O opens the map in the browser through the local server at a secret address (see [Online services](#online-services)); only a map served by GEOSnap can record search areas and speed ranges (later sessions: key P on the file selection). Enter returns to the file list; PgUp/PgDn and the arrow keys scroll the summary (sections: Result, Counts, Analysis, Outputs, SHA-256 hashes, Next steps) |

The footer of every screen lists its keys; PgUp/PgDn appears there as "Scroll" (except on
the file selection screen, whose other five keys already fill a 60-column footer). On the file
selection, "h Formats" appears in the footer from a window width of 64 columns; on a
narrower window it is left out rather than cut off, and the screen text names it. The key
itself works at every width. The command palette (Ctrl+P) still opens but is not
listed in the footer.

### Window size

GEOSnap does not need a maximised window. Every text wraps at the window edge (hashes
and paths break anywhere rather than being cut), every screen scrolls vertically, and
dialogs scroll their body while their message line and buttons stay in view. The layout
has been checked at 60×20, 80×24, 100×30 and 120×30 characters (the file selection, the
format help, the projects list, the project setup, both dialogs, processing and the
summary). Below 100 columns the rows of the project setup and the dialog grids are
stacked instead of placed side by side. Below
60×20 a "Window too small" notice covers the screen until the window is enlarged; a
running project keeps running underneath and nothing is lost.

## Configuration

`config.toml` next to the executable is created on first start and documented
inline. Invalid values prevent the start, with a message naming the key; a
`timezone.display` that names a directory of the zone database (`Europe`) counts as
invalid. A `config.toml` saved in an encoding other than UTF-8 is refused with a
message naming the file. Warnings about the configuration (legacy values, implicit defaults) are written to the
application log `output/GEOSnap.log` at start.

**Accuracy and positioning method.** Two keys under `[analysis]` decide how
accuracies and positioning methods enter the analysis (see [Analysis](#analysis)):

```toml
accuracy_confidence = "p95"             # or "reported"
excluded_positioning_methods = ["cell"] # any of gnss, wifi, cell, network; [] keeps all
```

`accuracy_confidence = "p95"` scales the radii of sources at 68 % or of unknown level
by 1.6215 to 95 % wherever an accuracy is used as an uncertainty; `"reported"` uses
them as the source gives them. The accuracy level itself is chosen per source in the
project setup, not in `config.toml`. Any other value of either key prevents the start,
with a message naming the key (for a method list that is not a list of strings: "must be
a list of methods such as ["cell"]"); a method listed twice counts once. Both effective
values are recorded in `metadata.json`
(`settings`) and `analysis.json` (`parameters`) and shown in the report; the project
setup shows them in its run settings.

**Display time zone per run.** `timezone.display` in `config.toml` is the
persistent default. The setup screen shows a **Display time zone** field for the
current run, prefilled with `config.toml`'s zone or, when that says `local`,
with the **host system's zone** by name; the note under the field says which.
The run uses the field's zone (recorded as `settings.display_timezone` in
`metadata.json`, so the products name a zone instead of `local`); an empty field
keeps `config.toml`'s setting. `config.toml` is never changed by the screen.
The display zone is also used to read **GeoJSON times without offset**. Its origin is
recorded as `settings.display_timezone_origin`: `display_setting` (from
`config.toml`), `examiner` (chosen in the field), `host_system` (suggested from
the host clock) or `host_system_confirmed`. A zone that merely comes from the host
is never applied to evidence times without confirmation: with a GeoJSON source the setup
screen shows a switch "Read GeoJSON times without offset in <zone>, the host system's zone"
and refuses to start until it is switched on (Space); a run started any other way
with origin `host_system` rejects such times ("display zone comes from the host system, not
confirmed"). A zone typed or picked in the field needs no confirmation.

**Host zone (forensic caveat).** GEOSnap names the host's zone from, in order,
the `TZ` variable, the Windows registry (`TimeZoneKeyName` under
`HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation`, mapped to an
IANA name), the `/etc/localtime` link into a zoneinfo directory, or
`/etc/timezone`. It is the zone of the **examiner's** computer, not of the
device that recorded the data: as a suggestion for naive CSV times it must be
confirmed in the Columns dialog, and `csv_zone_origin` records that it was taken
from the host. A `TZ` value that is not an IANA name (a POSIX rule such as `UTC0` or
`CET-1CEST,M3.5.0,M10.5.0/3`, or the placeholders `localtime`/`posixrules`)
means the process runs in a zone without a name: nothing is suggested, and the
remaining probes are not consulted. When none of the probes names a zone,
the fields start empty. The Windows mapping is `geosnap/windows_zones.py`, generated from the Unicode CLDR file
`windowsZones.xml` (the `territory="001"` rows, 139 Windows IDs; CLDR data
under the Unicode licence, the notice is in the generated file). The Windows
registry path is covered only by unit tests with a substitute `winreg`; the
developers have not run it on Windows.

**An older `config.toml`.** An existing `config.toml` is kept. If it has no
`online.overpass_fallback_endpoint`, the default applies: when `overpass-api.de`
refuses a live place check from a map opened from disk, the map sends it once to
`https://overpass.private.coffee/api/interpreter` (operated by Private.coffee).
GEOSnap logs a warning at every start while the key is missing. To disable the
fallback, add `overpass_fallback_endpoint = ""` to the `[online]` section; to keep
it without the warning, add the key with the default value. With
`online.enabled = false` nothing is sent and no warning is logged.

## Build and license

Building from source and the license are described in [README.md](README.md).
