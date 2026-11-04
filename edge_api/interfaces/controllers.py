import hashlib
import hmac
from flask import Blueprint, jsonify, request, g
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from ..application.errors import ValidationError
from ..application.node_credentials import AuthenticationError, ForbiddenError
from ..domain.commands import (
    ApplyCalibrationCommand,
    CaptureSensorReadingCommand,
    GetCalibrationProfileQuery,
    GetLocalBufferQuery,
    RegisterSensorNodeCommand,
    SyncLocalBufferCommand,
)
from . import resources as res


def _json_body(optional=False) -> dict:
    if optional and not request.get_data():
        return {}
    try:
        body = request.get_json()
    except (BadRequest, UnsupportedMediaType):
        raise ValidationError('El cuerpo debe ser un JSON válido')
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

    @bp.before_request
    def authenticate():
        if request.endpoint != 'edge.register_reading' and not c.config.allows_admin_address(request.remote_addr):
            raise ForbiddenError('Edge administration is not allowed from this address')
        node_key = request.headers.get('X-Node-Credential')
        admin_key = request.headers.get('X-Edge-Admin-Key')
        if node_key is not None and admin_key is not None:
            raise AuthenticationError('Do not combine node and administrative credentials')
        if request.endpoint == 'edge.register_reading':
            if admin_key is not None:
                raise ForbiddenError('A node credential is required')
            g.node_device_code = c.credentials.authenticate(node_key)
        else:
            # New blueprint routes are administrative unless explicitly allowed above.
            if node_key is not None:
                raise ForbiddenError('Node credentials cannot administer Edge')
            if (not admin_key or len(admin_key) > 4096 or not hmac.compare_digest(
                    hashlib.sha256(admin_key.encode()).digest(), hashlib.sha256(c.config.admin_key.encode()).digest())):
                raise AuthenticationError('Invalid administrative credential')
            g.admin_actor = c.config.admin_id

    # SensorReadingController
    @bp.post("/readings")
    def register_reading():
        values = res.raw_reading_resource_to_values(_json_body())
        if values['device_code'] != g.node_device_code:
            raise ForbiddenError('Credential does not authorize this deviceCode')
        reading = c.capture.handle(CaptureSensorReadingCommand(**values))
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
            ), actor=g.admin_actor
        )
        return jsonify(res.calibration_profile_resource(profile))

    @bp.get('/calibration/<device_code>/changes')
    def calibration_changes(device_code):
        profile = c.calibration_query.handle(GetCalibrationProfileQuery(device_code))
        return jsonify([{'actor': row.actor, 'changedAt': res.iso(row.changed_at),
            'oldTemperatureOffset': row.old_temperature_offset, 'oldHumidityOffset': row.old_humidity_offset,
            'newTemperatureOffset': row.new_temperature_offset, 'newHumidityOffset': row.new_humidity_offset}
            for row in c.calibration_changes.list_for_profile(profile.id, _limit())])

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
        body = _json_body(optional=True)
        batch_size = res.require_integer(body, 'batchSize', c.config.sync_batch_size, maximum=500)
        return jsonify(c.sync.handle(SyncLocalBufferCommand(batch_size)))

    # NodeStatusController
    @bp.post("/sensor-nodes")
    def register_node():
        body = _json_body()
        profile, credential = c.register_node.handle(
            RegisterSensorNodeCommand(
                device_code=res.require_device_code(body),
                sampling_interval_seconds=res.require_integer(body, 'samplingIntervalSeconds', c.config.default_sampling_interval),
                temperature_offset=res.require_number(body, "temperatureOffset", 0.0),
                humidity_offset=res.require_number(body, "humidityOffset", 0.0),
            ), actor=g.admin_actor
        )
        return jsonify({**res.calibration_profile_resource(profile), 'nodeCredential': credential}), 201, {'Cache-Control': 'no-store'}

    @bp.post('/sensor-nodes/<device_code>/credentials')
    def rotate_node_credential(device_code):
        return jsonify({'deviceCode': device_code, 'nodeCredential': c.credentials.issue(device_code)}), 200, {'Cache-Control': 'no-store'}

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
        return jsonify({"status": "UP", "gatewayId": c.config.gateway_id, 'delivery':_dates(c.sync.health())})

    @bp.get('/quarantine')
    def quarantine():
        try:
            offset = int(request.args.get('offset',0))
            if offset < 0:
                raise ValueError()
        except ValueError:
            raise ValidationError('offset debe ser un entero no negativo')
        result = c.sync.quarantine(_limit(),offset)
        for item in result['items']:
            item['reading'] = res.calibrated_reading_resource(item['reading'])
        return jsonify(_dates(result))

    @bp.post('/quarantine/<int:quarantine_id>/reprocess')
    def reprocess(quarantine_id):
        c.sync.reprocess(quarantine_id,g.admin_actor)
        return jsonify({'queued':True}),202

    return bp


def _dates(value):
    from datetime import datetime
    if isinstance(value,datetime):
        return res.iso(value)
    if isinstance(value,dict):
        return {key:_dates(item) for key,item in value.items()}
    if isinstance(value,list):
        return [_dates(item) for item in value]
    if isinstance(value,float) and not __import__('math').isfinite(value):
        return str(value)  # Legacy nonfinite evidence is represented explicitly, never invalid JSON.
    return value
