"""Exercise the user's local workflow without internet access or credentials."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("streamlit_folium")
from streamlit.testing.v1 import AppTest


APP = Path(__file__).parents[1] / "app.py"


def element(elements, label):
    return next(item for item in elements if item.label == label)


def test_bbox_preview_and_changed_settings_require_new_preview():
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    element(app.button, "Preview download").click().run()
    assert not app.exception
    assert element(app.metric, "Output rasters").value == "1"
    assert element(app.metric, "Estimated output size").value.endswith(" kB")
    assert int(element(app.metric, "Grid pixels").value.replace(",", "")) > 0
    assert not element(app.button, "Download GeoTIFFs").disabled
    element(app.number_input, "West").set_value(-77.06).run()
    assert not app.exception
    assert "Download GeoTIFFs" not in [button.label for button in app.button]


def test_invalid_region_does_not_enable_download():
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    element(app.number_input, "West").set_value(-76.9).run()
    assert not app.exception
    assert any("Bounding box" in item.value for item in app.error)
    assert element(app.button, "Preview download").disabled


def test_dynamic_world_preview_requires_project_before_download(monkeypatch):
    monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    element(app.selectbox, "Product").set_value("dynamic-world").run()
    assert not app.exception
    assert element(app.date_input, "Start date")
    assert element(app.slider, "Minimum confidence").value == 0
    element(app.button, "Preview download").click().run()
    assert not app.exception
    assert element(app.metric, "Output rasters").value == "3"
    assert any("nine-band probabilities" in item.value for item in app.info)
    assert any("cutoff masks the class map only" in item.value for item in app.caption)
    assert element(app.button, "Download GeoTIFFs").disabled
    assert any("project ID" in item.value for item in app.warning)


def test_earth_engine_project_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("EARTHENGINE_PROJECT", "test-project")
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    element(app.selectbox, "Product").set_value("dynamic-world").run()
    assert not app.exception
    assert element(app.text_input, "Earth Engine project ID").value == "test-project"
    element(app.button, "Preview download").click().run()
    assert not element(app.button, "Download GeoTIFFs").disabled


def test_cdl_defaults_to_latest_year_and_needs_no_project(monkeypatch):
    from datetime import date
    from lulc.catalog import PRODUCTS
    monkeypatch.delenv("EARTHENGINE_PROJECT", raising=False)
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    element(app.selectbox, "Product").set_value("cdl").run()
    assert not app.exception
    assert "Earth Engine project ID" not in [item.label for item in app.text_input]
    assert element(app.selectbox, "First year").value == (PRODUCTS["cdl"].last_year or date.today().year - 1)
    assert any(item.value == "No account required." for item in app.caption)
    element(app.button, "Preview download").click().run()
    assert not app.exception
    assert element(app.metric, "Output rasters").value == "1"
    assert not element(app.button, "Download GeoTIFFs").disabled


def test_invalid_crs_is_a_user_error_not_a_crash():
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    element(app.text_input, "Projected coordinate system").set_value("invalid-crs").run()
    assert not app.exception
    assert app.error
    assert element(app.button, "Preview download").disabled
