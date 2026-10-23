# Land-cover downloader

A Python tool you download from GitHub and run on your own computer. Use the local browser interface, command line, or Python API to download land cover by region and date. Draw a polygon, enter a bounding box, or upload GeoJSON; choose a product and dates; save clipped, georeferenced GeoTIFFs and their provenance JSON.

The interface opens in your browser, with Python running locally and output files saved directly to your computer. Internet access is needed to retrieve source data, and Dynamic World composites use Google Earth Engine. You do not need to deploy or manage a server.

NLCD, WorldCover, and USDA Cropland Data Layer download without accounts or Google setup. Dynamic World uses Earth Engine and requires its optional package, authentication, and a registered project.

## Download and install

Install **Python 3.11 or newer**. On the GitHub repository page, choose **Code → Download ZIP**, extract the ZIP, and open a terminal in the extracted project folder containing `pyproject.toml`. Alternatively, clone the repository and open its folder.

On **macOS or Linux**:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[ui]'
.venv/bin/lulc ui
```

On **Windows**, use PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[ui]"
.\.venv\Scripts\lulc.exe ui
```

These commands create a project-specific environment and install the local interface. NLCD, WorldCover, and USDA CDL are ready to use without accounts. Install the optional Earth Engine package below if you want Dynamic World.

## Run and save files

For later sessions, open a terminal in the project folder and run:

```bash
# macOS / Linux
.venv/bin/lulc ui
```

```powershell
# Windows PowerShell
.\.venv\Scripts\lulc.exe ui
```

Open the localhost URL printed in the terminal if the browser does not open automatically. Keep the terminal running while using the interface; press **Ctrl+C** to stop it. If the default port is busy, use `lulc ui --port 8502` with the same executable path as above.

The default example is a small region in Washington, D.C. Choose **Preview download**, review the output size, then download. The preview shows the estimated total output size in kB, MB, or GB, plus a size per period. Estimates describe uncompressed raster data for every selected output, including Dynamic World's probability and confidence maps; compressed files and source network transfers can differ. Files are saved directly in the local `downloads/` folder by default. Open **Output settings → Output folder** to choose another local folder, such as an absolute path on your computer. Large rasters are already saved to that folder; the additional browser download buttons for small outputs are optional.

The **About the dataset** panel updates when you choose a product. It summarizes pixel resolution, coverage, available dates, useful applications, and interpretation notes for each dataset.

## Products

| Choice | Best use | Supported dates | Resolution | Access |
| --- | --- | --- | --- | --- |
| Annual NLCD Collection 1.1 | Historical land cover in the lower 48 states | Annual, 1985–2024 | 30 m | Public COG mirror; no login |
| Dynamic World | Recent seasonal or custom date windows | From June 27, 2015; actual availability checked live | 10 m | Google Earth Engine |
| ESA WorldCover | Global reference maps | 2020 v100 and 2021 v200 | 10 m | Official public COGs; no login |
| USDA Cropland Data Layer | U.S. crop types | Annual, 2008–2025 | 30 m through 2023; 10 m from 2024 | Official USDA regional TIFFs; no login |

