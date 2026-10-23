"""Authenticated Earth Engine sources and bounded, exact-grid GeoTIFF downloads."""
from __future__ import annotations

import math
import os
import time
from typing import Any

import numpy as np
import requests
from rasterio.crs import CRS
from rasterio.io import MemoryFile

from .models import DownloadRequest


NODATA = 255
PROBABILITY_NODATA = -9999
DW_COLLECTION = "GOOGLE/DYNAMICWORLD/V1"
CDL_COLLECTION = "USDA/NASS/CDL"
DW_BANDS = [
    "water", "trees", "grass", "flooded_vegetation", "crops",
    "shrub_and_scrub", "built", "bare", "snow_and_ice",
]
DW_EXPORT_BANDS = ["class", *DW_BANDS, "confidence"]
DW_ATTRIBUTION = (
    "This dataset is produced for the Dynamic World Project by Google in partnership "
    "with National Geographic Society and the World Resources Institute. "
    "Contains modified Copernicus Sentinel data [2015-present]."
)


def _initialize_ee(project: str | None):
    project = (project or os.environ.get("EARTHENGINE_PROJECT", "")).strip()
    if not project:
        raise RuntimeError(
            "Earth Engine needs a registered Google Cloud project. Set --project "
            "or EARTHENGINE_PROJECT, then authenticate once with 'earthengine authenticate'."
        )
    try:
        import ee
    except ImportError as exc:
        raise RuntimeError("Dynamic World needs the Earth Engine extra. Install it with: python -m pip install '.[earthengine]'") from exc
    try:
        ee.Initialize(project=project)
    except Exception as exc:
        raise RuntimeError(
            f"Could not initialize Earth Engine with project {project!r}. "
            "Run 'earthengine authenticate' and ensure the project is registered for "
            "Earth Engine with the API enabled and your account has access."
        ) from exc
    return ee, project


