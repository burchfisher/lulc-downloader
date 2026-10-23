"""Anonymous, versioned raster sources, read with HTTP range requests.

NLCD is a community mirror of the USGS C1V1 files. USGS's own S3
bucket is requester-pays; using it is not equivalent to anonymous access.
"""
from __future__ import annotations

from functools import lru_cache
import math
import re

import requests
from shapely.geometry import box, shape

from .models import DownloadRequest


NLCD_BASE = (
    "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/"
    "kylebarron/usgs-landcover/annual-nlcd/c1/v1/cu/mosaic"
)
WORLDCOVER_BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WORLDCOVER_VERSIONS = {2020: "v100", 2021: "v200"}
WORLDCOVER_INDEXES = {
    2020: f"{WORLDCOVER_BASE}/v100/2020/esa_worldcover_2020_grid.geojson",
    2021: f"{WORLDCOVER_BASE}/v200/2021/esa_worldcover_grid.geojson",
}


def worldcover_tile_name(west: int, south: int) -> str:
    """Name a 3-degree cell from its lower-left corner."""
    return f"{'N' if south >= 0 else 'S'}{abs(south):02d}{'E' if west >= 0 else 'W'}{abs(west):03d}"


def worldcover_tiles(geometry: dict) -> list[str]:
    """Intersect exact cells, excluding edge-only contacts and polygon holes."""
    region = shape(geometry)
    west, south, east, north = region.bounds
    result = []
    for y in range(3 * math.floor(south / 3), 3 * math.ceil(north / 3), 3):
        for x in range(3 * math.floor(west / 3), 3 * math.ceil(east / 3), 3):
            if region.intersection(box(x, y, x + 3, y + 3)).area > 0:
                result.append(worldcover_tile_name(x, y))
    return result


@lru_cache(maxsize=2)
def _available_worldcover_tiles(year: int) -> frozenset[str]:
    """Load each official tile inventory once per process, never the rasters."""
    url = WORLDCOVER_INDEXES[year]
    try:
        response = requests.get(url, timeout=(10, 60))
        response.raise_for_status()
        features = response.json()["features"]
        names = frozenset(feature["properties"]["ll_tile"] for feature in features)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Could not load the official WorldCover {year} tile index: {url}") from exc
    if not names or any(not isinstance(name, str) or not re.fullmatch(r"[NS]\d{2}[EW]\d{3}", name) for name in names):
        raise RuntimeError(f"Unexpected tile identifiers in WorldCover {year} index: {url}")
    return names


def resolve_sources(
    request: DownloadRequest,
    period: tuple[str, str, str],
) -> tuple[list[str], dict]:
    """Return raster URLs and provenance for one annual output period."""
    label, start, end_exclusive = period
    year = int(label)
    if request.product == "nlcd":
        if not 1985 <= year <= 2024:
            raise ValueError("The public Annual NLCD C1V1 mirror supports 1985–2024.")
        urls = [f"{NLCD_BASE}/Annual_NLCD_LndCov_{year}_CU_C1V1.tif"]
        return urls, {
            "version": "C1V1",
            "year": year,
            "native_resolution_m": 30,
            "source_nodata": 250,
            "provider": "Kyle Barron / Source Cooperative public mirror of USGS Annual NLCD",
            "source_is_mirror": True,
            "source_catalog": "https://source.coop/kylebarron/usgs-landcover",
            "attribution": "U.S. Geological Survey, Annual NLCD Collection 1.1 Science Products (2025). Public domain.",
            "source_urls": urls,
            "temporal_semantics": "Full calendar-year land-cover map; dates select intersecting years.",
        }
    if request.product == "worldcover":
        if year not in WORLDCOVER_VERSIONS:
            raise ValueError("ESA WorldCover is available only for 2020 and 2021.")
        available = _available_worldcover_tiles(year)
        tiles = [tile for tile in worldcover_tiles(request.region.geometry) if tile in available]
        if not tiles:
            raise ValueError(f"No ESA WorldCover {year} tiles intersect this region.")
        version = WORLDCOVER_VERSIONS[year]
        urls = [f"{WORLDCOVER_BASE}/{version}/{year}/map/ESA_WorldCover_10m_{year}_{version}_{tile}_Map.tif" for tile in tiles]
        return urls, {
            "version": version,
            "year": year,
            "native_resolution_m": 10,
            "source_nodata": 0,
            "provider": "ESA WorldCover official public AWS bucket",
            "source_is_mirror": False,
            "source_catalog": "https://esa-worldcover.org/en/data-access",
            "tile_index_url": WORLDCOVER_INDEXES[year],
            "tiles": tiles,
            "attribution": f"© ESA WorldCover project {year} / Contains modified Copernicus Sentinel data ({year}) processed by ESA WorldCover consortium. CC BY 4.0.",
            "source_urls": urls,
            "temporal_semantics": "Annual snapshot. The 2020 and 2021 algorithms differ; differences are not purely land-cover change.",
        }
    raise ValueError(f"{request.product!r} does not use the public COG provider.")
