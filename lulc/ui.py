"""A local Streamlit interface; all exported rasters remain on this computer."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path

import folium
from folium.plugins import Draw
import streamlit as st
from streamlit_folium import st_folium

from lulc.catalog import PRODUCTS, Product
from lulc.models import DownloadRequest, Region


DEFAULT_BBOX = (-77.05, 38.88, -77.00, 38.92)
MAX_BROWSER_FILE = 32 * 1024 * 1024
MAX_BROWSER_TOTAL = 128 * 1024 * 1024


def _format_size(size: int | float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1000


def _estimated_bytes(row: dict) -> int:
    # Existing preview sessions may still contain the older MB-only plan rows.
    return int(row["estimated_bytes"]) if "estimated_bytes" in row else round(row.get("estimated_mb", 0) * 1_000_000)


def _about_dataset(product: Product):
    if product.id == "dynamic-world":
        dates = "June 27, 2015 onward; observation availability varies by region and date."
    elif product.years:
        dates = " and ".join(map(str, product.years)) + " annual maps."
    else:
        dates = f"{product.first_year}–{product.last_year} annual maps."
    with st.expander("About the dataset", expanded=True):
        st.write(f"**{product.name}**")
        facts, context = st.columns([1, 1.35], gap="large")
        with facts:
            st.markdown(f"**Resolution:** {product.resolution_note}")
            st.markdown(f"**Coverage:** {product.coverage}")
            st.markdown(f"**Available in this app:** {dates}")
        with context:
            st.markdown(f"**Typical uses:** {product.useful_for}")
            st.markdown(f"**Notes:** {product.interpretation_note}")
        st.caption("Resolution describes pixel size, not classification accuracy. Choosing a finer output pixel size does not add source detail.")
        st.markdown(f"[Dataset documentation]({product.source})")


def _map(region: Region | None = None, draw: bool = False):
    result = folium.Map(location=[38.90, -77.025], zoom_start=13, tiles="OpenStreetMap", control_scale=True)
    if region is not None:
        west, south, east, north = region.bounds
        folium.GeoJson(region.geometry, style_function=lambda _: {
            "color": "#137967", "weight": 2, "fillColor": "#219a80", "fillOpacity": 0.13,
        }).add_to(result)
        result.fit_bounds([[south, west], [north, east]])
    if draw:
        Draw(
            export=False,
            draw_options={"polyline": False, "circle": False, "circlemarker": False, "marker": False,
                          "polygon": {"allowIntersection": False}, "rectangle": True},
            edit_options={"edit": True, "remove": True},
        ).add_to(result)
    return result


def _choose_region() -> Region | None:
    method = st.radio("Region input", ["Bounding box", "Draw on map", "GeoJSON file"], horizontal=True)
    region = None
    if method == "Bounding box":
        columns = st.columns(4)
        coordinates = []
        for column, label, value in zip(columns, ["West", "South", "East", "North"], DEFAULT_BBOX):
            coordinates.append(column.number_input(label, value=value, step=0.01, format="%.5f"))
        try:
            region = Region.from_bbox(coordinates)
        except ValueError as exc:
            st.error(str(exc))
    elif method == "GeoJSON file":
        upload = st.file_uploader("Upload a polygon boundary", type=["geojson", "json"])
        st.caption("Use longitude/latitude coordinates (EPSG:4326). Multiple polygons are combined.")
        if upload is not None:
            try:
                region = Region.from_geojson(json.loads(upload.getvalue()))
            except Exception as exc:
                st.error(f"Unable to read this boundary: {exc}")
    else:
        st.caption("Use the rectangle or polygon tool on the map. Multiple shapes are combined.")
        state = st_folium(_map(draw=True), key="region-drawing", height=390, use_container_width=True,
                         returned_objects=["all_drawings"])
        drawings = state.get("all_drawings")
        if drawings:
            try:
                region = Region.from_geojson({"type": "FeatureCollection", "features": drawings})
            except ValueError as exc:
                st.error(str(exc))
        if region is None:
            st.info("Draw a region to prepare a download.")
        return region
    st_folium(_map(region), height=350, use_container_width=True, returned_objects=[], key="region-preview")
    return region


def _request_data(request: DownloadRequest) -> dict:
    return {
        "product": request.product, "start": request.start, "end": request.end,
        "region": request.region.geometry, "output_dir": str(request.output_dir.resolve()),
        "crs": request.crs, "resolution": request.resolution, "project": request.project,
        "min_confidence": request.min_confidence, "max_pixels": request.max_pixels,
    }


def _signature(request: DownloadRequest) -> str:
    return hashlib.sha256(json.dumps(_request_data(request), sort_keys=True).encode()).hexdigest()


def _render_outputs(request: DownloadRequest, paths: list[str], signature: str):
    st.success(f"Saved {len(paths)} raster{'s' if len(paths) != 1 else ''} to {request.output_dir.resolve()}")
    budget = MAX_BROWSER_TOTAL
    file_list = []
    for raw in paths:
        raster = Path(raw)
        if not raster.is_file():
            st.warning(f"File is no longer present: {raster}")
            continue
        sidecar = next((p for p in [raster.with_suffix(".json"), Path(str(raster) + ".json")] if p.is_file()), None)
        label = "GeoTIFF"
        description = None
        if request.product == "dynamic-world":
            if raster.stem.endswith("_probabilities"):
                label, description = "probabilities", "Nine named bands · mean class probabilities · 0–1"
            elif raster.stem.endswith("_confidence"):
                label, description = "confidence", "One band · highest mean class probability · 0–1"
            else:
                label, description = "class map", "One band · most probable class · confidence cutoff applied"
        with st.container(border=True):
            st.write(f"**{raster.name}**")
            if description:
                st.caption(description)
            st.caption(str(raster.resolve()))
            left, right = st.columns(2)
            size = raster.stat().st_size
            if size <= MAX_BROWSER_FILE and size <= budget:
                left.download_button(f"Download {label}", data=raster.read_bytes(), file_name=raster.name,
                                     mime="image/tiff", key=f"raster-{signature}-{raster}")
                budget -= size
            else:
                left.caption(f"{size / (1024 ** 2):,.1f} MiB · open the saved file from disk.")
            if sidecar is not None:
                right.download_button("Download metadata", data=sidecar.read_bytes(), file_name=sidecar.name,
                                      mime="application/json", key=f"meta-{signature}-{raster}")
            file_list.append({"raster": str(raster.resolve()), "kind": label,
                              "metadata": str(sidecar.resolve()) if sidecar else None})
    manifest = {"request": _request_data(request), "source": PRODUCTS[request.product].source, "outputs": file_list}
    st.download_button("Download run manifest", json.dumps(manifest, indent=2), file_name="lulc-run.json",
                       mime="application/json", key=f"manifest-{signature}")


def main():
    st.set_page_config(page_title="Land-cover data downloader", page_icon="🌎", layout="wide")
    st.markdown("""<style>
      .block-container {max-width: 1220px; padding-top: 4rem;}
      h1 {letter-spacing: -.04em;}
      div[data-testid="stMetricValue"] {font-size: 1.8rem;}
    </style>""", unsafe_allow_html=True)
    st.title("Land-cover data downloader")
    st.write("Select a dataset, region, and date range. GeoTIFFs and metadata are saved to the output folder on this computer.")

    with st.sidebar:
        st.header("Dataset settings")
        product_id = st.selectbox("Product", list(PRODUCTS), format_func=lambda value: PRODUCTS[value].name)
        product = PRODUCTS[product_id]
        resolution_description = "30 m through 2023; 10 m from 2024" if product_id == "cdl" else f"{product.resolution} m nominal resolution"
        st.caption(f"{product.coverage} · {resolution_description}")
        st.markdown(f"[Product documentation]({product.source})")
        if product.requires_ee:
            st.info("Requires Earth Engine authentication and an enabled Google Cloud project.")
            project = st.text_input("Earth Engine project ID", value=os.environ.get("EARTHENGINE_PROJECT", ""),
                                    placeholder="my-earth-engine-project") or None
            st.caption("First-time setup: install the `earthengine` extra, run `lulc auth` in your terminal, then enter your registered project ID here.")
        else:
            project = None
            st.caption("No account required.")
        st.divider()
        today = date.today()
        if product_id == "dynamic-world":
            st.subheader("Date window")
            start = st.date_input("Start date", value=date(today.year - 1, 1, 1), min_value=date(2015, 6, 27), max_value=today)
            end = st.date_input("End date", value=date(today.year - 1, 12, 31), min_value=date(2015, 6, 27), max_value=today)
            st.caption("Both dates are included. Creates one composite for the full window.")
            confidence = st.slider("Minimum confidence", 0.0, 1.0, 0.0, 0.05,
                                   help="Mask the class map where the winning mean class probability is below this threshold. Probability and confidence maps retain these observations.")
        else:
            st.subheader("Annual maps")
            last_year = product.last_year or today.year - 1
            years = list(product.years or range(product.first_year, last_year + 1))
            default_year = years[-1]
            start_year = st.selectbox("First year", years, index=years.index(default_year), key=f"first-{product_id}")
            end_options = [year for year in years if year >= start_year]
            end_year = st.selectbox("Last year", end_options, key=f"last-{product_id}-{start_year}")
            start, end, confidence = date(start_year, 1, 1), date(end_year, 12, 31), 0.0
            st.caption("Creates one raster per selected year. These are annual maps; dates within a year do not select new observations.")
        with st.expander("Output settings"):
            output_dir = st.text_input("Output folder", value="downloads")
            crs = st.text_input("Projected coordinate system", value="EPSG:5070",
                                help="EPSG:5070 for the contiguous U.S.; EPSG:6933 globally, or a local projected CRS with metre units.")
            native_resolution = st.checkbox("Use product resolution", value=True)
            resolution = None if native_resolution else st.number_input("Pixel size (metres)", min_value=1.0, value=float(product.resolution))
            max_pixels = st.number_input("Maximum pixels per raster", min_value=1_000, max_value=1_000_000_000,
                                         value=100_000_000, step=1_000_000)
            st.caption("Larger or finer requests take longer. Class codes use nearest-neighbor resampling.")
        with st.expander("Run again locally"):
            st.caption("Open a terminal in the project folder and run:")
            command = r".\.venv\Scripts\lulc.exe ui" if os.name == "nt" else ".venv/bin/lulc ui"
            st.code(command, language="powershell" if os.name == "nt" else "bash")

    _about_dataset(product)

    left, right = st.columns([1.65, 1], gap="large")
    with left:
        st.subheader("1. Select a region")
        st.caption("Example region: central Washington, D.C. Enter bounds, draw a polygon, or upload GeoJSON.")
        region = _choose_region()
    with right:
        st.subheader("2. Download settings")
        st.write(f"**{product.name}**")
        st.write(f"{start.isoformat()} → {end.isoformat()}")
        st.caption("GeoTIFF · source class codes preserved · polygon clipping")
        if product_id == "cdl":
            st.caption("Direct USDA downloads: 30 m through 2023, 10 m from 2024. USDA crop class codes are preserved.")
        elif product_id == "dynamic-world":
            st.info("Includes three aligned GeoTIFFs: class map, nine-band probabilities, and confidence (0–1).")
            st.caption("Probabilities are means across valid observations; confidence is the highest mean. The cutoff masks the class map only.")
        request = None
        if region is not None:
            try:
                request = DownloadRequest(product=product_id, region=region, start=start.isoformat(), end=end.isoformat(),
                                          output_dir=Path(output_dir), crs=crs, resolution=resolution, project=project,
                                          min_confidence=confidence, max_pixels=int(max_pixels))
            except Exception as exc:
                st.error(str(exc))
        signature = _signature(request) if request else None
        if st.button("Preview download", type="primary", disabled=request is None, use_container_width=True):
            try:
                from lulc.pipeline import plan
                with st.spinner("Calculating output size…"):
                    rows = plan(request)
                st.session_state["lulc_plan"] = {"signature": signature, "rows": rows}
            except Exception as exc:
                st.error(f"Could not prepare the download: {exc}")
        saved_plan = st.session_state.get("lulc_plan", {})
        if saved_plan.get("signature") == signature and request is not None:
            rows = saved_plan["rows"]
            pixels = sum(row.get("pixels", 0) for row in rows)
            estimated_bytes = sum(_estimated_bytes(row) for row in rows)
            st.metric("Estimated output size", f"~{_format_size(estimated_bytes)}",
                      help="Combined uncompressed raster data for all selected years and files, based on the grid, band count, and data type. Excludes file headers and metadata.")
            st.caption("Before compression, for all output rasters. Saved file sizes vary; source downloads can transfer more data.")
            metric_left, metric_right = st.columns(2)
            metric_left.metric("Output rasters", sum(row.get("raster_count", 1) for row in rows))
            metric_right.metric("Grid pixels", f"{pixels:,}")
            st.caption("This preview estimates output grids. Source availability is checked when downloading.")
            st.dataframe([
                {"Period": row.get("period", ""), "Width": row.get("width", ""), "Height": row.get("height", ""),
                 "Metres / pixel": row.get("resolution", ""),
                 "Est. size": f"~{_format_size(_estimated_bytes(row))}"}
                for row in rows
            ], hide_index=True, use_container_width=True)
            st.caption(f"Saving to: {request.output_dir.resolve()}")
            needs_project = product.requires_ee and not project
            if needs_project:
                st.warning("Enter your Earth Engine project ID in the sidebar to download this product.")
            if st.button("Download GeoTIFFs", type="primary", disabled=needs_project or not rows, use_container_width=True):
                from lulc.pipeline import download
                with st.status("Downloading land cover…", expanded=True) as status:
                    try:
                        paths = download(request, progress=status.write)
                        st.session_state["lulc_outputs"] = {"signature": signature, "paths": [str(path) for path in paths]}
                        status.update(label="Download complete", state="complete", expanded=False)
                    except Exception as exc:
                        status.update(label="Download stopped", state="error", expanded=True)
                        st.error(str(exc))
        elif saved_plan and request is not None:
            st.caption("Settings have changed. Preview again to update the download plan.")
        if region is not None:
            st.download_button("Save region GeoJSON", json.dumps({"type": "Feature", "properties": {}, "geometry": region.geometry}, indent=2),
                               file_name="region.geojson", mime="application/geo+json")
        if product.classes:
            with st.expander("Class legend"):
                for code, (label, color) in product.classes.items():
                    st.markdown(f'<span style="display:inline-block;width:11px;height:11px;border-radius:2px;background:#{color};margin-right:8px"></span>**{code}** · {label}', unsafe_allow_html=True)

    outputs = st.session_state.get("lulc_outputs", {})
    if request is not None and outputs.get("signature") == signature:
        st.divider()
        st.subheader("Saved files")
        _render_outputs(request, outputs["paths"], signature)
    st.caption("Products have different class definitions and observation methods. Their class codes are not interchangeable.")


if __name__ == "__main__":
    main()
