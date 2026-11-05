import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable, Optional

from flask import Flask, jsonify
from flask.json.provider import DefaultJSONProvider
import math

from .application.command_services import (
    ApplyCalibrationCommandService,
    CaptureSensorReadingCommandService,
    DetectOfflineNodesCommandService,
    RegisterSensorNodeCommandService,
)
from .application.errors import ApplicationError
from .application.delivery import DeliveryCoordinator
from .application.runtime import EdgeRuntime, stop_previous_runtime
from .application.node_credentials import NodeCredentialService
from .application.query_services import (
    GetCalibrationProfileQueryService,
    GetLocalBufferQueryService,
    GetSensorNodeStatusQueryService,
    GetSensorReadingsQueryService,
)
from .config import Config
from .domain.services import NodeAvailabilityService, ReadingCalibrationService
from .infrastructure.cloud_client import CloudGateway, HttpCloudClient
from .infrastructure.models import database, init_database
from .infrastructure.repositories import (
    CalibrationProfileRepository,
    LocalBufferRepository,
    OfflineNodeRepository,
    SensorReadingRepository,
    NodeCredentialRepository,
    CalibrationChangeRepository,
)
from .interfaces.controllers import build_blueprint

logger = logging.getLogger(__name__)


class StrictJSONProvider(DefaultJSONProvider):
    def loads(self, value, **kwargs):
        def invalid(_value):
            raise ValueError('Non-finite JSON number')
        def finite_float(number):
            result = float(number)
            return result if math.isfinite(result) else invalid(number)
        kwargs.update(parse_constant=invalid, parse_float=finite_float)
        try:
            return super().loads(value, **kwargs)
        except RecursionError as error:
            raise ValueError('JSON nesting is too deep') from error


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_app(
    config: Optional[Config] = None,
    cloud: Optional[CloudGateway] = None,
    clock: Callable[[], datetime] = utc_now,
) -> Flask:
    config = config or Config()
    config.validate()
    stop_previous_runtime()
    init_database(config.database_path)
    cloud = cloud or HttpCloudClient(config.cloud_base_url, config.device_credential,config.delivery_http_timeout_seconds)

    profiles = CalibrationProfileRepository()
    readings = SensorReadingRepository()
    buffer = LocalBufferRepository(config.gateway_id, config.buffer_capacity)
    offline_nodes = OfflineNodeRepository()
    availability = NodeAvailabilityService(config.offline_grace_ratio)
    delivery = DeliveryCoordinator(readings, buffer, cloud, clock, config)
    changes = CalibrationChangeRepository()
    credentials = NodeCredentialService(profiles, NodeCredentialRepository(), clock)

    services = SimpleNamespace(
        config=config,
        register_node=RegisterSensorNodeCommandService(profiles, config, clock, credentials, changes),
        apply_calibration=ApplyCalibrationCommandService(profiles, clock, config, changes),
        credentials=credentials,
        calibration_changes=changes,
        capture=CaptureSensorReadingCommandService(
            profiles, readings, buffer, offline_nodes, ReadingCalibrationService(), delivery, clock, config
        ),
        sync=delivery,
        detect_offline=DetectOfflineNodesCommandService(profiles, readings, offline_nodes, availability, clock),
        buffer_query=GetLocalBufferQueryService(buffer),
        calibration_query=GetCalibrationProfileQueryService(profiles),
        reading_queries=GetSensorReadingsQueryService(readings),
        node_queries=GetSensorNodeStatusQueryService(profiles, readings, offline_nodes, clock),
    )

    app = Flask(__name__)
    app.json = StrictJSONProvider(app)
    app.config['MAX_CONTENT_LENGTH'] = config.max_content_bytes
    app.config["services"] = services
    app.register_blueprint(build_blueprint(services))

    @app.teardown_appcontext
    def close_request_database(_error):
        if not database.is_closed():
            database.close()

    @app.errorhandler(ApplicationError)
    def handle_application_error(error: ApplicationError):
        return jsonify({"error": error.message}), error.status_code

    services.runtime = EdgeRuntime(services)
    services.runtime.start()
    return app
