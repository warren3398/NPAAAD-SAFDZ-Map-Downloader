import os,io,re,tempfile,zipfile,json,threading,time
from pathlib import Path
from flask import Flask,request,jsonify,send_file,render_template
import boto3,geopandas as gpd
from botocore.config import Config
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
app=Flask(__name__)
BUCKET=os.getenv('R2_BUCKET','npaaad-safdz-data')
account=os.getenv('R2_ACCOUNT_ID')
key=os.getenv('R2_ACCESS_KEY_ID')
secret=os.getenv('R2_SECRET_ACCESS_KEY')
s3=boto3.client('s3',endpoint_url=f'https://{account}.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=key,aws_secret_access_key=secret,config=Config(signature_version='s3v4')) if all((account,key,secret)) else None
CACHE_LOCK=threading.Lock()
PAT=re.compile(r'^nationwide/(luzon|visayas|mindanao)/([a-z0-9-]{2,60})/(npaaad|safdz)[.]gpkg$')
@app.get('/health')
def health():return jsonify(ok=True,storageConfigured=bool(s3))
@app.get('/api/inventory')
def inventory():
 if not s3:return jsonify(error='R2 not configured'),503
 try:
  rows=[]
  for page in s3.get_paginator('list_objects_v2').paginate(Bucket=BUCKET,Prefix='nationwide/'):
   for obj in page.get('Contents',[]):
    m=PAT.fullmatch(obj['Key'])
    if m and obj.get('Size',0)>0:rows.append(dict(island=m[1],province=m[2],layer=m[3],size=obj['Size']))
  return jsonify(items=sorted(rows,key=lambda x:(x['island'],x['province'],x['layer'])))
 except Exception:return jsonify(error='R2 inventory unavailable'),503
def load(preview=False):
 island=request.args.get('island','');province=request.args.get('province','');layer=request.args.get('layer','')
 path=f'nationwide/{island}/{province}/{layer}.gpkg'
 if not PAT.fullmatch(path):raise ValueError('Invalid dataset')
 if not s3:raise RuntimeError('R2 not configured')
 with CACHE_LOCK:
  file=Path(tempfile.gettempdir())/('npaaad_cache_'+island+'_'+province+'_'+layer+'.gpkg')
  if not file.exists() or file.stat().st_size==0:
   staging=file.with_suffix('.part')
   try:
    s3.download_file(BUCKET,path,str(staging))
    staging.replace(file)
   finally:
    if staging.exists():staging.unlink()
 gdf=gpd.read_file(file, skip_features=max(0,int(request.args.get('offset','0'))), max_features=200) if preview else gpd.read_file(file)
 if gdf.crs is None or gdf.empty:raise ValueError('Empty data or missing CRS')
 gdf=gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
 return gdf,province,layer
@app.get('/api/preview')
def preview():
 try:
  gdf,_,_=load(preview=True)
  gdf=gdf.to_crs(4326)
  gdf['geometry']=gdf.geometry.simplify(0.001, preserve_topology=True)
  gdf=gdf[['geometry']]
  return jsonify(features=json.loads(gdf.to_json())['features'], count=len(gdf), nextOffset=int(request.args.get('offset','0'))+len(gdf))
 except ValueError as e:return jsonify(error=str(e)),400
 except Exception as e:
  app.logger.exception('Preview failure')
  return jsonify(error='Preview unavailable: '+type(e).__name__),503
@app.get('/api/download')
def download():
 fmt=request.args.get('format','')
 if fmt not in ('gpkg','zip','pdf','jpeg'):return jsonify(error='Invalid format'),400
 try:
  if fmt=='gpkg':
   island=request.args.get('island','');province=request.args.get('province','');layer=request.args.get('layer','')
   key=f'nationwide/{island}/{province}/{layer}.gpkg'
   if not PAT.fullmatch(key):return jsonify(error='Invalid dataset'),400
   if not s3:return jsonify(error='Storage unavailable'),503
   from botocore.exceptions import ClientError
   from flask import Response,stream_with_context
   obj=s3.get_object(Bucket=BUCKET,Key=key)
   def chunks():
    try:
     for chunk in obj['Body'].iter_chunks(chunk_size=1024*1024):
      if chunk:yield chunk
    finally:obj['Body'].close()
   return Response(stream_with_context(chunks()),headers={'Content-Disposition':f'attachment; filename="{province}_{layer}.gpkg"','Content-Length':str(obj['ContentLength'])},mimetype='application/geopackage+sqlite3')
  island=request.args.get('island','');province=request.args.get('province','');layer=request.args.get('layer','')
  key=f'nationwide/{island}/{province}/{layer}.gpkg'
  if not PAT.fullmatch(key):return jsonify(error='Invalid dataset'),400
  if not s3:return jsonify(error='Storage unavailable'),503
  name=f'{province}_{layer}'
  if fmt in ('jpeg','pdf'):
   from PIL import Image,ImageDraw
   import pyogrio
   with CACHE_LOCK:
    file=Path(tempfile.gettempdir())/('npaaad_cache_'+island+'_'+province+'_'+layer+'.gpkg')
    if not file.exists() or file.stat().st_size==0:
     staging=file.with_suffix('.part')
     try:
      s3.download_file(BUCKET,key,str(staging));staging.replace(file)
     finally:
      if staging.exists():staging.unlink()
   info=pyogrio.read_info(str(file))
   bounds=info.get('total_bounds')
   if bounds is None:raise ValueError('Dataset has no extent')
   xmin,ymin,xmax,ymax=map(float,bounds)
   if xmax<=xmin or ymax<=ymin:raise ValueError('Invalid dataset extent')
   width,height=1700,1700
   margin=65
   scale=min((width-2*margin)/(xmax-xmin),(height-2*margin)/(ymax-ymin))
   image=Image.new('RGB',(width,height),'white')
   draw=ImageDraw.Draw(image)
   def coords(ring):
    return [((x-xmin)*scale+margin,height-margin-(y-ymin)*scale) for x,y in ring]
   def paint(geom):
    if geom is None or geom.is_empty:return
    if geom.geom_type=='Polygon':
     draw.polygon(coords(geom.exterior.coords),fill='#76b69b')
     for interior in geom.interiors:draw.polygon(coords(interior.coords),fill='white')
    elif geom.geom_type in ('MultiPolygon','GeometryCollection'):
     for part in geom.geoms:paint(part)
   offset=0
   while True:
    frame=pyogrio.read_dataframe(str(file),skip_features=offset,max_features=200,columns=[])
    if frame.empty:break
    for geom in frame.geometry:paint(geom)
    offset+=len(frame)
    if len(frame)<200:break
   out=io.BytesIO()
   if fmt=='jpeg':image.save(out,format='JPEG',quality=90,optimize=True)
   else:image.save(out,format='PDF',resolution=150)
   out.seek(0)
   return send_file(out,mimetype='image/jpeg' if fmt=='jpeg' else 'application/pdf',as_attachment=True,download_name=name+('.jpg' if fmt=='jpeg' else '.pdf'))
  gdf,province,layer=load()
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp)/f'{name}.shp';gdf.to_file(base,driver='ESRI Shapefile',encoding='UTF-8')
   out=io.BytesIO()
   with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
    for f in Path(tmp).glob(name+'.*'):z.write(f,f.name)
   out.seek(0)
  return send_file(out,mimetype='application/zip',as_attachment=True,download_name=name+'.zip')
 except ValueError as e:return jsonify(error=str(e)),400
 except Exception:return jsonify(error='Export failed'),503
@app.get('/')
def home():return render_template('index.html')
