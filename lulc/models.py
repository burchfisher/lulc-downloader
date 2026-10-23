from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
import math
from pathlib import Path

from pyproj import CRS
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from .catalog import get_product


@dataclass(frozen=True)
class Region:
    """A valid polygon or multipolygon in longitude/latitude (EPSG:4326)."""
    geometry: dict

    def __post_init__(self):
        geom = shape(self.geometry)
        if geom.geom_type not in ("Polygon", "MultiPolygon") or geom.is_empty or not geom.is_valid:
            raise ValueError("Region must contain valid, non-empty Polygon or MultiPolygon geometry.")
        west, south, east, north = geom.bounds
        if not all(math.isfinite(v) for v in geom.bounds) or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("Region coordinates must be longitude/latitude in EPSG:4326.")
        if east - west > 180:
            raise ValueError("Regions spanning the antimeridian must be split into separate requests.")

    @classmethod
    def from_bbox(cls, bounds):
        if len(bounds) != 4:
            raise ValueError("A bounding box needs west, south, east, north.")
        west, south, east, north = map(float, bounds)
        if not all(math.isfinite(v) for v in (west, south, east, north)) or west >= east or south >= north:
            raise ValueError("Bounding box must be ordered west, south, east, north with positive area.")
        return cls(mapping(box(west, south, east, north)))

    @classmethod
    def from_geojson(cls, value):
        if isinstance(value, (str, Path)):
            value = json.loads(Path(value).read_text())
        if not isinstance(value, dict):
            raise ValueError("Expected a GeoJSON object.")
        if value.get("crs"):
            name = value["crs"].get("properties", {}).get("name", "")
            if name not in ("EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:EPSG::4326"):
                raise ValueError("Reproject GeoJSON to EPSG:4326 before uploading it.")
        kind = value.get("type")
        if kind == "FeatureCollection":
            regions = [cls.from_geojson(f) for f in value.get("features", [])]
            if not regions:
                raise ValueError("GeoJSON FeatureCollection is empty.")
            return cls(mapping(unary_union([shape(r.geometry) for r in regions])))
        if kind == "Feature":
            value = value.get("geometry")
            if not value:
                raise ValueError("GeoJSON feature has no geometry.")
        return cls(value)

    @property
    def bounds(self):
        return shape(self.geometry).bounds


@dataclass(frozen=True)
class DownloadRequest:
    product: str
    region: Region
    start: str
    end: str
    output_dir: Path = Path("downloads")
    crs: str = "EPSG:5070"
    resolution: float | None = None
    project: str | None = None
    min_confidence: float = 0.0
    max_pixels: int = 100_000_000

    def __post_init__(self):
        product = get_product(self.product)
        start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if end < start:
            raise ValueError("End date must be on or after start date.")
        if end == date.max:
            raise ValueError("End date is too large.")
        first = date(2015, 6, 27) if self.product == "dynamic-world" else date(product.first_year, 1, 1)
        if start < first:
            raise ValueError(f"{product.name} starts {first.isoformat()}; adjust the start date.")
        if product.last_year and end.year > product.last_year:
            raise ValueError(f"{product.name} supports years through {product.last_year}; adjust the end date.")
        if self.resolution is not None and (not math.isfinite(self.resolution) or self.resolution <= 0):
            raise ValueError("Resolution must be a positive number of metres.")
        if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ValueError("Confidence must be between 0 and 1.")
        if self.min_confidence and self.product != "dynamic-world":
            raise ValueError("Confidence filtering is available only for Dynamic World.")
        if self.max_pixels < 1:
            raise ValueError("max_pixels must be positive.")
        output_crs = CRS.from_user_input(self.crs)
        if not output_crs.is_projected or any(a.unit_name.lower() not in ("metre", "meter") for a in output_crs.axis_info[:2]):
            raise ValueError("Choose a projected output CRS with metre units (e.g. EPSG:5070 for CONUS or EPSG:6933 globally).")
        if self.crs.upper() == "EPSG:5070":
            west, south, east, north = self.region.bounds
            if west < -125 or east > -66 or south < 24 or north > 50:
                raise ValueError("EPSG:5070 is the CONUS default. For other regions choose EPSG:6933 or a local projected CRS.")
        if self.product in ("nlcd", "cdl"):
            if not shape(self.region.geometry).intersects(box(-125, 24, -66, 50)):
                raise ValueError(f"{product.name} only covers the conterminous U.S.; this region is outside its extent.")
        object.__setattr__(self, "output_dir", Path(self.output_dir))

    @property
    def end_exclusive(self) -> str:
        return (date.fromisoformat(self.end) + timedelta(days=1)).isoformat()

    @property
    def periods(self) -> list[tuple[str, str, str]]:
        if self.product == "dynamic-world":
            return [(f"{self.start}_{self.end}", self.start, self.end_exclusive)]
        return [(str(y), f"{y}-01-01", f"{y+1}-01-01")
                for y in range(date.fromisoformat(self.start).year, date.fromisoformat(self.end).year + 1)]

    def scale_for(self, label: str) -> float:
        if self.resolution is not None:
            return self.resolution
        if self.product == "cdl" and int(label) >= 2024:
            return 10
        return get_product(self.product).resolution
