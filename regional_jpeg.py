"""Render regional JPEGs from small province rasters, without a merged GIS export."""
import hashlib
import json
import io
import logging
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

from map_layout import STYLES, classify, find_field

PIXELS = 1600
BATCH_SIZE = 4000


def grid(bounds):
    left, bottom, right, top = bounds
    width, height = right-left, top-bottom
    if not np.isfinite(bounds).all() or width <= 0 or height <= 0:
        raise ValueError('Source has invalid map bounds.')
    columns = max(1, round(PIXELS * width / max(width, height)))
    rows = max(1, round(PIXELS * height / max(width, height)))
    return (rows, columns), from_bounds(*bounds, columns, rows)


def class_code(value):
    if isinstance(value, (int, float)) and pd.notna(value) and float(value).is_integer():
        value = str(int(value))
    return classify(value)


def province_raster(storage, bucket, source, layer, cache):
    identity = json.dumps(['raster-v1', layer, source['Key'], source.get('ETag'), str(source.get('LastModified', '')), PIXELS])
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (hashlib.sha256(identity.encode()).hexdigest() + '.npz')
    if target.exists():
        with np.load(target, allow_pickle=False) as saved:
            return saved['pixels'], saved['bounds']
    remote_key = 'exports/regional-rasters/' + target.name
    persistent = hasattr(storage, 'put_object')
    if persistent:
        try:
            obj = storage.get_object(Bucket=bucket, Key=remote_key)
            try:
                payload = obj['Body'].read()
            finally:
                obj['Body'].close()
            with np.load(io.BytesIO(payload), allow_pickle=False) as saved:
                pixels, bounds = saved['pixels'], saved['bounds']
            target.write_bytes(payload)
            return pixels, bounds
        except Exception:
            # A missing or inaccessible derived cache must not block source downloads.
            pass
    palette = {code: i+1 for i, (code, _, _) in enumerate(STYLES[layer])}
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'source.gpkg'
        storage.download_file(bucket, source['Key'], str(path))
        info = pyogrio.read_info(path)
        if not info['crs']:
            raise ValueError('Source data has no CRS.')
        field = find_field(pd.DataFrame(columns=info['fields']), layer)
        if not info['features']:
            raise ValueError('Source data has no features.')
        bounds = np.array(Transformer.from_crs(info['crs'], 3857, always_xy=True).transform_bounds(*info['total_bounds'], densify_pts=21))
        shape, transform = grid(bounds)
        canvas = np.zeros(shape, dtype='uint8')
        for offset in range(0, info['features'], BATCH_SIZE):
            frame = gpd.read_file(path, columns=[field], skip_features=offset, max_features=BATCH_SIZE)
            frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty]
            frame = frame[frame.geom_type.isin(['Polygon', 'MultiPolygon'])].to_crs(3857)
            if frame.empty:
                continue
            values = frame[field].map(class_code).map(lambda code: palette.get(code, palette['OTHERS']))
            # GDAL burns only visible pixels; no merged full-detail geometry is retained.
            rasterize(zip(frame.geometry, values), out=canvas, transform=transform)
    staging = target.with_suffix('.part')
    with staging.open('wb') as stream:
        np.savez_compressed(stream, pixels=canvas, bounds=bounds)
    staging.replace(target)
    if persistent:
        try:
            storage.put_object(Bucket=bucket, Key=remote_key, Body=target.read_bytes(), ContentType='application/octet-stream')
        except Exception:
            logging.getLogger(__name__).warning('Regional raster retained locally; shared cache write unavailable')
    return canvas, bounds


def render_region(storage, bucket, sources, layer, title, target, cache):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    rasters = [province_raster(storage, bucket, source, layer, cache) for source in sources]
    bounds = (min(b[0] for _, b in rasters), min(b[1] for _, b in rasters), max(b[2] for _, b in rasters), max(b[3] for _, b in rasters))
    shape, transform = grid(bounds)
    canvas = np.zeros(shape, dtype='uint8')
    for pixels, source_bounds in rasters:
        projected = np.zeros(shape, dtype='uint8')
        reproject(pixels, projected,
                  src_transform=from_bounds(*source_bounds, pixels.shape[1], pixels.shape[0]), src_crs=3857,
                  dst_transform=transform, dst_crs=3857,
                  src_nodata=0, dst_nodata=0, resampling=Resampling.nearest)
        visible = projected != 0
        canvas[visible] = projected[visible]
    if not canvas.any():
        raise ValueError('No features are visible in the regional dataset.')
    styles = STYLES[layer]
    colors = [(1, 1, 1, 0)] + [tuple(channel/255 for channel in rgb) + (1,) for _, _, rgb in styles]
    fig, ax = plt.subplots(figsize=(11.7, 8.3))
    try:
        ax.imshow(canvas, cmap=ListedColormap(colors), vmin=0, vmax=len(styles), origin='upper', interpolation='nearest', extent=(bounds[0], bounds[2], bounds[1], bounds[3]))
        ax.set_title(f'{layer.upper()}\n{title}', fontsize=15, weight='bold')
        ax.set_axis_off()
        ax.annotate('N ↑', (.97, .95), xycoords='axes fraction', ha='right', fontsize=14)
        present = set(np.unique(canvas))
        handles = [Patch(facecolor=colors[i+1], label=f'{code} — {description}') for i, (code, description, _) in enumerate(styles) if i+1 in present]
        ax.legend(handles=handles, loc='center left', bbox_to_anchor=(1, .5), frameon=False, fontsize=8)
        fig.text(.03, .02, 'Source: NPAAAD / SAFDZ database • CRS: EPSG:3857', fontsize=8)
        fig.savefig(target, format='jpeg', dpi=150, bbox_inches='tight', facecolor='white')
    finally:
        plt.close(fig)
