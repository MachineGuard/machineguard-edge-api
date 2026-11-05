"""Kill the owning OS process after the HTTP double commits and before its response."""
import json,os,subprocess,sys,time
from datetime import timedelta

from conftest import post_reading
from test_thingspeak import secondary,jobs,SecondaryDouble
from test_thingspeak_http import http_double
from edge_api.application.thingspeak import ThingSpeakDelivery
from edge_api.infrastructure.models import ThingSpeakChannelModel as Channel


WORKER='''
import json,sys
from datetime import datetime
from functools import partial
from edge_api import create_app
from edge_api.config import Config
from edge_api.infrastructure.thingspeak_client import ThingSpeakClient,http_exchange
def main():
    values=json.loads(sys.stdin.readline());clock=lambda:datetime.fromisoformat(values['now'])
    config=Config(database_path=values['database'],admin_key='synthetic-admin-key-at-least-32-bytes',
        enable_delivery_worker=False,enable_availability_loop=False,thingspeak_enabled=True,
        thingspeak_device_code='selected',thingspeak_channel_id='123',
        thingspeak_write_key='synthetic-secondary-only',thingspeak_poll_seconds=60)
    app=create_app(config,clock=clock);service=app.config['services'].thingspeak
    service.client=ThingSpeakClient(5,partial(http_exchange,url=values['url']))
    try:service.cycle()
    finally:app.config['services'].runtime.stop()
if __name__=='__main__':main()
'''


def test_kill_after_remote_commit_retains_uncertainty_and_reconciles(secondary,clock,http_double):
    client,s=secondary
    post_reading(client,device='selected');s.thingspeak._plan(clock());clock.advance(seconds=20);s.thingspeak._plan(clock())
    http_double.slow=2;http_double.body=b'91'
    env=dict(os.environ)
    for key in list(env):
        if key.startswith(('THINGSPEAK_','EDGE_')):env.pop(key)
    process=subprocess.Popen([sys.executable,'-c',WORKER],env=env,stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        process.stdin.write(json.dumps({'now':clock().isoformat(),'database':s.config.database_path,
            'url':http_double.url+'/update'})+'\n');process.stdin.flush()
        deadline=time.monotonic()+6
        while not http_double.calls and time.monotonic()<deadline:time.sleep(.02)
        assert len(http_double.calls)==1  # Double has committed the remote write before its delayed reply.
        process.kill();stdout,stderr=process.communicate(timeout=5)
        assert 'synthetic-secondary-only' not in stdout+stderr
        assert jobs(client)[0]['state']=='SENDING'
        clock.advance(seconds=11)
        replacement=ThingSpeakDelivery(s.config,clock,monotonic=lambda:clock().timestamp())
        replacement.client=SecondaryDouble();replacement.cycle()
        row=jobs(client)[0]
        assert row['state']=='UNCERTAIN' and row['code']=='WORKER_LOST' and replacement.client.calls==[]
        result=client.post('/api/v1/edge/thingspeak/jobs/'+row['id']+'/reconcile',json={'action':'confirmed','entryId':91})
        assert result.status_code==202 and jobs(client)[0]['state']=='RECONCILED_CONFIRMED'
        assert jobs(client)[0]['entryId']=='91' and len(http_double.calls)==1
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)
