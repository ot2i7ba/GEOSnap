# Copyright (c) 2026 ot2i7ba
# https://github.com/ot2i7ba/
# This code is licensed under the MIT License (see LICENSE for details).

"""The place catalogue: category keys, labels, colours, symbols, OSM tag rules and density
limits for both the embedded and the live Overpass search.

The browser receives ``catalogue_document()`` in the map payload and duplicates nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TAG_VALUE_PATTERN = re.compile(r"^[a-z_]+$")
# The maximum search radius per density class; denser features would flood the query.
DENSITY_MAX_RADIUS_M: dict[str, int] = {"sparse": 20_000, "medium": 5_000, "dense": 2_000}

SAFETY = "Safety / authorities"
HEALTH = "Health"
EDUCATION = "Education / children"
TRANSPORT = "Transport"
RETAIL = "Retail / services"
FOOD_LODGING = "Food / lodging"
COMMUNITY = "Community"
LEISURE = "Leisure / outdoor"
WATER_NATURE = "Water / nature"
INFRASTRUCTURE = "Infrastructure / hazard"

GROUP_COLOURS: dict[str, str] = {
    SAFETY: "#1a237e",
    HEALTH: "#d81b60",
    EDUCATION: "#8e24aa",
    TRANSPORT: "#f9a825",
    RETAIL: "#00838f",
    FOOD_LODGING: "#6d4c41",
    COMMUNITY: "#455a64",
    LEISURE: "#43a047",
    WATER_NATURE: "#1e88e5",
    INFRASTRUCTURE: "#bf360c",
}

# Text-default characters need U+FE0F so browsers render them as colour emoji.
VARIATION_SELECTOR_16 = chr(0xFE0F)


@dataclass(frozen=True, slots=True)
class PlaceCategory:
    key: str
    label: str
    group: str
    emoji: str
    density: str
    rules: tuple[tuple[str, frozenset[str]], ...]

    @property
    def colour(self) -> str:
        return GROUP_COLOURS[self.group]


def _rule(tag_key: str, *values: str) -> tuple[str, frozenset[str]]:
    return (tag_key, frozenset(values))


# Canonical order: classification and every listing follow this sequence.
PLACE_CATALOGUE: tuple[PlaceCategory, ...] = (
    PlaceCategory(
        "police", "Police", SAFETY, "\U0001f693", "sparse", (_rule("amenity", "police"),)
    ),
    PlaceCategory(
        "emergency_services",
        "Fire / ambulance station",
        SAFETY,
        "\U0001f692",
        "sparse",
        (_rule("amenity", "fire_station"), _rule("emergency", "ambulance_station")),
    ),
    PlaceCategory(
        "justice",
        "Prison / courthouse",
        SAFETY,
        "\U0001f3db" + VARIATION_SELECTOR_16,
        "sparse",
        (_rule("amenity", "prison", "courthouse"),),
    ),
    PlaceCategory(
        "surveillance_camera",
        "Surveillance camera",
        SAFETY,
        "\U0001f4f9",
        "dense",
        (_rule("man_made", "surveillance"),),
    ),
    PlaceCategory(
        "hospital",
        "Hospital / clinic",
        HEALTH,
        "\U0001f3e5",
        "sparse",
        (_rule("amenity", "hospital", "clinic"),),
    ),
    PlaceCategory(
        "pharmacy",
        "Pharmacy / drugstore",
        HEALTH,
        "\U0001f48a",
        "medium",
        (_rule("amenity", "pharmacy"), _rule("shop", "chemist")),
    ),
    PlaceCategory(
        "social_facility",
        "Social facility / care home",
        HEALTH,
        "\U0001f3e0",
        "medium",
        (_rule("amenity", "social_facility"),),
    ),
    PlaceCategory(
        "kindergarten",
        "Kindergarten / childcare",
        EDUCATION,
        "\U0001f9f8",
        "medium",
        (_rule("amenity", "kindergarten", "childcare"),),
    ),
    PlaceCategory(
        "school",
        "School / college / university",
        EDUCATION,
        "\U0001f3eb",
        "medium",
        (_rule("amenity", "school", "college", "university"),),
    ),
    PlaceCategory(
        "playground",
        "Playground",
        EDUCATION,
        "\U0001f3a0",
        "medium",
        (_rule("leisure", "playground"),),
    ),
    PlaceCategory(
        "sports_facility",
        "Sports facility",
        EDUCATION,
        "⚽",
        "dense",
        (_rule("leisure", "sports_centre", "stadium", "pitch", "fitness_centre", "ice_rink"),),
    ),
    PlaceCategory(
        "railway_station",
        "Railway station / halt / subway entrance",
        TRANSPORT,
        "\U0001f689",
        "sparse",
        (_rule("railway", "station", "halt", "subway_entrance"),),
    ),
    PlaceCategory(
        "transport_stop",
        "Public transport stop / bus station / taxi rank",
        TRANSPORT,
        "\U0001f68f",
        "dense",
        (
            _rule("highway", "bus_stop"),
            _rule("public_transport", "stop_position", "platform"),
            _rule("railway", "tram_stop"),
            _rule("amenity", "bus_station", "taxi"),
        ),
    ),
    PlaceCategory(
        "railway_line",
        "Railway line / level crossing",
        TRANSPORT,
        "\U0001f6e4" + VARIATION_SELECTOR_16,
        "dense",
        (
            _rule(
                "railway",
                "rail",
                "light_rail",
                "subway",
                "tram",
                "narrow_gauge",
                "level_crossing",
                "crossing",
            ),
        ),
    ),
    PlaceCategory(
        "traffic_signals",
        "Traffic signals",
        TRANSPORT,
        "\U0001f6a6",
        "dense",
        (_rule("highway", "traffic_signals"), _rule("crossing", "traffic_signals")),
    ),
    PlaceCategory(
        "fuel_station",
        "Fuel station / motorway services",
        TRANSPORT,
        "⛽",
        "medium",
        (_rule("amenity", "fuel"), _rule("highway", "services", "rest_area")),
    ),
    PlaceCategory(
        "parking",
        "Parking",
        TRANSPORT,
        "\U0001f17f" + VARIATION_SELECTOR_16,
        "dense",
        (_rule("amenity", "parking"),),
    ),
    PlaceCategory(
        "airport_harbour",
        "Airport / harbour / ferry",
        TRANSPORT,
        "✈" + VARIATION_SELECTOR_16,
        "sparse",
        (
            _rule("aeroway", "aerodrome", "heliport", "helipad"),
            _rule("leisure", "marina", "slipway"),
            _rule("amenity", "ferry_terminal"),
            _rule("harbour", "yes"),
            _rule("landuse", "port"),
        ),
    ),
    PlaceCategory(
        "bank_atm", "Bank / ATM", RETAIL, "\U0001f3e7", "medium", (_rule("amenity", "bank", "atm"),)
    ),
    PlaceCategory(
        "supermarket",
        "Supermarket / convenience / kiosk",
        RETAIL,
        "\U0001f6d2",
        "dense",
        (_rule("shop", "supermarket", "convenience", "kiosk", "mall", "department_store"),),
    ),
    PlaceCategory(
        "restaurant",
        "Restaurant / cafe / fast food",
        FOOD_LODGING,
        "\U0001f37d" + VARIATION_SELECTOR_16,
        "dense",
        (_rule("amenity", "restaurant", "cafe", "fast_food", "food_court", "ice_cream"),),
    ),
    PlaceCategory(
        "nightlife",
        "Bar / pub / nightlife / liquor store",
        FOOD_LODGING,
        "\U0001f37a",
        "medium",
        (
            _rule(
                "amenity",
                "bar",
                "pub",
                "biergarten",
                "nightclub",
                "casino",
                "gambling",
                "stripclub",
                "brothel",
                "swingerclub",
            ),
            _rule("shop", "alcohol", "beverages"),
        ),
    ),
    PlaceCategory(
        "lodging",
        "Hotel / hostel / guest house",
        FOOD_LODGING,
        "\U0001f3e8",
        "medium",
        (_rule("tourism", "hotel", "motel", "hostel", "guest_house", "apartment", "chalet"),),
    ),
    PlaceCategory(
        "camp_shelter",
        "Camp site / hut / shelter",
        FOOD_LODGING,
        "⛺",
        "medium",
        (
            _rule("tourism", "camp_site", "caravan_site", "wilderness_hut", "alpine_hut"),
            _rule("amenity", "shelter", "hunting_stand"),
        ),
    ),
    PlaceCategory(
        "place_of_worship",
        "Place of worship",
        COMMUNITY,
        "⛪",
        "medium",
        (_rule("amenity", "place_of_worship", "monastery"),),
    ),
    PlaceCategory(
        "cemetery",
        "Cemetery",
        COMMUNITY,
        "⚰" + VARIATION_SELECTOR_16,
        "sparse",
        (_rule("landuse", "cemetery"), _rule("amenity", "grave_yard")),
    ),
    PlaceCategory(
        "park",
        "Park / green space / nature reserve",
        LEISURE,
        "\U0001f333",
        "medium",
        (
            _rule("leisure", "park", "garden", "nature_reserve", "dog_park"),
            _rule("landuse", "recreation_ground", "village_green"),
        ),
    ),
    PlaceCategory(
        "forest",
        "Forest / wood / scrub",
        LEISURE,
        "\U0001f332",
        "medium",
        (_rule("landuse", "forest"), _rule("natural", "wood", "scrub", "heath")),
    ),
    PlaceCategory(
        "water",
        "Water / river / bathing site",
        WATER_NATURE,
        "\U0001f30a",
        "medium",
        (
            _rule("natural", "water", "wetland", "bay", "beach"),
            _rule("waterway", "river", "stream", "canal"),
            _rule("landuse", "reservoir"),
            _rule("leisure", "swimming_area", "beach_resort"),
        ),
    ),
    PlaceCategory(
        "bridge",
        "Bridge / viaduct",
        INFRASTRUCTURE,
        "\U0001f309",
        "medium",
        (_rule("man_made", "bridge"), _rule("bridge", "viaduct")),
    ),
    PlaceCategory(
        "hazard_site",
        "Cliff / quarry / cave / derelict site",
        INFRASTRUCTURE,
        "⚠" + VARIATION_SELECTOR_16,
        "medium",
        (
            _rule("natural", "cliff", "cave_entrance"),
            _rule("landuse", "quarry", "brownfield", "landfill", "military"),
            _rule("man_made", "adit", "mineshaft"),
            _rule("building", "ruins"),
            _rule("historic", "ruins"),
        ),
    ),
)

PLACE_CATEGORY_KEYS: tuple[str, ...] = tuple(category.key for category in PLACE_CATALOGUE)

# The configured default: the categories left off are those with low relevance for locating
# a device (courthouse/prison, sports, parking, airport/harbour, restaurants, nightlife,
# places of worship, cemeteries).
DEFAULT_ON_KEYS: tuple[str, ...] = (
    "police",
    "emergency_services",
    "surveillance_camera",
    "hospital",
    "pharmacy",
    "social_facility",
    "kindergarten",
    "school",
    "playground",
    "railway_station",
    "transport_stop",
    "railway_line",
    "traffic_signals",
    "fuel_station",
    "bank_atm",
    "supermarket",
    "lodging",
    "camp_shelter",
    "park",
    "forest",
    "water",
    "bridge",
    "hazard_site",
)

_CATEGORIES_BY_KEY: dict[str, PlaceCategory] = {
    category.key: category for category in PLACE_CATALOGUE
}


def category_by_key(key: str) -> PlaceCategory:
    return _CATEGORIES_BY_KEY[key]


def validate_catalogue(catalogue: tuple[PlaceCategory, ...]) -> None:
    """Every tag key and value must match ``[a-z_]+`` so it can never break out of the
    quoted Overpass selector; raises ValueError naming the first offending entry."""
    for category in catalogue:
        for tag_key, values in category.rules:
            for token in (tag_key, *values):
                if not TAG_VALUE_PATTERN.match(token):
                    raise ValueError(f"Catalogue entry {category.key}: invalid tag token '{token}'")


def selectors_for(category: PlaceCategory) -> tuple[str, ...]:
    """Overpass QL tag selectors derived from the rules: one value gives ``["k"="v"]``,
    several give ``["k"~"^(a|b)$"]``. The tokens were validated at import."""
    selectors: list[str] = []
    for tag_key, values in category.rules:
        sorted_values = sorted(values)
        if len(sorted_values) == 1:
            selectors.append(f'["{tag_key}"="{sorted_values[0]}"]')
        else:
            selectors.append(f'["{tag_key}"~"^({"|".join(sorted_values)})$"]')
    return tuple(selectors)


def max_radius_m(category: PlaceCategory) -> int:
    return DENSITY_MAX_RADIUS_M[category.density]


# A malformed catalogue is a programming error: fail at import, not inside a project run.
validate_catalogue(PLACE_CATALOGUE)


def catalogue_document() -> list[dict[str, object]]:
    """The catalogue as plain JSON records for the map payload, in canonical order."""
    return [
        {
            "key": category.key,
            "label": category.label,
            "group": category.group,
            "colour": category.colour,
            "emoji": category.emoji,
            "density": category.density,
            "max_radius_m": max_radius_m(category),
            "selectors": list(selectors_for(category)),
            "rules": [[tag_key, sorted(values)] for tag_key, values in category.rules],
            "default_on": category.key in DEFAULT_ON_KEYS,
        }
        for category in PLACE_CATALOGUE
    ]
