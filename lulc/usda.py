"""Anonymous numeric CDL exports from USDA's official CroplandCROS service."""
from __future__ import annotations

import json
import math
import time

import numpy as np
from pyproj import CRS as Projection
import requests
from rasterio.crs import CRS
from rasterio.io import MemoryFile

from .models import DownloadRequest


SERVICE_URL = "https://pdi.scinet.usda.gov/image/rest/services/CDL_WM/ImageServer"
SOURCE_NODATA = 0
OUTPUT_NODATA = 255
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def _get(url: str, params: dict) -> requests.Response:
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=(15, 120))
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 2:
                raise RuntimeError("The public USDA CDL service did not respond after three attempts.") from exc
            time.sleep(2 ** attempt)
            continue
        if response.status_code in RETRY_STATUSES and attempt < 2:
            response.close()
            time.sleep(2 ** attempt)
            continue
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise RuntimeError(f"The public USDA CDL service returned HTTP {status}.")
        return response
    raise RuntimeError("The public USDA CDL request could not be completed.")


def _json(url: str, params: dict | None = None) -> dict:
    response = _get(url, {"f": "json", **(params or {})})
    try:
        data = response.json()
    except (ValueError, requests.JSONDecodeError) as exc:
        raise RuntimeError("The USDA CDL service returned a non-JSON metadata response.") from exc
    finally:
        response.close()
    if not isinstance(data, dict):
        raise RuntimeError("The USDA CDL service returned malformed metadata.")
    if "error" in data:
        error = data["error"]
        message = error.get("message", "Unknown service error") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"The USDA CDL service reported: {message}")
    return data


def _legend() -> dict[int, dict[str, str]]:
    data = _json(f"{SERVICE_URL}/rasterAttributeTable")
    legend = {}
    try:
        for feature in data["features"]:
            values = feature["attributes"]
            code, name = int(values["Value"]), values["Class_Names"]
            if not 0 < code < OUTPUT_NODATA or not name:
                continue
            colors = [int(values[channel]) for channel in ("Red", "Green", "Blue")]
            if any(not 0 <= color <= 255 for color in colors):
                raise ValueError("Invalid color channel")
            legend[code] = {"name": str(name).strip(), "color": "".join(f"{color:02x}" for color in colors)}
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("The USDA CDL service returned an invalid class legend.") from exc
    if not legend:
        raise RuntimeError("The USDA CDL service returned an empty class legend.")
    return legend


def prepare_cdl(request: DownloadRequest, period: tuple[str, str, str]) -> dict:
    """Resolve an exact annual raster, its native resolution, and official legend.

    Raster IDs are discovered from the live catalog rather than assumed from a
    year. Restricting this adapter to 2008 onward guarantees nationwide coverage.
    """
    if request.product != "cdl":
        raise ValueError("The USDA provider supports only the cdl product.")
    year = int(period[0])
    if not 2008 <= year <= 2025:
        raise ValueError("The public USDA CDL provider currently supports 2008–2025.")
    catalog = _json(f"{SERVICE_URL}/query", {
        "where": f"Year = {year} AND Category = 1",
        "outFields": "OBJECTID,Name,Year,Category,LowPS",
        "returnGeometry": "false",
    })
    try:
        candidates = [feature["attributes"] for feature in catalog["features"]
                      if int(feature["attributes"]["Year"]) == year
                      and int(feature["attributes"].get("Category", 1)) == 1]
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("The USDA CDL catalog returned malformed raster records.") from exc
    if not candidates:
        raise ValueError(f"The public USDA service has no national CDL raster for {year}.")
    if len(candidates) != 1 or catalog.get("exceededTransferLimit"):
        raise RuntimeError(f"The USDA CDL catalog has ambiguous raster records for {year}; refusing to choose silently.")
    item = candidates[0]
    try:
        raster_id = int(item["OBJECTID"])
        name = str(item["Name"])
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("The USDA CDL raster record has no valid identifier.") from exc
    native = _json(f"{SERVICE_URL}/{raster_id}/info")
    try:
        resolution = float(native["pixelSizeX"])
        resolution_y = float(native["pixelSizeY"])
        if (native["bandCount"] != 1 or native["pixelType"] != "U8"
                or not math.isfinite(resolution) or resolution <= 0
                or not math.isclose(resolution, resolution_y, rel_tol=0, abs_tol=1e-6)):
            raise ValueError("Expected a single uint8 raster with square pixels")
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("The USDA source is not the expected numeric CDL raster.") from exc
    legend = _legend()
    return {
        "provider": "USDA NASS official CroplandCROS public ImageServer",
        "service_url": SERVICE_URL,
        "collection": "USDA NASS Cropland Data Layer",
        "version": f"USDA-CDL-{year}-{name}",
        "year": year,
        "raster_id": raster_id,
        "raster_name": name,
        "source_assets": [f"{SERVICE_URL}/{raster_id}"],
        "source_urls": [f"{SERVICE_URL}?f=json", f"{SERVICE_URL}/{raster_id}/info?f=json"],
        "native_resolution_m": resolution,
        "native_raster_info": native,
        "source_nodata": SOURCE_NODATA,
        "background_masked": [SOURCE_NODATA],
        "class_legend": legend,
        "legend_url": f"{SERVICE_URL}/rasterAttributeTable?f=json",
        "legend_scope": "Official service-level CDL class definitions; class presence varies by year and region.",
        "source_is_mirror": False,
        "requires_account": False,
        "resampling": "nearest",
        "composite_method": "annual_provider_classification",
        "temporal_semantics": "Full calendar-year crop classification; dates select intersecting harvest years.",
        "license": "Public domain",
        "attribution": f"USDA National Agricultural Statistics Service, {year} Cropland Data Layer.",
        "citation": "USDA NASS Cropland Data Layer. https://www.nass.usda.gov/Research_and_Science/Cropland/SARS1a.php",
    }


