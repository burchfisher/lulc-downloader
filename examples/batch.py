"""Run from the project after installation: python examples/batch.py.

Public products only; no login needed. Change the region, years, or products below.
"""
from pathlib import Path

from lulc.models import DownloadRequest, Region
from lulc.pipeline import download

region = Region.from_geojson(Path(__file__).with_name("washington-dc.geojson"))
for product, year in [("nlcd", 2021), ("worldcover", 2021)]:
    request = DownloadRequest(
        product=product,
        region=region,
        start=f"{year}-01-01",
        end=f"{year}-12-31",
        output_dir=Path("downloads/batch"),
        resolution=30,  # Use one aligned 30 m grid for both products.
    )
    download(request, progress=print)
