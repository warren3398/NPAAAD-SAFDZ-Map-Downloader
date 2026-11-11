import numpy as np
import pytest
from PIL import Image
from pyproj import Transformer
from jpeg_layout import class_areas,compose
from test_direct_exports import setup


def test_hectares_use_equal_area_and_exclude_nodata():
    bounds=(0,0,1000,1000)
    canvas=np.array([[1,2],[0,2]],dtype='uint8')
    areas=class_areas(canvas,bounds,2)
    project=Transformer.from_crs(3857,6933,always_xy=True)
    x0,y0=project.transform(0,0);x1,y1=project.transform(1000,1000)
    _,ym=project.transform(0,500)
    assert areas[0]==pytest.approx((x1-x0)/2*(y1-ym)/10000)
    assert areas[1]==pytest.approx((x1-x0)/2*(y1-y0)/10000)
    assert areas.sum()<(x1-x0)*(y1-y0)/10000


def test_projected_pixel_area_accounts_for_latitude():
    project=Transformer.from_crs(4326,3857,always_xy=True)
    _,north=project.transform(0,60)
    canvas=np.ones((2,2),dtype='uint8')
    equator=class_areas(canvas,(0,0,1000,1000),1)[0]
    high=class_areas(canvas,(0,north,1000,north+1000),1)[0]
    assert .2*equator<high<.3*equator


@pytest.mark.parametrize('layer',['npaaad','safdz'])
def test_reference_layout_has_map_frame_green_panels_and_area_summary(tmp_path,layer):
    target=tmp_path/(layer+'.jpg')
    canvas=np.array([[1,1,2,2],[1,0,2,2]],dtype='uint8')
    result=compose(canvas,(0,0,1000,500),layer,'Bokod',target)
    image=Image.open(target)
    assert image.size==(3000,1880)
    red,green,blue=image.getpixel((2100,160))
    assert green>red+30
    assert result['total_ha']>0
    assert sum(result['areas_ha'])==pytest.approx(result['total_ha'])
    assert 'estimate' in result['area_method']


def test_old_cached_jpeg_is_not_reused_after_layout_change(setup,tmp_path):
    import hashlib,json
    import direct_exports as exports
    from test_direct_exports import args,REGION
    from jpeg_layout import LAYOUT_VERSION
    client,store=setup
    identity=json.dumps(['mindanao',REGION,'camiguin','province','','npaaad',[(o['Key'],o.get('ETag'),str(o.get('LastModified',''))) for o in store.objects]])
    folder=exports.ROOT/hashlib.sha256(identity.encode()).hexdigest()
    folder.mkdir(parents=True)
    (folder/'camiguin_npaaad.jpg').write_bytes(b'old layout')
    response=client.get('/api/download',query_string={**args('province','jpeg'),'place':''})
    assert response.status_code==200 and response.data.startswith(b'\xff\xd8')
    assert (folder/(LAYOUT_VERSION+'-camiguin_npaaad.jpg')).exists()
