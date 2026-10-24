#!/usr/bin/env python3
"""Trace visible Strava Global Heatmap lines inside the supplied Sydney-region box.

Install: python -m pip install numpy scipy pillow
Run:     python strava_heatmap_to_gpx.py --preview

Paste your browser's entire Cookie request-header VALUE below, if required.
Keep this file private after doing so. The script never prints the cookie.
The output is inferred from an aggregate image; it is not a set of recorded
GPS activities and cannot distinguish paths from roads or other routes.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from scipy import ndimage as ndi


# Paste only the value following "Cookie:" in your browser's request headers.
COOKIE_HEADER = ""

# The two corners from the question, in latitude/longitude order.
NORTH, EAST = -33.316046167288036, 151.5441820221408
SOUTH, WEST = -33.56914940407827, 151.1217400527635

TILE_URL = (
    "https://content-a.strava.com/identified/globalheat/all/grayscale/"
    "{z}/{x}/{y}@2x.png?v=20&missing=empty"
)
TILE_PX = 512  # Normalize each @2x tile to 512 pixels before tracing.
GPX_NS = "http://www.topografix.com/GPX/1/1"


def world_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    n = (2**zoom) * TILE_PX
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def latlon_from_pixel(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = (2**zoom) * TILE_PX
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return max(SOUTH, min(NORTH, lat)), max(WEST, min(EAST, lon))


def download_tile(z: int, x: int, y: int, cache: Path) -> Path:
    dest = cache / f"z{z}_{x}_{y}.png"
    if dest.exists() and dest.stat().st_size:
        return dest
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0",
        "Referer": "https://www.strava.com/",
        "Accept": "image/png,image/*;q=0.8",
    }
    if COOKIE_HEADER.strip():
        headers["Cookie"] = COOKIE_HEADER.strip()
    request = Request(TILE_URL.format(z=z, x=x, y=y), headers=headers)
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                data = response.read()
            with Image.open(BytesIO(data)) as img:
                img.verify()
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_suffix(".part")
            temp.write_bytes(data)
            temp.replace(dest)
            return dest
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(
                    f"Tile access denied (HTTP {exc.code}); paste a current Cookie "
                    "header value into COOKIE_HEADER while signed in to Strava."
                ) from exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"Tile {z}/{x}/{y}: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 3:
                raise RuntimeError(f"Could not fetch tile {z}/{x}/{y}: {exc}") from exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch tile {z}/{x}/{y} after retries")


def tile_signal(path: Path, channel: str) -> np.ndarray:
    with Image.open(path) as image:
        if image.size not in ((512, 512), (1024, 1024)):
            raise ValueError(f"Unexpected tile size {image.size} in {path}")
        if image.size != (TILE_PX, TILE_PX):
            image = image.resize((TILE_PX, TILE_PX), Image.Resampling.BOX)
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    alpha = rgba[:, :, 3].astype(np.uint16)
    if channel == "alpha":
        return alpha.astype(np.uint8)
    luminance = (rgba[:, :, :3].astype(np.uint16) *
                 np.array([54, 183, 19], dtype=np.uint16)).sum(axis=2) // 256
    if channel == "luminance":
        return luminance.astype(np.uint8)
    # A transparent tile can contain arbitrary RGB in transparent pixels.
    return ((luminance * alpha) // 255).astype(np.uint8)


def thinning_tables() -> tuple[np.ndarray, np.ndarray]:
    tables = []
    for phase in (0, 1):
        table = np.zeros(256, dtype=bool)
        for code in range(256):
            p = [(code >> i) & 1 for i in range(8)]  # N, NE, E, SE, S, SW, W, NW
            count = sum(p)
            transitions = sum(p[i] == 0 and p[(i + 1) % 8] == 1 for i in range(8))
            if not (2 <= count <= 6 and transitions == 1):
                continue
            n, e, s, w = p[0], p[2], p[4], p[6]
            if phase == 0:
                table[code] = not (n and e and s) and not (e and s and w)
            else:
                table[code] = not (n and e and w) and not (n and s and w)
        tables.append(table)
    return tables[0], tables[1]


THIN_LUT = thinning_tables()
NEIGHBOR_WEIGHTS = np.array([[128, 1, 2], [64, 0, 4], [32, 16, 8]], dtype=np.uint8)


def skeletonize(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning on a small patch, preserving forks and endpoints."""
    work = mask.copy()
    while True:
        removed = 0
        for table in THIN_LUT:
            code = ndi.correlate(work.astype(np.uint8), NEIGHBOR_WEIGHTS,
                                 mode="constant", cval=0)
            delete = work & table[code]
            removed += int(np.count_nonzero(delete))
            work[delete] = False
        if not removed:
            return work


