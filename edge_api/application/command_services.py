import logging
from datetime import datetime
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
from ..domain.services import BufferSynchronizationService, NodeAvailabilityService, ReadingCalibrationService
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
    def __init__(self, profiles: CalibrationProfileRepository, config: Config, clock: Clock):
        self.profiles, self.config, self.clock = profiles, config, clock

    def handle(self, command: RegisterSensorNodeCommand) -> CalibrationProfile:
        if not command.device_code.strip():
            raise ValidationError("deviceCode es obligatorio")
        if command.sampling_interval_seconds <= 0:
            raise ValidationError("samplingIntervalSeconds debe ser mayor que cero")
        if self.profiles.find_by_sensor_node_id(command.device_code):
            raise ConflictError(f"El nodo {command.device_code} ya está registrado")
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
        return self.profiles.save(profile)


class ApplyCalibrationCommandService:
    """Actualiza el Calibration Offset; solo afecta a las lecturas capturadas después del cambio."""

    def __init__(self, profiles: CalibrationProfileRepository, clock: Clock):
        self.profiles, self.clock = profiles, clock

    def handle(self, command: ApplyCalibrationCommand) -> CalibrationProfile:
        profile = self.profiles.find_by_sensor_node_id(command.device_code)
        if profile is None:
            raise NotFoundError(f"El nodo {command.device_code} no está registrado")
        profile.update_offset(CalibrationOffset(command.temperature_offset, command.humidity_offset), self.clock())
        return self.profiles.save(profile)


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
    ):
        self.profiles, self.readings, self.buffer = profiles, readings, buffer
        self.offline_nodes, self.calibration, self.cloud, self.clock = offline_nodes, calibration, cloud, clock

    def handle(self, command: CaptureSensorReadingCommand) -> SensorReading:
        profile = self.profiles.find_by_sensor_node_id(command.device_code)
        if profile is None:
            raise NotFoundError(f"El nodo {command.device_code} no está registrado")
        now = self.clock()
        raw = RawReading(command.temperature, command.humidity, command.captured_at or now)
        if not raw.has_valid_timestamp(now):
            raise ValidationError("capturedAt no puede estar en el futuro")

        with database.atomic():
            reading = self.calibration.process(command.device_code, raw, profile)
            self.readings.save(reading)
            self._restore_if_offline(command.device_code, now)
            if reading.status == ReadingStatus.DISCARDED:
                return reading
            self._publish_or_buffer(reading, now)
        return reading

    def _restore_if_offline(self, device_code: str, now: datetime) -> None:
        offline = self.offline_nodes.find_open_by_sensor_node_id(device_code)
        if offline is not None:
            offline.restore(now)
            self.offline_nodes.save(offline)

    def _publish_or_buffer(self, reading: SensorReading, now: datetime) -> None:
        # Si ya hay lecturas pendientes se encola para no romper el orden cronológico del historial.
        if not self.buffer.count_pending() and self.cloud.publish_reading(reading):
            reading.mark_as_synced()
            self.readings.save(reading)
            return
        self.buffer.enqueue(reading, now)
        self._enforce_capacity()

    def _enforce_capacity(self) -> None:
        overflow = self.buffer.current().overflow()
        if overflow <= 0:
            return
        dropped = self.buffer.pending_readings(limit=overflow)
        self.buffer.remove([r.id for r in dropped])
        for r in dropped:
            r.discard(r.calibration_profile_id)
            self.readings.save(r)
        logger.warning("Local Buffer lleno: se descartaron %d lecturas pendientes más antiguas", len(dropped))


class SyncLocalBufferCommandService:
    def __init__(
        self,
        readings: SensorReadingRepository,
        buffer: LocalBufferRepository,
        synchronization: BufferSynchronizationService,
        cloud: CloudGateway,
        clock: Clock,
    ):
        self.readings, self.buffer, self.synchronization = readings, buffer, synchronization
        self.cloud, self.clock = cloud, clock

    def handle(self, command: SyncLocalBufferCommand) -> dict:
        if command.batch_size <= 0:
            raise ValidationError("batchSize debe ser mayor que cero")
        synced = 0
        while True:
            batch = self.synchronization.next_batch(self.buffer.pending_readings(limit=command.batch_size), command.batch_size)
            if not batch:
                break
            if not self.cloud.publish_batch(batch):
                break
            with database.atomic():
                for reading in batch:
                    reading.mark_as_synced()
                    self.readings.save(reading)
                self.buffer.remove([r.id for r in batch])
            synced += len(batch)
        pending = self.buffer.count_pending()
        if synced:
            self.buffer.mark_synced_now(self.clock())
        # BufferSynced: el buffer quedó vacío tras haber enviado lecturas.
        return {"synced": synced, "pending": pending, "bufferSynced": synced > 0 and pending == 0}


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
            last_seen = self.readings.last_captured_at(profile.sensor_node_id) or profile.calibrated_at
            if self.availability.is_offline(last_seen, profile.sampling_interval, now):
                node = self.availability.detect(profile.sensor_node_id, last_seen, profile.sampling_interval, now)
                detected.append(self.offline_nodes.save(node))  # SensorNodeWentOffline
                logger.warning("SensorNodeWentOffline: %s", profile.sensor_node_id)
        return detected
