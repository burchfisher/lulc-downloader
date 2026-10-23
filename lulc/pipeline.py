"""Windowed downloads on one explicit projected grid, with polygon masks and provenance."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from pyproj import CRS
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds, transform_geom
from rasterio.windows import Window, transform as window_transform
from shapely.geometry import mapping, shape

from . import __version__
from .catalog import get_product
from .models import DownloadRequest

NODATA = 255
BLOCK_SIZE = 1024
DW_BLOCK_SIZE = 512
PROBABILITY_NODATA = -9999.0


@dataclass(frozen=True)
class Grid:
    crs: str
    transform: Affine
    width: int
    height: int
    resolution: float

    @property
    def pixels(self):
        return self.width * self.height


def make_grid(request: DownloadRequest, resolution: float) -> Grid:
    resolution = float(resolution)
    bounds = transform_bounds("EPSG:4326", request.crs, *request.region.bounds, densify_pts=41)
    if not all(math.isfinite(v) for v in bounds):
        raise ValueError("Region cannot be represented in the selected output CRS.")
    left = math.floor(bounds[0] / resolution) * resolution
    bottom = math.floor(bounds[1] / resolution) * resolution
    right = math.ceil(bounds[2] / resolution) * resolution
    top = math.ceil(bounds[3] / resolution) * resolution
    width, height = round((right - left) / resolution), round((top - bottom) / resolution)
    if width <= 0 or height <= 0:
        raise ValueError("Region does not contain an output pixel at this resolution.")
    if width * height > request.max_pixels:
        raise ValueError(f"This region needs {width * height:,} pixels at {resolution:g} m, above the {request.max_pixels:,}-pixel limit. Use a smaller region, coarser resolution, or increase max_pixels deliberately.")
    return Grid(CRS.from_user_input(request.crs).to_string(), Affine(resolution, 0, left, 0, -resolution, top), width, height, resolution)


def _identity(request, period, grid):
    identity = {
        "pipeline_version": __version__, "product": request.product,
        "period": period, "region": request.region.geometry,
        "crs": grid.crs, "resolution": grid.resolution,
        "min_confidence": request.min_confidence,
    }
    if request.product == "dynamic-world":
        identity["output_schema"] = "class-probabilities-confidence-v1"
    elif request.product == "cdl":
        identity["source_provider"] = "usda-croplandcros-v1"
    return identity


def _path(request, period, grid):
    encoded = json.dumps(_identity(request, period, grid), sort_keys=True).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()[:12]
    return request.output_dir / f"{request.product}_{period[0]}_{fingerprint}.tif"


@dataclass(frozen=True)
class RasterOutput:
    path: Path
    kind: str
    dtype: str
    nodata: float
    bands: tuple[str, ...]

    @property
    def count(self):
        return len(self.bands)


def _output_specs(request, period, grid):
    path = _path(request, period, grid)
    specs = [RasterOutput(path, "class", "uint8", NODATA, ("land_cover_class",))]
    if request.product == "dynamic-world":
        from .earth_engine import DW_BANDS
        specs.extend([
            RasterOutput(path.with_stem(path.stem + "_probabilities"), "probabilities", "float32", PROBABILITY_NODATA, tuple(DW_BANDS)),
            RasterOutput(path.with_stem(path.stem + "_confidence"), "confidence", "float32", PROBABILITY_NODATA, ("confidence",)),
        ])
    return specs


def plan(request: DownloadRequest) -> list[dict]:
    """Validate and estimate outputs without network access or credentials."""
    result = []
    for period in request.periods:
        grid = make_grid(request, request.scale_for(period[0]))
        specs = _output_specs(request, period, grid)
        bytes_per_pixel = sum(spec.count * np.dtype(spec.dtype).itemsize for spec in specs)
        estimated_bytes = grid.pixels * bytes_per_pixel
        result.append({
            "period": period[0], "width": grid.width, "height": grid.height,
            "pixels": grid.pixels, "resolution": grid.resolution, "crs": grid.crs,
            "estimated_bytes": estimated_bytes,
            "estimated_mb": round(estimated_bytes / 1_000_000, 2),
            "output": str(specs[0].path),
            "output_paths": [str(spec.path) for spec in specs],
            "raster_count": len(specs),
            "note": f"{bytes_per_pixel} bytes per pixel across {len(specs)} raster(s) before compression; source transfer can be larger.",
        })
    return result


def _windows(grid, block_size=BLOCK_SIZE):
    for row in range(0, grid.height, block_size):
        for col in range(0, grid.width, block_size):
            yield Window(col, row, min(block_size, grid.width-col), min(block_size, grid.height-row))


def _atomic_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json.partial", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def projected_region(request, resolution):
    # Longitude/latitude edges are curved in a projected CRS. Densify before
    # transforming so a large geographic rectangle does not become a quadrilateral.
    # At most 16 output pixels of latitude per segment, capped at 0.05 degrees.
    segment = min(0.05, resolution * 16 / 111_320)
    dense = shape(request.region.geometry).segmentize(segment)
    return transform_geom("EPSG:4326", request.crs, mapping(dense))


def _source_signature(metadata):
    return {key: sorted(metadata[key]) if key in ("source_assets", "source_urls") else metadata[key]
            for key in ("version", "source_assets", "source_urls", "collection") if key in metadata}


def _cached(spec, request, period, grid, source_info):
    path = spec.path
    sidecar = path.with_suffix(".json")
    if not path.exists() and not sidecar.exists():
        return False
    if path.exists() and sidecar.exists():
        try:
            metadata = json.loads(sidecar.read_text())
            # Compare serialized forms so GeoJSON tuples and JSON lists agree.
            expected = json.loads(json.dumps(_identity(request, period, grid)))
            if metadata.get("status") == "complete" and metadata.get("request") == expected:
                if _source_signature(metadata.get("source", {})) != _source_signature(source_info):
                    raise FileExistsError(f"Source data changed since {path.name} was downloaded. Choose a new output folder to save the updated data and preserve the prior snapshot.")
                with rasterio.open(path) as existing:
                    if (existing.width == grid.width and existing.height == grid.height
                            and existing.nodata == spec.nodata and existing.count == spec.count
                            and all(dtype == spec.dtype for dtype in existing.dtypes)
                            and existing.crs == rasterio.crs.CRS.from_user_input(grid.crs)
                            and existing.transform.almost_equals(grid.transform)
                            and metadata.get("output_type", "class") == spec.kind
                            and (spec.kind == "class" or existing.descriptions == spec.bands)):
                        return True
        except FileExistsError:
            raise
        except (OSError, ValueError, rasterio.errors.RasterioError):
            pass
    raise FileExistsError(f"Incomplete or mismatched output already exists: {path}. Move/remove that TIFF and its JSON sidecar or choose another output folder.")


def _split_dw_tile(tile, tile_shape):
    """Validate floating-point probabilities before separating the class band."""
    if tile.shape != (11, *tile_shape) or tile.dtype != np.dtype("float32"):
        raise ValueError("Dynamic World must return 11 float32 bands on the requested grid.")
    labels, probabilities, confidence = tile[0], tile[1:10], tile[10:11]
    if not np.all((labels == NODATA) | ((labels >= 0) & (labels <= 8) & (labels == np.floor(labels)))):
        raise ValueError("Dynamic World returned invalid class codes.")
    missing = probabilities == PROBABILITY_NODATA
    valid = ~missing.all(axis=0)
    if np.any(missing.any(axis=0) != missing.all(axis=0)) or np.any((confidence[0] == PROBABILITY_NODATA) != ~valid):
        raise ValueError("Dynamic World probability bands have inconsistent NoData masks.")
    if np.any((labels != NODATA) & ~valid):
        raise ValueError("Dynamic World classes have no corresponding probabilities.")
    if valid.any():
        values = probabilities[:, valid]
        scores = confidence[0, valid]
        if not np.all(np.isfinite(values) & (values >= 0) & (values <= 1)):
            raise ValueError("Dynamic World probabilities must be finite values from 0 to 1.")
        if not np.allclose(values.sum(axis=0), 1, rtol=0, atol=1e-4):
            raise ValueError("Dynamic World class probabilities do not sum to one.")
        if not np.allclose(scores, values.max(axis=0), rtol=0, atol=1e-6):
            raise ValueError("Dynamic World confidence does not match the maximum mean probability.")
    return labels.astype("uint8"), probabilities, confidence


def download(request: DownloadRequest, progress=None) -> list[Path]:
    """Download annual class maps or the three-file Dynamic World bundle.

    Dynamic World returns class, nine-band mean probabilities, and confidence
    GeoTIFF paths in that order. Each has its own provenance JSON. Existing
    complete bundles are reused; incomplete bundles are never overwritten.
    """
    progress = progress or (lambda message: None)
    plan(request)
    product = get_product(request.product)
    dynamic_world = request.product == "dynamic-world"
    outputs = []
    for period in request.periods:
        progress(f"Preparing {product.name}: {period[0]}")
        if product.requires_ee:
            from .earth_engine import prepare_ee, download_ee_tile, download_dw_tile
            ee_image, source_info = prepare_ee(request, period)
            scale = request.resolution or source_info.get("native_resolution_m") or request.scale_for(period[0])
            sources = []
        elif request.product == "cdl":
            from .usda import prepare_cdl, download_cdl_tile
            source_info = prepare_cdl(request, period)
            scale = request.resolution or source_info["native_resolution_m"]
            sources = []
        else:
            from .public_sources import resolve_sources
            sources, source_info = resolve_sources(request, period)
            scale = request.scale_for(period[0])
            if not sources:
                raise ValueError("No source tiles intersect this region for the requested year.")
        grid = make_grid(request, float(scale))
        specs = _output_specs(request, period, grid)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        cached = [_cached(spec, request, period, grid, source_info) for spec in specs]
        if all(cached):
            progress(f"Reusing complete output: {specs[0].path.name}")
            outputs.extend(spec.path for spec in specs)
            continue
        if any(cached):
            raise FileExistsError("An output bundle is incomplete. Choose a new output folder or move/remove all of this period's rasters and sidecars before downloading again.")
        polygon = projected_region(request, grid.resolution)
        profile = dict(driver="GTiff", width=grid.width, height=grid.height,
                       crs=grid.crs, transform=grid.transform,
                       tiled=True, blockxsize=256, blockysize=256, compress="deflate", BIGTIFF="IF_SAFER")
        temporary = {}
        published = []
        counts = np.zeros(256, dtype=np.int64)
        observed_pixels = 0
        block_size = DW_BLOCK_SIZE if dynamic_world else BLOCK_SIZE
        total = math.ceil(grid.width / block_size) * math.ceil(grid.height / block_size)
        try:
            with ExitStack() as stack:
                stack.enter_context(rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY=3, GDAL_HTTP_RETRY_DELAY=1, GDAL_HTTP_TIMEOUT=120))
                readers = []
                for source in sources:
                    src = stack.enter_context(rasterio.open(source))
                    readers.append(stack.enter_context(WarpedVRT(src, crs=grid.crs,
                        transform=grid.transform, width=grid.width, height=grid.height,
                        src_nodata=source_info.get("source_nodata", src.nodata), nodata=NODATA,
                        dtype="uint8", resampling=Resampling.nearest)))
                targets = {}
                for spec in specs:
                    descriptor, name = tempfile.mkstemp(suffix=".tif.partial", dir=request.output_dir)
                    temporary[spec.kind] = Path(name)
                    os.close(descriptor)
                    target = stack.enter_context(rasterio.open(name, "w", **profile,
                        count=spec.count, dtype=spec.dtype, nodata=spec.nodata,
                        predictor=3 if spec.dtype == "float32" else 2))
                    targets[spec.kind] = target
                    for band, name in enumerate(spec.bands, 1):
                        target.set_band_description(band, name)
                    target.update_tags(product=request.product, period=period[0], resampling="nearest",
                                       source=product.source, output_type=spec.kind)
                    if spec.kind == "class" and product.classes:
                        palette = {code: tuple(bytes.fromhex(color)) + (255,) for code, (_, color) in product.classes.items()}
                        palette[NODATA] = (0, 0, 0, 0)
                        target.write_colormap(1, palette)
                    elif spec.kind == "class" and source_info.get("class_legend"):
                        palette = {int(code): tuple(bytes.fromhex(entry["color"].lstrip("#"))) + (255,)
                                   for code, entry in source_info["class_legend"].items() if entry.get("color")}
                        palette[NODATA] = (0, 0, 0, 0)
                        target.write_colormap(1, palette)
                    elif spec.kind != "class":
                        target.update_tags(units="probability", valid_range="0,1", confidence_threshold_applied="false")
                for index, window in enumerate(_windows(grid, block_size), 1):
                    tile_transform = window_transform(window, grid.transform)
                    tile_shape = int(window.height), int(window.width)
                    inside = geometry_mask([polygon], out_shape=tile_shape, transform=tile_transform, invert=True)
                    values = np.full(tile_shape, NODATA, dtype="uint8")
                    if dynamic_world:
                        probabilities = np.full((9, *tile_shape), PROBABILITY_NODATA, dtype="float32")
                        confidence = np.full((1, *tile_shape), PROBABILITY_NODATA, dtype="float32")
                    if inside.any():
                        if dynamic_world:
                            tile = download_dw_tile(ee_image, grid.crs, list(tile_transform)[:6], int(window.width), int(window.height))
                            values, probabilities, confidence = _split_dw_tile(tile, tile_shape)
                        elif product.requires_ee:
                            values = download_ee_tile(ee_image, grid.crs, list(tile_transform)[:6], int(window.width), int(window.height))
                            if values.shape != tile_shape:
                                raise ValueError("Earth Engine returned a tile with unexpected dimensions.")
                        elif request.product == "cdl":
                            values = download_cdl_tile(source_info, grid.crs, list(tile_transform)[:6], int(window.width), int(window.height))
                            if values.shape != tile_shape or values.dtype != np.dtype("uint8"):
                                raise ValueError("USDA returned a tile with unexpected dimensions or class data type.")
                        else:
                            for reader in readers:
                                candidate = reader.read(1, window=window)
                                valid = (values == NODATA) & (candidate != NODATA)
                                values[valid] = candidate[valid]
                        values[~inside] = NODATA
                    counts += np.bincount(values.ravel(), minlength=256)
                    targets["class"].write(values, 1, window=window)
                    if dynamic_world:
                        probabilities[:, ~inside] = PROBABILITY_NODATA
                        confidence[:, ~inside] = PROBABILITY_NODATA
                        observed_pixels += int(np.count_nonzero(confidence[0] != PROBABILITY_NODATA))
                        targets["probabilities"].write(probabilities, window=window)
                        targets["confidence"].write(confidence, window=window)
                    if index == 1 or index == total or index % 10 == 0:
                        progress(f"{period[0]}: tile {index:,}/{total:,}")
                if counts[:NODATA].sum() == 0 and observed_pixels == 0:
                    raise ValueError("No valid land-cover observations were found. Check the region, dates, or source coverage.")
                factors = [f for f in (2, 4, 8, 16) if min(grid.width, grid.height) // f >= 1]
                if factors:
                    for target in targets.values():
                        target.build_overviews(factors, Resampling.nearest)
                        target.update_tags(ns="rio_overview", resampling="nearest")
            metadata = {
                "status": "complete", "created_utc": datetime.now(timezone.utc).isoformat(),
                "request": _identity(request, period, grid),
                "requested_dates_inclusive": {"start": request.start, "end": request.end},
                "temporal_interpretation": "Composite over requested dates" if dynamic_world else "Whole annual map for each calendar year intersected by the requested dates",
                "source": source_info, "source_urls": source_info.get("source_urls", sources),
                "grid": {"crs": grid.crs, "transform": list(grid.transform)[:6], "width": grid.width, "height": grid.height, "resolution_m": grid.resolution, "resampling": "nearest"},
                "bundle_outputs": [{"file": spec.path.name, "output_type": spec.kind} for spec in specs],
            }
            # Publish all rasters before their complete sidecars. Any caught failure
            # rolls back every file created in this period; prior periods stay intact.
            for spec in specs:
                temporary[spec.kind].replace(spec.path)
                published.append(spec.path)
            for spec in specs:
                details = dict(metadata, output_type=spec.kind, dtype=spec.dtype, bands=list(spec.bands),
                               grid=dict(metadata["grid"], nodata=spec.nodata))
                if spec.kind == "class":
                    legend = {str(code): {"name": name, "color": color} for code, (name, color) in product.classes.items()}
                    details.update({
                        "class_legend": legend or source_info.get("class_legend", {}),
                        "pixel_counts": {str(i): int(n) for i, n in enumerate(counts) if n},
                        "valid_pixels": int(counts[:NODATA].sum()),
                        "note": "Native class codes are preserved. Class schemes differ between products; compare only after an explicit crosswalk. Pixel counts are not area estimates unless the CRS is equal-area.",
                    })
                else:
                    details.update({
                        "valid_pixels": observed_pixels, "units": "probability", "valid_range": [0, 1],
                        "aggregation": "mean_per_class_over_valid_observations" if spec.kind == "probabilities" else "maximum_of_mean_class_probabilities",
                        "confidence_threshold_applied": False,
                        "mask_description": "Only outside-region pixels and pixels with no valid observations are NoData. Low-confidence probabilities are retained even if the class label is masked.",
                    })
                sidecar = spec.path.with_suffix(".json")
                _atomic_json(sidecar, details)
                published.append(sidecar)
        except BaseException:
            for path in published:
                path.unlink(missing_ok=True)
            raise
        finally:
            for path in temporary.values():
                path.unlink(missing_ok=True)
        for spec in specs:
            progress(f"Saved {spec.path}")
            outputs.append(spec.path)
    return outputs
