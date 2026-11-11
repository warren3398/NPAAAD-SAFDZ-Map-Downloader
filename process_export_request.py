"""Free-tier R2 request queue worker. Drain queued requests within a bounded GitHub Actions run."""
import json,os,subprocess,sys,time
from datetime import datetime,timezone
import boto3
from botocore.config import Config
account=os.environ['R2_ACCOUNT_ID'];bucket=os.getenv('R2_BUCKET','npaaad-safdz-data')
s3=boto3.client('s3',endpoint_url='https://'+account+'.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],config=Config(signature_version='s3v4'))
def read(key):
 return json.loads(s3.get_object(Bucket=bucket,Key=key)['Body'].read())
def write(key,data):
 data['updated_at']=datetime.now(timezone.utc).isoformat()
 s3.put_object(Bucket=bucket,Key=key,Body=json.dumps(data,indent=2).encode(),ContentType='application/json')
queued=[]
for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix='requests/'):
 for obj in page.get('Contents',[]):
  if obj['Key'].endswith('.json'):
   try:
    item=read(obj['Key'])
    if item.get('status')=='queued':queued.append((item.get('created_at',''),obj['Key'],item))
   except Exception as e:print('QUEUE_READ_ERROR',obj['Key'],repr(e),flush=True)
queued.sort()
if not queued:
 print('QUEUE_EMPTY',flush=True);sys.exit(0)
deadline=time.monotonic()+3000
failures=0
for _,item_key,item in queued:
 if deadline-time.monotonic()<60:
  print('QUEUE_TIME_BUDGET_REACHED; remaining requests stay queued',flush=True)
  break
 # Re-read before processing so requests already completed are skipped.
 item=read(item_key)
 if item.get('status')!='queued':continue
 item['status']='processing';item['started_at']=datetime.now(timezone.utc).isoformat();write(item_key,item)
 print('PROCESSING',item,flush=True)
 env=os.environ.copy()
 env['BOUNDARY_R2_ACCESS_KEY_ID']=env.get('BOUNDARY_R2_ACCESS_KEY_ID') or env.get('R2_ACCESS_KEY_ID','')
 env['BOUNDARY_R2_SECRET_ACCESS_KEY']=env.get('BOUNDARY_R2_SECRET_ACCESS_KEY') or env.get('R2_SECRET_ACCESS_KEY','')
 cmd=[sys.executable,'generate_scoped_exports.py','--island',item['island'],'--province',item['province'],'--layer',item['layer'],'--scope',item['scope'],'--offset','0','--limit','100'] + (['--place-filter',item['place']] if item['scope']!='province' else [])
 try:
  result=subprocess.run(cmd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=max(1,int(deadline-time.monotonic())))
  print(result.stdout,flush=True)
  if result.returncode:raise RuntimeError('Generator exit code '+str(result.returncode))
  prefix=('exports/{}/{}/{}'.format(item['island'],item['province'],item['layer']) if item['scope']=='province' else 'exports/{}/{}/{}/{}/{}'.format(item['island'],item['province'],item['scope'],item['place'],item['layer']))
  for ext in ('jpg','pdf','zip'):
   try:s3.head_object(Bucket=bucket,Key=prefix+'.'+ext)
   except Exception as e:raise RuntimeError('Expected output missing: '+ext+'; '+str(e))
  item['status']='ready';item['completed_at']=datetime.now(timezone.utc).isoformat();item['formats']=['jpeg','pdf','zip']
  write(item_key,item)
  print('REQUEST_READY',item['request_id'],flush=True)
 except Exception as exc:
  item['status']='failed';item['error']=str(exc)[:1000];item['failed_at']=datetime.now(timezone.utc).isoformat();write(item_key,item)
  print('REQUEST_FAILED',item['request_id'],repr(exc),flush=True)
  failures+=1
sys.exit(1 if failures else 0)
