import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable, Optional

from flask import Flask, jsonify

from .application.command_services import (
    ApplyCalibrationCommandService,
    CaptureSensorReadingCommandService,
    DetectOfflineNodesCommandService,
    RegisterSensorNodeCommandService,
    SyncLocalBufferCommandService,
)
from .application.errors import ApplicationError
from .application.query_services import (
    GetCalibrationProfileQueryService,
    GetLocalBufferQueryService,
    GetSensorNodeStatusQueryService,
    GetSensorReadingsQueryService,
)
from .config import Config
from .domain.services import BufferSynchronizationService, NodeAvailabilityService, ReadingCalibrationService
from .infrastructure.cloud_client import CloudGateway, HttpCloudClient
from .infrastructure.models import database, init_database
from .infrastructure.repositories import (
    CalibrationProfileRepository,
    LocalBufferRepository,
    OfflineNodeRepository,
    SensorReadingRepository,
)
from .interfaces.controllers import build_blueprint

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_app(
    config: Optional[Config] = None,
    cloud: Optional[CloudGateway] = None,
    clock: Callable[[], datetime] = utc_now,
) -> Flask:
    config = config or Config()
    init_database(config.database_path)
    cloud = cloud or HttpCloudClient(config.cloud_base_url, config.device_credential)

    profiles = CalibrationProfileRepository()
    readings = SensorReadingRepository()
    buffer = LocalBufferRepository(config.gateway_id, config.buffer_capacity)
    offline_nodes = OfflineNodeRepository()
    availability = NodeAvailabilityService()

    services = SimpleNamespace(
        config=config,
        register_node=RegisterSensorNodeCommandService(profiles, config, clock),
        apply_calibration=ApplyCalibrationCommandService(profiles, clock),
        capture=CaptureSensorReadingCommandService(
            profiles, readings, buffer, offline_nodes, ReadingCalibrationService(), cloud, clock
        ),
        sync=SyncLocalBufferCommandService(readings, buffer, BufferSynchronizationService(), cloud, clock),
        detect_offline=DetectOfflineNodesCommandService(profiles, readings, offline_nodes, availability, clock),
        buffer_query=GetLocalBufferQueryService(buffer),
        calibration_query=GetCalibrationProfileQueryService(profiles),
        reading_queries=GetSensorReadingsQueryService(readings),
        node_queries=GetSensorNodeStatusQueryService(profiles, readings, offline_nodes, clock),
    )

    app = Flask(__name__)
    app.config["services"] = services
    app.register_blueprint(build_blueprint(services))

    @app.errorhandler(ApplicationError)
    def handle_application_error(error: ApplicationError):
        return jsonify({"error": error.message}), error.status_code

    if config.enable_availability_loop:
        _start_availability_loop(services, config.availability_check_seconds)
    return app


def _start_availability_loop(services, interval_seconds: int) -> None:
    def loop():
        while True:
            threading.Event().wait(interval_seconds)
            try:
                services.detect_offline.handle()
            except Exception:  # el monitoreo de disponibilidad nunca debe tumbar el gateway
                logger.exception("Falló la revisión de disponibilidad de nodos")

    threading.Thread(target=loop, name="node-availability", daemon=True).start()
