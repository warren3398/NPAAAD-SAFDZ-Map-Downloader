import io
import json
from pathlib import Path
import zipfile

import geopandas as gpd
import pytest
from flask import Flask
from shapely.geometry import box

import direct_exports as exports

REGION = 'Region X – Northern Mindanao'

class Store:
    def __init__(self, root, provinces=('camiguin',)):
        self.root = root
        self.objects = []
        for province in provinces:
            key = f'nationwide/mindanao/{province}/npaaad.gpkg'
            frame = gpd.GeoDataFrame({'NPAAAD': ['A', 'B'], 'long_field_name': ['inside', 'outside']}, geometry=[box(0, 0, 2, 2), box(5, 5, 6, 6)], crs=4326)
            path = root / (province + '.gpkg')
            frame.to_file(path, driver='GPKG')
            self.objects.append({'Key': key, 'Size': path.stat().st_size, 'ETag': 'test-' + province})
    def get_paginator(self, name): return self
    def paginate(self, **kwargs): return [{'Contents': self.objects}]
    def download_file(self, bucket, key, target): Path(target).write_bytes((self.root / (key.split('/')[2] + '.gpkg')).read_bytes())
    def get_object(self, **kwargs):
        payload = (self.root / (kwargs['Key'].split('/')[2] + '.gpkg')).read_bytes()
        class Body(io.BytesIO):
            def iter_chunks(self, chunk_size):
                while chunk := self.read(chunk_size): yield chunk
        return {'Body': Body(payload), 'ContentLength': len(payload)}

@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    boundaries = {}
    for scope in ('municipality', 'barangay'):
        path = tmp_path / (scope + '.gpkg')
        frame = gpd.GeoDataFrame({'NAME_1':['Camiguin'], 'NAME_2':['Guinsiliban'], 'NAME_3':['Test']}, geometry=[box(0, 0, 1, 1)], crs=4326)
        frame.to_file(path, driver='GPKG')
        boundaries[scope] = path
    monkeypatch.setattr(exports, 'ROOT', tmp_path / 'cache')
    app = Flask(__name__)
    app.testing = True
    exports.register(app, store, 'data', boundaries.__getitem__)
    return app.test_client(), store

def args(scope, fmt):
    return dict(island='mindanao', region=REGION, province='camiguin', scope=scope, place='guinsiliban--test' if scope=='barangay' else 'guinsiliban', layer='npaaad', format=fmt)

@pytest.mark.parametrize('scope', ['province', 'municipality', 'barangay'])
@pytest.mark.parametrize('fmt', ['gpkg', 'zip', 'jpeg'])
def test_scoped_downloads(setup, tmp_path, scope, fmt):
    client, _ = setup
    response = client.get('/api/download', query_string=args(scope, fmt))
    assert response.status_code == 200, response.data
    assert 'attachment' in response.headers['Content-Disposition']
    if fmt == 'jpeg':
        assert response.data.startswith(b'\xff\xd8')
        return
    path = tmp_path / ('result.gpkg' if fmt=='gpkg' else 'result.zip')
    path.write_bytes(response.data)
    if fmt == 'zip':
        with zipfile.ZipFile(path) as archive:
            assert {'map.shp','map.shx','map.dbf','map.prj','fields.json'} <= set(archive.namelist())
            archive.extractall(tmp_path / 'shape')
        path = tmp_path / 'shape/map.shp'
    frame = gpd.read_file(path)
    assert len(frame) == (2 if scope=='province' else 1)
    if scope != 'province': assert frame.total_bounds.tolist() == [0,0,1,1]

@pytest.mark.parametrize('fmt', ['gpkg', 'zip', 'jpeg'])
def test_region_combines_all_provinces(tmp_path, monkeypatch, fmt):
    provinces = ['bukidnon','camiguin','lanao-del-norte','misamis-occidental','misamis-oriental']
    store = Store(tmp_path, provinces)
    monkeypatch.setattr(exports, 'ROOT', tmp_path / 'cache')
    app = Flask(__name__)
    exports.register(app, store, 'data', lambda scope: None)
    response = app.test_client().get('/api/download', query_string={**args('region', fmt), 'province':''})
    assert response.status_code == 200, response.data
    if fmt=='gpkg':
        path = tmp_path / 'region.gpkg'; path.write_bytes(response.data)
        frame = gpd.read_file(path)
        assert len(frame)==10
        assert set(frame.source_prov)==set(provinces)

def test_incomplete_region_fails_explicitly(setup):
    client, _ = setup
    response = client.get('/api/download', query_string=args('region','gpkg'))
    assert response.status_code == 400
    assert 'bukidnon' in response.json['error']

def test_province_outside_region_rejected(setup):
    client, _ = setup
    response = client.get('/api/download', query_string={**args('province','gpkg'),'province':'abra'})
    assert response.status_code == 400


def test_second_format_uses_cached_clip(setup, monkeypatch):
    client, store = setup
    assert client.get('/api/download', query_string=args('municipality','gpkg')).status_code==200
    def fail(*args): raise AssertionError('Source should not download twice')
    monkeypatch.setattr(store, 'download_file', fail)
    assert client.get('/api/download', query_string=args('municipality','zip')).status_code==200

@pytest.mark.parametrize('fmt', ['gpkg','zip','jpeg'])
def test_safdz_downloads(setup, tmp_path, fmt):
    client, store = setup
    path = tmp_path / 'camiguin.gpkg'
    path.unlink()
    gpd.GeoDataFrame({'SAFDZ':[1.0,2.0]}, geometry=[box(0,0,2,2),box(5,5,6,6)],crs=4326).to_file(path,driver='GPKG')
    store.objects[0]['Key'] = store.objects[0]['Key'].replace('npaaad','safdz')
    response=client.get('/api/download',query_string={**args('municipality',fmt),'layer':'safdz','download_token':'a'*32})
    assert response.status_code==200,response.data
    assert 'npaaad_download='+'a'*32 in response.headers['Set-Cookie']
    if fmt=='gpkg':
        result=tmp_path/'safdz-result.gpkg';result.write_bytes(response.data)
        assert gpd.read_file(result).map_class.tolist()==['1']

def test_source_dataset_listing(setup):
    client, _=setup
    response=client.get('/api/datasets')
    assert response.json=={'items':[{'island':'mindanao','province':'camiguin','layer':'npaaad'}]}
