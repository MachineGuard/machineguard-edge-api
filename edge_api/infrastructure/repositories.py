from datetime import datetime
from typing import Optional

from ..domain.model import (
    CalibrationOffset,
    CalibrationProfile,
    LocalBuffer,
    OfflineNode,
    PlausibilityRange,
    RawReading,
    ReadingStatus,
    SamplingInterval,
    SensorReading,
)
from .models import (
    CalibrationProfileModel,
    LocalBufferEntryModel,
    OfflineNodeModel,
    SensorReadingModel,
    SyncStateModel,
    NodeCredentialModel,
    CalibrationChangeModel,
)


class NodeCredentialRepository:
    def save(self, profile_id, credential_hash, issued_at):
        (NodeCredentialModel.insert(profile=profile_id, credential_hash=credential_hash, issued_at=issued_at)
         .on_conflict(conflict_target=[NodeCredentialModel.profile], update={
             NodeCredentialModel.credential_hash: credential_hash, NodeCredentialModel.issued_at: issued_at}).execute())

    def find_device_code(self, credential_hash):
        row = NodeCredentialModel.get_or_none(NodeCredentialModel.credential_hash == credential_hash)
        return row.profile.sensor_node_id if row else None


class CalibrationChangeRepository:
    def record(self, profile, old_offset, actor, changed_at):
        CalibrationChangeModel.create(profile=profile.id, actor=actor, changed_at=changed_at,
            old_temperature_offset=old_offset.temperature_offset, old_humidity_offset=old_offset.humidity_offset,
            new_temperature_offset=profile.offset.temperature_offset, new_humidity_offset=profile.offset.humidity_offset)

    def list_for_profile(self, profile_id, limit):
        rows = list(CalibrationChangeModel.select().where(CalibrationChangeModel.profile == profile_id)
                    .order_by(CalibrationChangeModel.id.desc()).limit(limit))
        return list(reversed(rows))


class CalibrationProfileRepository:
    @staticmethod
    def _to_domain(m: CalibrationProfileModel) -> CalibrationProfile:
        return CalibrationProfile(
            id=m.id,
            sensor_node_id=m.sensor_node_id,
            offset=CalibrationOffset(m.temperature_offset, m.humidity_offset),
            temperature_range=PlausibilityRange(m.temperature_min, m.temperature_max),
            humidity_range=PlausibilityRange(m.humidity_min, m.humidity_max),
            sampling_interval=SamplingInterval(m.sampling_interval_seconds),
            calibrated_at=m.calibrated_at,
            updated_at=m.updated_at,
        )

    def find_by_sensor_node_id(self, sensor_node_id: str) -> Optional[CalibrationProfile]:
        m = CalibrationProfileModel.get_or_none(CalibrationProfileModel.sensor_node_id == sensor_node_id)
        return self._to_domain(m) if m else None

    def list_all(self) -> list[CalibrationProfile]:
        return [self._to_domain(m) for m in CalibrationProfileModel.select().order_by(CalibrationProfileModel.id)]

    def save(self, p: CalibrationProfile) -> CalibrationProfile:
        data = dict(
            sensor_node_id=p.sensor_node_id,
            temperature_offset=p.offset.temperature_offset,
            humidity_offset=p.offset.humidity_offset,
            temperature_min=p.temperature_range.minimum_value,
            temperature_max=p.temperature_range.maximum_value,
            humidity_min=p.humidity_range.minimum_value,
            humidity_max=p.humidity_range.maximum_value,
            sampling_interval_seconds=p.sampling_interval.seconds,
            calibrated_at=p.calibrated_at,
            updated_at=p.updated_at,
        )
        if p.id is None:
            p.id = CalibrationProfileModel.create(**data).id
        else:
            CalibrationProfileModel.update(**data).where(CalibrationProfileModel.id == p.id).execute()
        return p