def prepare_ee(
    request: DownloadRequest, period: tuple[str, str, str]
) -> tuple[Any, dict]:
    """Build clipped classes and, for Dynamic World, their mean probabilities.

    No interactive login is started. Annual CDL resolution and legend come from
    the selected live asset, because the provider and EE catalog can differ.
    """
    if request.product not in ("dynamic-world", "cdl"):
        raise ValueError("Earth Engine supports dynamic-world and cdl in this pipeline.")
    ee, project = _initialize_ee(request.project)
    label, start, end_exclusive = period
    region = ee.Geometry(request.region.geometry, proj="EPSG:4326", geodesic=False)
    collection_id = DW_COLLECTION if request.product == "dynamic-world" else CDL_COLLECTION
    collection = (ee.ImageCollection(collection_id)
                  .filterDate(start, end_exclusive).filterBounds(region))
    try:
        count = int(collection.size().getInfo())
    except Exception as exc:
        raise RuntimeError(f"Could not query Earth Engine availability for {collection_id}.") from exc
    if count == 0:
        raise ValueError(
            f"No {request.product} images are available for this region during "
            f"{start} through {end_exclusive} (exclusive). Choose another period; "
            "new provider releases may not yet be available in Earth Engine."
        )

    metadata = {
        "provider": "Google Earth Engine",
        "collection": collection_id,
        "project": project,
        "period": label,
        "start": start,
        "end_exclusive": end_exclusive,
        "source_image_count": count,
        "nodata": NODATA,
        "resampling": "nearest",
    }
    if request.product == "dynamic-world":
        # Average probabilities before deciding the class; averaging numeric class
        # codes or pairing a modal label with another class's confidence is invalid.
        mean = collection.select(DW_BANDS).mean()
        confidence = mean.reduce(ee.Reducer.max())
        result = (mean.toArray().arrayArgmax().arrayGet([0]).rename("class")
                  .updateMask(confidence.gte(request.min_confidence)).toUint8())
        source_indices = collection.aggregate_array("system:index").getInfo()
        metadata.update({
            "source_assets": [f"{collection_id}/{index}" for index in sorted(source_indices)],
            "native_resolution_m": 10.0,
            "composite_method": "argmax_of_mean_class_probabilities",
            "composite_description": (
                "Mean of each of the nine class probabilities over valid, cloud-masked "
                "observations, followed by argmax. Ties choose the first class in "
                "probability_band_order. Pixels without valid observations are nodata. "
                "Confidence is the maximum mean probability, not a calibrated accuracy."
            ),
            "probability_band_order": DW_BANDS.copy(),
            "export_band_order": DW_EXPORT_BANDS.copy(),
            "probability_bands": DW_BANDS.copy(),
            "probability_dtype": "float32",
            "probability_nodata": PROBABILITY_NODATA,
            "probability_range": [0.0, 1.0],
            "probability_aggregation": "time_mean_of_each_class_probability_over_valid_observations",
            "probability_mask": (
                "No valid observations and pixels outside the requested region are nodata. "
                "The minimum confidence threshold applies only to the class band; "
                "probabilities and confidence retain all observed pixels."
            ),
            "confidence_band": "confidence",
            "confidence_definition": "maximum_of_the_nine_mean_class_probabilities",
            "minimum_mean_class_probability": request.min_confidence,
            "license": "CC BY 4.0",
            "attribution": DW_ATTRIBUTION,
            "citation": "Brown et al. (2022), https://doi.org/10.1038/s41597-022-01307-4",
            "source": "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1",
        })
    else:
        if count != 1:
            raise ValueError(f"Expected one annual CDL image for {label}; found {count}.")
        source = ee.Image(collection.first())
        crop = source.select("cropland")
        source_properties = source.toDictionary([
            "system:index", "system:time_start", "cropland_class_values",
            "cropland_class_names", "cropland_class_palette",
        ]).getInfo()
        native_resolution = float(crop.projection().nominalScale().getInfo())
        if not math.isfinite(native_resolution) or native_resolution <= 0:
            raise RuntimeError("Earth Engine returned an invalid native CDL resolution.")
        # CDL zero is background; Dynamic World zero, in contrast, is valid water.
        result = crop.updateMask(crop.neq(0)).rename("class").toUint8()
        values = source_properties.get("cropland_class_values", [])
        names = source_properties.get("cropland_class_names", [])
        palette = source_properties.get("cropland_class_palette", [])
        legend = {
            int(value): {"name": name, "color": color}
            for value, name, color in zip(values, names, palette)
            if int(value) != 0
        }
        metadata.update({
            "source_assets": [f"{collection_id}/{source_properties.get('system:index', label)}"],
            "native_resolution_m": native_resolution,
            "native_projection": crop.projection().getInfo(),
            "source_properties": source_properties,
            "class_legend": legend,
            "composite_method": "annual_provider_classification",
            "date_interpretation": "Annual crop classification; year refers to harvest year.",
            "background_masked": [0],
            "license": "Public domain",
            "attribution": "USDA National Agricultural Statistics Service (USDA-NASS), Cropland Data Layer.",
            "citation": (
                f"USDA National Agricultural Statistics Service Cropland Data Layer. {label}. "
                "Published crop-specific data layer. USDA-NASS, Washington, DC. "
                "https://www.nass.usda.gov/Research_and_Science/Cropland/SARS1a.php"
            ),
            "source": "https://developers.google.com/earth-engine/datasets/catalog/USDA_NASS_CDL",
        })

    # Clip first: expanding the footprint before this step could populate outside
    # the requested polygon. The expanded filled image permits exact tile grids.
    result = result.clip(region).unmask(NODATA, sameFootprint=False).toUint8()
    if request.product == "dynamic-world":
        # Retain the unthresholded distributions: a low-confidence class is still
        # associated with useful probabilities and must not erase those values.
        probabilities = mean.clip(region).unmask(PROBABILITY_NODATA, sameFootprint=False).toFloat()
        confidence_band = (confidence.rename("confidence").clip(region)
                           .unmask(PROBABILITY_NODATA, sameFootprint=False).toFloat())
        result = result.addBands(probabilities).addBands(confidence_band).select(DW_EXPORT_BANDS)
    return result, metadata


