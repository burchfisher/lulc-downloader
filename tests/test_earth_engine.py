from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import requests
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from lulc import earth_engine as provider
from lulc.models import DownloadRequest, Region


def request(product="dynamic-world", **kwargs):
    return DownloadRequest(
        product, Region.from_bbox([-100, 40, -99.99, 40.01]),
        "2024-01-01", "2024-12-31", **kwargs,
    )


def ee_collection(monkeypatch, count=1):
    ee = MagicMock()
    collection = ee.ImageCollection.return_value.filterDate.return_value.filterBounds.return_value
    collection.size.return_value.getInfo.return_value = count
    collection.aggregate_array.return_value.getInfo.return_value = ["sample"]
    monkeypatch.setattr(provider, "_initialize_ee", lambda project: (ee, "test-project"))
    return ee, collection


def test_auth_requires_project_without_starting_login(monkeypatch):
    ee = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "ee", ee)
    monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="registered Google Cloud project"):
        provider._initialize_ee(None)
    ee.Authenticate.assert_not_called()
    ee.Initialize.assert_not_called()


def test_auth_uses_environment_and_explains_failed_initialization(monkeypatch):
    ee = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "ee", ee)
    monkeypatch.setenv("EARTHENGINE_PROJECT", "environment-project")
    assert provider._initialize_ee(None) == (ee, "environment-project")
    ee.Initialize.assert_called_once_with(project="environment-project")
    ee.Initialize.side_effect = Exception("expired credentials")
    with pytest.raises(RuntimeError, match="earthengine authenticate"):
        provider._initialize_ee("explicit-project")
    ee.Authenticate.assert_not_called()


def test_dynamic_world_averages_probabilities_then_classifies_and_clips(monkeypatch):
    ee, collection = ee_collection(monkeypatch)
    req = request(min_confidence=0.65)
    result, metadata = provider.prepare_ee(req, req.periods[0])
    ee.ImageCollection.assert_called_once_with("GOOGLE/DYNAMICWORLD/V1")
    ee.ImageCollection.return_value.filterDate.assert_called_once_with("2024-01-01", "2025-01-01")
    collection.select.assert_called_once_with(provider.DW_BANDS)
    collection.select.return_value.mean.assert_called_once_with()
    collection.mode.assert_not_called()
    mean = collection.select.return_value.mean.return_value
    mean.reduce.assert_called_once_with(ee.Reducer.max.return_value)
    mean.toArray.return_value.arrayArgmax.return_value.arrayGet.assert_called_once_with([0])
    confidence = mean.reduce.return_value
    confidence.gte.assert_called_once_with(0.65)
    classification = mean.toArray.return_value.arrayArgmax.return_value.arrayGet.return_value.rename.return_value
    classification.updateMask.assert_called_once_with(confidence.gte.return_value)
    typed = classification.updateMask.return_value.toUint8.return_value
    typed.clip.assert_called_once_with(ee.Geometry.return_value)
    typed.unmask.assert_not_called()
    typed.clip.return_value.unmask.assert_called_once_with(255, sameFootprint=False)
    class_image = typed.clip.return_value.unmask.return_value.toUint8.return_value
    mean.updateMask.assert_not_called()
    mean.clip.assert_called_once_with(ee.Geometry.return_value)
    mean.clip.return_value.unmask.assert_called_once_with(-9999, sameFootprint=False)
    probabilities = mean.clip.return_value.unmask.return_value.toFloat.return_value
    confidence.updateMask.assert_not_called()
    confidence.rename.assert_called_once_with("confidence")
    confidence.rename.return_value.clip.assert_called_once_with(ee.Geometry.return_value)
    confidence.rename.return_value.clip.return_value.unmask.assert_called_once_with(-9999, sameFootprint=False)
    confidence_image = confidence.rename.return_value.clip.return_value.unmask.return_value.toFloat.return_value
    class_image.addBands.assert_called_once_with(probabilities)
    class_image.addBands.return_value.addBands.assert_called_once_with(confidence_image)
    combined = class_image.addBands.return_value.addBands.return_value
    combined.select.assert_called_once_with(provider.DW_EXPORT_BANDS)
    assert result is combined.select.return_value
    assert metadata["native_resolution_m"] == 10
    assert metadata["minimum_mean_class_probability"] == 0.65
    assert metadata["source_assets"] == ["GOOGLE/DYNAMICWORLD/V1/sample"]
    assert "National Geographic Society" in metadata["attribution"]
    assert metadata["probability_bands"] == provider.DW_BANDS
    assert metadata["probability_nodata"] == -9999
    assert metadata["confidence_definition"] == "maximum_of_the_nine_mean_class_probabilities"
    assert "only to the class band" in metadata["probability_mask"]


