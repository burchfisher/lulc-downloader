import json

import numpy as np
import pytest
import requests
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from lulc.models import DownloadRequest, Region
from lulc import usda


TRANSFORM = [30, 0, 500000, 0, -30, 2000000]


def response(content=b"", status=200):
    result = requests.Response()
    result.status_code = status
    result._content = content if isinstance(content, bytes) else json.dumps(content).encode()
    result._content_consumed = True
    return result


def raster_bytes(array, transform=TRANSFORM, crs="EPSG:5070"):
    if array.ndim == 2:
        array = array[None]
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=array.shape[2], height=array.shape[1],
                         count=array.shape[0], dtype=array.dtype, crs=crs,
                         transform=Affine(*transform), nodata=0) as dataset:
            dataset.write(array)
        return memory.read()


def cdl_request():
    return DownloadRequest("cdl", Region.from_bbox((-90, 40, -89, 41)), "2025-01-01", "2025-12-31")


def test_prepare_discovers_year_id_native_resolution_and_official_legend(monkeypatch):
    payloads = [
        {"features": [{"attributes": {"OBJECTID": 913, "Name": "2025_10m_cdls", "Year": 2025, "Category": 1}}]},
        {"pixelSizeX": 10, "pixelSizeY": 10, "bandCount": 1, "pixelType": "U8"},
        {"features": [{"attributes": {"Value": code, "Class_Names": name, "Red": 255, "Green": 211, "Blue": 0}}
                      for code, name in [(0, "Background"), (1, "Corn"), (81, "Clouds/No Data"), (255, None)]]},
    ]
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response(payloads[len(calls) - 1])
    monkeypatch.setattr(usda.requests, "get", get)
    request = cdl_request()
    info = usda.prepare_cdl(request, request.periods[0])
    assert calls[0][1]["params"]["where"] == "Year = 2025 AND Category = 1"
    assert calls[1][0].endswith("/913/info")
    assert info["raster_id"] == 913
    assert info["native_resolution_m"] == 10
    assert info["requires_account"] is False
    assert info["source_nodata"] == 0
    assert info["class_legend"] == {1: {"name": "Corn", "color": "ffd300"}, 81: {"name": "Clouds/No Data", "color": "ffd300"}}


@pytest.mark.parametrize("features,message,error_type", [
    ([], "no national CDL", ValueError),
    ([{"attributes": {"Year": 2025}}, {"attributes": {"Year": 2025}}], "ambiguous", RuntimeError),
])
def test_missing_or_ambiguous_catalog_year_never_uses_latest(monkeypatch, features, message, error_type):
    monkeypatch.setattr(usda.requests, "get", lambda *args, **kwargs: response({"features": features}))
    request = cdl_request()
    with pytest.raises(error_type, match=message):
        usda.prepare_cdl(request, request.periods[0])


def test_raw_numeric_export_locks_year_and_preserves_crop_codes(monkeypatch):
    array = np.array([[0, 1], [81, 254]], dtype="uint8")
    captured = {}
    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return response(raster_bytes(array))
    monkeypatch.setattr(usda.requests, "get", get)
    result = usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", TRANSFORM, 2, 2)
    assert result.dtype == np.uint8
    np.testing.assert_array_equal(result, [[255, 1], [81, 254]])
    params = captured["params"]
    assert params["bbox"] == "500000,1999940,500060,2000000"
    assert params["size"] == "2,2"
    assert params["imageSR"] == params["bboxSR"] == 5070
    assert json.loads(params["mosaicRule"])["lockRasterIds"] == [913]
    assert json.loads(params["renderingRule"]) == {"rasterFunction": "None"}
    assert params["interpolation"] == "RSP_NearestNeighbor"
    assert params["format"] == "tiff"
    assert params["adjustAspectRatio"] == "false"


@pytest.mark.parametrize("content,message", [
    (raster_bytes(np.ones((3, 2, 2), dtype="uint8")), "one uint8 class band"),
    (raster_bytes(np.ones((2, 2), dtype="uint16")), "one uint8 class band"),
    (raster_bytes(np.ones((3, 2), dtype="uint8")), "dimensions"),
    (raster_bytes(np.ones((2, 2), dtype="uint8"), [30, 0, 500030, 0, -30, 2000000]), "georeferencing"),
    (raster_bytes(np.ones((2, 2), dtype="uint8"), crs="EPSG:6933"), "georeferencing"),
])
def test_export_rejects_rgb_or_wrong_numeric_grid(monkeypatch, content, message):
    monkeypatch.setattr(usda.requests, "get", lambda *args, **kwargs: response(content))
    with pytest.raises(RuntimeError, match=message):
        usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", TRANSFORM, 2, 2)


@pytest.mark.parametrize("content", [b'{"error":{"code":500,"message":"Cannot export"}}', b"<ExceptionReport>Failed</ExceptionReport>"])
def test_http_200_service_error_is_not_treated_as_a_raster(monkeypatch, content):
    monkeypatch.setattr(usda.requests, "get", lambda *args, **kwargs: response(content))
    with pytest.raises(RuntimeError, match="non-TIFF"):
        usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", TRANSFORM, 2, 2)


def test_transient_http_and_network_failures_are_retried_at_most_three_times(monkeypatch):
    calls, sleeps = [], []
    def get(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise requests.Timeout()
        if len(calls) == 2:
            return response(status=503)
        return response(raster_bytes(np.ones((2, 2), dtype="uint8")))
    monkeypatch.setattr(usda.requests, "get", get)
    monkeypatch.setattr(usda.time, "sleep", sleeps.append)
    result = usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", TRANSFORM, 2, 2)
    assert result.shape == (2, 2)
    assert len(calls) == 3
    assert sleeps == [1, 2]
    calls.clear()
    def fail(*args, **kwargs):
        calls.append(True)
        raise requests.ConnectionError()
    monkeypatch.setattr(usda.requests, "get", fail)
    with pytest.raises(RuntimeError, match="three attempts"):
        usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", TRANSFORM, 2, 2)
    assert len(calls) == 3


@pytest.mark.parametrize("width,height", [(0, 2), (2, 1025), (True, 2), (2.0, 2)])
def test_size_limit_precedes_network(monkeypatch, width, height):
    monkeypatch.setattr(usda.requests, "get", lambda *args, **kwargs: pytest.fail("Unexpected network request"))
    with pytest.raises(ValueError, match="1 to 1024"):
        usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", TRANSFORM, width, height)


def test_rotated_grid_is_rejected_before_export(monkeypatch):
    monkeypatch.setattr(usda.requests, "get", lambda *args, **kwargs: pytest.fail("Unexpected network request"))
    with pytest.raises(ValueError, match="north-up"):
        usda.download_cdl_tile({"raster_id": 913}, "EPSG:5070", [30, 1, 500000, 0, -30, 2000000], 2, 2)


def test_metadata_service_errors_are_explained(monkeypatch):
    monkeypatch.setattr(usda.requests, "get", lambda *args, **kwargs: response({"error": {"code": 500, "message": "Catalog unavailable"}}))
    with pytest.raises(RuntimeError, match="Catalog unavailable"):
        usda.prepare_cdl(cdl_request(), cdl_request().periods[0])
