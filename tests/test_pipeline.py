import json
from dataclasses import replace

import numpy as np
import pytest
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom

from lulc.models import DownloadRequest, Region
from lulc.pipeline import download, make_grid, plan, projected_region


@pytest.fixture
def download_request(tmp_path):
    # L-shaped polygon tests that clipping is not merely to its bounding box.
    region = Region.from_geojson({"type":"Polygon", "coordinates":[[
        [-77.05,38.88],[-77,38.88],[-77,38.90],[-77.025,38.90],
        [-77.025,38.92],[-77.05,38.92],[-77.05,38.88]]]})
    return DownloadRequest("nlcd", region, "2024-01-01", "2024-12-31", tmp_path / "outputs")


def write_source(download_request, tmp_path, fill=41):
    grid = make_grid(download_request, 30)
    path = tmp_path / "source.tif"
    with rasterio.open(path, "w", driver="GTiff", width=grid.width, height=grid.height, count=1,
                       crs=grid.crs, transform=grid.transform, dtype="uint8", nodata=250) as src:
        array = np.full((grid.height, grid.width), fill, dtype="uint8")
        array[:3] = 250
        src.write(array, 1)
    return str(path)


def test_polygon_mask_nodata_grid_provenance_and_cache(download_request, tmp_path, monkeypatch):
    source = write_source(download_request, tmp_path)
    monkeypatch.setattr("lulc.public_sources.resolve_sources", lambda *args: ([source], {"source_nodata":250,"version":"test"}))
    paths = download(download_request)
    assert len(paths) == 1
    assert str(paths[0]) == plan(download_request)[0]["output"]
    with rasterio.open(paths[0]) as raster:
        array = raster.read(1)
        polygon = projected_region(download_request, 30)
        inside = geometry_mask([polygon], out_shape=array.shape, transform=raster.transform, invert=True)
        assert np.all(array[~inside] == 255)
        assert set(np.unique(array)) == {41,255}
        assert raster.crs.to_epsg() == 5070
        assert raster.res == (30,30)
        assert raster.nodata == 255
    metadata = json.loads(paths[0].with_suffix(".json").read_text())
    assert metadata["source"]["version"] == "test"
    assert metadata["valid_pixels"] == int((array == 41).sum())
    before = paths[0].stat().st_mtime_ns
    assert download(download_request) == paths
    assert paths[0].stat().st_mtime_ns == before


def test_empty_data_leaves_no_partial_or_success_file(download_request, tmp_path, monkeypatch):
    source = write_source(download_request, tmp_path, fill=250)
    monkeypatch.setattr("lulc.public_sources.resolve_sources", lambda *args: ([source], {"source_nodata":250}))
    with pytest.raises(ValueError, match="No valid"):
        download(download_request)
    assert list(download_request.output_dir.iterdir()) == []


def test_nearest_resampling_cannot_invent_classes(download_request, tmp_path, monkeypatch):
    source = write_source(download_request, tmp_path)
    with rasterio.open(source, "r+") as src:
        arr = src.read(1)
        arr[:,::2] = 82
        src.write(arr, 1)
    monkeypatch.setattr("lulc.public_sources.resolve_sources", lambda *args: ([source], {"source_nodata":250}))
    path = download(replace(download_request, resolution=47))[0]
    with rasterio.open(path) as src:
        assert set(np.unique(src.read(1))) <= {41,82,255}


def test_different_polygon_or_resolution_gets_different_filename(download_request):
    a = plan(download_request)[0]
    b = plan(replace(download_request, resolution=60))[0]
    c = plan(replace(download_request, region=Region.from_bbox(download_request.region.bounds)))[0]
    assert len({a["output"],b["output"],c["output"]}) == 3


def test_size_limit_precedes_network(download_request):
    with pytest.raises(ValueError, match="pixel limit"):
        plan(replace(download_request, max_pixels=1))


def test_existing_file_is_never_silently_overwritten(download_request, tmp_path, monkeypatch):
    source = write_source(download_request, tmp_path)
    monkeypatch.setattr("lulc.public_sources.resolve_sources", lambda *args: ([source], {"source_nodata":250}))
    paths = download(download_request)
    paths[0].with_suffix(".json").unlink()
    with pytest.raises(FileExistsError):
        download(download_request)