def download_cdl_tile(
    source_info: dict, crs: str, transform: list[float], width: int, height: int,
) -> np.ndarray:
    """Export one exact-grid tile of raw crop codes, mapping background to 255."""
    if any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1024
           for n in (width, height)):
        raise ValueError("USDA CDL tile dimensions must be integers from 1 to 1024.")
    if len(transform) != 6 or not all(math.isfinite(value) for value in transform):
        raise ValueError("Grid transform must contain six finite coefficients.")
    a, b, c, d, e, f = transform
    if b != 0 or d != 0 or a <= 0 or e >= 0:
        raise ValueError("USDA exports require a north-up grid with positive pixel width.")
    target_crs = CRS.from_user_input(crs)
    projection = Projection.from_user_input(crs)
    epsg = projection.to_epsg()
    spatial_reference = epsg if epsg else json.dumps({"wkt": projection.to_wkt(version="WKT1_ESRI")})
    raster_id = source_info["raster_id"]
    if isinstance(raster_id, bool) or not isinstance(raster_id, int) or raster_id <= 0:
        raise ValueError("USDA CDL raster ID must be a positive catalog identifier.")
    params = {
        "f": "image", "format": "tiff", "pixelType": "U8",
        "bbox": f"{c},{f + e * height},{c + a * width},{f}",
        "bboxSR": spatial_reference, "imageSR": spatial_reference,
        "size": f"{width},{height}", "adjustAspectRatio": "false",
        "interpolation": "RSP_NearestNeighbor",
        "mosaicRule": json.dumps({"mosaicMethod": "esriMosaicLockRaster", "lockRasterIds": [raster_id], "mosaicOperation": "MT_FIRST"}),
        "renderingRule": json.dumps({"rasterFunction": "None"}),
        "noData": SOURCE_NODATA, "noDataInterpretation": "esriNoDataMatchAny",
    }
    response = _get(f"{SERVICE_URL}/exportImage", params)
    try:
        content = response.content
    finally:
        response.close()
    if content[:2] not in (b"II", b"MM"):
        raise RuntimeError("USDA CDL returned an error or non-TIFF response instead of numeric raster data.")
    try:
        with MemoryFile(content) as memory:
            with memory.open() as dataset:
                if dataset.count != 1 or dataset.dtypes[0] != "uint8":
                    raise ValueError("Expected one uint8 class band, not rendered RGB imagery.")
                if (dataset.width, dataset.height) != (width, height):
                    raise ValueError("Returned dimensions differ from the requested grid.")
                if dataset.crs != target_crs or not np.allclose(
                    tuple(dataset.transform)[:6], transform, rtol=0, atol=1e-6,
                ):
                    raise ValueError("Returned georeferencing differs from the requested grid.")
                values = dataset.read(1, masked=True).filled(OUTPUT_NODATA)
                values[values == SOURCE_NODATA] = OUTPUT_NODATA
                return values
    except Exception as exc:
        raise RuntimeError(f"Invalid USDA CDL GeoTIFF: {exc}") from exc
