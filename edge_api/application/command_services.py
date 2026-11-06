import logging
import math
import re
from datetime import datetime, timedelta
from typing import Callable

from ..config import Config
from ..domain.commands import (
    ApplyCalibrationCommand,
    CaptureSensorReadingCommand,
    RegisterSensorNodeCommand,
    SyncLocalBufferCommand,
)
from ..domain.model import (
    CalibrationOffset,
    CalibrationProfile,
    PlausibilityRange,
    RawReading,
    ReadingStatus,
    SamplingInterval,
    SensorReading,
)
from ..domain.services import NodeAvailabilityService, ReadingCalibrationService
from ..infrastructure.cloud_client import CloudGateway
from ..infrastructure.models import database
from ..infrastructure.repositories import (
    CalibrationProfileRepository,
    LocalBufferRepository,
    OfflineNodeRepository,
    SensorReadingRepository,
)
from .errors import ConflictError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]


class RegisterSensorNodeCommandService:
    def __init__(self, profiles: CalibrationProfileRepository, config: Config, clock: Clock, credentials, changes):
        self.profiles, self.config, self.clock = profiles, config, clock
        self.credentials, self.changes = credentials, changes

    def handle(self, command: RegisterSensorNodeCommand, actor: str):
        if not command.device_code.strip():
            raise ValidationError("deviceCode es obligatorio")
        if command.sampling_interval_seconds <= 0:
            raise ValidationError("samplingIntervalSeconds debe ser mayor que cero")
        if self.profiles.find_by_sensor_node_id(command.device_code):
            raise ConflictError(f"El nodo {command.device_code} ya está registrado")
        _validate_offset(command.temperature_offset, command.humidity_offset, self.config)
        now = self.clock()
        profile = CalibrationProfile(
            sensor_node_id=command.device_code,
            offset=CalibrationOffset(command.temperature_offset, command.humidity_offset),
            temperature_range=PlausibilityRange(*self.config.temperature_range),
            humidity_range=PlausibilityRange(*self.config.humidity_range),
            sampling_interval=SamplingInterval(command.sampling_interval_seconds),
            calibrated_at=now,
            updated_at=now,
        )
        with database.atomic():
            self.profiles.save(profile)
            self.changes.record(profile, CalibrationOffset(), actor, now)
            credential = self.credentials.issue(command.device_code)
        return profile, credential


class ApplyCalibrationCommandService:
    """Actualiza el Calibration Offset; solo afecta a las lecturas capturadas después del cambio."""

    def __init__(self, profiles: CalibrationProfileRepository, clock: Clock, config: Config, changes):
        self.profiles, self.clock = profiles, clock
        self.config, self.changes = config, changes

    def handle(self, command: ApplyCalibrationCommand, actor: str) -> CalibrationProfile:
        _validate_offset(command.temperature_offset, command.humidity_offset, self.config)
        with database.atomic('IMMEDIATE'):
            profile = self.profiles.find_by_sensor_node_id(command.device_code)
            if profile is None:
                raise NotFoundError(f"El nodo {command.device_code} no está registrado")
            old_offset = profile.offset
            now = self.clock()
            profile.update_offset(CalibrationOffset(command.temperature_offset, command.humidity_offset), now)
            self.profiles.save(profile)
            self.changes.record(profile, old_offset, actor, now)
        return profile


def _validate_offset(temperature, humidity, config):
    for value, limit in [(temperature, config.max_temperature_offset), (humidity, config.max_humidity_offset)]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
            raise ValidationError('Calibration offset is outside the configured finite limits')