def test_large_geographic_box_keeps_curved_projected_edges():
    from pyproj import Transformer
    req = DownloadRequest("nlcd", Region.from_bbox([-120,30,-100,45]), "2024-01-01", "2024-12-31", resolution=5000)
    grid = make_grid(req, 5000)
    actual = geometry_mask([projected_region(req, 5000)], out_shape=(grid.height,grid.width), transform=grid.transform, invert=True)
    rows, cols = np.indices(actual.shape)
    x = grid.transform.c + (cols + .5) * 5000
    y = grid.transform.f - (rows + .5) * 5000
    lon, lat = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform(x,y)
    expected = (lon >= -120) & (lon <= -100) & (lat >= 30) & (lat <= 45)
    # Inverse-transforming pixel centers is an independent check of polygon membership.
    assert np.count_nonzero(actual != expected) <= 2


def test_cache_detects_changed_source_version(download_request, tmp_path, monkeypatch):
    source = write_source(download_request, tmp_path)
    info = {"source_nodata":250,"version":"v1"}
    monkeypatch.setattr("lulc.public_sources.resolve_sources", lambda *args: ([source], info.copy()))
    download(download_request)
    info["version"] = "v2"
    with pytest.raises(FileExistsError, match="Source data changed"):
        download(download_request)


def test_cdl_download_needs_no_earth_engine_and_preserves_legend(download_request, monkeypatch):
    import builtins
    req = replace(download_request, product="cdl")
    info = {"native_resolution_m":10,"source_assets":["usda/test-2024"],
            "provider":"USDA CroplandCROS", "source_urls":["https://example.test/usda"],
            "class_legend": {"1":{"name":"Corn","color":"ffd400"}}}
    monkeypatch.setattr("lulc.usda.prepare_cdl", lambda *args: info)
    monkeypatch.setattr("lulc.usda.download_cdl_tile", lambda source,crs,transform,width,height: np.full((height,width),1,dtype="uint8"))
    original_import = builtins.__import__
    def without_earth_engine(name, *args, **kwargs):
        if name == "ee" or name.endswith("earth_engine"):
            raise AssertionError("CDL must not import or initialize Earth Engine")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins,"__import__",without_earth_engine)
    monkeypatch.delenv("EARTHENGINE_PROJECT",raising=False)
    path = download(req)[0]
    with rasterio.open(path) as src:
        assert 1 in np.unique(src.read(1))
        assert src.res == (10,10)
        assert src.colormap(1)[1] == (255,212,0,255)
    metadata = json.loads(path.with_suffix(".json").read_text())
    assert metadata["valid_pixels"] > 0
    assert metadata["class_legend"]["1"]["name"] == "Corn"
    assert metadata["source_urls"] == info["source_urls"]
    assert str(path) == plan(req)[0]["output"]


def test_cdl_plan_uses_native_resolution_for_each_year(download_request):
    req = replace(download_request,product="cdl",start="2023-01-01",end="2025-12-31")
    assert [row["resolution"] for row in plan(req)] == [30,10,10]
    assert [row["resolution"] for row in plan(replace(req,resolution=60))] == [60,60,60]


PROBABILITIES = np.array([.37,.20,.13,.10,.06,.05,.04,.03,.02], dtype="float32")