class SensorReadingRepository:
    @staticmethod
    def _to_domain(m: SensorReadingModel) -> SensorReading:
        return SensorReading(
            id=m.id,
            sensor_node_id=m.sensor_node_id,
            raw=RawReading(m.raw_temperature, m.raw_humidity, m.captured_at),
            status=ReadingStatus(m.status),
            calibrated_temperature=m.calibrated_temperature,
            calibrated_humidity=m.calibrated_humidity,
            calibration_profile_id=m.calibration_profile_id,
        )

    def save(self, r: SensorReading) -> SensorReading:
        data = dict(
            sensor_node_id=r.sensor_node_id,
            calibration_profile=r.calibration_profile_id,
            raw_temperature=r.raw.temperature,
            raw_humidity=r.raw.humidity,
            calibrated_temperature=r.calibrated_temperature,
            calibrated_humidity=r.calibrated_humidity,
            captured_at=r.raw.captured_at,
            status=r.status.value,
        )
        if r.id is None:
            r.id = SensorReadingModel.create(**data).id
        else:
            SensorReadingModel.update(**data).where(SensorReadingModel.id == r.id).execute()
        return r

    def find_latest(self, limit: int, status: Optional[ReadingStatus] = None) -> list[SensorReading]:
        query = SensorReadingModel.select().order_by(SensorReadingModel.captured_at.desc(), SensorReadingModel.id.desc())
        if status is not None:
            query = query.where(SensorReadingModel.status == status.value)
        return [self._to_domain(m) for m in query.limit(limit)]

    def last_captured_at(self, sensor_node_id: str) -> Optional[datetime]:
        m = (
            SensorReadingModel.select(SensorReadingModel.captured_at)
            .where((SensorReadingModel.sensor_node_id == sensor_node_id)
                   & SensorReadingModel.status.in_([ReadingStatus.CALIBRATED.value, ReadingStatus.SYNCED.value]))
            .order_by(SensorReadingModel.captured_at.desc())
            .first()
        )
        return m.captured_at if m else None


class LocalBufferRepository:
    def __init__(self, gateway_id: str, capacity: int):
        self.gateway_id = gateway_id
        self.capacity = capacity

    def enqueue(self, reading: SensorReading, now: datetime) -> None:
        LocalBufferEntryModel.create(sensor_reading=reading.id, gateway_id=self.gateway_id, enqueued_at=now)

    def pending_readings(self, limit: Optional[int] = None) -> list[SensorReading]:
        query = (
            SensorReadingModel.select()
            .join(LocalBufferEntryModel, on=(LocalBufferEntryModel.sensor_reading == SensorReadingModel.id))
            .order_by(SensorReadingModel.captured_at.asc(), SensorReadingModel.id.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        return [SensorReadingRepository._to_domain(m) for m in query]

    def remove(self, reading_ids: list[int]) -> None:
        if reading_ids:
            LocalBufferEntryModel.delete().where(LocalBufferEntryModel.sensor_reading.in_(reading_ids)).execute()

    def count_pending(self) -> int:
        return LocalBufferEntryModel.select().count()

    def mark_synced_now(self, now: datetime) -> None:
        state, _ = SyncStateModel.get_or_create(gateway_id=self.gateway_id)
        state.last_sync_at = now
        state.save()

    def current(self) -> LocalBuffer:
        oldest = self.pending_readings(limit=1)
        state = SyncStateModel.get_or_none(SyncStateModel.gateway_id == self.gateway_id)
        return LocalBuffer(
            gateway_id=self.gateway_id,
            capacity=self.capacity,
            pending_count=self.count_pending(),
            oldest_pending_at=oldest[0].raw.captured_at if oldest else None,
            last_sync_at=state.last_sync_at if state else None,
        )


class OfflineNodeRepository:
    @staticmethod
    def _to_domain(m: OfflineNodeModel) -> OfflineNode:
        return OfflineNode(
            id=m.id,
            sensor_node_id=m.sensor_node_id,
            last_seen_at=m.last_seen_at,
            expected_sampling_interval=m.expected_sampling_interval,
            detected_at=m.detected_at,
            restored_at=m.restored_at,
        )

    def find_open_by_sensor_node_id(self, sensor_node_id: str) -> Optional[OfflineNode]:
        m = (
            OfflineNodeModel.select()
            .where((OfflineNodeModel.sensor_node_id == sensor_node_id) & (OfflineNodeModel.restored_at.is_null()))
            .first()
        )
        return self._to_domain(m) if m else None

    def list_open(self) -> list[OfflineNode]:
        query = OfflineNodeModel.select().where(OfflineNodeModel.restored_at.is_null()).order_by(OfflineNodeModel.detected_at)
        return [self._to_domain(m) for m in query]

    def save(self, node: OfflineNode) -> OfflineNode:
        data = dict(
            sensor_node_id=node.sensor_node_id,
            last_seen_at=node.last_seen_at,
            expected_sampling_interval=node.expected_sampling_interval,
            detected_at=node.detected_at,
            restored_at=node.restored_at,
        )
        if node.id is None:
            node.id = OfflineNodeModel.create(**data).id
        else:
            OfflineNodeModel.update(**data).where(OfflineNodeModel.id == node.id).execute()
        return node
