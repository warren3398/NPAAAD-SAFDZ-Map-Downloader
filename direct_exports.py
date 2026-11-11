"""Download selected administrative areas directly from the source GeoPackages."""
import hashlib
import json
import re
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from flask import jsonify, request, send_file

CATALOG = json.loads(Path(__file__).with_name('administrative_catalog.json').read_text())
LOCK = threading.Lock()
ROOT = Path(tempfile.gettempdir()) / 'npaaad_direct_exports'


class ExportBusy(Exception):
    pass


@contextmanager
def export_slot():
    if not LOCK.acquire(timeout=1):
        raise ExportBusy('Another download is being prepared. Please wait for it to finish, then retry.')
    try:
        yield
    finally:
        LOCK.release()


def slug(value):
    return re.sub(r'[^a-z0-9]+', '-', str(value).lower()).strip('-')


def canonical(value):
    value = slug(value).removeprefix('province-of-')
    return CATALOG['aliases'].get(value, value)


def source_selection(storage, bucket, island, region, province, layer):
    regions = {r['name']: r for r in CATALOG['regions']}
    if region not in regions:
        raise ValueError('Select a valid region.')
    expected = {canonical(p) for p in regions[region]['provinces']}
    if province and canonical(province) not in expected:
        raise ValueError('Province does not belong to the selected region.')
    available = {}
    pattern = re.compile(r'nationwide/(luzon|visayas|mindanao)/([a-z0-9-]{2,60})/' + layer + r'\.gpkg')
    for page in storage.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=f'nationwide/{island}/'):
        for obj in page.get('Contents', []):
            match = pattern.fullmatch(obj['Key'])
            if not match or not obj.get('Size', 0):
                continue
            name = match[2]
            norm = canonical(name)
            if norm not in expected or (province and name != province):
                continue
            # Use one source per province, preferring the canonical folder name.
            if norm not in available or name == norm:
                available[norm] = obj
    missing = ({canonical(province)} if province else expected) - available.keys()
    if missing:
        raise ValueError('Source data unavailable for: ' + ', '.join(sorted(missing)) + '. A complete download cannot be prepared.')
    return [available[p] for p in sorted(available)]


def selected_boundary(boundary_path, scope, province, place):
    path = boundary_path(scope)
    table = pyogrio.read_dataframe(path, columns=['NAME_1', 'NAME_2'] + (['NAME_3'] if scope == 'barangay' else []), read_geometry=False, fid_as_index=True)
    mask = table.NAME_1.map(canonical).eq(canonical(province))
    if scope == 'barangay':
        municipality, separator, barangay = place.partition('--')
        if not separator:
            raise ValueError('Select a municipality and barangay.')
        mask &= table.NAME_2.map(slug).eq(municipality) & table.NAME_3.map(slug).eq(barangay)
    else:
        mask &= table.NAME_2.map(slug).eq(place)
    ids = table.index[mask].tolist()
    if not ids:
        raise ValueError('Selected administrative boundary was not found.')
    return gpd.read_file(path, fids=ids)


def read_area(path, boundary=None):
    if boundary is None:
        frame = gpd.read_file(path)
    else:
        info = pyogrio.read_info(path)
        if not info['crs']:
            raise ValueError('Source data has no CRS.')
        boundary = boundary.to_crs(info['crs'])
        frame = gpd.read_file(path, bbox=tuple(boundary.total_bounds))
        if not frame.empty:
            frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
            frame.geometry = frame.geometry.make_valid()
            frame = gpd.clip(frame, boundary, keep_geom_type=True)
    if frame.crs is None:
        raise ValueError('Source data has no CRS.')
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    return frame[frame.geom_type.isin(['Polygon', 'MultiPolygon'])]


