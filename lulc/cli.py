"""Command-line entry points for repeatable downloads and the local interface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .catalog import PRODUCTS
from .models import DownloadRequest, Region


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lulc",
        description="Download land-cover GeoTIFFs for a region and inclusive date range.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("products", help="List products, coverage, and authentication requirements")
    commands.add_parser("auth", help="Authenticate for Dynamic World through Google Earth Engine")
    ui = commands.add_parser("ui", help="Open the local browser interface")
    ui.add_argument("--port", type=int, default=8501, help="Local port (default: 8501)")
    for command in ("plan", "download"):
        child = commands.add_parser(command, help="Preview output grids" if command == "plan" else "Download clipped GeoTIFFs")
        child.add_argument("--product", choices=PRODUCTS, required=True)
        region = child.add_mutually_exclusive_group(required=True)
        region.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                            help="Longitude/latitude bounds in EPSG:4326")
        region.add_argument("--region", type=Path, help="GeoJSON polygon or multipolygon file in EPSG:4326")
        child.add_argument("--start", required=True, metavar="YYYY-MM-DD", help="Inclusive first date")
        child.add_argument("--end", required=True, metavar="YYYY-MM-DD", help="Inclusive last date")
        child.add_argument("--output-dir", type=Path, default=Path("downloads"))
        child.add_argument("--crs", default="EPSG:5070", help="Projected CRS with metre units (default: EPSG:5070)")
        child.add_argument("--resolution", type=float, help="Pixel size in metres; defaults to product resolution")
        child.add_argument("--project", help="Google Cloud project registered for Earth Engine (Dynamic World only)")
        child.add_argument("--min-confidence", type=float, default=0.0,
                           help="Minimum winning mean class probability for Dynamic World (0–1)")
        child.add_argument("--max-pixels", type=int, default=100_000_000,
                           help="Maximum pixels per output (default: 100000000)")
    return parser


def request_from_args(args: argparse.Namespace) -> DownloadRequest:
    region = Region.from_bbox(args.bbox) if args.bbox is not None else Region.from_geojson(args.region)
    return DownloadRequest(
        product=args.product, region=region, start=args.start, end=args.end,
        output_dir=args.output_dir, crs=args.crs, resolution=args.resolution,
        project=args.project, min_confidence=args.min_confidence, max_pixels=args.max_pixels,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "products":
            for product in PRODUCTS.values():
                years = ", ".join(map(str, product.years)) if product.years else f"{product.first_year}–{product.last_year or 'latest available'}"
                resolution = "resolution varies by year" if product.id == "cdl" else f"{product.resolution} m"
                print(f"{product.id}: {product.name} | {resolution} | {years}")
                print(f"  {product.coverage}. {'Earth Engine authentication required.' if product.requires_ee else 'No account required.'}")
                print(f"  {product.description}\n  {product.source}\n")
            return 0
        if args.command == "auth":
            try:
                import ee
            except ImportError as exc:
                raise RuntimeError("Dynamic World authentication needs the optional Earth Engine package. Run: python -m pip install '.[earthengine]'") from exc
            ee.Authenticate()
            print("Authentication complete. Supply --project when downloading Dynamic World.")
            return 0
        if args.command == "ui":
            import importlib.util
            if importlib.util.find_spec("streamlit") is None:
                raise RuntimeError('The interface is not installed. Run: python -m pip install -e ".[ui]"')
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
            script = Path(__file__).with_name("ui.py")
            return subprocess.call([
                sys.executable, "-m", "streamlit", "run", str(script),
                "--server.address", "127.0.0.1", "--server.port", str(args.port),
                "--browser.gatherUsageStats", "false",
            ])
        from .pipeline import download, plan
        request = request_from_args(args)
        if args.command == "plan":
            print(json.dumps(plan(request), indent=2, default=str))
        else:
            outputs = download(request, progress=lambda message: print(message, file=sys.stderr))
            for path in outputs:
                print(Path(path).resolve())
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
