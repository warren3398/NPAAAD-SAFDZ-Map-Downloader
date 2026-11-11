"""Resumable offline exports. One province/layer per GitHub Actions run."""
import argparse,os,re,tempfile,zipfile
import pyogrio
from pathlib import Path
import boto3,geopandas as gpd
from botocore.config import Config
from map_layout import render_map
def slug(v):
 return re.sub(r'[^a-z0-9]+','-',str(v).lower()).strip('-')
p=argparse.ArgumentParser()
p.add_argument('--island',required=True,choices=['luzon','visayas','mindanao'])
p.add_argument('--province',required=True)
p.add_argument('--layer',required=True,choices=['npaaad','safdz'])
p.add_argument('--offset',type=int,default=0)
p.add_argument('--limit',type=int,default=20)
p.add_argument('--scope',choices=['province','municipality','barangay','both'],default='both')
p.add_argument('--place-filter',default='',help='Process only this exact scoped place slug')
a=p.parse_args()
if a.offset<0 or not 1<=a.limit<=100:p.error('Invalid offset or limit')
account=os.environ['R2_ACCOUNT_ID']
def client(key,secret):
 return boto3.client('s3',endpoint_url=f'https://{account}.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=key,aws_secret_access_key=secret,config=Config(signature_version='s3v4'))
s3=client(os.environ['R2_ACCESS_KEY_ID'],os.environ['R2_SECRET_ACCESS_KEY'])
bs3=client(os.environ['BOUNDARY_R2_ACCESS_KEY_ID'],os.environ['BOUNDARY_R2_SECRET_ACCESS_KEY'])
bucket=os.getenv('R2_BUCKET','npaaad-safdz-data')
bbucket=os.getenv('BOUNDARY_R2_BUCKET','slopemap-gis-data')
province=a.province
def exists(key):
 try:s3.head_object(Bucket=bucket,Key=key);return True
 except s3.exceptions.ClientError as e:
  if e.response['Error']['Code'] in ('404','NoSuchKey','NotFound'):return False
  raise
with tempfile.TemporaryDirectory() as temp:
 root=Path(temp)
 source=root/'source.gpkg'
 s3.download_file(bucket,f'nationwide/{a.island}/{province}/{a.layer}.gpkg',str(source))
 # Boundary geometry and source are processed offline, never on Render.
 source_frame=gpd.read_file(source)
 if source_frame.crs is None:raise ValueError('Source CRS missing')
 source_frame=source_frame[source_frame.geometry.notna() & ~source_frame.geometry.is_empty].copy()
 levels=['municipality','barangay'] if a.scope=='both' else ([] if a.scope=='province' else [a.scope])
 if a.scope=='province':
  from shapely.ops import unary_union
  tasks=[('province','',province,'',unary_union(source_frame.geometry))]
 if a.scope!='province':tasks=[]
 for level in levels:
  name='municipalities.gpkg' if level=='municipality' else 'barangays.gpkg'
  path=root/name
  bs3.download_file(bbucket,'boundaries/'+name,str(path))
  boundaries=gpd.read_file(path)
  if not {'NAME_1','NAME_2'}.issubset(boundaries.columns):raise ValueError('Boundary schema missing NAME_1/NAME_2')
  boundaries=boundaries[boundaries['NAME_1'].astype(str).map(slug)==province].copy()
  if level=='barangay' and 'NAME_3' not in boundaries:raise ValueError('Boundary missing NAME_3')
  for _,row in boundaries.iterrows():
   mun=str(row['NAME_2']).strip()
   brgy=str(row['NAME_3']).strip() if level=='barangay' else ''
   place=slug(mun)+'--'+slug(brgy) if brgy else slug(mun)
   if not place or row.geometry is None or row.geometry.is_empty:continue
   tasks.append((level,place,mun,brgy,gpd.GeoSeries([row.geometry],crs=boundaries.crs).to_crs(source_frame.crs).iloc[0]))
 tasks.sort(key=lambda t:(t[0],t[1]))
 # Deduplicate names that appear in multipart administrative layers.
 unique={}
 for task in tasks:
  key=(task[0],task[1])
  if key in unique:
   old=unique[key]
   from shapely.ops import unary_union
   unique[key]=(*old[:4],unary_union([old[4],task[4]]))
  else:unique[key]=task
 tasks=list(unique.values())
 if a.place_filter:
  tasks=[task for task in tasks if task[1]==a.place_filter]
  if not tasks:raise SystemExit('Requested administrative location not found: '+a.place_filter)
 print(f'PLAN {a.island}/{province}/{a.layer}: {len(tasks)} administrative locations; offset={a.offset} limit={a.limit}',flush=True)
 completed=0;skipped=0;empty=0
 for level,place,mun,brgy,geom in tasks[a.offset:a.offset+a.limit]:
  prefix=(f'exports/{a.island}/{province}/{a.layer}' if level=='province' else f'exports/{a.island}/{province}/{level}/{place}/{a.layer}')
  keys={ext:f'{prefix}.{ext}' for ext in ('jpg','pdf','zip')}
  missing={ext for ext,key in keys.items() if not exists(key)}
  if not missing:
   skipped+=1;print('SKIP existing',prefix,flush=True);continue
  try:
   subset=(source_frame.copy() if level=='province' else source_frame.loc[source_frame.geometry.intersects(geom)].copy())
   if subset.empty:empty+=1;print('EMPTY',prefix,flush=True);continue
   if level!='province':subset['geometry']=subset.geometry.intersection(geom)
   subset=subset[subset.geometry.notna() & ~subset.geometry.is_empty]
   subset=subset[subset.geometry.geom_type.isin(['Polygon','MultiPolygon'])]
   if subset.empty:empty+=1;continue
   # Prepare only missing exports; never rerender a map to rebuild ZIP.
   if 'jpg' in missing or 'pdf' in missing:
    gpkg=root/'clipped.gpkg'
    if gpkg.exists():gpkg.unlink()
    pyogrio.write_dataframe(subset,gpkg,driver='GPKG')
    jpg=root/'map.jpg';pdf=root/'map.pdf'
    render_map(gpkg,a.layer,f'{brgy}, {mun}, {province}' if brgy else f'{mun}, {province}',jpg,pdf)
   if 'zip' in missing:
    shape=root/'shape'
    shape.mkdir(exist_ok=True)
    for file in shape.iterdir():file.unlink()
    pyogrio.write_dataframe(subset.reset_index(drop=True),shape/'dataset.shp',driver='ESRI Shapefile',encoding='UTF-8')
    archive=root/'dataset.zip'
    # Shapefile compression is typically CPU-bound; fast DEFLATE is much quicker.
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
     for file in shape.iterdir():z.write(file,file.name)
   paths={'jpg':root/'map.jpg','pdf':root/'map.pdf','zip':root/'dataset.zip'}
   for ext in ('jpg','pdf','zip'):
    if ext in missing:
     s3.upload_file(str(paths[ext]),bucket,keys[ext],ExtraArgs={'ContentType':{'jpg':'image/jpeg','pdf':'application/pdf','zip':'application/zip'}[ext]})
   completed+=1;print('UPLOADED',prefix,'features',len(subset),flush=True)
  except Exception as exc:
   print('FAILED',prefix,repr(exc),flush=True)
   raise
 print(f'SUMMARY completed={completed} existing={skipped} empty={empty} next_offset={a.offset+a.limit} total={len(tasks)}',flush=True)