def mosaic(z: int, cache: Path, channel: str, workers: int) -> tuple[np.memmap, int, int]:
    xw, yn = world_pixel(NORTH, WEST, z)
    xe, ys = world_pixel(SOUTH, EAST, z)
    tx0, ty0 = math.floor(xw / TILE_PX), math.floor(yn / TILE_PX)
    tx1, ty1 = math.floor(xe / TILE_PX), math.floor(ys / TILE_PX)
    width, height = (tx1 - tx0 + 1) * TILE_PX, (ty1 - ty0 + 1) * TILE_PX
    jobs = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
    print(f"Fetching/caching {len(jobs)} tiles ({tx0}..{tx1}, {ty0}..{ty1})")
    paths: dict[tuple[int, int], Path] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_xy = {pool.submit(download_tile, z, x, y, cache): (x, y)
                        for x, y in jobs}
        try:
            for index, future in enumerate(as_completed(future_to_xy), 1):
                paths[future_to_xy[future]] = future.result()
                if index % 25 == 0 or index == len(jobs):
                    print(f"  {index}/{len(jobs)} tiles", flush=True)
        except Exception:
            for future in future_to_xy:
                future.cancel()
            raise
    signal = np.memmap(cache / "working_signal.dat", dtype=np.uint8, mode="w+",
                       shape=(height, width))
    for x, y in jobs:
        yy, xx = (y - ty0) * TILE_PX, (x - tx0) * TILE_PX
        signal[yy:yy + TILE_PX, xx:xx + TILE_PX] = tile_signal(paths[(x, y)], channel)
    # Exclude the margin of the outermost tiles outside the requested bbox.
    left, right = math.ceil(xw - tx0 * TILE_PX), math.floor(xe - tx0 * TILE_PX)
    top, bottom = math.ceil(yn - ty0 * TILE_PX), math.floor(ys - ty0 * TILE_PX)
    signal[:top, :] = 0
    signal[bottom + 1:, :] = 0
    signal[:, :left] = 0
    signal[:, right + 1:] = 0
    signal.flush()
    return signal, tx0 * TILE_PX, ty0 * TILE_PX


def trace_mosaic(signal: np.ndarray, cache: Path, threshold: int) -> np.memmap:
    height, width = signal.shape
    skeleton = np.memmap(cache / "working_skeleton.dat", dtype=np.uint8,
                         mode="w+", shape=(height, width))
    skeleton[:] = 0
    halo = 16
    rows = math.ceil(height / TILE_PX)
    cols = math.ceil(width / TILE_PX)
    for row in range(rows):
        for col in range(cols):
            y0, x0 = row * TILE_PX, col * TILE_PX
            y1, x1 = min(y0 + TILE_PX, height), min(x0 + TILE_PX, width)
            a, b = max(0, y0 - halo), min(height, y1 + halo)
            c, d = max(0, x0 - halo), min(width, x1 + halo)
            patch = np.asarray(signal[a:b, c:d], dtype=np.uint8)
            # Mild smoothing and a one-pixel bridge suppress JPEG/renderer gaps.
            smooth = ndi.gaussian_filter(patch.astype(np.float32), 0.65)
            foreground = smooth >= threshold
            foreground = ndi.binary_closing(foreground, iterations=1)
            thin = skeletonize(foreground)
            skeleton[y0:y1, x0:x1] = thin[y0-a:y1-a, x0-c:x1-c]
        print(f"  Traced tile row {row + 1}/{rows}", flush=True)
    skeleton.flush()
    return skeleton


