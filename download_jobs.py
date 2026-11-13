"""Prepare one-click downloads without keeping an HTTP request open during GIS work."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import uuid

from flask import jsonify,request
from memory_utils import release_memory


def register(app,download):
    executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='gis-download')
    jobs={}
    lock=threading.Lock()
    allowed={'island','region','province','scope','place','layer','format','boundary_id'}

    def prepare(job_id,params):
        with lock:
            jobs[job_id].update(status='processing')
        try:
            release_memory()
            with app.test_request_context('/api/download',query_string=params):
                response=app.make_response(download())
                try:
                    if response.status_code==200:
                        result={'status':'ready'}
                    else:
                        result={'status':'failed','error':(response.get_json(silent=True) or {}).get('error','Could not prepare this download. Please retry.')}
                finally:
                    response.close()
        except Exception:
            app.logger.exception('Background download preparation failed')
            result={'status':'failed','error':'Download preparation was interrupted. Please retry.'}
        release_memory()
        with lock:
            jobs[job_id].update(result,updated=time.monotonic())

    @app.post('/api/prepare-download')
    def start():
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or any(not isinstance(v,str) for v in data.values()):
            return jsonify(error='Invalid download selection.'),400
        params={key:data.get(key,'') for key in allowed}
        if params['format'] not in ('gpkg','zip','jpeg') or params['layer'] not in ('npaaad','safdz'):
            return jsonify(error='Invalid download format or layer.'),400
        # This file is already stored in the requested format and streams directly.
        if not params['boundary_id'] and params['scope']=='province' and params['format']=='gpkg':
            return jsonify(status='ready')
        now=time.monotonic()
        with lock:
            expired=[key for key,value in jobs.items() if value['status'] not in ('queued','processing') and now-value['updated']>3600]
            for key in expired:
                del jobs[key]
            # Repeated clicks share the same active preparation.
            existing=next((key for key,value in jobs.items() if value['params']==params and value['status'] in ('queued','processing')),None)
            if existing:
                return jsonify(job_id=existing,status=jobs[existing]['status']),202
            if sum(value['status'] in ('queued','processing') for value in jobs.values())>=4:
                return jsonify(error='Downloads are busy. Please retry shortly.'),429,{'Retry-After':'5'}
            job_id=uuid.uuid4().hex
            jobs[job_id]={'status':'queued','params':params,'updated':now}
        executor.submit(prepare,job_id,params)
        return jsonify(job_id=job_id,status='processing'),202

    @app.get('/api/download-status/<job_id>')
    def status(job_id):
        with lock:
            job=jobs.get(job_id)
            if not job:
                return jsonify(error='Download preparation was interrupted by a server restart. Please retry.'),404
            return jsonify(status=job['status'],error=job.get('error'))
