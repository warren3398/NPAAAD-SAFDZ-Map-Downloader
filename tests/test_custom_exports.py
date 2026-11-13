import io,time
import geopandas as gpd
import pytest
from PIL import Image
from flask import Flask
from shapely.geometry import box,Polygon
import shapely
import boundary_upload as upload
import direct_exports as exports
from test_direct_exports import Store

@pytest.fixture
def custom(tmp_path,monkeypatch):
    monkeypatch.setattr(upload,'ROOT',tmp_path/'uploads')
    monkeypatch.setattr(exports,'ROOT',tmp_path/'cache')
    store=Store(tmp_path,('camiguin','bukidnon'))
    geometries=[box(0,0,1,2),box(1,0,2,2)]
    for name,geometry in zip(('camiguin','bukidnon'),geometries):
        p=tmp_path/(name+'.gpkg');p.unlink()
        gpd.GeoDataFrame({'NPAAAD':['A'],'SAFDZ':['1']},geometry=[geometry],crs=4326).to_file(p,driver='GPKG')
    for obj in list(store.objects):store.objects.append({**obj,'Key':obj['Key'].replace('npaaad','safdz')})
    province=tmp_path/'provinces.gpkg'
    gpd.GeoDataFrame({'ADM2_EN':['Camiguin','Bukidnon']},geometry=geometries,crs=4326).to_file(province,driver='GPKG')
    app=Flask(__name__);app.testing=True
    exports.register(app,store,'data',lambda _:province)
    mask=Polygon(box(.25,.25,1.75,1.75).exterior.coords,[box(.8,.8,1.2,1.2).exterior.coords])
    raw=gpd.GeoDataFrame(geometry=[mask],crs=4326).to_json().encode()
    client=app.test_client()
    response=client.post('/api/upload-boundary',data={'boundary':(io.BytesIO(raw),'test-cross-province.geojson')})
    assert response.status_code==201,response.data
    return client,response.json,mask,tmp_path

@pytest.mark.parametrize('layer',['npaaad','safdz'])
@pytest.mark.parametrize('fmt',['gpkg','zip','jpeg'])
def test_uploaded_boundary_clips_all_formats_without_admin_selection(custom,layer,fmt):
    client,meta,mask,tmp_path=custom
    params={'boundary_id':meta['boundary_id'],'layer':layer,'format':fmt}
    response=client.get('/api/download',query_string=params)
    assert response.status_code==200,response.data[:500]
    if fmt=='jpeg':
        image=Image.open(io.BytesIO(response.data));assert image.size==(3840,2406)
        import numpy as np
        from pyproj import Transformer
        x,y=Transformer.from_crs(4326,3857,always_xy=True).transform(1,1)
        for cache in (upload.ROOT/meta['boundary_id']).rglob('*.npz'):
            with np.load(cache) as raster:
                pixels,bounds=raster['pixels'],raster['bounds']
                row=int((bounds[3]-y)/(bounds[3]-bounds[1])*pixels.shape[0])
                col=int((x-bounds[0])/(bounds[2]-bounds[0])*pixels.shape[1])
                assert pixels[row,col]==0,'JPEG must preserve the uploaded polygon hole'
    else:
        path=tmp_path/('out.'+fmt);path.write_bytes(response.data)
        frame=gpd.read_file('zip://'+str(path) if fmt=='zip' else path)
        union=shapely.union_all(frame.geometry)
        assert union.symmetric_difference(mask).area<1e-10
        assert set(frame['source_pro' if fmt=='zip' else 'source_prov'])=={'camiguin','bukidnon'}


def test_custom_job_preparation_and_missing_upload_are_explicit(custom):
    client,meta,_,_=custom
    params={'boundary_id':meta['boundary_id'],'layer':'npaaad','format':'gpkg'}
    response=client.post('/api/prepare-download',json=params)
    assert response.status_code==202
    for _ in range(200):
        result=client.get('/api/download-status/'+response.json['job_id']).json
        if result['status'] in ('ready','failed'):break
        time.sleep(.01)
    assert result['status']=='ready',result
    bad=client.get('/api/download',query_string={**params,'boundary_id':'a'*32})
    assert bad.status_code==400 and 'Upload' in bad.json['error']
    invalid=client.post('/api/upload-boundary',data={'boundary':(io.BytesIO(b'bad'),'boundary.geojson')})
    assert invalid.status_code==400


def test_independent_city_boundary_resolves_actual_source_parent(custom):
    client,meta,mask,tmp_path=custom
    store=Store(tmp_path,('camiguin','bukidnon'))
    city=tmp_path/'city.gpkg'
    municipalities=tmp_path/'parents.gpkg'
    geometries=[box(0,0,1,2),box(1,0,2,2)]
    gpd.GeoDataFrame({'ADM2_EN':['City of Example','Bukidnon']},geometry=geometries,crs=4326).to_file(city,driver='GPKG')
    gpd.GeoDataFrame({'NAME_1':['Camiguin','Bukidnon'],'NAME_2':['Example','Other']},geometry=geometries,crs=4326).to_file(municipalities,driver='GPKG')
    frame=gpd.GeoDataFrame(geometry=[mask],crs=4326)
    sources=exports.custom_sources(store,'data',lambda kind:city if kind=='province' else municipalities,frame,'npaaad')
    assert {s['Key'].split('/')[2] for s in sources}=={'camiguin','bukidnon'}


def test_native_transfer_tracks_bytes_without_buffering(custom):
    client,meta,_,_=custom
    params={'boundary_id':meta['boundary_id'],'format':'gpkg','layer':'npaaad','transfer_id':'b'*32}
    response=client.get('/api/download',query_string=params,buffered=False)
    assert response.status_code==200
    before=client.get('/api/transfer-progress/'+'b'*32).json
    assert before['status']=='sending' and before['bytes_sent']==0
    payload=b''.join(response.response)
    after=client.get('/api/transfer-progress/'+'b'*32).json
    assert after['status']=='complete' and after['percent']==100
    assert after['bytes_sent']==after['total_bytes']==len(payload)
