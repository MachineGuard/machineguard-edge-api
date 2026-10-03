from flask import Blueprint, jsonify, request

from ..application.errors import ValidationError
from ..domain.commands import (
    ApplyCalibrationCommand,
    CaptureSensorReadingCommand,
    GetCalibrationProfileQuery,
    GetLocalBufferQuery,
    RegisterSensorNodeCommand,
    SyncLocalBufferCommand,
)
from . import resources as res


def _json_body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValidationError("El cuerpo debe ser un JSON válido")
    return body


def _limit(default: int = 50) -> int:
    try:
        return max(1, min(int(request.args.get("limit", default)), 500))
    except ValueError:
        raise ValidationError("limit debe ser un entero")


def build_blueprint(c) -> Blueprint:
    """`c` es el contenedor de servicios armado en create_app."""
    bp = Blueprint("edge", __name__, url_prefix="/api/v1/edge")

    # SensorReadingController
    @bp.post("/readings")
    def register_reading():
        reading = c.capture.handle(CaptureSensorReadingCommand(**res.raw_reading_resource_to_values(_json_body())))
        status = 201 if reading.status.value != "DISCARDED" else 202
        return jsonify(res.calibrated_reading_resource(reading)), status

    @bp.get("/readings")
    def latest_readings():
        return jsonify([res.calibrated_reading_resource(r) for r in c.reading_queries.latest(_limit())])

    @bp.get("/readings/discarded")
    def discarded_readings():
        return jsonify([res.calibrated_reading_resource(r) for r in c.reading_queries.discarded(_limit())])

    # CalibrationController
    @bp.get("/calibration/<device_code>")
    def get_calibration(device_code):
        profile = c.calibration_query.handle(GetCalibrationProfileQuery(device_code))
        return jsonify(res.calibration_profile_resource(profile))

    @bp.put("/calibration/<device_code>/offset")
    def update_offset(device_code):
        body = _json_body()
        profile = c.apply_calibration.handle(
            ApplyCalibrationCommand(
                device_code,
                res.require_number(body, "temperatureOffset"),
                res.require_number(body, "humidityOffset"),
            )
        )
        return jsonify(res.calibration_profile_resource(profile))

    @bp.get("/calibration/<device_code>/plausibility-range")
    def get_plausibility_range(device_code):
        profile = res.calibration_profile_resource(c.calibration_query.handle(GetCalibrationProfileQuery(device_code)))
        return jsonify({"temperatureRange": profile["temperatureRange"], "humidityRange": profile["humidityRange"]})

    # LocalBufferController
    @bp.get("/buffer")
    def buffer_status():
        return jsonify(res.local_buffer_status_resource(c.buffer_query.handle(GetLocalBufferQuery())))

    @bp.post("/buffer/sync")
    def sync_buffer():
        body = request.get_json(silent=True) or {}
        batch_size = int(body.get("batchSize", c.config.sync_batch_size))
        return jsonify(c.sync.handle(SyncLocalBufferCommand(batch_size)))

    # NodeStatusController
    @bp.post("/sensor-nodes")
    def register_node():
        body = _json_body()
        profile = c.register_node.handle(
            RegisterSensorNodeCommand(
                device_code=str(body.get("deviceCode", "")),
                sampling_interval_seconds=int(body.get("samplingIntervalSeconds", c.config.default_sampling_interval)),
                temperature_offset=res.require_number(body, "temperatureOffset", 0.0),
                humidity_offset=res.require_number(body, "humidityOffset", 0.0),
            )
        )
        return jsonify(res.calibration_profile_resource(profile)), 201

    @bp.get("/sensor-nodes")
    def nodes_status():
        c.detect_offline.handle()
        return jsonify([res.sensor_node_status_resource(i) for i in c.node_queries.all()])

    @bp.get("/sensor-nodes/offline")
    def offline_nodes():
        c.detect_offline.handle()
        return jsonify([res.offline_node_resource(n) for n in c.node_queries.offline()])

    @bp.get("/health")
    def health():
        return jsonify({"status": "UP", "gatewayId": c.config.gateway_id})

    return bp
