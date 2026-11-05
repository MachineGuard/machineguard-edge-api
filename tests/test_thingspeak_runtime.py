import time
from types import SimpleNamespace

from conftest import post_reading


def wait_for(predicate):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('Background secondary worker did not progress')


def test_secondary_worker_automatically_plans_delivers_and_stops_during_io(monkeypatch,make_client,clock,cloud):
    for name,value in {'THINGSPEAK_ENABLED':'true','THINGSPEAK_DEVICE_CODE':'selected',
        'THINGSPEAK_CHANNEL_ID':'123','THINGSPEAK_WRITE_KEY':'synthetic-secondary-only',
        'THINGSPEAK_POLL_SECONDS':'.1'}.items():monkeypatch.setenv(name,value)
    client=make_client();service=client.client.application.config['services'];calls=[]
    def publish(key,payload,cancelled):
        calls.append(dict(payload))
        while not cancelled():time.sleep(.02)
        return SimpleNamespace(state='UNCERTAIN',code='CANCELLED',entry_id=None,retry_after=0,http_status=None)
    service.thingspeak.client=SimpleNamespace(publish=publish)
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'selected','samplingIntervalSeconds':10})
    try:
        post_reading(client,device='selected')
        wait_for(lambda:bool(client.get('/api/v1/edge/thingspeak/jobs').json['items']))
        clock.advance(seconds=20)
        wait_for(lambda:bool(calls))
        assert post_reading(client,device='selected').status_code==201 and len(cloud.published)==2
        start=time.monotonic();service.runtime.stop()
        assert time.monotonic()-start<2 and not service.runtime.secondary_thread.is_alive()
        assert client.get('/api/v1/edge/thingspeak/jobs').json['items'][0]['state']=='UNCERTAIN'
    finally:service.runtime.stop()