def mock_dynamic_world(monkeypatch, *, threshold=0, missing=False):
    info = {"native_resolution_m":30, "source_assets":["test/dw-scene"]}
    monkeypatch.setattr("lulc.earth_engine.prepare_ee", lambda *args: (object(), info))
    calls = []
    def tile(image, crs, transform, width, height):
        calls.append((width,height))
        values = np.empty((11,height,width), dtype="float32")
        values[0] = 255 if threshold > float(PROBABILITIES.max()) else 0
        values[1:10] = PROBABILITIES[:,None,None]
        values[10] = PROBABILITIES.max()
        if missing:
            values[0,height//2,width//4] = 255
            values[1:,height//2,width//4] = -9999
        return values
    monkeypatch.setattr("lulc.earth_engine.download_dw_tile", tile)
    return calls


def test_dw_bundle_preserves_precision_alignment_masks_metadata_and_cache(download_request, monkeypatch):
    from lulc.earth_engine import DW_BANDS
    req = replace(download_request, product="dynamic-world")
    calls = mock_dynamic_world(monkeypatch, missing=True)
    paths = download(req)
    assert len(paths) == 3
    estimated = plan(replace(req,resolution=30))[0]
    assert [str(path) for path in paths] == estimated["output_paths"]
    assert estimated["raster_count"] == 3
    assert estimated["estimated_mb"] == round(estimated["pixels"] * 41 / 1e6, 2)
    assert estimated["estimated_bytes"] == estimated["pixels"] * 41
    with rasterio.open(paths[0]) as classes, rasterio.open(paths[1]) as probabilities, rasterio.open(paths[2]) as confidence:
        assert classes.crs == probabilities.crs == confidence.crs
        assert classes.transform == probabilities.transform == confidence.transform
        assert classes.shape == probabilities.shape == confidence.shape
        assert classes.nodata == 255
        assert probabilities.nodata == confidence.nodata == -9999
        assert probabilities.count == 9 and confidence.count == 1
        assert probabilities.dtypes == ("float32",) * 9
        assert confidence.dtypes == ("float32",)
        assert probabilities.descriptions == tuple(DW_BANDS)
        labels, probs, scores = classes.read(1), probabilities.read(), confidence.read(1)
        valid = scores != -9999
        inside = geometry_mask([projected_region(req,30)], out_shape=labels.shape, transform=classes.transform, invert=True)
        assert valid.any() and np.any(inside & ~valid)
        assert np.all(labels[valid] == 0)  # Water stays valid, not NoData.
        assert np.all(probs[:, ~inside] == -9999)
        assert np.all(labels[~valid] == 255)
        np.testing.assert_allclose(probs[:,valid], np.broadcast_to(PROBABILITIES[:,None], (9,valid.sum())))
        np.testing.assert_allclose(probs[:,valid].sum(axis=0),1,atol=1e-6)
        np.testing.assert_allclose(scores[valid], probs[:,valid].max(axis=0))
    metadata = json.loads(paths[1].with_suffix(".json").read_text())
    assert metadata["bands"] == DW_BANDS
    assert metadata["output_type"] == "probabilities"
    assert metadata["confidence_threshold_applied"] is False
    before = [path.stat().st_mtime_ns for path in paths]
    count = len(calls)
    assert download(req) == paths
    assert len(calls) == count
    assert [path.stat().st_mtime_ns for path in paths] == before


def test_dw_low_confidence_does_not_discard_probabilities(download_request, monkeypatch):
    req = replace(download_request, product="dynamic-world", min_confidence=.8)
    mock_dynamic_world(monkeypatch,threshold=.8)
    paths = download(req)
    with rasterio.open(paths[0]) as src:
        assert np.all(src.read(1) == 255)
    with rasterio.open(paths[1]) as src:
        assert np.any(src.read(1) > 0)
    assert json.loads(paths[0].with_suffix(".json").read_text())["valid_pixels"] == 0
    assert json.loads(paths[2].with_suffix(".json").read_text())["valid_pixels"] > 0


def test_dw_incomplete_bundle_cannot_be_reused(download_request, monkeypatch):
    req = replace(download_request, product="dynamic-world")
    mock_dynamic_world(monkeypatch)
    paths = download(req)
    paths[1].unlink()
    paths[1].with_suffix(".json").unlink()
    with pytest.raises(FileExistsError,match="bundle is incomplete"):
        download(req)
    assert not paths[1].exists()


def test_dw_failed_publication_rolls_back_entire_bundle(download_request, monkeypatch):
    import lulc.pipeline as pipeline
    req = replace(download_request, product="dynamic-world")
    mock_dynamic_world(monkeypatch)
    original = pipeline._atomic_json
    def fail_on_probability(path,data):
        if data["output_type"] == "probabilities":
            raise OSError("simulated disk failure")
        original(path,data)
    monkeypatch.setattr(pipeline,"_atomic_json",fail_on_probability)
    with pytest.raises(OSError,match="simulated disk failure"):
        download(req)
    assert list(req.output_dir.iterdir()) == []


def test_dw_tiles_are_bounded_below_multiband_download_limit(download_request, monkeypatch):
    req = replace(download_request, product="dynamic-world", resolution=10)
    calls = mock_dynamic_world(monkeypatch)
    download(req)
    assert len(calls) > 1
    assert all(width <= 512 and height <= 512 for width,height in calls)