OFFSETS = ((-1, 0), (-1, 1), (0, 1), (1, 1),
           (1, 0), (1, -1), (0, -1), (-1, -1))


def make_adjacency(skeleton: np.ndarray) -> dict[tuple[int, int], tuple]:
    ys, xs = np.nonzero(skeleton)
    pixels = set(zip(ys.tolist(), xs.tolist()))
    graph = {}
    for y, x in pixels:
        neighbors = []
        for dy, dx in OFFSETS:
            q = (y + dy, x + dx)
            if q not in pixels:
                continue
            # Suppress a diagonal shortcut where an orthogonal connection exists.
            if dx and dy and ((y + dy, x) in pixels or (y, x + dx) in pixels):
                continue
            neighbors.append(q)
        graph[(y, x)] = tuple(neighbors)
    return graph


def edges(graph: dict) -> list[list[tuple[int, int]]]:
    used = set()
    result = []

    def key(p, q):
        return (p, q) if p < q else (q, p)

    def walk(p, q):
        line = [p]
        while True:
            used.add(key(p, q))
            line.append(q)
            if len(graph[q]) != 2:
                break
            nxt = graph[q][0] if graph[q][1] == p else graph[q][1]
            if key(q, nxt) in used:
                break
            p, q = q, nxt
        return line

    # First open branches, then the remaining closed loops.
    for p, neighbors in graph.items():
        if len(neighbors) == 2:
            continue
        for q in neighbors:
            if key(p, q) not in used:
                result.append(walk(p, q))
    for p, neighbors in graph.items():
        for q in neighbors:
            if key(p, q) not in used:
                result.append(walk(p, q))
    return result


