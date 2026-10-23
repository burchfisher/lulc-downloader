from dataclasses import replace
import pytest
from lulc.models import DownloadRequest, Region


def request(**kwargs):
    defaults = dict(product="nlcd", region=Region.from_bbox([-77.05, 38.88, -77, 38.92]), start="2020-05-01", end="2021-09-01")
    return DownloadRequest(**(defaults | kwargs))


def test_annual_dates_select_each_intersected_year():
    assert request().periods == [("2020", "2020-01-01", "2021-01-01"), ("2021", "2021-01-01", "2022-01-01")]


def test_dynamic_world_end_is_inclusive_across_leap_day():
    value = request(product="dynamic-world", start="2020-02-28", end="2020-02-29")
    assert value.end_exclusive == "2020-03-01"
    assert value.periods[0][1:] == ("2020-02-28", "2020-03-01")


@pytest.mark.parametrize("bounds", [[-77,39,-78,40], [181,0,182,1], [-10,2,10,2], [-179,0,179,1], [float('nan'),0,1,1]])
def test_invalid_bbox_rejected(bounds):
    with pytest.raises(ValueError):
        Region.from_bbox(bounds)


def test_geojson_union_preserves_holes():
    geom = {"type":"Polygon","coordinates":[[[-77,38],[-76,38],[-76,39],[-77,39],[-77,38]], [[-76.8,38.2],[-76.8,38.8],[-76.2,38.8],[-76.2,38.2],[-76.8,38.2]]]}
    region = Region.from_geojson({"type":"FeatureCollection","features":[{"type":"Feature","geometry":geom,"properties":{}}]})
    assert len(region.geometry["coordinates"]) == 2


@pytest.mark.parametrize("changes", [
    {"start":"2022-01-01","end":"2021-01-01"},
    {"end":"2025-01-01"},
    {"product":"worldcover","start":"2019-01-01"},
    {"product":"dynamic-world","start":"2015-01-01"},
    {"crs":"EPSG:4326"}, {"resolution":0}, {"min_confidence":0.5},
    {"region":Region.from_bbox([0,40,1,41])},
])
def test_invalid_request_rejected(changes):
    with pytest.raises(ValueError):
        request(**changes)
