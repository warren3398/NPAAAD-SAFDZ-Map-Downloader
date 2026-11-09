"""Nationwide resumable coordinator; R2 holds the checkpoint across GitHub runners."""
import json,os,re,subprocess,sys,time
from datetime import datetime,timezone
import boto3
from botocore.config import Config
s3=boto3.client('s3',endpoint_url='https://'+os.environ['R2_ACCOUNT_ID']+'.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],config=Config(signature_version='s3v4'))
bucket=os.getenv('R2_BUCKET') or 'npaaad-safdz-data'
checkpoint='automation/nationwide-scoped-progress-v1.json'
pattern=re.compile(r'^nationwide/(luzon|visayas|mindanao)/([a-z0-9-]+)/(npaaad|safdz)\.gpkg$')
datasets=set()
for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix='nationwide/'):
 for item in page.get('Contents',[]):
  m=pattern.match(item['Key'])
  if m and item.get('Size',0)>0:datasets.add((m[1],m[2],m[3]))
datasets=sorted(datasets)
if not datasets:raise SystemExit('No provincial NPAAAD/SAFDZ sources found; stopping safely')
try:
 state=json.loads(s3.get_object(Bucket=bucket,Key=checkpoint)['Body'].read())
except s3.exceptions.ClientError as exc:
 if exc.response['Error']['Code'] not in ('NoSuchKey','404','NotFound'):raise
 state={'dataset':0,'stage':'province','offset':0,'failures':{},'completed_datasets':[]}
state.setdefault('failures',{})
state.setdefault('completed_datasets',[])
def save():
 state['updated_utc']=datetime.now(timezone.utc).isoformat()
 state['total_source_datasets']=len(datasets)
 s3.put_object(Bucket=bucket,Key=checkpoint,Body=json.dumps(state,indent=2).encode(),ContentType='application/json')
limit=int(os.getenv('BATCH_LIMIT','30'))
max_batches=int(os.getenv('MAX_BATCHES','3'))
if not 1<=limit<=100 or not 1<=max_batches<=12:raise ValueError('Invalid batch settings')
print('NATIONWIDE SOURCES',len(datasets),'checkpoint',state,flush=True)
for batch in range(max_batches):
 idx=state['dataset']
 if idx>=len(datasets):
  print('NATIONWIDE COMPLETE',len(datasets),'source datasets',flush=True);save();break
 island,province,layer=datasets[idx]
 stage=state['stage']
 key='/'.join([island,province,layer,stage,str(state['offset'])])
 if stage=='province':
  cmd=[sys.executable,'generate_exports.py','--island',island,'--province',province,'--layer',layer]
 else:
  cmd=[sys.executable,'generate_scoped_exports.py','--island',island,'--province',province,'--layer',layer,'--scope','both','--offset',str(state['offset']),'--limit',str(limit)]
 print('START',batch+1,key,flush=True)
 try:
  proc=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=3600)
  print(proc.stdout[-18000:],flush=True)
  if proc.returncode:raise RuntimeError('Export exited with code '+str(proc.returncode))
  if stage=='province':
   state['stage']='scoped';state['offset']=0
  else:
   match=re.search(r'SUMMARY completed=\d+ existing=\d+ empty=\d+ next_offset=(\d+) total=(\d+)',proc.stdout)
   if not match:raise RuntimeError('No verified SUMMARY; refusing to advance checkpoint')
   next_offset,total=map(int,match.groups())
   if next_offset>=total:
    state['completed_datasets'].append('/'.join([island,province,layer]))
    state['dataset']+=1;state['stage']='province';state['offset']=0
   else:state['offset']=next_offset
  state['failures'].pop(key,None)
 except Exception as exc:
  n=state['failures'].get(key,0)+1
  state['failures'][key]=n
  print('BATCH FAILED',key,'attempt',n,repr(exc),flush=True)
  # Do not silently skip a failed location. Retry on the next scheduled run.
  save()
  raise SystemExit(1)
 save()
print('CHECKPOINT',json.dumps(state),flush=True)
