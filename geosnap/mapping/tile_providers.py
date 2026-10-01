# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""Built-in raster tile providers selectable in the map.

URL templates, attribution and origins are fixed here and never built from user input.
The Content-Security-Policy of an online map lists the origins of all entries, so the
style can be switched in the browser without regenerating the file. Only providers that
serve tiles without an API key or a Referer header (pages opened from disk) are listed;
each was checked on 2026-09-16.
"""

from __future__ import annotations

from dataclasses import dataclass

OSM_COPYRIGHT = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
)
ESRI_ORIGIN = "https://server.arcgisonline.com"


@dataclass(frozen=True, slots=True)
class TileProvider:
    key: str
    label: str
    url_template: str
    subdomains: tuple[str, ...]
    attribution: str
    max_zoom: int
    origins: tuple[str, ...]
    policy_note: str
    # Name of the style alone, for the map's style menu; label stays the full name.
    short_label: str = ""

    def document(self) -> dict[str, object]:
        """The record the browser payload carries (metadata.json lists only the keys)."""
        return {
            "key": self.key,
            "label": self.label,
            "short_label": self.short_label or self.label,
            "url": self.url_template,
            "subdomains": list(self.subdomains),
            "attribution": self.attribution,
            "max_zoom": self.max_zoom,
            "origins": list(self.origins),
            "policy_note": self.policy_note,
        }


TILE_PROVIDERS: tuple[TileProvider, ...] = (
    TileProvider(
        key="osm-de",
        short_label="OpenStreetMap Germany",
        label="OpenStreetMap Germany (FOSSGIS)",
        url_template="https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        subdomains=(),
        attribution=OSM_COPYRIGHT,
        max_zoom=18,
        origins=("https://tile.openstreetmap.de",),
        policy_note=(
            "Operated by FOSSGIS e.V. under the OpenStreetMap tile usage policy; "
            "serves tiles to pages opened from disk."
        ),
    ),
    TileProvider(
        key="opentopomap",
        short_label="OpenTopoMap",
        label="OpenTopoMap (topography)",
        url_template="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        subdomains=("a", "b", "c"),
        attribution=(
            f"Map data: {OSM_COPYRIGHT}, SRTM | Map style: &copy; "
            '<a href="https://opentopomap.org">OpenTopoMap</a> '
            '(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)'
        ),
        max_zoom=17,
        origins=(
            "https://a.tile.opentopomap.org",
            "https://b.tile.opentopomap.org",
            "https://c.tile.opentopomap.org",
        ),
        policy_note="Volunteer-run service; heavy or automated use is discouraged.",
    ),
    TileProvider(
        key="esri-imagery",
        short_label="Esri Imagery",
        label="Esri World Imagery (aerial)",
        url_template=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/"
            "tile/{z}/{y}/{x}"
        ),
        subdomains=(),
        attribution=(
            "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, "
            "and the GIS User Community"
        ),
        max_zoom=19,
        origins=(ESRI_ORIGIN,),
        policy_note="Esri World Imagery under the Esri terms of use; attribution required.",
    ),
    TileProvider(
        key="esri-topo",
        short_label="Esri Topo",
        label="Esri World Topo",
        url_template=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/"
            "tile/{z}/{y}/{x}"
        ),
        subdomains=(),
        attribution="Tiles &copy; Esri &mdash; Esri, HERE, Garmin, FAO, NOAA, USGS",
        max_zoom=19,
        origins=(ESRI_ORIGIN,),
        policy_note="Esri World Topographic Map under the Esri terms of use; attribution required.",
    ),
)
DEFAULT_TILE_PROVIDER_KEY = "osm-de"
TILE_PROVIDER_KEYS: tuple[str, ...] = tuple(provider.key for provider in TILE_PROVIDERS)
_PROVIDERS_BY_KEY: dict[str, TileProvider] = {provider.key: provider for provider in TILE_PROVIDERS}


def tile_provider_by_key(key: str) -> TileProvider:
    """Look up a built-in provider; settings validation guarantees the key exists."""
    try:
        return _PROVIDERS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"Unknown tile provider: {key}") from None


def all_tile_origins() -> tuple[str, ...]:
    """Every origin a built-in provider loads tiles from, sorted and unique (for the CSP)."""
    return tuple(sorted({origin for provider in TILE_PROVIDERS for origin in provider.origins}))


def tile_providers_document() -> list[dict[str, object]]:
    """The provider list as embedded in the map payload."""
    return [provider.document() for provider in TILE_PROVIDERS]
