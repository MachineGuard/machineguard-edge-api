from datetime import datetime
from typing import Iterable

from .model import (
    CalibrationProfile,
    OfflineNode,
    RawReading,
    ReadingStatus,
    SamplingInterval,
    SensorReading,
)


class ReadingCalibrationService:
    """Valida la plausibilidad de una lectura cruda y, si procede, la calibra."""

    def process(self, sensor_node_id: str, raw: RawReading, profile: CalibrationProfile) -> SensorReading:
        reading = SensorReading.capture(sensor_node_id, raw)
        if not profile.is_plausible(raw):
            reading.discard(profile.id)
            return reading
        reading.apply_calibration(profile)
        return reading


class BufferSynchronizationService:
    """Ordena y filtra los lotes pendientes antes de enviarlos a la nube."""

    def next_batch(self, pending: Iterable[SensorReading], size: int) -> list[SensorReading]:
        ordered = sorted(
            (r for r in pending if r.status == ReadingStatus.CALIBRATED),
            key=lambda r: r.raw.captured_at,
        )
        return ordered[:size]


class NodeAvailabilityService:
    """Determina si un nodo sigue reportando dentro de su Sampling Interval."""

    def is_offline(self, last_seen_at: datetime, interval: SamplingInterval, now: datetime) -> bool:
        return interval.is_exceeded(last_seen_at, now)

    def detect(self, sensor_node_id: str, last_seen_at: datetime, interval: SamplingInterval, now: datetime) -> OfflineNode:
        return OfflineNode.detect(sensor_node_id, last_seen_at, interval, now)