def test_empty_collection_is_rejected_before_reduction(monkeypatch):
    _, collection = ee_collection(monkeypatch, count=0)
    req = request()
    with pytest.raises(ValueError, match="No dynamic-world images"):
        provider.prepare_ee(req, req.periods[0])
    collection.select.assert_not_called()


def test_cdl_uses_live_resolution_and_legend_and_masks_background(monkeypatch):
    ee, _ = ee_collection(monkeypatch)
    source = ee.Image.return_value
    source.toDictionary.return_value.getInfo.return_value = {
        "system:index": "2024", "cropland_class_values": [0, 1, 5],
        "cropland_class_names": ["Background", "Corn", "Soybeans"],
        "cropland_class_palette": ["000000", "ffd400", "267300"],
    }
    crop = source.select.return_value
    # Deliberately use 30 even in 2024: an EE mirror may differ from native USDA.
    crop.projection.return_value.nominalScale.return_value.getInfo.return_value = 30
    crop.projection.return_value.getInfo.return_value = {"crs": "EPSG:5070"}
    req = request("cdl")
    _, metadata = provider.prepare_ee(req, req.periods[0])
    source.select.assert_called_once_with("cropland")
    crop.neq.assert_called_once_with(0)
    crop.updateMask.assert_called_once_with(crop.neq.return_value)
    assert metadata["native_resolution_m"] == 30
    assert metadata["class_legend"] == {
        1: {"name": "Corn", "color": "ffd400"},
        5: {"name": "Soybeans", "color": "267300"},
    }


TRANSFORM = [30, 0, 1000, 0, -30, 2000]


def tif_bytes(array=None, transform=TRANSFORM):
    if array is None:
        array = np.array([[0, 1], [8, 255]], dtype="uint8")
    if array.ndim == 2:
        array = array[None]
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=array.shape[2], height=array.shape[1],
                         count=array.shape[0], dtype=str(array.dtype), crs="EPSG:5070",
                         transform=Affine(*transform),
                         nodata=-9999 if array.dtype.kind == "f" else 255) as dataset:
            dataset.write(array)
        return memory.read()


def response(status=200, content=None):
    return SimpleNamespace(status_code=status, content=tif_bytes() if content is None else content,
                           close=MagicMock())


def test_tile_preserves_water_and_nodata_on_exact_grid(monkeypatch):
    get = MagicMock(return_value=response())
    monkeypatch.setattr(provider.requests, "get", get)
    image = MagicMock()
    image.getDownloadURL.return_value = "https://example.test/tile"
    result = provider.download_ee_tile(image, "EPSG:5070", TRANSFORM, 2, 2)
    image.getDownloadURL.assert_called_once_with({
        "bands": ["class"], "format": "GEO_TIFF", "crs": "EPSG:5070",
        "crs_transform": TRANSFORM, "dimensions": [2, 2],
    })
    np.testing.assert_array_equal(result, np.array([[0, 1], [8, 255]], dtype="uint8"))
    assert result.dtype == np.uint8


def test_tile_retries_transient_http_failures(monkeypatch):
    get = MagicMock(side_effect=[response(503), response(429), response()])
    sleep = MagicMock()
    monkeypatch.setattr(provider.requests, "get", get)
    monkeypatch.setattr(provider.time, "sleep", sleep)
    provider.download_ee_tile(MagicMock(), "EPSG:5070", TRANSFORM, 2, 2)
    assert get.call_count == 3
    assert sleep.call_count == 2


def test_tile_does_not_retry_permanent_http_failure(monkeypatch):
    get = MagicMock(return_value=response(403))
    monkeypatch.setattr(provider.requests, "get", get)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        provider.download_ee_tile(MagicMock(), "EPSG:5070", TRANSFORM, 2, 2)
    assert get.call_count == 1


