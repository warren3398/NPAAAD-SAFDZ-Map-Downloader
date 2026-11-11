"""Bounded-memory vector exports; geometry is never simplified for GIS downloads."""
from pathlib import Path
import tempfile
import re
import json
import zipfile

import geopandas as gpd
import pandas as pd
import numpy as np
import pyogrio
from boto3.s3.transfer import TransferConfig

BATCH_SIZE = 128
TRANSFER = TransferConfig(max_concurrency=2, max_io_queue=4, io_chunksize=256*1024)


def area_batches(path, boundary=None, columns=None):
    info = pyogrio.read_info(path)
    if not info['crs']:
        raise ValueError('Source data has no CRS.')
    clip = boundary.to_crs(info['crs']) if boundary is not None else None
    bbox = tuple(clip.total_bounds) if clip is not None else None
    if clip is not None:
        clip = clip.copy()
        clip.geometry = clip.geometry.make_valid()
    for offset in range(0, info['features'], BATCH_SIZE):
        frame = gpd.read_file(path, columns=columns, bbox=bbox, skip_features=offset, max_features=BATCH_SIZE, fid_as_index=True)
        if frame.empty:
            break
        frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
        if clip is not None and not frame.empty:
            invalid = ~frame.geometry.is_valid
            if invalid.any():
                frame.loc[invalid, frame.geometry.name] = frame.loc[invalid].geometry.make_valid()
            frame = gpd.clip(frame, clip, keep_geom_type=True)
        frame = frame[frame.geom_type.isin(['Polygon', 'MultiPolygon'])]
        if not frame.empty:
            yield frame


def schema_for(infos):
    fields = {}
    for info in infos:
        for name, dtype in zip(info['fields'], info['dtypes']):
            fields.setdefault(str(name), []).append(np.dtype(dtype).kind)
    return {name: ('Int64' if set(kinds) <= {'i'} else 'float64' if set(kinds) <= {'i','u','f'} and 'u' not in kinds else 'string') for name, kinds in fields.items()}


def build_geopackage(storage, bucket, sources, layer, boundary, target):
    from map_layout import STYLES, find_field
    from regional_jpeg import class_code
    palette = {code for code, _, _ in STYLES[layer]}
    target = Path(target)
    target.unlink(missing_ok=True)
    written = 0
    with tempfile.TemporaryDirectory() as scratch:
        paths, infos = [], []
        for index, source in enumerate(sources):
            path = Path(scratch) / f'{index}.gpkg'
            storage.download_file(bucket, source['Key'], str(path), Config=TRANSFER)
            paths.append(path)
            infos.append(pyogrio.read_info(path))
        schema = schema_for(infos)
        # Derived provenance names are stable; retain original attributes too.
        derived = {'map_class': 'string', 'source_prov': 'string', 'source_fid': 'string'}
        schema.update(derived)
        for path, info, source in zip(paths, infos, sources):
            try:
                class_field = find_field(pd.DataFrame(columns=info['fields']), layer)
            except ValueError:
                class_field = None
            for frame in area_batches(path, boundary):
                source_ids = frame.index.astype(str)
                codes = frame[class_field].map(class_code) if class_field else pd.Series(None, index=frame.index)
                frame['map_class'] = codes.map(lambda code: code if code in palette else 'OTHERS') if class_field else None
                frame['source_prov'] = source['Key'].split('/')[2]
                frame['source_fid'] = source_ids
                for name, dtype in schema.items():
                    if name not in frame:
                        frame[name] = pd.NA
                    if dtype == 'string':
                        frame[name] = frame[name].astype('string')
                    else:
                        frame[name] = pd.to_numeric(frame[name]).astype(dtype)
                frame = frame[[*schema, frame.geometry.name]].to_crs(4326)
                frame.to_file(target, driver='GPKG', layer='map', index=False, mode='a' if written else 'w', promote_to_multi=True)
                written += len(frame)
                del frame
    if not written:
        raise ValueError('No NPAAAD / SAFDZ features found in the selected area.')
    return written


def shapefile_from_geopackage(gpkg, target, folder):
    info = pyogrio.read_info(gpkg)
    renamed, used = {}, {'geometry'}
    for column in info['fields']:
        base = re.sub(r'[^A-Za-z0-9_]', '_', str(column))[:10] or 'field'
        name, index = base, 1
        while name.lower() in used:
            suffix = str(index)
            name = base[:10-len(suffix)] + suffix
            index += 1
        renamed[column] = name
        used.add(name.lower())
    shape = Path(folder) / 'shapefile'
    shape.mkdir(exist_ok=True)
    for path in shape.glob('map.*'):
        path.unlink()
    written = 0
    for frame in area_batches(gpkg):
        frame.rename(columns=renamed).to_file(shape / 'map.shp', driver='ESRI Shapefile', encoding='UTF-8', index=False, mode='a' if written else 'w')
        written += len(frame)
        del frame
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in shape.glob('map.*'):
            archive.write(path, path.name)
        archive.writestr('fields.json', json.dumps(renamed, indent=2))