def download_ee_tile(
    image, crs: str, transform: list[float], width: int, height: int
) -> np.ndarray:
    """Fetch a single-band uint8 class tile (up to 1024² pixels).

    The input must already be clipped and filled by prepare_ee. Retry transient
    HTTP/server and connection errors for up to three total attempts.
    """
    return _download_tile(
        image, crs, transform, width, height,
        bands=["class"], dtype="uint8", max_dimension=1024, nodata_values=[NODATA],
    )[0]


def download_dw_tile(
    image, crs: str, transform: list[float], width: int, height: int
) -> np.ndarray:
    """Fetch class, nine probabilities, and confidence as an 11-band float32 tile.

    A common floating type avoids truncating probabilities in the transfer.
    Class codes (including 255 nodata) remain exact; probability/confidence
    nodata is -9999. The 512² limit keeps eleven float32 bands under 32 MB.
    """
    return _download_tile(
        image.select(DW_EXPORT_BANDS).toFloat(), crs, transform, width, height,
        bands=DW_EXPORT_BANDS, dtype="float32", max_dimension=512,
        nodata_values=[NODATA] + [PROBABILITY_NODATA] * (len(DW_EXPORT_BANDS) - 1),
    )


def _download_tile(
    image, crs: str, transform: list[float], width: int, height: int,
    *, bands: list[str], dtype: str, max_dimension: int, nodata_values: list[int],
) -> np.ndarray:
    """Shared HTTP handling and validation; return bands × rows × columns."""
    if any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= max_dimension
           for n in (width, height)):
        raise ValueError(f"Earth Engine tile width and height must be integers from 1 to {max_dimension}.")
    if len(transform) != 6 or not all(math.isfinite(v) for v in transform):
        raise ValueError("Grid transform must contain six finite affine coefficients.")
    if transform[0] * transform[4] - transform[1] * transform[3] == 0:
        raise ValueError("Grid transform must be invertible.")
    target_crs = CRS.from_user_input(crs)
    params = {
        "bands": bands, "format": "GEO_TIFF", "crs": crs,
        "crs_transform": list(transform), "dimensions": [width, height],
    }
    try:
        url = image.getDownloadURL(params)
    except Exception as exc:
        raise RuntimeError("Earth Engine could not create a tile download URL.") from exc

    for attempt in range(3):
        try:
            response = requests.get(url, timeout=(15, 120))
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 2:
                raise RuntimeError("Earth Engine tile download failed after three attempts.") from exc
            time.sleep(2 ** attempt)
            continue
        status = response.status_code
        if status in (408, 429, 500, 502, 503, 504) and attempt < 2:
            response.close()
            time.sleep(2 ** attempt)
            continue
        if status != 200:
            response.close()
            raise RuntimeError(f"Earth Engine tile download failed (HTTP {status}).")
        content = response.content
        response.close()
        break

    try:
        with MemoryFile(content) as memory:
            with memory.open() as dataset:
                if dataset.count != len(bands) or any(actual != dtype for actual in dataset.dtypes):
                    raise ValueError(f"Expected {len(bands)} {dtype} band(s).")
                if (dataset.width, dataset.height) != (width, height):
                    raise ValueError("Returned raster dimensions differ from the requested grid.")
                if dataset.crs != target_crs or not np.allclose(
                    tuple(dataset.transform)[:6], transform, rtol=0, atol=1e-7
                ):
                    raise ValueError("Returned raster georeferencing differs from the requested grid.")
                data = dataset.read(masked=True)
                fill = np.asarray(nodata_values, dtype=dtype)[:, None, None]
                return np.where(np.ma.getmaskarray(data), fill, np.asarray(data)).astype(dtype, copy=False)
    except Exception as exc:
        raise RuntimeError(f"Invalid Earth Engine tile GeoTIFF: {exc}") from exc
