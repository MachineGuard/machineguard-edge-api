"""Bounded outbox delivery. SQLite transactions end before every network call."""
import math
import logging
import threading
import uuid
from peewee import DatabaseError
from datetime import datetime, timedelta, timezone

from ..domain.model import ReadingStatus
from ..infrastructure.cloud_client import DeliveryResult, SAMPLE_CODES, NODE_CODES
from ..infrastructure.models import (database, SensorReadingModel, LocalBufferEntryModel,
    DeliveryAttemptModel, QuarantineModel, DeliveryLossModel, NodeDeliveryStateModel, DeliveryStateModel)
from ..infrastructure.repositories import SensorReadingRepository
from .errors import ValidationError, NotFoundError, ConflictError

logger = logging.getLogger(__name__)


def _real_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DeliveryCoordinator:
    def __init__(self, readings, buffer, cloud, clock, config):
        self.readings, self.buffer, self.cloud, self.clock, self.config = readings, buffer, cloud, clock, config
        self.lock = threading.Lock()
        self.owner = uuid.uuid4().hex
        self.cancelled = lambda: False
        DeliveryStateModel.get_or_create(gateway_id=config.gateway_id)

    def _lease(self):
        now = _real_now()
        return (DeliveryStateModel.update(lease_owner=self.owner, lease_until=now+timedelta(seconds=self.config.delivery_lease_seconds))
            .where((DeliveryStateModel.gateway_id == self.config.gateway_id) & (
                (DeliveryStateModel.lease_owner == self.owner) | DeliveryStateModel.lease_until.is_null()
                | (DeliveryStateModel.lease_until <= now))).execute()) == 1

    def _owns(self):
        return DeliveryStateModel.select().where((DeliveryStateModel.gateway_id == self.config.gateway_id)
            & (DeliveryStateModel.lease_owner == self.owner) & (DeliveryStateModel.lease_until > _real_now())).exists()

    def _paused(self):
        return {row.sensor_node_id for row in NodeDeliveryStateModel.select().where(NodeDeliveryStateModel.paused_until > self.clock())}

    def _eligible(self, size, force):
        now = self.clock()
        query = (SensorReadingModel.select().join(LocalBufferEntryModel)
            .where(LocalBufferEntryModel.inflight_until.is_null() | (LocalBufferEntryModel.inflight_until <= _real_now())))
        if not force:
            query = query.where((LocalBufferEntryModel.next_attempt_at.is_null() | (LocalBufferEntryModel.next_attempt_at <= now))
                                & SensorReadingModel.sensor_node_id.not_in(self._paused()))
        return [SensorReadingRepository._to_domain(row) for row in query.order_by(SensorReadingModel.captured_at, SensorReadingModel.id).limit(size)]

    def _send(self, batch, single):
        # Legacy nonfinite rows must be isolated without attempting invalid JSON serialization.
        if any(r.calibrated_temperature is None or r.calibrated_humidity is None
               or not math.isfinite(r.calibrated_temperature) or not math.isfinite(r.calibrated_humidity) for r in batch):
            return DeliveryResult(code='LOCAL_READING_INVALID')
        accepted = self.cloud.publish_reading(batch[0]) if single else self.cloud.publish_batch(batch)
        result = getattr(self.cloud, 'last_result', DeliveryResult(accepted, 'CONFIRMED' if accepted else 'UNCONFIRMED'))
        if not accepted and result.accepted:
            return DeliveryResult(code='INVALID_RECEIPT')
        return result

    def handle(self, command, *, force=True, immediate=False):
        if type(command.batch_size) is not int or not 1 <= command.batch_size <= 500:
            raise ValidationError('batchSize debe ser un entero entre 1 y 500')
        if not self.lock.acquire(blocking=False):
            return {'synced':0,'pending':self.buffer.count_pending(),'bufferSynced':False,'busy':True}
        synced = 0
        try:
            with database.atomic('IMMEDIATE'):
                if not self._lease():
                    return {'synced':0,'pending':self.buffer.count_pending(),'bufferSynced':False,'busy':True}
            budget = self.config.delivery_max_requests
            paused_this_cycle = set()
            while budget > 0 and not self.cancelled():
                batch = [r for r in self._eligible(command.batch_size, force) if r.sensor_node_id not in paused_this_cycle]
                if not batch:
                    break
                queue = [(batch,0)]
                while queue and budget > 0 and not self.cancelled():
                    current,depth = queue.pop(0)
                    current = [r for r in current if r.sensor_node_id not in paused_this_cycle
                               and LocalBufferEntryModel.select().where(LocalBufferEntryModel.sensor_reading == r.id).exists()]
                    if not current:
                        continue
                    ids = [r.id for r in current]
                    with database.atomic('IMMEDIATE'):
                        if not self._lease():
                            return {'synced':synced,'pending':self.buffer.count_pending(),'bufferSynced':False,'busy':True}
                        LocalBufferEntryModel.update(inflight_until=_real_now()+timedelta(seconds=self.config.delivery_lease_seconds)).where(LocalBufferEntryModel.sensor_reading.in_(ids)).execute()
                        LocalBufferEntryModel.update(attempts=LocalBufferEntryModel.attempts+1).where(LocalBufferEntryModel.sensor_reading.in_(ids)).execute()
                        for reading in current:
                            DeliveryAttemptModel.create(reading=reading.id,attempted_at=self.clock(),code='ATTEMPT_STARTED',outcome='STARTED')
                    try:
                        result = self._send(current, immediate and len(current)==1)
                    except Exception:
                        # Preserve the queue; never log a request/credential-bearing exception.
                        result = DeliveryResult(code='DELIVERY_ERROR')
                    budget -= 1
                    now = self.clock()
                    with database.atomic('IMMEDIATE'):
                        if not self._owns():
                            # A replacement owner will reconcile via Core's idempotent receipt.
                            return {'synced':synced,'pending':self.buffer.count_pending(),'bufferSynced':False,'busy':True}
                        attempts = []
                        for reading in current:
                            row = LocalBufferEntryModel.get_or_none(LocalBufferEntryModel.sensor_reading == reading.id)
                            if row is None:
                                continue
                            attempts.append(row.attempts)
                            row.inflight_until = None
                            delay = min(self.config.delivery_max_backoff_seconds,
                                        self.config.delivery_backoff_seconds * 2**min(row.attempts-1,16))
                            row.next_attempt_at = now + timedelta(seconds=delay)
                            row.save()
                            DeliveryAttemptModel.create(reading=reading.id, attempted_at=now,code=result.code,
                                outcome='CONFIRMED' if result.accepted else 'DEFERRED',http_status=result.http_status)
                        credential = ('CONFIRMED' if result.accepted else 'REJECTED' if result.http_status in (401,403)
                                      else 'MISSING' if result.code=='UNCONFIGURED' else 'UNKNOWN')
                        state = dict(transport='CONFIRMED' if result.accepted else 'DEGRADED', credential=credential,
                                     code=result.code,last_attempt_at=now)
                        if result.accepted:
                            state['last_confirmed_at'] = now
                        DeliveryStateModel.update(**state).where(DeliveryStateModel.gateway_id == self.config.gateway_id).execute()
                        # HTTP status takes precedence over any misleading body code.
                        sample_error = result.code in SAMPLE_CODES and (result.http_status is None or result.http_status in (400,409))
                        node_error = result.code in NODE_CODES and result.http_status in (404,409)
                        if result.accepted:
                            SensorReadingModel.update(status=ReadingStatus.SYNCED.value).where(SensorReadingModel.id.in_(ids)).execute()
                            self.buffer.remove(ids)
                            NodeDeliveryStateModel.delete().where(NodeDeliveryStateModel.sensor_node_id.in_({r.sensor_node_id for r in current})).execute()
                            self.buffer.mark_synced_now(now)
                            synced += len(ids)
                        elif sample_error and len(current)==1:
                            reading = current[0]
                            QuarantineModel.create(reading=reading.id,code=result.code,reason=result.code,quarantined_at=now)
                            SensorReadingModel.update(status=ReadingStatus.QUARANTINED.value).where(SensorReadingModel.id==reading.id).execute()
                            self.buffer.remove(ids)
                            DeliveryAttemptModel.create(reading=reading.id,attempted_at=now,code=result.code,outcome='QUARANTINED',http_status=result.http_status)
                        elif node_error and len({r.sensor_node_id for r in current})==1:
                            device=current[0].sensor_node_id
                            (NodeDeliveryStateModel.insert(sensor_node_id=device,code=result.code,paused_until=now+timedelta(seconds=self.config.node_pause_seconds))
                                .on_conflict(conflict_target=[NodeDeliveryStateModel.sensor_node_id],update={
                                    NodeDeliveryStateModel.code:result.code, NodeDeliveryStateModel.paused_until:now+timedelta(seconds=self.config.node_pause_seconds)}).execute())
                            paused_this_cycle.add(device)
                        elif (sample_error or node_error or (result.http_status is not None and result.http_status>=500
                                and attempts and min(attempts)>=2)) and len(current)>1 and depth<self.config.delivery_max_depth:
                            mid=len(current)//2
                            queue[0:0] = [(current[:mid],depth+1),(current[mid:],depth+1)]
                # Forced manual work retries each selected row once per cycle; descendants
                # may retry only through the bounded partition queue above.
                force = False
                if immediate:
                    break
            pending = self.buffer.count_pending()
            return {'synced':synced,'pending':pending,'bufferSynced':synced>0 and pending==0}
        finally:
            try:
                DeliveryStateModel.update(lease_owner=None,lease_until=None).where(
                    (DeliveryStateModel.gateway_id==self.config.gateway_id) & (DeliveryStateModel.lease_owner==self.owner)).execute()
            except DatabaseError:
                # Leave persistent lease/inflight expiry intact for the next cycle.
                logger.warning('Delivery lease cleanup failed; a later cycle will reconcile pending readings')
            finally:
                self.lock.release()

    def enforce_capacity(self):
        overflow = self.buffer.count_pending()-self.config.buffer_capacity
        if overflow <= 0:
            return
        # An in-flight row remains protected until the request/lease expires.
        rows = (LocalBufferEntryModel.select().where(LocalBufferEntryModel.inflight_until.is_null()
                | (LocalBufferEntryModel.inflight_until <= _real_now())).order_by(LocalBufferEntryModel.enqueued_at,LocalBufferEntryModel.id).limit(overflow))
        for entry in rows:
            SensorReadingModel.update(status=ReadingStatus.DROPPED_OVERFLOW.value).where(SensorReadingModel.id==entry.sensor_reading_id).execute()
            DeliveryLossModel.create(reading=entry.sensor_reading_id,lost_at=self.clock())
            entry.delete_instance()

    def health(self):
        state=DeliveryStateModel.get_by_id(self.config.gateway_id)
        oldest=LocalBufferEntryModel.select().order_by(LocalBufferEntryModel.enqueued_at).first()
        return {'transport':state.transport,'credential':state.credential,'code':state.code,
                'lastAttemptAt':state.last_attempt_at,'lastConfirmedAt':state.last_confirmed_at,
                'pending':self.buffer.count_pending(),
                'quarantined':SensorReadingModel.select().where(SensorReadingModel.status==ReadingStatus.QUARANTINED.value).count(),
                'lost':DeliveryLossModel.select().count(),
                'oldestPendingAt':oldest.enqueued_at if oldest else None,
                'oldestPendingAgeSeconds':max(0,(self.clock()-oldest.enqueued_at).total_seconds()) if oldest else None,
                'pausedNodes':[{'deviceCode':r.sensor_node_id,'code':r.code,'retryAt':r.paused_until} for r in NodeDeliveryStateModel.select()]}

    def quarantine(self, limit, offset):
        rows=QuarantineModel.select().order_by(QuarantineModel.id).offset(offset).limit(limit)
        return {'total':QuarantineModel.select().count(),'items':[{
            'id':row.id,'code':row.code,'reason':row.reason,'quarantinedAt':row.quarantined_at,
            'reading':SensorReadingRepository._to_domain(row.reading),
            'attempts':[{'at':a.attempted_at,'code':a.code,'outcome':a.outcome,'httpStatus':a.http_status,'actor':a.actor}
                        for a in DeliveryAttemptModel.select().where(DeliveryAttemptModel.reading==row.reading_id).order_by(DeliveryAttemptModel.id)]}
            for row in rows]}

    def reprocess(self, quarantine_id, actor):
        with database.atomic('IMMEDIATE'):
            item=QuarantineModel.get_or_none(QuarantineModel.id==quarantine_id)
            if item is None:
                raise NotFoundError('Quarantine entry not found')
            reading=item.reading
            if reading.status != ReadingStatus.QUARANTINED.value:
                raise ConflictError('Only a currently quarantined reading can be reprocessed')
            reading.status=ReadingStatus.CALIBRATED.value
            reading.save()
            self.buffer.enqueue(SensorReadingRepository._to_domain(reading),self.clock())
            DeliveryAttemptModel.create(reading=reading.id,attempted_at=self.clock(),code='ADMIN_REPROCESS',outcome='REQUEUED',actor=actor)
            self.enforce_capacity()