def simple_jpeg(frame, layer, title, target):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from map_layout import STYLES
    styles = STYLES[layer]
    fig, ax = plt.subplots(figsize=(11.7, 8.3))
    try:
        view = frame.to_crs(3857)
        # Simplification affects the JPEG display only; GIS exports retain geometry.
        extent = view.total_bounds
        tolerance = max(extent[2] - extent[0], extent[3] - extent[1]) / 2400
        view.geometry = view.geometry.simplify(tolerance, preserve_topology=True)
        handles = []
        for code, description, rgb in styles:
            part = view[view['map_class'] == code]
            if part.empty:
                continue
            color = tuple(c / 255 for c in rgb)
            part.plot(ax=ax, color=color, edgecolor='none')
            handles.append(Patch(facecolor=color, label=f'{code} — {description}'))
        ax.set_title(f'{layer.upper()}\n{title}', fontsize=15, weight='bold')
        ax.set_axis_off()
        ax.annotate('N ↑', (.97, .95), xycoords='axes fraction', ha='right', fontsize=14)
        if handles:
            ax.legend(handles=handles, loc='center left', bbox_to_anchor=(1, .5), frameon=False, fontsize=8)
        fig.text(.03, .02, 'Source: NPAAAD / SAFDZ database • CRS: EPSG:3857', fontsize=8)
        fig.savefig(target, format='jpeg', dpi=150, bbox_inches='tight', facecolor='white')
    finally:
        plt.close(fig)


def shapefile_zip(frame, target, folder):
    shape = folder / 'shapefile'
    shape.mkdir(exist_ok=True)
    renamed = {}
    used = {'geometry'}
    for column in frame.columns:
        if column == frame.geometry.name:
            continue
        base = re.sub(r'[^A-Za-z0-9_]', '_', str(column))[:10] or 'field'
        name, index = base, 1
        while name.lower() in used:
            suffix = str(index)
            name = base[:10-len(suffix)] + suffix
            index += 1
        renamed[column] = name
        used.add(name.lower())
    frame.rename(columns=renamed).to_file(shape / 'map.shp', driver='ESRI Shapefile', encoding='UTF-8', index=False)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in shape.glob('map.*'):
            archive.write(path, path.name)
        archive.writestr('fields.json', json.dumps(renamed, indent=2))


