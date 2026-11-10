import os,io,re,tempfile,zipfile,json,threading,time,uuid,datetime
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
  # These field names were verified against the actual R2 GeoPackage schemas.
  name_col=admin_col(columns,['NAME_3','VARNAME_3'] if municipality else ['NAME_2','VARNAME_2'])
  prov_col=admin_col(columns,['NAME_1'])
  mun_col=admin_col(columns,['NAME_2']) if municipality else None
  if not name_col or not prov_col:return jsonify(error='Required NAME fields missing from boundary GeoPackage'),503
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

@app.get('/api/nationwide-progress')
def nationwide_progress():
 if not s3:return jsonify(error='R2 not configured'),503
 try:
  from botocore.exceptions import ClientError
  try:
   obj=s3.get_object(Bucket=BUCKET,Key='automation/nationwide-scoped-progress-v1.json')
  except ClientError as exc:
   if exc.response.get('Error',{}).get('Code') in ('NoSuchKey','404','NotFound'):
    return jsonify(status='not_started',message='Nationwide checkpoint not yet created')
   raise
  state=json.loads(obj['Body'].read())
  total=int(state.get('total_source_datasets',0))
  done=len(state.get('completed_datasets',[]))
  index=int(state.get('dataset',0))
  failed=state.get('failures',{})
  return jsonify(status='complete' if total and index>=total else 'attention' if failed else 'processing',total=total,completed=done,current_dataset_index=index+1 if index<total else total,stage=state.get('stage'),offset=state.get('offset',0),failures=failed,updated_utc=state.get('updated_utc'),completed_datasets=state.get('completed_datasets',[]))
 except Exception:
  app.logger.exception('Nationwide progress unavailable')
  return jsonify(error='Unable to load nationwide checkpoint'),503