def simplify_pixels(line: list[tuple[int, int]], tolerance: float) -> list[tuple[int, int]]:
    if len(line) <= 2 or tolerance <= 0:
        return line
    points = np.asarray([(x, y) for y, x in line], dtype=np.float64)
    keep = np.zeros(len(line), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(line) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i <= 1:
            continue
        a, b = points[i], points[j]
        v = b - a
        section = points[i+1:j]
        length2 = float(v @ v)
        if length2:
            t = np.clip(((section - a) @ v) / length2, 0, 1)
            distances2 = np.sum((section - (a + t[:, None] * v))**2, axis=1)
        else:
            distances2 = np.sum((section - a)**2, axis=1)
        k = int(np.argmax(distances2)) + i + 1
        if distances2[k - i - 1] > tolerance**2:
            keep[k] = True
            stack.extend(((i, k), (k, j)))
    return [line[i] for i in np.flatnonzero(keep)]


def write_gpx(lines: list[list[tuple[int, int]]], output: Path,
              origin_x: int, origin_y: int, zoom: int,
              min_length_m: float, simplify_m: float) -> tuple[int, float]:
    ET.register_namespace("", GPX_NS)
    root = ET.Element(f"{{{GPX_NS}}}gpx", version="1.1",
                      creator="strava_heatmap_to_gpx.py")
    meta = ET.SubElement(root, f"{{{GPX_NS}}}metadata")
    ET.SubElement(meta, f"{{{GPX_NS}}}name").text = "Inferred Strava heatmap centerlines"
    ET.SubElement(meta, f"{{{GPX_NS}}}desc").text = (
        "Automated extraction from aggregated imagery. Not recorded GPS tracks; "
        "verify routes, access and alignment before using or mapping."
    )
    lat_center = (NORTH + SOUTH) / 2
    meters_per_pixel = (2 * math.pi * 6378137 * math.cos(math.radians(lat_center)) /
                        (2**zoom * TILE_PX))
    count = 0
    total_m = 0.0
    for line in lines:
        if len(line) < 2:
            continue
        length_m = sum(math.hypot(b[0]-a[0], b[1]-a[1]) * meters_per_pixel
                       for a, b in zip(line, line[1:]))
        if length_m < min_length_m:
            continue
        line = simplify_pixels(line, simplify_m / meters_per_pixel)
        count += 1
        total_m += length_m
        track = ET.SubElement(root, f"{{{GPX_NS}}}trk")
        ET.SubElement(track, f"{{{GPX_NS}}}name").text = f"Heatmap line {count:05d}"
        segment = ET.SubElement(track, f"{{{GPX_NS}}}trkseg")
        for y, x in line:
            lat, lon = latlon_from_pixel(origin_x + x + 0.5,
                                         origin_y + y + 0.5, zoom)
            ET.SubElement(segment, f"{{{GPX_NS}}}trkpt",
                          lat=f"{lat:.8f}", lon=f"{lon:.8f}")
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return count, total_m


def save_preview(signal: np.ndarray, skeleton: np.ndarray, path: Path) -> None:
    # Red lines over the original gray heatmap; shrink for manageable output.
    scale = max(1, math.ceil(max(signal.shape) / 2600))
    size = (math.ceil(signal.shape[1] / scale), math.ceil(signal.shape[0] / scale))
    small_gray = Image.fromarray(np.asarray(signal)).resize(size, Image.Resampling.BOX)
    rgb = np.stack([np.asarray(small_gray)] * 3, axis=2).copy()
    # Downscale the line image by averaging, then count even a single red pixel.
    line_image = Image.fromarray(np.asarray(skeleton) * 255).resize(
        size, Image.Resampling.BOX)
    small_sk = np.asarray(line_image) > 0
    rgb[small_sk] = (255, 60, 45)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=Path("strava_heatmap_traces.gpx"))
    parser.add_argument("--cache", type=Path, default=Path("strava_tile_cache"))
    parser.add_argument("--zoom", type=int, default=14, choices=range(11, 17))
    parser.add_argument("--threshold", type=int, default=45,
                        help="Heat brightness 1..255; lower includes fainter lines (default 45)")
    parser.add_argument("--channel", choices=("visible", "alpha", "luminance"),
                        default="visible", help="Choose image channel for unusual tiles")
    parser.add_argument("--min-length-m", type=float, default=20,
                        help="Discard isolated trace segments shorter than this (default 20)")
    parser.add_argument("--simplify-m", type=float, default=4,
                        help="Polyline simplification tolerance (default 4)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--preview", nargs="?", const="strava_trace_preview.png",
                        help="Save downscaled heatmap with red extracted lines")
    args = parser.parse_args()
    if not 1 <= args.threshold <= 255 or not 1 <= args.workers <= 6:
        parser.error("threshold must be 1..255 and workers must be 1..6")
    signal, origin_x, origin_y = mosaic(args.zoom, args.cache, args.channel, args.workers)
    print(f"Tracing lines at brightness >= {args.threshold} ...", flush=True)
    skeleton = trace_mosaic(signal, args.cache, args.threshold)
    if args.preview:
        save_preview(signal, skeleton, Path(args.preview))
        print(f"Preview: {args.preview}")
    print("Building tracks ...", flush=True)
    lines = edges(make_adjacency(skeleton))
    count, total = write_gpx(lines, args.output, origin_x, origin_y, args.zoom,
                             args.min_length_m, args.simplify_m)
    print(f"Wrote {count} segments, ~{total/1000:.1f} km total, to {args.output}")
    if not count:
        print("No lines found. Check the preview; try --threshold 20 or --channel alpha.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