**NLCD version:** the public adapter uses the [Source Cooperative mirror](https://source.coop/kylebarron/usgs-landcover) of USGS Collection 1.1. The current official [Collection 1.2](https://www.usgs.gov/centers/eros/science/annual-nlcd-data-access) extends through 2025, but that release is not available through this adapter. The official S3 route uses requester-pays access. No fallback to another year or version occurs.

**Dates:** both input dates are inclusive. Annual products return one full annual map for each calendar year intersected by the date range; a June request does not turn NLCD into a June observation. Dynamic World returns one composite over the exact requested interval, exported as three aligned GeoTIFFs: class labels, class probabilities, and confidence. It averages each class's probabilities over valid observations, then selects the class with the highest mean probability. The optional confidence cutoff applies to the class map only; the probability and confidence rasters retain valid observations below that cutoff. Confidence is not a calibrated accuracy estimate. Cloud masks and missing observations can leave gaps. Separate monthly or seasonal outputs can be produced by calling the Python API once per period.

**Resolution:** the default is the product's native resolution. USDA CDL uses 30 m through 2023 and 10 m from 2024. The public USDA service provides regional TIFFs, so you do not need to download a national ZIP or sign in. For comparisons, set the same output CRS and resolution for every product. Class codes are preserved, so different products still require an explicit class crosswalk. These products primarily describe land cover; they do not provide parcel zoning or ownership.

**WorldCover:** ESA used different algorithms for 2020 and 2021, so differences between these maps are not purely land-cover change. See [ESA's data access and limitations](https://esa-worldcover.org/en/data-access).

## Dynamic World probability maps

Every Dynamic World request includes these three files, with the same region, date window, projected grid, and pixel size:

| Output | Contents | Data type and NoData |
| --- | --- | --- |
| `<stem>.tif` | Most probable class, codes 0–8; optional confidence cutoff applied | `uint8`, NoData `255` |
| `<stem>_probabilities.tif` | Nine bands containing each class's mean probability, from 0 to 1 | `float32`, NoData `-9999` |
| `<stem>_confidence.tif` | One band containing the highest of the nine mean probabilities, from 0 to 1 | `float32`, NoData `-9999` |

Probability bands are named and ordered: `water`, `trees`, `grass`, `flooded_vegetation`, `crops`, `shrub_and_scrub`, `built`, `bare`, `snow_and_ice`. The class map selects the largest probability; the confidence map records that value. These are temporal means of the source predictions, rather than the fraction of observations assigned to each class. All outputs mask pixels outside the region and pixels with no valid observations. Each file has its own JSON metadata sidecar. Probability and confidence maps remain available where a higher confidence cutoff masks the class map, so you can inspect uncertainty or apply a different cutoff later.

## Earth Engine setup (Dynamic World only)

Use a Google Cloud project registered for Earth Engine with its API enabled and permission for your Google account. Follow Google's [access guide](https://developers.google.com/earth-engine/guides/access) and [authentication guide](https://developers.google.com/earth-engine/guides/auth). Noncommercial access requires eligibility verification; commercial use has separate terms.

```bash
# macOS / Linux, from the project folder
.venv/bin/python -m pip install '.[earthengine]'
.venv/bin/lulc auth
```

```powershell
# Windows PowerShell, from the project folder
.\.venv\Scripts\python.exe -m pip install ".[earthengine]"
.\.venv\Scripts\lulc.exe auth
```

Enter your registered project ID in the interface or pass `--project` on the command line. You can also set `EARTHENGINE_PROJECT` in your environment. Authentication opens Google's normal sign-in flow on your own computer. Each person uses their own Earth Engine credentials and project access. Credentials are managed by Earth Engine, not copied into this project. NLCD, WorldCover, and CDL need neither a Google account nor the optional Earth Engine package. The Dynamic World adapter does not create Cloud resources or submit Drive export jobs.

## Command line

The examples below activate the environment on macOS/Linux. On Windows, replace `lulc` with `.\.venv\Scripts\lulc.exe` and enter each command on one line.

```sh
source .venv/bin/activate
lulc products

# Inspect size and dates without downloading or signing in.
lulc plan --product nlcd --bbox -77.05 38.88 -77.00 38.92 \
  --start 2024-01-01 --end 2024-12-31

# Download a polygon from a GeoJSON file.
lulc download --product nlcd --region examples/washington-dc.geojson \
  --start 2020-01-01 --end 2024-12-31

# Download crop types directly from USDA, without authentication.
lulc download --product cdl --bbox -77.05 38.88 -77.00 38.92 \
  --start 2025-01-01 --end 2025-12-31

# A summer composite with class, probability, and confidence GeoTIFFs.
lulc download --product dynamic-world --bbox -77.05 38.88 -77.00 38.92 \
  --start 2024-06-01 --end 2024-08-31 --min-confidence 0.5 \
  --project your-registered-project-id

# Align WorldCover to a 30 m grid for a later comparison.
lulc download --product worldcover --region examples/washington-dc.geojson \
  --start 2021-01-01 --end 2021-12-31 --resolution 30
```

Bounding boxes use **west, south, east, north** in longitude/latitude. GeoJSON must use EPSG:4326 and contain valid polygons or multipolygons. FeatureCollections are unioned, including polygon holes. Convert shapefiles or GeoPackages to GeoJSON in your GIS first. Regions spanning the antimeridian must be split into separate requests.

The default output CRS is **EPSG:5070**, an equal-area projection for the conterminous U.S. Use `--crs EPSG:6933` or a suitable local projection for regions elsewhere. Only projected CRSs with metre units are accepted. Annual NLCD and nationwide CDL coverage is CONUS; use global products for Alaska, Hawaii, and territories.

## Python and batch use

```python
from pathlib import Path
from lulc.models import DownloadRequest, Region
from lulc.pipeline import plan, download

request = DownloadRequest(
    product="nlcd",
    region=Region.from_bbox([-77.05, 38.88, -77.00, 38.92]),
    start="2020-01-01",
    end="2024-12-31",
    output_dir=Path("downloads/my-region"),
)
print(plan(request))
paths = download(request, progress=print)
```

`download()` returns all output raster paths: one per year for annual products, or three paths (class, probabilities, confidence) for a Dynamic World date window. `plan()` has one row per period; `raster_count` and `output_paths` describe all files in that period, and the size estimate includes every band.

See `examples/batch.py` for downloading multiple products to one aligned grid.

## Outputs and limits

- Tiled, DEFLATE-compressed GeoTIFFs. Every annual product has one single-band class map per year. Dynamic World has a class map plus nine-band probability and one-band confidence rasters per date window. Nearest-neighbor resampling preserves class codes. Class-map NoData is **255**; probability/confidence NoData is **-9999**, including outside your polygon. Dynamic World class **0 remains valid water**.
- A matching JSON sidecar records the region, requested dates, actual period, source URLs or Earth Engine asset IDs, version, attribution, class legend, grid, and pixel counts.
- Names include a deterministic request fingerprint. Complete matching outputs are reused. Incomplete or mismatched outputs produce an error instead of overwriting files. There is no mid-file resume; failed partial rasters are cleaned up, while completed earlier years remain.
- NLCD and WorldCover rasters are read by HTTP ranges, so the tool reads intersecting source blocks instead of downloading national mosaics. CDL requests regional TIFF tiles directly from USDA's public service. Dynamic World uses exact-grid Earth Engine tiles of at most 512 × 512 pixels for its eleven-band transfer, below the [direct download limits](https://developers.google.com/earth-engine/apidocs/ee-image-getdownloadurl).
- The default cap is **100 million grid pixels per file**. A Dynamic World bundle uses about **41 bytes per grid pixel before compression** across its three files (one class byte plus ten 32-bit probability/confidence values). Source transfer and processing can be much larger than the output. State-scale 10 m work may need a coarser resolution or regional batches. For genuinely national or continental processing, a dedicated cloud batch export workflow is more suitable than this local downloader.
- A nominally finer output pixel size does not create new source detail. Cache reuse verifies source version and asset IDs; if those change, the tool asks you to use a new output folder to preserve both snapshots. A provider replacing an asset in place without changing its ID cannot be detected; use a new output directory for a forced refresh.

## Validation

To install the development tools and run the tests:

```sh
.venv/bin/python -m pip install -e '.[ui,earthengine,dev]'
.venv/bin/python -m pytest -q
```

Tests cover date semantics, geometry validation, polygon clipping, NoData handling, categorical reprojection, source tile selection, output reuse, size limits, public USDA requests, and Earth Engine request construction with mocks. The development environment passed all **81 tests**.

Fresh live downloads on **October 23, 2026** verified these public datasets on selected Iowa regions:

| Dataset | Years checked | Output resolution |
| --- | --- | --- |
| Annual NLCD Collection 1.1 | 1985, 2024 | 30 m |
| ESA WorldCover | 2020, 2021 | 10 m |
| USDA CDL | 2008, 2023 | 30 m |
| USDA CDL | 2024, 2025 | 10 m |

All **9 download cases** passed, including an additional 1,107 × 1,481-pixel CDL 2025 region spanning four service tiles. Checks covered GeoTIFF structure, CRS, resolution, class codes and legends, polygon clipping, NoData, provenance, and output reuse. Independently requested strips matched both tile boundaries. Downloads through the local browser interface also passed for NLCD 2024, WorldCover 2021, and CDL 2025 on the Washington, D.C. example. These are checks of selected years and regions, not an exhaustive test of all coverage or classification accuracy.

Downloaded rasters and local environments are excluded from Git and are not included when you download the repository. Dynamic World still requires your authenticated Earth Engine project and was tested with mocks rather than a live account.

Sources: [Annual NLCD](https://www.usgs.gov/centers/eros/science/annual-nlcd-data-access), [Dynamic World](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1), [WorldCover](https://esa-worldcover.org/en/data-access), [USDA CDL](https://www.nass.usda.gov/Research_and_Science/Cropland/SARS1a.php).