@app.post('/api/request-export')
def request_export():
 if not s3:return jsonify(error='Storage unavailable'),503
 data=request.get_json(silent=True) or {}
 island=str(data.get('island',''));province=str(data.get('province',''));layer=str(data.get('layer',''));scope=str(data.get('scope',''));place=str(data.get('place',''))
 if island not in ('luzon','visayas','mindanao') or not re.fullmatch(r'[a-z0-9-]{2,60}',province) or layer not in ('npaaad','safdz') or scope not in ('province','municipality','barangay') or (scope!='province' and not re.fullmatch(r'[a-z0-9-]{2,90}',place)):
  return jsonify(error='Invalid export request'),400
 prefix=(f'exports/{island}/{province}/{layer}' if scope=='province' else f'exports/{island}/{province}/{scope}/{place}/{layer}')
 keys=[prefix+'.jpg',prefix+'.pdf',prefix+'.zip']
 try:
  found=[]
  for key_name in keys:
   try:s3.head_object(Bucket=BUCKET,Key=key_name);found.append(True)
   except Exception as exc:
    code=getattr(exc,'response',{}).get('Error',{}).get('Code','')
    if code in ('404','NoSuchKey','NotFound'):found.append(False)
    else:raise
  if all(found):return jsonify(status='ready',message='Exports already exist',formats=['jpeg','pdf','zip'])
  if any(found):return jsonify(error='Partial export exists; administrator review required'),409
  # Stable request key prevents repeated clicks from creating a storm of jobs.
  import hashlib
  identity='|'.join([island,province,scope,place,layer])
  request_id=hashlib.sha256(identity.encode()).hexdigest()[:32]
  key_name='requests/'+request_id+'.json'
  try:
   existing=s3.get_object(Bucket=BUCKET,Key=key_name)
   state=json.loads(existing['Body'].read())
   if state.get('status')=='processing':
    return jsonify(request_id=request_id,status='processing',message='GIS worker is processing this request'),202
   if state.get('status')=='ready':
    return jsonify(request_id=request_id,status='ready',message='Generation completed; refreshing exports')
   # Existing queued requests also need an immediate dispatch attempt.
   if state.get('status')!='queued':
    state={'request_id':request_id,'island':island,'province':province,'layer':layer,'scope':scope,'place':place,'status':'queued','created_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    s3.put_object(Bucket=BUCKET,Key=key_name,Body=json.dumps(state).encode(),ContentType='application/json')
  except Exception as exc:
   code=getattr(exc,'response',{}).get('Error',{}).get('Code','')
   if code not in ('404','NoSuchKey','NotFound'):raise
   state={'request_id':request_id,'island':island,'province':province,'layer':layer,'scope':scope,'place':place,'status':'queued','created_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
   s3.put_object(Bucket=BUCKET,Key=key_name,Body=json.dumps(state).encode(),ContentType='application/json')
  # Dispatch for new AND previously queued requests; avoid repeated dispatches.
  token=os.getenv('GITHUB_ACTIONS_TOKEN','').strip()
  if not token:
   return jsonify(request_id=request_id,status='queued',dispatched=False,message='GitHub Actions token is not visible to the running Render service. Check the Environment key and redeploy.'),202
  last=state.get('last_dispatch_attempt_at','')
  if last:
   try:
    elapsed=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(last)).total_seconds()
    if elapsed<120:return jsonify(request_id=request_id,status='queued',dispatched=bool(state.get('dispatched')),message=state.get('dispatch_message','Dispatch attempted recently; waiting for worker.')),202
   except (ValueError,TypeError):pass
  dispatched=False
  dispatch_message=''
  try:
   import urllib.request,urllib.error
   payload=json.dumps({'ref':'main'}).encode()
   req=urllib.request.Request('https://api.github.com/repos/warren3398/NPAAAD-SAFDZ-Map-Downloader/actions/workflows/free-on-demand-exports.yml/dispatches',data=payload,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'npaaad-map-generator','Content-Type':'application/json'},method='POST')
   with urllib.request.urlopen(req,timeout=12) as response:dispatched=response.status==204
   dispatch_message='GitHub Actions workflow triggered. Waiting for GIS worker.' if dispatched else 'GitHub Actions did not accept the workflow dispatch.'
  except urllib.error.HTTPError as exc:
   try:
    gh_error=json.loads(exc.read(4096).decode('utf-8','replace'))
    gh_message=str(gh_error.get('message',''))[:240]
   except Exception:gh_message=''
   dispatch_message='GitHub Actions dispatch rejected (HTTP '+str(exc.code)+')'+(': '+gh_message if gh_message else '. Check GitHub repository Actions settings and token access.')
   app.logger.warning('GitHub dispatch rejected: HTTP %s; reason=%s',exc.code,gh_message)
  except Exception:
   dispatch_message='Could not reach GitHub Actions. Scheduled processing remains available.'
   app.logger.exception('Immediate GitHub workflow dispatch failed')
  state['last_dispatch_attempt_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
  state['dispatched']=dispatched
  state['dispatch_message']=dispatch_message
  s3.put_object(Bucket=BUCKET,Key=key_name,Body=json.dumps(state).encode(),ContentType='application/json')
  return jsonify(request_id=request_id,status='queued',dispatched=dispatched,message=dispatch_message),202
 except Exception:
  app.logger.exception('Could not queue export')
  return jsonify(error='Unable to queue export right now'),503

@app.get('/api/request-export/<request_id>')
def request_export_status(request_id):
 if not re.fullmatch(r'[a-f0-9]{32}',request_id):return jsonify(error='Invalid request ID'),400
 if not s3:return jsonify(error='Storage unavailable'),503
 try:
  obj=s3.get_object(Bucket=BUCKET,Key='requests/'+request_id+'.json')
  return jsonify(json.loads(obj['Body'].read()))
 except Exception as exc:
  code=getattr(exc,'response',{}).get('Error',{}).get('Code','')
  if code in ('404','NoSuchKey','NotFound'):return jsonify(error='Request not found'),404
  app.logger.exception('Request status lookup failed')
  return jsonify(error='Request status unavailable'),503