def register(app, storage, bucket, boundary_path):
    @app.get('/api/datasets')
    def datasets():
        if not storage:
            return jsonify(error='Source storage is unavailable.'), 503
        try:
            items = []
            pattern = re.compile(r'nationwide/(luzon|visayas|mindanao)/([a-z0-9-]{2,60})/(npaaad|safdz)\.gpkg')
            for page in storage.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix='nationwide/'):
                for obj in page.get('Contents', []):
                    match = pattern.fullmatch(obj['Key'])
                    if match and obj.get('Size', 0) and match[2] != 'north':
                        items.append(dict(island=match[1], province=match[2], layer=match[3]))
            return jsonify(items=items), 200, {'X-Regional-Renderer': 'raster-v1'}
        except Exception:
            app.logger.exception('Source dataset listing failed')
            return jsonify(error='Source dataset list is unavailable.'), 503

    @app.get('/api/download')
    def direct_download():
        island = request.args.get('island', '')
        region = request.args.get('region', '')
        province = request.args.get('province', '')
        scope = request.args.get('scope', 'province')
        place = request.args.get('place', '')
        layer = request.args.get('layer', '')
        fmt = request.args.get('format', '')
        if island not in ('luzon', 'visayas', 'mindanao') or layer not in ('npaaad', 'safdz') or fmt not in ('gpkg', 'zip', 'jpeg'):
            return jsonify(error='Invalid download selection.'), 400
        if scope not in ('region', 'province', 'municipality', 'barangay') or (scope != 'region' and not re.fullmatch(r'[a-z0-9-]{2,60}', province)) or (scope in ('municipality', 'barangay') and not re.fullmatch(r'[a-z0-9-]{2,90}', place)):
            return jsonify(error='Invalid administrative area.'), 400
        if scope == 'region':
            province = ''
        if not storage:
            return jsonify(error='Source storage is unavailable.'), 503
        def ready(response):
            token = request.args.get('download_token', '')
            if re.fullmatch(r'[a-f0-9]{32}', token):
                response.set_cookie('npaaad_download', token, max_age=120, samesite='Lax')
            return response
        try:
            sources = source_selection(storage, bucket, island, region, province, layer)
            # Province GeoPackage is already in the requested format: stream it directly.
            if scope == 'province' and fmt == 'gpkg':
                from flask import Response, stream_with_context
                obj = storage.get_object(Bucket=bucket, Key=sources[0]['Key'])
                def chunks():
                    try:
                        yield from obj['Body'].iter_chunks(chunk_size=1024*1024)
                    finally:
                        obj['Body'].close()
                return ready(Response(stream_with_context(chunks()), mimetype='application/geopackage+sqlite3', headers={'Content-Disposition': f'attachment; filename="{province}_{layer}.gpkg"', 'Content-Length': str(obj['ContentLength'])}))
            identity = json.dumps([island, region, province, scope, place, layer, [(o['Key'], o.get('ETag'), str(o.get('LastModified', ''))) for o in sources]])
            folder = ROOT / hashlib.sha256(identity.encode()).hexdigest()
            ext = {'jpeg': 'jpg', 'gpkg': 'gpkg', 'zip': 'zip'}[fmt]
            label = place or province or slug(region)
            name = f'{label}_{layer}.{ext}'
            target = folder / name
            if scope == 'region' and fmt == 'jpeg':
                from regional_jpeg import render_region
                with export_slot():
                    folder.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        staging = folder / 'regional.part.jpg'
                        render_region(storage, bucket, sources, layer, region, staging, ROOT / 'province_rasters')
                        staging.replace(target)
                response = ready(send_file(target, as_attachment=True, download_name=name))
                response.headers['X-Regional-Renderer'] = 'raster-v1'
                return response
            with export_slot():
                folder.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    gpkg = folder / 'selected.gpkg'
                    if not gpkg.exists():
                        boundary = selected_boundary(boundary_path, scope, province, place) if scope in ('municipality', 'barangay') else None
                        frames = []
                        from map_layout import find_field, classify, STYLES
                        for source in sources:
                            with tempfile.TemporaryDirectory() as scratch:
                                path = Path(scratch) / 'source.gpkg'
                                storage.download_file(bucket, source['Key'], str(path))
                                frame = read_area(path, boundary)
                            if frame.empty:
                                continue
                            try:
                                field = find_field(frame, layer)
                                valid = {s[0] for s in STYLES[layer]}
                                frame['map_class'] = frame[field].map(lambda value: classify(str(int(value)) if isinstance(value, (int, float)) and pd.notna(value) and float(value).is_integer() else value)).map(lambda x: x if x in valid else 'OTHERS')
                            except ValueError:
                                frame['map_class'] = None
                                if fmt == 'jpeg':
                                    raise
                            frame['source_prov'] = source['Key'].split('/')[2]
                            frames.append(frame.to_crs(4326))
                        if not frames:
                            return jsonify(error='No NPAAAD / SAFDZ features found in the selected area.'), 404
                        combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry='geometry', crs=4326)
                        # Mixed source attributes are stored as text when their types differ.
                        for column in combined.select_dtypes(include=['object']).columns:
                            if column != combined.geometry.name:
                                combined[column] = combined[column].map(lambda value: None if value is None else str(value))
                        staging = folder / 'selected.part.gpkg'
                        staging.unlink(missing_ok=True)
                        combined.to_file(staging, driver='GPKG', layer='map', index=False)
                        staging.replace(gpkg)
                    if fmt == 'gpkg':
                        target = gpkg
                    else:
                        frame = gpd.read_file(gpkg)
                        staging = folder / ('download.part.' + ext)
                        if fmt == 'jpeg':
                            if 'map_class' not in frame or frame['map_class'].isna().any():
                                raise ValueError('Classification field missing; JPEG cannot be styled correctly.')
                            simple_jpeg(frame, layer, label.replace('--', ' / ').replace('-', ' ').title(), staging)
                        else:
                            shapefile_zip(frame, staging, folder)
                        staging.replace(target)
            return ready(send_file(target, as_attachment=True, download_name=name))
        except ExportBusy as exc:
            return jsonify(error=str(exc)), 409, {'Retry-After': '5'}
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:
            app.logger.exception('Direct export failed')
            return jsonify(error='Could not prepare this download. Please retry or select a smaller area.'), 503
