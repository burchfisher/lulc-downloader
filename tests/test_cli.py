"""CLI contract: machine-readable plans and useful failures."""
import json

from lulc.cli import main


ARGS = ["--product", "nlcd", "--bbox", "-77.05", "38.88", "-77.00", "38.92",
        "--start", "2023-01-01", "--end", "2024-12-31"]


def test_plan_is_json_and_needs_no_credentials(capsys):
    assert main(["plan", *ARGS]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["period"] for row in result] == ["2023", "2024"]
    assert all(row["pixels"] > 0 for row in result)


def test_invalid_region_reports_error_without_traceback(capsys):
    args = ["plan", "--product", "nlcd", "--bbox", "-76", "39", "-77", "38",
            "--start", "2024-01-01", "--end", "2024-12-31"]
    assert main(args) == 1
    error = capsys.readouterr().err
    assert "Bounding box" in error
    assert "Traceback" not in error


def test_dynamic_world_plan_includes_probability_and_confidence_files(capsys):
    args = ["plan", "--product", "dynamic-world", "--bbox", "-77.05", "38.88", "-77.00", "38.92",
            "--start", "2024-06-01", "--end", "2024-08-31"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 1
    row = result[0]
    assert row["raster_count"] == 3
    assert len(row["output_paths"]) == 3
    assert any(path.endswith("_probabilities.tif") for path in row["output_paths"])
    assert any(path.endswith("_confidence.tif") for path in row["output_paths"])
    assert abs(row["estimated_mb"] - row["pixels"] * 41 / 1_000_000) < 0.01


def test_cdl_product_listing_has_no_login_requirement(capsys):
    assert main(["products"]) == 0
    output = capsys.readouterr().out
    cdl_description = output.split("cdl: ", 1)[1]
    assert "No account required." in cdl_description
    assert "Earth Engine authentication required." not in cdl_description


def test_auth_reports_optional_package_install_command(monkeypatch, capsys):
    import builtins
    real_import = builtins.__import__

    def import_without_earth_engine(name, *args, **kwargs):
        if name == "ee":
            raise ModuleNotFoundError("No module named 'ee'", name="ee")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_earth_engine)
    assert main(["auth"]) == 1
    assert "python -m pip install '.[earthengine]'" in capsys.readouterr().err
