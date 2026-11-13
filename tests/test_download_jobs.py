import threading
import time
from flask import Flask,jsonify
from download_jobs import register

PARAMS=dict(island='luzon',region='Cordillera Administrative Region',province='benguet',scope='municipality',place='bokod',layer='npaaad',format='jpeg')


def wait_for(client,job):
    for _ in range(100):
        data=client.get('/api/download-status/'+job).json
        if data['status'] in ('ready','failed'):return data
        time.sleep(.01)
    raise AssertionError('Preparation did not finish')


def test_preparation_returns_without_waiting_and_deduplicates_active_work():
    app=Flask(__name__);entered=threading.Event();release=threading.Event()
    def download():
        entered.set();release.wait(3);return jsonify(ok=True)
    register(app,download);client=app.test_client()
    try:
        started=time.monotonic();response=client.post('/api/prepare-download',json=PARAMS)
        assert response.status_code==202
        assert time.monotonic()-started<1
        assert entered.wait(1)
        duplicate=client.post('/api/prepare-download',json=PARAMS)
        assert duplicate.json['job_id']==response.json['job_id']
        assert client.get('/api/download-status/'+response.json['job_id']).json['status']=='processing'
    finally:release.set()
    assert wait_for(client,response.json['job_id'])['status']=='ready'


def test_preparation_reports_export_failure():
    app=Flask(__name__)
    register(app,lambda:(jsonify(error='Boundary not found'),400))
    client=app.test_client();response=client.post('/api/prepare-download',json=PARAMS)
    state=wait_for(client,response.json['job_id'])
    assert state['status']=='failed' and state['error']=='Boundary not found'


def test_original_province_geopackage_needs_no_preparation():
    app=Flask(__name__)
    def unexpected():raise AssertionError('Original GeoPackage must stream directly')
    register(app,unexpected)
    response=app.test_client().post('/api/prepare-download',json={**PARAMS,'scope':'province','format':'gpkg'})
    assert response.json['status']=='ready' and response.json['progress']==100


def test_lost_job_returns_clear_restart_error():
    app=Flask(__name__);register(app,lambda:None)
    response=app.test_client().get('/api/download-status/missing')
    assert response.status_code==404 and 'server restart' in response.json['error']


def test_stage_progress_is_visible_before_completion():
    from threading import Event
    from job_progress import report
    app=Flask(__name__);entered=Event();finish=Event()
    def download():
        report(35,'Clipping selected polygons');entered.set();finish.wait(5)
        return 'ready'
    register(app,download);client=app.test_client()
    response=client.post('/api/prepare-download',json=PARAMS)
    assert entered.wait(3)
    state=client.get('/api/download-status/'+response.json['job_id']).json
    assert state['status']=='processing' and state['progress']==35
    assert state['stage']=='Clipping selected polygons' and state['progress_kind']=='stage-estimate'
    finish.set()
