import os,io,re,tempfile,zipfile,json
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
 with tempfile.TemporaryDirectory() as tmp:
  file=Path(tmp)/'data.gpkg';s3.download_file(BUCKET,path,str(file));gdf=gpd.read_file(file, rows=800) if preview else gpd.read_file(file)
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
  return jsonify(json.loads(gdf.to_json()))
 except ValueError as e:return jsonify(error=str(e)),400
 except Exception:return jsonify(error='Preview unavailable'),503
@app.get('/api/download')
def download():
 fmt=request.args.get('format','')
 if fmt not in ('zip','pdf','jpeg'):return jsonify(error='Invalid format'),400
 try:
  gdf,province,layer=load();name=f'{province}_{layer}'
  if fmt=='zip':
   with tempfile.TemporaryDirectory() as tmp:
    base=Path(tmp)/f'{name}.shp';gdf.to_file(base,driver='ESRI Shapefile',encoding='UTF-8')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
     for f in Path(tmp).glob(name+'.*'):z.write(f,f.name)
    out.seek(0)
   return send_file(out,mimetype='application/zip',as_attachment=True,download_name=name+'.zip')
  projected=gdf.to_crs(3857)
  fig,ax=plt.subplots(figsize=(11.7,8.3))
  projected.plot(ax=ax,color='#77b49a',edgecolor='#333333',linewidth=.15)
  try:
   import contextily as ctx
   ctx.add_basemap(ax,source=ctx.providers.Esri.WorldImagery,attribution=False)
  except Exception:pass
  ax.set_axis_off();ax.set_title(f'{layer.upper()} — {province.replace("-"," ").title()}')
  out=io.BytesIO();fig.savefig(out,format='jpeg' if fmt=='jpeg' else 'pdf',dpi=180,bbox_inches='tight');plt.close(fig);out.seek(0)
  return send_file(out,mimetype='image/jpeg' if fmt=='jpeg' else 'application/pdf',as_attachment=True,download_name=name+('.jpg' if fmt=='jpeg' else '.pdf'))
 except ValueError as e:return jsonify(error=str(e)),400
 except Exception:return jsonify(error='Export failed'),503
@app.get('/')
def home():return render_template('index.html')