class CaptureSensorReadingCommandService:
    def __init__(
        self,
        profiles: CalibrationProfileRepository,
        readings: SensorReadingRepository,
        buffer: LocalBufferRepository,
        offline_nodes: OfflineNodeRepository,
        calibration: ReadingCalibrationService,
        cloud: CloudGateway,
        clock: Clock,
        config: Config,
    ):
        self.profiles, self.readings, self.buffer = profiles, readings, buffer
        self.offline_nodes, self.calibration, self.clock = offline_nodes, calibration, clock
        self.delivery = cloud
        self.config = config

    def handle(self, command: CaptureSensorReadingCommand) -> SensorReading:
        if (not isinstance(command.sample_id, str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}-(0|[1-9][0-9]{0,19})', command.sample_id) is None):
            raise ValidationError('sampleId debe tener formato bootId-secuencia (bootId de 1..64 caracteres)')
        profile = self.profiles.find_by_sensor_node_id(command.device_code)
        if profile is None:
            raise NotFoundError(f"El nodo {command.device_code} no está registrado")
        now = self.clock()
        raw = RawReading(command.temperature, command.humidity, command.captured_at or now)
        if not math.isfinite(raw.temperature) or not math.isfinite(raw.humidity):
            raise ValidationError('Reading values must be finite')
        supplied = command.captured_at is not None
        with database.atomic('IMMEDIATE'):
            existing = self.readings.find_by_sample(command.device_code, command.sample_id)
            if existing is not None:
                if (existing.raw.temperature != raw.temperature or existing.raw.humidity != raw.humidity
                        or existing.captured_at_supplied != supplied
                        or (supplied and existing.raw.captured_at != raw.captured_at)):
                    raise ConflictError('sampleId ya existe con otros datos')
                return existing
            if raw.captured_at < self.config.minimum_capture:
                raise ValidationError('capturedAt es anterior a la fecha mínima configurada')
            if raw.captured_at > now + timedelta(seconds=self.config.max_future_seconds):
                raise ValidationError('capturedAt supera la tolerancia de tiempo futuro')
            if raw.captured_at < now - timedelta(days=self.config.max_reading_age_days):
                raise ValidationError('capturedAt supera la antigüedad máxima configurada')
            reading = self.calibration.process(command.device_code, raw, profile)
            reading.sample_id = command.sample_id
            reading.captured_at_supplied = supplied
            self.readings.save(reading)
            if reading.status == ReadingStatus.DISCARDED:
                return reading
            self._restore_if_offline(command.device_code, now)
            reading.received_at = now
            self.readings.save(reading)
            was_empty = self.buffer.count_pending() == 0
            self.buffer.enqueue(reading, now)
            self.delivery.enforce_capacity()
        if was_empty:
            self.delivery.handle(SyncLocalBufferCommand(1), force=False, immediate=True)
        from ..infrastructure.models import SensorReadingModel
        return self.readings._to_domain(SensorReadingModel.get_by_id(reading.id))

    def _restore_if_offline(self, device_code: str, now: datetime) -> None:
        offline = self.offline_nodes.find_open_by_sensor_node_id(device_code)
        if offline is not None:
            offline.restore(now)
            self.offline_nodes.save(offline)

    @property
    def cloud(self):
        return self.delivery.cloud

    @cloud.setter
    def cloud(self, value):
        self.delivery.cloud = value


class DetectOfflineNodesCommandService:
    def __init__(
        self,
        profiles: CalibrationProfileRepository,
        readings: SensorReadingRepository,
        offline_nodes: OfflineNodeRepository,
        availability: NodeAvailabilityService,
        clock: Clock,
    ):
        self.profiles, self.readings, self.offline_nodes = profiles, readings, offline_nodes
        self.availability, self.clock = availability, clock

    def handle(self) -> list:
        now = self.clock()
        detected = []
        for profile in self.profiles.list_all():
            if self.offline_nodes.find_open_by_sensor_node_id(profile.sensor_node_id):
                continue
            last_seen = self.readings.last_received_at(profile.sensor_node_id) or profile.calibrated_at
            if self.availability.is_offline(last_seen, profile.sampling_interval, now):
                node = self.availability.detect(profile.sensor_node_id, last_seen, profile.sampling_interval, now)
                detected.append(self.offline_nodes.save(node))  # SensorNodeWentOffline
                logger.warning("SensorNodeWentOffline: %s", profile.sensor_node_id)
        return detected
