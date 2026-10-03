from datetime import datetime
from typing import Callable

from ..domain.commands import GetCalibrationProfileQuery, GetLocalBufferQuery
from ..domain.model import CalibrationProfile, LocalBuffer, ReadingStatus
from ..infrastructure.repositories import (
    CalibrationProfileRepository,
    LocalBufferRepository,
    OfflineNodeRepository,
    SensorReadingRepository,
)
from .errors import NotFoundError


class GetLocalBufferQueryService:
    def __init__(self, buffer: LocalBufferRepository):
        self.buffer = buffer

    def handle(self, _query: GetLocalBufferQuery) -> LocalBuffer:
        return self.buffer.current()


class GetCalibrationProfileQueryService:
    def __init__(self, profiles: CalibrationProfileRepository):
        self.profiles = profiles

    def handle(self, query: GetCalibrationProfileQuery) -> CalibrationProfile:
        profile = self.profiles.find_by_sensor_node_id(query.device_code)
        if profile is None:
            raise NotFoundError(f"El nodo {query.device_code} no está registrado")
        return profile


class GetSensorReadingsQueryService:
    def __init__(self, readings: SensorReadingRepository):
        self.readings = readings

    def latest(self, limit: int):
        return self.readings.find_latest(limit)

    def discarded(self, limit: int):
        return self.readings.find_latest(limit, ReadingStatus.DISCARDED)


class GetSensorNodeStatusQueryService:
    def __init__(
        self,
        profiles: CalibrationProfileRepository,
        readings: SensorReadingRepository,
        offline_nodes: OfflineNodeRepository,
        clock: Callable[[], datetime],
    ):
        self.profiles, self.readings, self.offline_nodes, self.clock = profiles, readings, offline_nodes, clock

    def all(self) -> list[dict]:
        open_nodes = {n.sensor_node_id: n for n in self.offline_nodes.list_open()}
        statuses = []
        for p in self.profiles.list_all():
            statuses.append(
                {
                    "profile": p,
                    "lastSeenAt": self.readings.last_captured_at(p.sensor_node_id),
                    "offline": open_nodes.get(p.sensor_node_id),
                }
            )
        return statuses

    def offline(self):
        return self.offline_nodes.list_open()
