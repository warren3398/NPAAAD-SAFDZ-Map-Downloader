import numpy as np
import geopandas as gpd
import pytest
from flask import Flask
from shapely.geometry import box
from PIL import Image

import direct_exports as exports
import regional_jpeg as regional
from test_direct_exports import Store, args

PROVINCES = ['bukidnon', 'camiguin', 'lanao-del-norte', 'misamis-occidental', 'misamis-oriental']


def test_region_jpeg_skips_merged_geopackage_and_reuses_province_rasters(tmp_path, monkeypatch):
    store = Store(tmp_path, PROVINCES)
    monkeypatch.setattr(exports, 'ROOT', tmp_path / 'cache')
    def forbidden(*a, **kw):
        raise AssertionError('Regional JPEG must not create a merged GeoPackage')
    monkeypatch.setattr(gpd.GeoDataFrame, 'to_file', forbidden)
    app = Flask(__name__)
    exports.register(app, store, 'data', lambda _: None)
    client = app.test_client()
    response = client.get('/api/download', query_string=args('region', 'jpeg'))
    assert response.status_code == 200, response.data
    assert response.data.startswith(b'\xff\xd8')
    assert not list((tmp_path / 'cache').rglob('*.gpkg'))
    assert len(list((tmp_path / 'cache').rglob('*.npz'))) == 5
    # Remove only the final image: a rebuild must use cached province rasters.
    for path in (tmp_path / 'cache').rglob('*.jpg'): path.unlink()
    def no_download(*a, **kw): raise AssertionError('Cached province raster was not reused')
    monkeypatch.setattr(store, 'download_file', no_download)
    assert client.get('/api/download', query_string=args('region', 'jpeg')).status_code == 200


def test_raster_keeps_class_colors_bounds_and_all_batches(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(regional, 'BATCH_SIZE', 1)
    pixels, bounds = regional.province_raster(store, 'data', store.objects[0], 'npaaad', tmp_path / 'cache')
    assert pixels.shape[0] <= regional.PIXELS and pixels.shape[1] <= regional.PIXELS
    assert {0, 1, 2} <= set(np.unique(pixels))
    assert bounds[0] == pytest.approx(0)
    assert bounds[2] > 600000
    assert bounds[3] > 600000


def test_cache_invalidated_by_source_version(tmp_path, monkeypatch):
    store = Store(tmp_path)
    cache = tmp_path / 'cache'
    regional.province_raster(store, 'data', store.objects[0], 'npaaad', cache)
    calls = []
    download = store.download_file
    def record(*args, **kwargs): calls.append(args); download(*args, **kwargs)
    monkeypatch.setattr(store, 'download_file', record)
    store.objects[0]['ETag'] = 'changed'
    regional.province_raster(store, 'data', store.objects[0], 'npaaad', cache)
    assert len(calls) == 1


def test_shared_raster_cache_survives_local_cache_clear(tmp_path, monkeypatch):
    import io
    import shutil
    store = Store(tmp_path)
    shared = {}
    def get_object(Bucket, Key): return {'Body': io.BytesIO(shared[Key])}
    def put_object(Bucket, Key, Body, **kwargs): shared[Key] = Body
    monkeypatch.setattr(store, 'get_object', get_object)
    monkeypatch.setattr(store, 'put_object', put_object, raising=False)
    cache = tmp_path / 'cache'
    first, bounds = regional.province_raster(store, 'data', store.objects[0], 'npaaad', cache)
    assert len(shared) == 1
    shutil.rmtree(cache)
    def fail(*args): raise AssertionError('Shared cached raster should avoid source download')
    monkeypatch.setattr(store, 'download_file', fail)
    second, second_bounds = regional.province_raster(store, 'data', store.objects[0], 'npaaad', cache)
    np.testing.assert_array_equal(first, second)
    np.testing.assert_array_equal(bounds, second_bounds)
