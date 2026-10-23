"""Explicit, versioned product definitions; class codes are never harmonized silently."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    resolution: int
    first_year: int
    last_year: int | None
    years: tuple[int, ...] | None
    requires_ee: bool
    coverage: str
    description: str
    source: str
    classes: dict[int, tuple[str, str]]
    resolution_note: str = ""
    useful_for: str = ""
    interpretation_note: str = ""


NLCD_CLASSES = {
    11: ("Open water", "466b9f"), 12: ("Perennial ice/snow", "d1def8"),
    21: ("Developed, open space", "dec5c5"), 22: ("Developed, low intensity", "d99282"),
    23: ("Developed, medium intensity", "eb0000"), 24: ("Developed, high intensity", "ab0000"),
    31: ("Barren land", "b3ac9f"), 41: ("Deciduous forest", "68ab5f"),
    42: ("Evergreen forest", "1c5f2c"), 43: ("Mixed forest", "b5c58f"),
    52: ("Shrub/scrub", "ccb879"), 71: ("Grassland/herbaceous", "dfdfc2"),
    81: ("Pasture/hay", "dcd939"), 82: ("Cultivated crops", "ab6c28"),
    90: ("Woody wetlands", "b8d9eb"), 95: ("Emergent herbaceous wetlands", "6c9fb8"),
}
DW_CLASSES = dict(enumerate([
    ("Water", "419bdf"), ("Trees", "397d49"), ("Grass", "88b053"),
    ("Flooded vegetation", "7a87c6"), ("Crops", "e49635"),
    ("Shrub and scrub", "dfc35a"), ("Built", "c4281b"),
    ("Bare", "a59b8f"), ("Snow and ice", "b39fe1"),
]))
WC_CLASSES = {
    10: ("Tree cover", "006400"), 20: ("Shrubland", "ffbb22"),
    30: ("Grassland", "ffff4c"), 40: ("Cropland", "f096ff"),
    50: ("Built-up", "fa0000"), 60: ("Bare/sparse vegetation", "b4b4b4"),
    70: ("Snow and ice", "f0f0f0"), 80: ("Permanent water", "0064c8"),
    90: ("Herbaceous wetland", "0096a0"), 95: ("Mangroves", "00cf75"),
    100: ("Moss and lichen", "fae6a0"),
}
PRODUCTS = {
    "nlcd": Product("nlcd", "Annual NLCD (Collection 1.1)", 30, 1985, 2024, None, False, "Conterminous U.S.",
        "Annual maps, 1985–2024, via a public Source Cooperative mirror. The newer USGS Collection 1.2 (through 2025) is not served by this adapter.",
        "https://www.usgs.gov/centers/eros/science/annual-nlcd-data-access", NLCD_CLASSES,
        resolution_note="30 m pixels (about 900 m² each).",
        useful_for="Long-term U.S. land-cover comparisons, including forests, wetlands, agriculture, and developed land.",
        interpretation_note="Each output represents a full year. This app serves Collection 1.1 through 2024; the newer Collection 1.2 is not available here."),
    "dynamic-world": Product("dynamic-world", "Dynamic World", 10, 2015, None, None, True, "Global, Sentinel-2 coverage",
        "Date-window composite: class with highest mean probability across valid observations. Available from 2015-06-27.",
        "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1", DW_CLASSES,
        resolution_note="10 m pixels (about 100 m² each).",
        useful_for="Recent land cover over custom seasonal or annual windows, with probabilities for nine broad classes.",
        interpretation_note="This app averages class probabilities across valid observations in your date window. Clouds and missing observations can leave gaps; confidence is the highest mean probability, not an accuracy guarantee."),
    "worldcover": Product("worldcover", "ESA WorldCover", 10, 2020, 2021, (2020, 2021), False, "Global land",
        "Two annual snapshots (2020 v100, 2021 v200). Algorithm changes mean differences are not purely land-cover change.",
        "https://esa-worldcover.org/en/data-access", WC_CLASSES,
        resolution_note="10 m pixels (about 100 m² each).",
        useful_for="A global reference map with 11 broad land-cover classes, including mangroves, wetlands, and built-up land.",
        interpretation_note="Only two annual snapshots are available: 2020 v100 and 2021 v200. Their algorithms differ, so map differences do not measure land-cover change alone."),
    "cdl": Product("cdl", "USDA Cropland Data Layer", 30, 2008, 2025, None, False, "Conterminous U.S. (nationwide since 2008)",
        "Annual crop types downloaded directly from USDA, with no account required. 30 m through 2023; native 10 m from 2024.",
        "https://www.nass.usda.gov/Research_and_Science/Cropland/Release/", {},
        resolution_note="30 m through 2023; 10 m from 2024 (about 900 m² and 100 m² per pixel).",
        useful_for="Annual crop-type mapping, such as corn and soybeans, and comparing cropping patterns across years.",
        interpretation_note="Downloads come directly from USDA without an account. The resolution changes in 2024; use a common output resolution when comparing years. Crop class codes are preserved."),
}


def get_product(product_id: str) -> Product:
    try:
        return PRODUCTS[product_id]
    except KeyError as exc:
        raise ValueError(f"Unknown product {product_id!r}. Choose: {', '.join(PRODUCTS)}") from exc
