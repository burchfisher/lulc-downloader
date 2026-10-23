from shapely.geometry import Polygon, box, mapping

from lulc.models import DownloadRequest, Region
from lulc import public_sources


def test_exact_worldcover_boundary_uses_one_tile():
    assert public_sources.worldcover_tiles(mapping(box(-78, 39, -75, 42))) == ["N39W078"]


def test_worldcover_crosses_equator_and_prime_meridian():
    assert public_sources.worldcover_tiles(mapping(box(-0.1, -0.1, 0.1, 0.1))) == [
        "S03W003", "S03E000", "N00W003", "N00E000",
    ]


def test_worldcover_polygon_hole_excludes_empty_cell():
    polygon = Polygon(box(0, 0, 9, 9).exterior.coords, [box(3, 3, 6, 6).exterior.coords])
    tiles = public_sources.worldcover_tiles(mapping(polygon))
    assert len(tiles) == 8
    assert "N03E003" not in tiles


def test_worldcover_filters_against_year_specific_inventory(monkeypatch):
    monkeypatch.setattr(public_sources, "_available_worldcover_tiles", lambda year: frozenset({"N39W078"}))
    request = DownloadRequest("worldcover", Region.from_bbox((-78, 39, -72, 42)), "2021-01-01", "2021-12-31")
    urls, metadata = public_sources.resolve_sources(request, request.periods[0])
    assert len(urls) == 1
    assert urls[0].endswith("v200/2021/map/ESA_WorldCover_10m_2021_v200_N39W078_Map.tif")
    assert metadata["source_nodata"] == 0


def test_nlcd_is_version_pinned_and_identifies_mirror():
    request = DownloadRequest("nlcd", Region.from_bbox((-78, 39, -77, 40)), "2024-04-01", "2024-05-01")
    urls, metadata = public_sources.resolve_sources(request, request.periods[0])
    assert urls[0].endswith("Annual_NLCD_LndCov_2024_CU_C1V1.tif")
    assert metadata["source_is_mirror"] is True
    assert metadata["source_nodata"] == 250
