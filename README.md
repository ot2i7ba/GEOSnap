# GEOSnap
GEOSnap turns location records into one interactive map. Hand it a KML export, a GPX track, a CSV list, a Google location history or a text file with one position per line (or several at once) and it lays them over each other, works out where each device stayed, moved and met another one, and writes a self-contained HTML map, a report and a set of exports into one project folder. Every source file and every file of the project is hashed, so you can still show months later that nothing has changed. It runs in the terminal, can work entirely offline, and the map is a single HTML file that opens in any current browser.

> [!NOTE]
> GEOSnap grew out of my earlier tools [UFEDMapper](https://github.com/ot2i7ba/UFEDMapper) and [UFEDKMLstacker](https://github.com/ot2i7ba/UFEDKMLstacker). It is easier to use and does a good deal more than both of them together. More features for reading and working with KML files will follow in coming updates.

> [!IMPORTANT]
> GEOSnap shows where a **device reported** its position, with the accuracy the device stated. It does not show where a person was, and a gap in the records does not mean the device was somewhere else. The crystal ball is an estimate, not a finding.

> [!WARNING]
> Please note that this tool is still under development, and I cannot provide a 100% guarantee that it operates in a forensically sound manner. It is tailored to meet specific needs at this stage. Use it with caution, especially in environments where forensic integrity is critical.

## Why GEOSnap?!
Location data rarely arrives in the shape you want. One phone gives you a KML file, a GPS logger a GPX track, someone else a spreadsheet with the coordinates split over two columns and the time over another two. Getting all of that onto one map usually means a lot of copy and paste, a converter of doubtful origin and at least one moment where latitude and longitude quietly swap places.

My first answer to that was a pair of small Plotly scripts: UFEDMapper drew one KML file, UFEDKMLstacker stacked several of them in different colours. They did the job, but every new question ("when exactly were these two devices at the same place?", "how fast must it have travelled between these two points?") meant another script. GEOSnap is what happened when I stopped writing scripts and wrote the tool instead. It reads the formats I keep running into, overlays any number of sources, answers those questions with documented methods, and keeps a paper trail of everything it did. The terminal interface is built for colleagues who would rather not type commands: arrow keys, Enter, done.

## Table of Contents
- [Why GEOSnap](#why-geosnap)
- [Features](#features)
- [Screenshots](#screenshots)
- [A closer look](#a-closer-look)
- [Requirements](#requirements)
- [Building](#building)
- [Usage](#usage)
- [The project folder](#the-project-folder)
- [Online services and privacy](#online-services-and-privacy)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Project structure](#project-structure)
- [Third-party libraries](#third-party-libraries)
- [License](#license)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)
- [Conclusion](#conclusion)
- [Transparency](#transparency)

## Features
- **Many input formats**: text files with one position per line, KML and KMZ, GPX, CSV, GeoJSON and the Google location exports (Records.json, Timeline and Semantic Location History). Key **H** in the program lists what each format needs. A CSV file gets a column dialog that suggests the mapping and gives the reason for every suggestion; anything it can only guess has to be confirmed.
- **Several sources on one map**: each source gets its own name, type and colour. GEOSnap finds encounters (two sources close in time and place), shared places and joint movement.
- **Stays, gaps and movement**: places where a device lingered, with the time window of its arrival and departure; silences in the records; every step classed as stationary, walking, cycling, vehicle, implausible or unknown.
- **Timeline**: one lane per source below the map, with stays, gaps, encounters and a time cursor you can play back.
- **Presence matrix**: places as rows, sources as columns. It shows at a glance which source was where, how often and for how long, and whether two sources were there at the same time.
- **Crystal ball**: an estimate of where a device is likely to be at a given time, based on its own routine, with search rings, a tendency direction and the days it is based on.
- **Speed tool**: pick two records and get the minimum average speed the data allow, computed on the WGS84 ellipsoid, with the accuracy of both ends taken into account.
- **Case places**: enter locations with a radius and a time window, and GEOSnap gives every source one result: present, possibly present, elsewhere, or no reports in the window.
- **Accuracy handled with care**: reported accuracy radii are scaled to a common 95 % level, every speed comes with the interval its data allow, and records without an accuracy are named as such.
- **Paper trail**: SHA-256 of every source and output file, a report for printing to PDF, a manifest and a `--verify` check for the whole project folder, and hash chains for anything recorded from the map later.
- **Works offline**: the map embeds everything it needs. Online map tiles and place lookups are on by default and can be switched off completely.

## Screenshots
| | |
|---|---|
| ![Several sources on one map](screenshots/overview.png) | ![Timeline](screenshots/timeline.png) |
| Several sources on one map | The timeline below the map |
| ![Presence matrix](screenshots/matrix.png) | ![Crystal ball](screenshots/crystal-ball.png) |
| The presence matrix | The crystal ball |
| ![Speed tool](screenshots/speed.png) | ![Terminal interface](screenshots/tui.png) |
| The speed tool | The terminal interface |

All screenshots show invented sample data.

## A closer look

### Overlaying sources
The real value usually lies between the files, not in one of them. GEOSnap puts every source on the same map in its own colour and compares them: an **encounter** is a time where two sources reported within 10 minutes and 100 metres of each other by default, or within the combined accuracy of the two reports. If both moved together for a while, the encounter becomes a **joint movement**. Stays of two sources at the same spot become **shared places**. Each encounter states its smallest time offset and its closest distance, so you can judge how close "close" really was.

### Timeline
The timeline opens below the map and gives every source its own lane on one time axis. Stays are bars in the source colour, gaps are hatched, and encounters connect the lanes. Click anywhere to move the time cursor; the map then shows each source's position at that moment, and Play runs the whole thing like a film, at a speed you choose.

### Presence matrix
The matrix answers the question that usually comes first: which device reported where, and when? Shared places, case places and the stay places of each source are rows, the sources are columns, and each cell gives the number of visits, the total time and the first and last report. Rows where two sources overlapped in time are highlighted and listed first. Click a cell and the map flies to the place and the time cursor jumps to the visit.

### Crystal ball
Given a reference time (now, by default), the crystal ball estimates where a device is likely to be, from the device's own routine: on which comparable days it was where, how long its stays usually lasted and in which direction it tended to leave. It shows the most likely places with percentages, search rings that held 50, 80 and 95 % of comparable cases, and a tendency direction with its uncertainty. It also tells you how thin its basis is: with only a few comparable days it says so and names no "most likely" place at all. The estimate can be recorded with a hash, as a one-page sheet you can pass on or print.

> [!TIP]
> The crystal ball is statistics, not magic. It has to be acknowledged once per page, it states its basis, and it was backtested on public GPS logger data. Treat it as a well-informed starting point, not as an answer.

### Speed tool
Pick two records of one source and GEOSnap computes how fast the device must at least have travelled between them: the straight line on the WGS84 ellipsoid (GeographicLib), shortened by the accuracy of both ends, divided by the elapsed time plus the time resolution of the source. This holds as long as the true positions lie within their accuracy circles. The path over all records in between is shown as well, but only as an estimate, since position noise makes a path longer and sparse records make it shorter. You can leave out single records with a reason, and the calculation can be recorded with a hash.

## Requirements
- **To run the executable:** Windows 11 and a current browser (Edge, Chrome, Firefox or Brave) for the map. Internet Explorer will politely be told that it is not invited.
- **To build or run from source:** Python 3.11 or newer.
  - Run-time packages: textual, tzdata, geographiclib (see `requirements.txt`)
  - Build: PyInstaller 6.10 or newer

GEOSnap is developed and tested on Windows 11 and Linux. From source it should run anywhere Python and Textual do; macOS has not been tested.

## Building
1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
2. Download or clone this repository and open a command prompt in its folder.
3. Create a virtual environment, install the packages and build:

   ```bat
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt "pyinstaller>=6.10"
   .venv\Scripts\python -m PyInstaller GEOSnap.spec --noconfirm --clean
   ```

   Result: `dist\GEOSnap.exe`, a single console executable.

> [!NOTE]
> On its first start, the executable writes `config.toml` next to itself and creates the `input\` and `output\` folder.

> [!TIP]
> To run GEOSnap without building it, install the packages into the virtual environment (`.venv\Scripts\python -m pip install -r requirements.txt`) and start `.venv\Scripts\python GEOSnap.py`.

## Usage
### Step by step
1. Put the files you want to look at into the `input\` folder next to `GEOSnap.exe`.
2. Start `GEOSnap.exe`. The file list shows the supported files in `input\`; press **H** to see which formats are accepted.
3. Mark one or more files with **Space** and press **Enter**. With nothing marked, a file's number opens just that one.
4. In the project setup, name the project and give each source a name, type, colour and accuracy level. Optionally add a reference and your name (fields *Case reference* and *Examiner*) and places to check the sources against (*Case places…*). For a CSV file, **Columns…** sets the column mapping; the dialog opens by itself when something needs your confirmation.
5. Press **Enter** to start. GEOSnap reads, checks, analyses and writes the project folder.
6. On the summary screen, press **O** to open the map in your browser. The summary also shows the SHA-256 of the manifest: note it down, it is the anchor for every other hash.

To come back to a finished project later, press **P** on the file list; from there you can verify it and open its map again. **G** opens my GitHub page, in case you want to say hello. :)

### Command line
| Command | Purpose |
|---|---|
| `GEOSnap.exe` | Start the terminal application |
| `GEOSnap.exe --verify <project folder>` | Check every file of a project folder against its manifest, the hash chains and the source files |
| `GEOSnap.exe --version` | Print the version |

`--verify` ends with exit code 0 when everything matches, 1 on any deviation and 2 on a call error.

> [!TIP]
> Open the map through GEOSnap (**O** on the summary screen) rather than by double-clicking the HTML file. The map then runs on a private local address, live place checks work with the main Overpass server, and search areas and speed calculations can be recorded into the project.

## The project folder
Every run writes one folder under `output\YYYY\MM\`:

```
output\2026\10\20261001_101500_Demo\
├── map_<stamp>.html                 the interactive map (single file, works offline)
├── report_<stamp>.html              the report: sources, method, results, files and hashes
├── points_<stamp>.csv               every accepted record of every source
├── rejected_<stamp>.csv             records that failed validation, with the reason
├── stays_<stamp>.csv, gaps_…, …     one CSV per analysis (segments, encounters, case places, matrix, ...)
├── route_<stamp>.gpx / .kml         tracks, stays and encounters for other mapping tools
├── analysis.json                    every analysis result in one machine-readable file
├── metadata.json                    sources, hashes, settings and counts of the run
├── GEOSnap.log                      the complete log of this run
├── MANIFEST.sha256                  SHA-256 of every file except the two folders below
├── search_areas\                    crystal ball estimates recorded from the map (only when used)
└── speed_ranges\                    speed calculations recorded from the map (only when used)
```

> [!IMPORTANT]
> CSV cells contain the text of the source files unchanged. Open them in Excel via *Data → From Text/CSV* with every column as text, not by double-clicking, or a cell that starts with `=`, `+`, `-` or `@` may be read as a formula.

## Online services and privacy
> [!WARNING]
> With the default settings, GEOSnap uses online services: the map loads its tiles from a tile server, and the generation asks Overpass for places nearby and Nominatim for addresses. These services see the map area you look at and the positions GEOSnap looks up. The live place check in the map sends the checked area to Overpass as well; from a map opened by double-click it may go once to a fallback server (`overpass_fallback_endpoint`, by default overpass.private.coffee). If the data must not leave your computer, set `enabled = false` in the `[online]` section of `config.toml`. The map then works fully offline, just without a base map.

The report and `metadata.json` list the services used while generating the project, with their endpoints and requests. What the map asks for later in the browser (tiles, live place checks) is not recorded.

## Configuration
Settings live in `config.toml` next to the executable; every key is explained in the file. The ones you are most likely to touch:

| Setting | Default | Meaning |
|---|---|---|
| `[timezone] display` | `"local"` | Time zone for local times, or any IANA name such as `"Europe/Berlin"` |
| `[map] tile_source` | `"online"` | `online`, `local` (tiles from a folder) or `none` |
| `[analysis] stop_radius_m` / `stop_min_minutes` | `50` / `10` | When lingering counts as a stay |
| `[analysis] max_accuracy_m` | `200` | Records with a larger accuracy radius stay on the map but are left out of stays and movement |
| `[analysis] encounter_max_minutes` / `encounter_max_distance_m` | `10` / `100` | When two sources count as having met |
| `[analysis] accuracy_confidence` | `"p95"` | Scale accuracy radii to 95 %, or `"reported"` to use them as given |
| `[online] enabled` | `true` | Master switch for every online service |

## Documentation
This README is the short tour. [MANUAL.md](MANUAL.md) is the full reference: every input format and its rules, every analysis and its thresholds, every map tool, the report, the verification and all settings. Inside the map, the **Help** window explains each tool on the spot, and the report's method section documents every definition the run used.

## Project structure
| Package | Role |
|---|---|
| `geosnap/extraction` | Readers for every input format, coordinate and time notations, duplicates |
| `geosnap/analysis` | Stays, gaps, segments and speeds, encounters, case places, presence matrix, exports |
| `geosnap/mapping` | Builds the single-file map from its templates and the bundled libraries |
| `geosnap/project` | The run itself: workspace, report, manifest, verification, local map server, records from the map |
| `geosnap/online` | Overpass and Nominatim clients and the place catalogue |
| `geosnap/tui` | The terminal interface (Textual) |

## Third-party libraries
GEOSnap relies on these open-source projects. Copyright remains with the respective authors; their licenses apply.

- **Python:** [Textual](https://github.com/Textualize/textual) (MIT) and its dependencies such as Rich and Pygments for the terminal interface, [GeographicLib](https://geographiclib.sourceforge.io/) (MIT) for the geodesic calculations, [tzdata](https://github.com/python/tzdata) (Apache-2.0) for the time zone database.
- **Map:** [Leaflet](https://leafletjs.com/) 1.9.4 and [Leaflet.heat](https://github.com/Leaflet/Leaflet.heat) 0.2.0 (both BSD-2-Clause), [geographiclib-geodesic](https://github.com/geographiclib/geographiclib-js) 2.2.0 (MIT). They are bundled in `geosnap/mapping/vendor/` together with their licenses.
- **Build:** [PyInstaller](https://pyinstaller.org/) (GPL with an exception that allows distributing the programs it builds).
- **Map data:** © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, queried through [Overpass](https://overpass-api.de/) and [Nominatim](https://nominatim.org/). The tile providers and their attributions are shown in the map.

## License
GEOSnap is released under the **[MIT license](LICENSE)**: use it, change it, share it.

## Contributing
Contributions are welcome! Please fork the repository and submit a pull request for review.

## Disclaimer
This software is provided "as is", without warranty of any kind. GEOSnap supports the analysis of location records; it does not replace the judgment and responsibility of the person using it. Location records can be wrong, incomplete or deliberately altered, and every result is only as good as the records behind it. Verify results independently before relying on them, and use the tool only on data you are authorized to process.

## Conclusion
GEOSnap started as "let me just put two KML files on one map" and, as these things go, did not stop there. It will not replace a full GIS suite, and it does not try to. It answers the everyday questions about location records (where, when, how fast, together or not [^1] quickly, with the method written down and the hashes to prove that the files are still the ones you looked at. If it saves you an afternoon of copying coordinates between spreadsheets, or spares a console-averse colleague their first encounter with a command line, it has done its job. The [compiled](https://github.com/ot2i7ba/GEOSnap/releases) version spares you the build step entirely. [^2] 😉

## Transparency
English is not my native language. I used AI-based tools such as DeepL to translate the comments, docstrings, MANUAL.md, this README.md and texts of the application into English. The texts are based on German texts provided by me. In addition, I used the code review feature of Claude (Max plan) with the Fable 5.1 model to review the code.

[^1]: A special thank you to JK for his support!
[^2]: Greetings to PPHA-IuK.