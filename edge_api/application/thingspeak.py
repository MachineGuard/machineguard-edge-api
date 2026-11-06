"""Independent, append-only secondary aggregation and delivery; never owns Core's queue."""
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime,timedelta

from peewee import fn

from .errors import ConflictError,NotFoundError,ValidationError
from ..infrastructure.models import (database,SensorReadingModel,ThingSpeakChannelModel as Channel,
    ThingSpeakJobModel as Job,ThingSpeakAttemptModel as Attempt,ThingSpeakSourceLossModel as Loss)
from ..infrastructure.thingspeak_client import ThingSpeakClient

ACTIVE=('OPEN','PENDING','SENDING','UNCERTAIN')
logger=logging.getLogger(__name__)


class ThingSpeakDelivery:
    def __init__(self,config,clock,monotonic=time.monotonic):
        self.config,self.clock=config,clock
        self.monotonic=monotonic;self.monotonic_next=0
        self.code=config.thingspeak_configuration()
        self.lock=threading.Lock();self.owner=uuid.uuid4().hex;self.cancelled=lambda:False
        self.client=ThingSpeakClient(config.thingspeak_http_seconds if self.code=='READY' else 5)
        self.last_observation=None
        if self.code=='READY':
            with database.atomic('IMMEDIATE'):
                channel,created=Channel.get_or_create(channel_id=config.thingspeak_channel_id,defaults={
                    'device_code':config.thingspeak_device_code,'window_seconds':config.thingspeak_window_seconds,
                    'cursor':SensorReadingModel.select(fn.COALESCE(fn.MAX(SensorReadingModel.id),0)).scalar()})
                if channel.device_code!=config.thingspeak_device_code or channel.window_seconds!=config.thingspeak_window_seconds:
                    self.code='CHANNEL_BINDING_CONFLICT'
                if channel.last_attempt_at is not None:
                    # A new process waits a full interval even if wall time jumped during reboot.
                    self.monotonic_next=self.monotonic()+channel.window_seconds

    def _channel(self):return Channel.get_by_id(self.config.thingspeak_channel_id)

    def _plan(self,now):
        # Bounded scanner over already-committed evidence. No hook into capture/Core transactions.
        with database.atomic('IMMEDIATE'):
            channel=self._channel()
            rows=list(SensorReadingModel.select().where((SensorReadingModel.sensor_node_id==channel.device_code)
                & (SensorReadingModel.id>channel.cursor)).order_by(SensorReadingModel.id).limit(self.config.thingspeak_scan_size))
            for reading in rows:
                channel.cursor=reading.id
                if reading.received_at is None:continue  # Discarded/legacy evidence is not invented.
                values=(reading.calibrated_temperature,reading.calibrated_humidity)
                if any(value is None or not math.isfinite(value) for value in values):
                    Loss.create(channel=channel,reading=reading,at=now,code='INVALID_SOURCE');continue
                epoch=datetime(1970,1,1)
                seconds=int((reading.received_at-epoch).total_seconds())
                start=epoch+timedelta(seconds=(seconds//channel.window_seconds)*channel.window_seconds)
                job,created=Job.get_or_create(channel=channel,window_start=start,defaults={
                    'id':uuid.uuid4().hex,'window_end':start+timedelta(seconds=channel.window_seconds)})
                if created and Job.select().where((Job.channel==channel)&Job.state.in_(ACTIVE)).count()>self.config.thingspeak_capacity:
                    job.state='DROPPED_OVERFLOW';job.code='QUEUE_CAPACITY';job.save()
                    Attempt.create(job=job,at=now,code=job.code)
                if job.state!='OPEN' or job.count>=self.config.thingspeak_max_samples:
                    Loss.create(channel=channel,reading=reading,at=now,
                                code='QUEUE_CAPACITY' if job.state=='DROPPED_OVERFLOW' else
                                    'LATE_AFTER_FREEZE' if job.state!='OPEN' else 'WINDOW_CAPACITY');continue
                ids=json.loads(job.source_ids);ids.append(reading.id)
                job.source_ids=json.dumps(ids);job.count+=1
                job.temperature_sum+=values[0];job.humidity_sum+=values[1];job.save()
            channel.save()
            # Do not freeze a partially scanned backlog. A later committed old window is recorded as loss.
            if SensorReadingModel.select().where((SensorReadingModel.sensor_node_id==channel.device_code)
                    & (SensorReadingModel.id>channel.cursor)).exists():return
            opened=Job.select().where((Job.channel==channel)&(Job.state=='OPEN')&(Job.window_end<=now)).order_by(Job.window_start).limit(self.config.thingspeak_scan_size)
            occupied=Job.select().where((Job.channel==channel)&Job.state.in_(('PENDING','SENDING','UNCERTAIN'))).count()
            for job in opened:
                job.payload=json.dumps({'field1':str(round(job.temperature_sum/job.count,4)),
                    'field2':str(round(job.humidity_sum/job.count,4)),'field3':str(job.count),'status':'mg:'+job.id},sort_keys=True)
                job.frozen_at=now
                job.state='PENDING' if occupied<self.config.thingspeak_capacity else 'DROPPED_OVERFLOW'
                job.code='SCHEDULED' if job.state=='PENDING' else 'QUEUE_CAPACITY'
                occupied+=int(job.state=='PENDING');job.save()
                Attempt.create(job=job,at=now,code=job.code)

    def _reserve(self,now):
        with database.atomic('IMMEDIATE'):
            channel=self._channel()
            if channel.lease_owner:
                if channel.lease_until and channel.lease_until>now:return None
                for job in Job.select().where((Job.channel==channel)&(Job.state=='SENDING')):
                    job.state='UNCERTAIN';job.code='WORKER_LOST';job.save()
                    Attempt.create(job=job,at=now,code='WORKER_LOST')
                channel.lease_owner=None;channel.lease_until=None;channel.code='WORKER_LOST'
                channel.next_attempt_at=max(channel.next_attempt_at or now,now+timedelta(seconds=channel.window_seconds))
                self.monotonic_next=max(self.monotonic_next,self.monotonic()+channel.window_seconds)
                channel.save()
            if channel.next_attempt_at and channel.next_attempt_at>now:return None
            if self.monotonic()<self.monotonic_next:return None
            job=Job.select().where((Job.channel==channel)&(Job.state=='PENDING')).order_by(Job.window_start,Job.id).first()
            if job is None:return None
            channel.lease_owner=self.owner
            channel.lease_until=now+timedelta(seconds=self.config.thingspeak_http_seconds+5)
            channel.last_attempt_at=now;channel.code='ATTEMPT_STARTED'
            channel.next_attempt_at=now+timedelta(seconds=channel.window_seconds);channel.save()
            self.monotonic_next=self.monotonic()+channel.window_seconds
            job.state='SENDING';job.code='ATTEMPT_STARTED';job.attempts+=1;job.save()
            Attempt.create(job=job,at=now,code=job.code)
            return job

    def cycle(self):
        if self.code!='READY' or self.cancelled() or not self.lock.acquire(blocking=False):return False
        try:
            self._plan(self.clock());job=self._reserve(self.clock())
            if job is None:return False
            try:
                result=self.client.publish(self.config.thingspeak_write_key,json.loads(job.payload),self.cancelled)
            except Exception:
                from ..infrastructure.thingspeak_client import ThingSpeakResult
                result=ThingSpeakResult('UNCERTAIN','TRANSPORT_ERROR')
            if isinstance(self.client,ThingSpeakClient):
                diagnostic=self.client.diagnostics()
                if diagnostic is not None:
                    self.last_observation={**diagnostic,'jobId':job.id}
                    try:logger.log(logging.WARNING if result.state=='UNCERTAIN' else logging.INFO,
                        'ThingSpeak local job %s attempt %s',job.id,diagnostic['attemptId'])
                    except Exception:pass
            now=self.clock()
            with database.atomic('IMMEDIATE'):
                channel=self._channel()
                if channel.lease_owner!=self.owner:return False
                job.state=result.state;job.code=result.code
                job.entry_id=str(result.entry_id) if result.entry_id is not None else None;job.save()
                Attempt.create(job=job,at=now,code=result.code,entry_id=job.entry_id)
                delay=max(channel.window_seconds,result.retry_after)
                if result.state=='PENDING':
                    delay=max(delay,min(self.config.thingspeak_max_backoff_seconds,
                        self.config.thingspeak_backoff_seconds*2**min(job.attempts-1,16)))
                channel.next_attempt_at=max(channel.next_attempt_at,now+timedelta(seconds=delay))
                self.monotonic_next=max(self.monotonic_next,self.monotonic()+delay)
                channel.code=result.code;channel.lease_owner=None;channel.lease_until=None
                if result.state=='CONFIRMED':channel.last_confirmed_at=now
                channel.save()
            return True
        finally:self.lock.release()

    def health(self):
        channel=Channel.get_or_none(Channel.channel_id==self.config.thingspeak_channel_id) if self.config.thingspeak_channel_id else None
        pending=Job.select().where(Job.state.in_(ACTIVE))
        oldest=pending.order_by(Job.window_start).first()
        return {'enabled':self.code=='READY','configuration':self.code,
            'lastObservation':self.last_observation,
            'channelId':channel.channel_id if channel else None,'deviceCode':channel.device_code if channel else None,
            'windowSeconds':channel.window_seconds if channel else None,
            'code':channel.code if self.code=='READY' and channel else self.code,
            'pending':pending.count(),'uncertain':Job.select().where(Job.state=='UNCERTAIN').count(),
            'rejected':Job.select().where(Job.state=='REJECTED').count(),
            'lostWindows':Job.select().where(Job.state=='DROPPED_OVERFLOW').count(),'lostSources':Loss.select().count(),
            'oldestPendingAt':oldest.window_start if oldest else None,
            'oldestPendingAgeSeconds':max(0,(self.clock()-oldest.window_start).total_seconds()) if oldest else None,
            'lastAttemptAt':channel.last_attempt_at if channel else None,
            'lastConfirmedAt':channel.last_confirmed_at if channel else None,
            'nextAttemptAt':channel.next_attempt_at if channel else None}

    def jobs(self,limit,offset=0,state=None):
        query=Job.select()
        if state is not None:
            if state not in ACTIVE+('CONFIRMED','REJECTED','DROPPED_OVERFLOW','RECONCILED_CONFIRMED','ABANDONED'):
                raise ValidationError('Unknown secondary state')
            query=query.where(Job.state==state)
        rows=query.order_by(Job.window_start,Job.id).offset(offset).limit(limit)
        return {'total':query.count(),'items':[{'id':row.id,'channelId':row.channel_id,
            'deviceCode':row.channel.device_code,'windowStart':row.window_start,'windowEnd':row.window_end,
            'count':row.count,'sourceIds':json.loads(row.source_ids),'payload':json.loads(row.payload) if row.payload else None,
            'state':row.state,'code':row.code,'entryId':row.entry_id,'frozenAt':row.frozen_at,
            'attempts':[{'at':a.at,'code':a.code,'actor':a.actor,'entryId':a.entry_id} for a in Attempt.select().where(Attempt.job==row).order_by(Attempt.id)]}
            for row in rows]}

    def losses(self,limit,offset=0):
        return {'total':Loss.select().count(),'items':[{'readingId':row.reading_id,'channelId':row.channel_id,
            'at':row.at,'code':row.code} for row in Loss.select().order_by(Loss.id).offset(offset).limit(limit)]}

    def reconcile(self,identity,body,actor):
        with database.atomic('IMMEDIATE'):
            row=Job.get_or_none(Job.id==identity)
            if row is None:raise NotFoundError('Secondary job not found')
            if row.state not in ('UNCERTAIN','REJECTED'):raise ConflictError('Only uncertain or rejected jobs can be reconciled')
            action=body.get('action');entry=None
            if action=='retry' and body.get('acceptDuplicateRisk') is True:
                row.state='PENDING';code='ADMIN_RETRY_DUPLICATE_RISK'
            elif action=='confirmed' and type(body.get('entryId')) is int and 1<=body['entryId']<10**19:
                row.state='RECONCILED_CONFIRMED';entry=str(body['entryId']);row.entry_id=entry;code='ADMIN_RECONCILED'
            elif action=='abandon':row.state='ABANDONED';code='ADMIN_ABANDONED'
            else:raise ValidationError('Require confirmed/entryId, abandon, or retry/acceptDuplicateRisk=true')
            row.code=code;row.save();Attempt.create(job=row,at=self.clock(),code=code,actor=actor,entry_id=entry)
