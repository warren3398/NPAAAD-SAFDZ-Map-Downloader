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
  datasets={}
  for page in s3.get_paginator('list_objects_v2').paginate(Bucket=BUCKET,Prefix='nationwide/'):
   for obj in page.get('Contents',[]):
    key=obj['Key'];m=PAT.fullmatch(key)
    if m and obj.get('Size',0)>0:
     identity=(m[1],m[2],m[3]);datasets[identity]=dict(island=m[1],province=m[2],layer=m[3],size=obj['Size'],formats=['gpkg'])
  for page in s3.get_paginator('list_objects_v2').paginate(Bucket=BUCKET,Prefix='exports/'):
   for obj in page.get('Contents',[]):
    m=re.fullmatch(r'exports/(luzon|visayas|mindanao)/([a-z0-9-]{2,60})/(npaaad|safdz)[.](jpg|pdf|zip)',obj['Key'])
    if m and obj.get('Size',0)>0 and (m[1],m[2],m[3]) in datasets:
     datasets[(m[1],m[2],m[3])]['formats'].append({'jpg':'jpeg','pdf':'pdf','zip':'zip'}[m[4]])
  scoped=[]
  pattern=re.compile(r'exports/(luzon|visayas|mindanao)/([a-z0-9-]{2,60})/(municipality|barangay)/([a-z0-9-]{2,90})/(npaaad|safdz)[.](gpkg|jpg|pdf|zip)$')
  for page in s3.get_paginator('list_objects_v2').paginate(Bucket=BUCKET,Prefix='exports/'):
   for obj in page.get('Contents',[]):
    m=pattern.fullmatch(obj['Key'])
    if m and obj.get('Size',0)>0:
     scoped.append(dict(island=m[1],province=m[2],scope=m[3],place=m[4],layer=m[5],format={'jpg':'jpeg'}.get(m[6],m[6])))
  return jsonify(items=sorted(datasets.values(),key=lambda x:(x['island'],x['province'],x['layer'])),scoped=scoped)
 except Exception:
  app.logger.exception('Inventory failed')
  return jsonify(error='R2 inventory unavailable'),503
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
 island=request.args.get('island','');province=request.args.get('province','');layer=request.args.get('layer','')
 source=f'nationwide/{island}/{province}/{layer}.gpkg'
 if not PAT.fullmatch(source):return jsonify(error='Invalid dataset'),400
 ext={'gpkg':'gpkg','zip':'zip','jpeg':'jpg','pdf':'pdf'}.get(fmt)
 if not ext:return jsonify(error='Invalid format'),400
 if not s3:return jsonify(error='Storage unavailable'),503
 scope=request.args.get('scope','province')
 place=request.args.get('place','')
 if scope not in ('province','municipality','barangay'):return jsonify(error='Invalid administrative level'),400
 if scope!='province':
  if not re.fullmatch(r'[a-z0-9-]{2,90}',place):return jsonify(error='Invalid location'),400
  object_key=f'exports/{island}/{province}/{scope}/{place}/{layer}.{ext}'
 else:
  object_key=source if fmt=='gpkg' else f'exports/{island}/{province}/{layer}.{ext}'
 from botocore.exceptions import ClientError
 from flask import Response,stream_with_context
 try:
  obj=s3.get_object(Bucket=BUCKET,Key=object_key)
 except ClientError as exc:
  code=exc.response.get('Error',{}).get('Code','')
  if code in ('NoSuchKey','404','NotFound'):
   return jsonify(error='This export is not yet pre-generated. Download the original GeoPackage instead.'),404
  app.logger.exception('R2 export fetch failed')
  return jsonify(error='Download temporarily unavailable'),503
 def chunks():
  try:
   for chunk in obj['Body'].iter_chunks(chunk_size=1024*1024):
    if chunk:yield chunk
  finally:obj['Body'].close()
 mime={'gpkg':'application/geopackage+sqlite3','zip':'application/zip','jpeg':'image/jpeg','pdf':'application/pdf'}[fmt]
 return Response(stream_with_context(chunks()),headers={'Content-Disposition':f'attachment; filename="{province}_{layer}.{ext}"','Content-Length':str(obj['ContentLength'])},mimetype=mime)

