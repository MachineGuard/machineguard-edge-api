from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class ReadingStatus(str, Enum):
    CAPTURED = "CAPTURED"
    CALIBRATED = "CALIBRATED"
    DISCARDED = "DISCARDED"
    SYNCED = "SYNCED"


class BufferStatus(str, Enum):
    EMPTY = "EMPTY"
    PENDING = "PENDING"
    FULL = "FULL"


@dataclass(frozen=True)
class RawReading:
    temperature: float
    humidity: float
    captured_at: datetime

@dataclass(frozen=True)
class CalibrationOffset:
    temperature_offset: float = 0.0
    humidity_offset: float = 0.0

    def apply_to(self, raw: RawReading) -> tuple[float, float]:
        return (
            round(raw.temperature + self.temperature_offset, 2),
            round(raw.humidity + self.humidity_offset, 2),
        )


@dataclass(frozen=True)
class PlausibilityRange:
    minimum_value: float
    maximum_value: float

    def contains(self, value: float) -> bool:
        return self.minimum_value <= value <= self.maximum_value

    def is_out_of_range(self, value: float) -> bool:
        return not self.contains(value)


@dataclass(frozen=True)
class SamplingInterval:
    seconds: int

    def is_exceeded(self, last_seen_at: datetime, now: datetime) -> bool:
        return (now - last_seen_at).total_seconds() > self.seconds


@dataclass
class CalibrationProfile:
    sensor_node_id: str
    offset: CalibrationOffset
    temperature_range: PlausibilityRange
    humidity_range: PlausibilityRange
    sampling_interval: SamplingInterval
    calibrated_at: datetime
    updated_at: datetime
    id: Optional[int] = None

    def is_plausible(self, raw: RawReading) -> bool:
        return self.temperature_range.contains(raw.temperature) and self.humidity_range.contains(raw.humidity)

    def apply_to(self, raw: RawReading) -> tuple[float, float]:
        return self.offset.apply_to(raw)

    def update_offset(self, offset: CalibrationOffset, now: datetime) -> None:
        self.offset = offset
        self.updated_at = now
        self.calibrated_at = now


@dataclass
class SensorReading:
    sensor_node_id: str
    raw: RawReading
    status: ReadingStatus = ReadingStatus.CAPTURED
    calibrated_temperature: Optional[float] = None
    calibrated_humidity: Optional[float] = None
    calibration_profile_id: Optional[int] = None
    id: Optional[int] = None
    sample_id: Optional[str] = None
    captured_at_supplied: bool = False

    @classmethod
    def capture(cls, sensor_node_id: str, raw: RawReading) -> "SensorReading":
        return cls(sensor_node_id=sensor_node_id, raw=raw)

    def apply_calibration(self, profile: CalibrationProfile) -> None:
        self.calibrated_temperature, self.calibrated_humidity = profile.apply_to(self.raw)
        self.calibration_profile_id = profile.id
        self.status = ReadingStatus.CALIBRATED

    def discard(self, profile_id: Optional[int] = None) -> None:
        self.calibration_profile_id = profile_id
        self.status = ReadingStatus.DISCARDED

    def mark_as_synced(self) -> None:
        self.status = ReadingStatus.SYNCED

    def is_calibrated(self) -> bool:
        return self.status == ReadingStatus.CALIBRATED

    def is_synced(self) -> bool:
        return self.status == ReadingStatus.SYNCED


@dataclass
class OfflineNode:
    sensor_node_id: str
    last_seen_at: datetime
    expected_sampling_interval: int
    detected_at: datetime
    restored_at: Optional[datetime] = None
    id: Optional[int] = None

    @classmethod
    def detect(cls, sensor_node_id: str, last_seen_at: datetime, interval: SamplingInterval, now: datetime):
        return cls(sensor_node_id, last_seen_at, interval.seconds, now)

    def restore(self, now: datetime) -> None:
        self.restored_at = now

    def is_still_offline(self) -> bool:
        return self.restored_at is None

    def elapsed_since_last_reading(self, now: datetime) -> timedelta:
        return now - self.last_seen_at


@dataclass
class LocalBuffer:
    """Estado del buffer local derivado de las entradas pendientes persistidas."""

    gateway_id: str
    capacity: int
    pending_count: int = 0
    oldest_pending_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None

    @property
    def status(self) -> BufferStatus:
        if self.pending_count == 0:
            return BufferStatus.EMPTY
        if self.is_full():
            return BufferStatus.FULL
        return BufferStatus.PENDING

    def has_pending_readings(self) -> bool:
        return self.pending_count > 0

    def is_full(self) -> bool:
        return self.pending_count >= self.capacity

    def overflow(self) -> int:
        """Cantidad de lecturas pendientes más antiguas que hay que descartar para respetar la capacidad."""
        return max(0, self.pending_count - self.capacity)