def test_tile_retries_timeout_at_most_three_attempts(monkeypatch):
    get = MagicMock(side_effect=requests.Timeout("connection timeout"))
    monkeypatch.setattr(provider.requests, "get", get)
    monkeypatch.setattr(provider.time, "sleep", MagicMock())
    with pytest.raises(RuntimeError, match="three attempts"):
        provider.download_ee_tile(MagicMock(), "EPSG:5070", TRANSFORM, 2, 2)
    assert get.call_count == 3


@pytest.mark.parametrize("content", [b"<html>service error</html>",
                                    tif_bytes(transform=[30, 0, 999, 0, -30, 2000])])
def test_tile_rejects_invalid_or_wrong_grid_geotiff(monkeypatch, content):
    monkeypatch.setattr(provider.requests, "get", MagicMock(return_value=response(content=content)))
    with pytest.raises(RuntimeError, match="Invalid Earth Engine tile GeoTIFF"):
        provider.download_ee_tile(MagicMock(), "EPSG:5070", TRANSFORM, 2, 2)


def test_tile_size_limit_is_checked_before_network():
    image = MagicMock()
    with pytest.raises(ValueError, match="1 to 1024"):
        provider.download_ee_tile(image, "EPSG:5070", TRANSFORM, 1025, 1)
    image.getDownloadURL.assert_not_called()


def test_dw_tile_retains_fractional_probabilities_and_both_nodata_values(monkeypatch):
    array = np.full((11, 2, 2), -9999, dtype="float32")
    array[0] = [[0, 255], [8, 255]]
    array[1:10, 0, 0] = [0.7] + [0.3 / 8] * 8
    # A thresholded class is nodata but its observed probability distribution is retained.
    array[1:10, 0, 1] = [1 / 9] * 9
    array[1:10, 1, 0] = [0.1 / 8] * 8 + [0.9]
    array[10] = [[0.7, 1 / 9], [0.9, -9999]]
    monkeypatch.setattr(provider.requests, "get", MagicMock(return_value=response(content=tif_bytes(array))))
    image = MagicMock()
    result = provider.download_dw_tile(image, "EPSG:5070", TRANSFORM, 2, 2)
    image.select.assert_called_once_with(provider.DW_EXPORT_BANDS)
    image.select.return_value.toFloat.assert_called_once_with()
    image.select.return_value.toFloat.return_value.getDownloadURL.assert_called_once_with({
        "bands": provider.DW_EXPORT_BANDS, "format": "GEO_TIFF", "crs": "EPSG:5070",
        "crs_transform": TRANSFORM, "dimensions": [2, 2],
    })
    assert result.shape == (11, 2, 2)
    assert result.dtype == np.float32
    np.testing.assert_array_equal(result, array)
    assert result[0, 0, 0] == 0  # Water is not nodata.
    assert result[0, 0, 1] == 255
    assert result[1, 0, 1] == pytest.approx(1 / 9)
    assert result[0, 1, 1] == 255
    assert result[1, 1, 1] == -9999


def test_dw_tile_accepts_512_but_rejects_larger_before_network(monkeypatch):
    array = np.full((11, 512, 512), -9999, dtype="float32")
    array[0] = 255
    get = MagicMock(return_value=response(content=tif_bytes(array)))
    monkeypatch.setattr(provider.requests, "get", get)
    image = MagicMock()
    result = provider.download_dw_tile(image, "EPSG:5070", TRANSFORM, 512, 512)
    assert result.shape == (11, 512, 512)
    for width, height in ((513, 1), (1, 513)):
        with pytest.raises(ValueError, match="1 to 512"):
            provider.download_dw_tile(image, "EPSG:5070", TRANSFORM, width, height)
    assert get.call_count == 1


@pytest.mark.parametrize("array", [np.zeros((10, 2, 2), dtype="float32"),
                                   np.zeros((11, 2, 2), dtype="uint8")])
def test_dw_tile_rejects_missing_bands_or_integer_probabilities(monkeypatch, array):
    monkeypatch.setattr(provider.requests, "get", MagicMock(return_value=response(content=tif_bytes(array))))
    with pytest.raises(RuntimeError, match="Expected 11 float32"):
        provider.download_dw_tile(MagicMock(), "EPSG:5070", TRANSFORM, 2, 2)
