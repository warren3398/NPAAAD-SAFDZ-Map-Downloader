"""Offline batch export generator. Run on a machine with sufficient RAM, not Render free web service.
Usage: python generate_exports.py --island luzon --province abra --layer npaaad
Requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
Writes exports/<island>/<province>/<layer>.{jpg,pdf,zip} to the same R2 bucket.
"""
import argparse,os,tempfile,zipfile
from pathlib import Path
import boto3,geopandas as gpd
from PIL import Image,ImageDraw
from botocore.config import Config
import pyogrio
p=argparse.ArgumentParser()
for n in ('island','province','layer'):p.add_argument('--'+n,required=True)
a=p.parse_args()
assert a.island in ('luzon','visayas','mindanao')
assert a.layer in ('npaaad','safdz')
assert a.province.replace('-','').isalnum()
account=os.environ['R2_ACCOUNT_ID']
bucket=os.getenv('R2_BUCKET','npaaad-safdz-data')
s3=boto3.client('s3',endpoint_url=f'https://{account}.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],config=Config(signature_version='s3v4'))
prefix=f'{a.island}/{a.province}/{a.layer}'
with tempfile.TemporaryDirectory() as td:
 td=Path(td);gpkg=td/'source.gpkg'
 s3.download_file(bucket,f'nationwide/{a.island}/{a.province}/{a.layer}.gpkg',str(gpkg))
 info=pyogrio.read_info(gpkg)
 bounds=info['total_bounds']
 xmin,ymin,xmax,ymax=map(float,bounds)
 if xmax<=xmin or ymax<=ymin:raise ValueError('Invalid extent')
 width=2000;height=2000;margin=65
 scale=min((width-2*margin)/(xmax-xmin),(height-2*margin)/(ymax-ymin))
 im=Image.new('RGB',(width,height),'white');draw=ImageDraw.Draw(im)
 def coords(ring):return [((x-xmin)*scale+margin,height-margin-(y-ymin)*scale) for x,y in ring]
 def paint(geom):
  if geom is None or geom.is_empty:return
  if geom.geom_type=='Polygon':
   draw.polygon(coords(geom.exterior.coords),fill='#79b99c')
   for ring in geom.interiors:draw.polygon(coords(ring.coords),fill='white')
  elif geom.geom_type in ('MultiPolygon','GeometryCollection'):
   for part in geom.geoms:paint(part)
 offset=0
 while True:
  frame=pyogrio.read_dataframe(gpkg,skip_features=offset,max_features=200,columns=[])
  if frame.empty:break
  for geom in frame.geometry:paint(geom)
  offset+=len(frame)
  if len(frame)<200:break
 for ext in ('jpg','pdf'):
  out=td/('map.'+ext)
  if ext=='jpg':im.save(out,'JPEG',quality=92,optimize=True)
  else:im.save(out,'PDF',resolution=150)
  s3.upload_file(str(out),bucket,f'exports/{prefix}.{ext}',ExtraArgs={'ContentType':'image/jpeg' if ext=='jpg' else 'application/pdf'})
 # Conversion is an offline operation. Original source remains untouched.
 gdf=gpd.read_file(gpkg)
 shape=td/'shapefile';shape.mkdir()
 gdf.to_file(shape/'dataset.shp',driver='ESRI Shapefile',encoding='UTF-8')
 zpath=td/'dataset.zip'
 with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
  for f in shape.iterdir():z.write(f,f.name)
 s3.upload_file(str(zpath),bucket,f'exports/{prefix}.zip',ExtraArgs={'ContentType':'application/zip'})
 print(f'Uploaded exports/{prefix}.jpg, .pdf, .zip ({offset} mapped features)')
