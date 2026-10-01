# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

# PyInstaller build specification for GEOSnap (console application, one file).
from PyInstaller.utils.hooks import collect_all, collect_data_files

textual_datas, textual_binaries, textual_hidden_imports = collect_all("textual")

bundled_resources = [
    ("geosnap/mapping/template", "geosnap/mapping/template"),
    ("geosnap/mapping/vendor", "geosnap/mapping/vendor"),
    ("geosnap/tui/styles.tcss", "geosnap/tui"),
    ("config.toml", "."),
]

analysis = Analysis(
    ["GEOSnap.py"],
    pathex=[],
    binaries=textual_binaries,
    datas=bundled_resources + textual_datas + collect_data_files("tzdata"),
    hiddenimports=textual_hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

archive = PYZ(analysis.pure)

executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="GEOSnap",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon="assets/GEOSnap.ico",
)