# Administrative names are read from the existing Slope Map R2 GeoPackages.
# Keep boundary credentials separate from the export bucket credentials.
from functools import lru_cache
import pyogrio
BOUNDARY_BUCKET=os.getenv('BOUNDARY_R2_BUCKET','slopemap-gis-data')
boundary_key=os.getenv('BOUNDARY_R2_ACCESS_KEY_ID')
boundary_secret=os.getenv('BOUNDARY_R2_SECRET_ACCESS_KEY')
boundary_account=os.getenv('BOUNDARY_R2_ACCOUNT_ID') or account
boundary_s3=boto3.client('s3',endpoint_url=f'https://{boundary_account}.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=boundary_key,aws_secret_access_key=boundary_secret,config=Config(signature_version='s3v4')) if all((boundary_account,boundary_key,boundary_secret)) else None
BOUNDARY_NAMES={'municipality':'municipalities.gpkg','barangay':'barangays.gpkg'}
def boundary_path(level):
 if not boundary_s3:raise RuntimeError('Boundary R2 credentials are not configured')
 path=Path(tempfile.gettempdir())/('npaaad_admin_'+BOUNDARY_NAMES[level])
 if path.exists() and path.stat().st_size>0:return path
 with CACHE_LOCK:
  if path.exists() and path.stat().st_size>0:return path
  tmp=path.with_suffix('.part')
  try:
   boundary_s3.download_file(BOUNDARY_BUCKET,'boundaries/'+BOUNDARY_NAMES[level],str(tmp))
   tmp.replace(path)
  finally:
   if tmp.exists():tmp.unlink()
 return path
def admin_col(cols,options):
 lower={x.lower():x for x in cols}
 return next((lower[n.lower()] for n in options if n.lower() in lower),None)
@lru_cache(maxsize=2)
def boundary_table(level):
 path=boundary_path(level)
 info=pyogrio.read_info(str(path))
 columns=list(info['fields'])
 # Read only attributes: avoid loading thousands of heavy polygon geometries.
 frame=pyogrio.read_dataframe(str(path),columns=columns,read_geometry=False)
 return frame,columns
@app.get('/api/boundary-info')
def boundary_info():
 if not boundary_s3:return jsonify(configured=False),503
 try:
  return jsonify(configured=True,layers={level:{'columns':boundary_table(level)[1],'rows':len(boundary_table(level)[0])} for level in BOUNDARY_NAMES})
 except Exception:
  app.logger.exception('Boundary info failed')
  return jsonify(error='Unable to read boundary files'),503
@app.get('/api/locations')
def locations():
 province=request.args.get('province','').strip()
 municipality=request.args.get('municipality','').strip()
 if not re.fullmatch(r'[a-z0-9-]{2,90}',province):return jsonify(error='Select a province'),400
 level='barangay' if municipality else 'municipality'
 try:
  frame,columns=boundary_table(level)
  name_col=admin_col(columns,['ADM4_EN','ADM3_EN' if municipality else 'ADM2_EN','barangay','barangay_name','brgy_name','BRGY_NAME','name','NAME_4' if municipality else 'NAME_2','MUN_NAME','MUNICIPALITY','CITY_MUN','municipality_name','NAME'])
  prov_col=admin_col(columns,['ADM2_EN' if municipality else 'ADM1_EN','province','province_name','PROV_NAME','NAME_1','adm2_name','PROVINCE','prov'])
  mun_col=admin_col(columns,['ADM3_EN','municipality','municipality_name','city_mun','MUN_NAME','CITY_NAME','NAME_2','adm3_name','municity'])
  if not name_col:return jsonify(error='Boundary file has no recognized location name column'),503
  def norm(value):return re.sub(r'[^a-z0-9]','',str(value).lower())
  if prov_col:
   target=norm(province.replace('-',' '))
   frame=frame[frame[prov_col].map(norm)==target]
  else:
   return jsonify(error='Province field not detected in boundary GeoPackage'),503
  if municipality:
   if not mun_col:return jsonify(error='Municipality field not detected in barangay GeoPackage'),503
   frame=frame[frame[mun_col].map(norm)==norm(municipality)]
  names=sorted(set(str(x).strip() for x in frame[name_col].dropna() if str(x).strip()),key=str.casefold)
  return jsonify(items=[{'code':name,'name':name} for name in names])
 except Exception:
  app.logger.exception('Boundary location lookup failed')
  return jsonify(error='Boundary lookup unavailable; check R2 permissions and boundary columns'),503

@app.get('/')
def home():return render_template('index.html')
