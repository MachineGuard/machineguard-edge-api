from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class CaptureSensorReadingCommand:
    device_code: str
    temperature: float
    humidity: float
    captured_at: Optional[datetime] = None


@dataclass(frozen=True)
class ApplyCalibrationCommand:
    device_code: str
    temperature_offset: float
    humidity_offset: float


@dataclass(frozen=True)
class SyncLocalBufferCommand:
    batch_size: int = 100


@dataclass(frozen=True)
class RegisterSensorNodeCommand:
    device_code: str
    sampling_interval_seconds: int = 60
    temperature_offset: float = 0.0
    humidity_offset: float = 0.0


@dataclass(frozen=True)
class GetLocalBufferQuery:
    pass


@dataclass(frozen=True)
class GetCalibrationProfileQuery:
    device_code: str
