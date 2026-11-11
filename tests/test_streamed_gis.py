import geopandas as gpd
import pandas as pd
import pyogrio
from flask import Flask
from shapely.geometry import box

import direct_exports as exports
import streamed_gis as vectors
from test_direct_exports import Store,args


def test_streamed_clip_and_append_keep_all_features_and_numeric_attributes(tmp_path, monkeypatch):
    store=Store(tmp_path)
    path=tmp_path/'camiguin.gpkg';path.unlink()
    source=gpd.GeoDataFrame({'NPAAAD':['A','B','C'],'quantity':[1,2,3],'label':['a',None,'c']},geometry=[box(0,0,.3,1),box(.3,0,.6,1),box(.6,0,2,1)],crs=4326)
    source.to_file(path,driver='GPKG')
    monkeypatch.setattr(vectors,'BATCH_SIZE',1)
    boundary=gpd.GeoDataFrame(geometry=[box(0,0,1,1)],crs=4326)
    target=tmp_path/'selected.gpkg'
    assert vectors.build_geopackage(store,'data',store.objects,'npaaad',boundary,target)==3
    result=gpd.read_file(target)
    assert result.quantity.tolist()==[1,2,3]
    assert len(set(result.source_fid))==3
    assert result.geometry.area.sum()==1
    assert result.label.isna().sum()==1
    vectors.shapefile_from_geopackage(target,tmp_path/'map.zip',tmp_path)
    shape=gpd.read_file(tmp_path/'shapefile/map.shp')
    assert len(shape)==3 and shape.quantity.tolist()==[1,2,3]


def test_region_union_schema_handles_missing_fields_and_preserves_types(tmp_path,monkeypatch):
    store=Store(tmp_path,('camiguin','bukidnon'))
    path=tmp_path/'bukidnon.gpkg';path.unlink()
    gpd.GeoDataFrame({'NPAAAD':['C'],'extra':['new'],'count':[5]},geometry=[box(2,2,3,3)],crs=4326).to_file(path,driver='GPKG')
    monkeypatch.setattr(vectors,'BATCH_SIZE',1)
    target=tmp_path/'region.gpkg'
    assert vectors.build_geopackage(store,'data',store.objects,'npaaad',None,target)==3
    result=gpd.read_file(target)
    assert result.extra.isna().sum()==2
    assert result['count'].isna().sum()==2
    assert result['count'].dropna().tolist()==[5]
    assert set(result.source_prov)=={'camiguin','bukidnon'}


def test_every_jpeg_scope_skips_merged_vector_export(tmp_path,monkeypatch):
    store=Store(tmp_path)
    boundary_path=tmp_path/'boundary.gpkg'
    gpd.GeoDataFrame({'NAME_1':['Camiguin'],'NAME_2':['Guinsiliban'],'NAME_3':['Test']},geometry=[box(0,0,1,1)],crs=4326).to_file(boundary_path,driver='GPKG')
    monkeypatch.setattr(exports,'ROOT',tmp_path/'cache')
    def no_vector_write(*args,**kwargs):raise AssertionError('JPEG must not write a full-detail vector file')
    monkeypatch.setattr(gpd.GeoDataFrame,'to_file',no_vector_write)
    app=Flask(__name__);exports.register(app,store,'data',lambda _:boundary_path)
    for scope in ['province','municipality','barangay']:
        response=app.test_client().get('/api/download',query_string=args(scope,'jpeg'))
        assert response.status_code==200,response.data
        assert response.data.startswith(b'\xff\xd8')
    assert not list((tmp_path/'cache').rglob('*.gpkg'))
