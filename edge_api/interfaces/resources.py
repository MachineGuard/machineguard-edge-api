from datetime import datetime, timezone
from typing import Optional
import math

from ..application.errors import ValidationError
from ..domain.model import CalibrationProfile, LocalBuffer, OfflineNode, SensorReading


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z")


def parse_instant(value) -> Optional[datetime]:
    """Convierte un ISO-8601 a datetime UTC naive (la base local guarda todo en UTC)."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError("capturedAt debe ser una fecha ISO-8601")
    return parsed


def require_number(body: dict, field: str, default: Optional[float] = None) -> float:
    value = body.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} es obligatorio y debe ser numérico")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValidationError(f'{field} debe ser finito')
    if not math.isfinite(result):
        raise ValidationError(f'{field} debe ser finito')
    return result


def require_integer(body, field, default, minimum=1, maximum=2147483647):
    value = body.get(field, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f'{field} debe ser un entero entre {minimum} y {maximum}')
    return value


def require_device_code(body):
    code = body.get('deviceCode')
    if (not isinstance(code, str) or not code.strip() or code != code.strip()
            or len(code) > 100 or not code.isprintable()):
        raise ValidationError('deviceCode debe ser texto no vacío de hasta 100 caracteres')
    return code


def raw_reading_resource_to_values(body: dict) -> dict:
    """RawReadingResource -> argumentos de CaptureSensorReadingCommand."""
    device_code = require_device_code(body)
    return {
        "device_code": device_code,
        "temperature": require_number(body, "temperature"),
        "humidity": require_number(body, "humidity"),
        "captured_at": parse_instant(body.get("capturedAt")),
    }


def calibrated_reading_resource(r: SensorReading) -> dict:
    return {
        "id": r.id,
        "deviceCode": r.sensor_node_id,
        "rawTemperature": r.raw.temperature,
        "rawHumidity": r.raw.humidity,
        "calibratedTemperature": r.calibrated_temperature,
        "calibratedHumidity": r.calibrated_humidity,
        "capturedAt": iso(r.raw.captured_at),
        "status": r.status.value,
    }


def calibration_profile_resource(p: CalibrationProfile) -> dict:
    return {
        "deviceCode": p.sensor_node_id,
        "temperatureOffset": p.offset.temperature_offset,
        "humidityOffset": p.offset.humidity_offset,
        "temperatureRange": {"min": p.temperature_range.minimum_value, "max": p.temperature_range.maximum_value},
        "humidityRange": {"min": p.humidity_range.minimum_value, "max": p.humidity_range.maximum_value},
        "samplingIntervalSeconds": p.sampling_interval.seconds,
        "calibratedAt": iso(p.calibrated_at),
        "updatedAt": iso(p.updated_at),
    }


def local_buffer_status_resource(b: LocalBuffer) -> dict:
    return {
        "gatewayId": b.gateway_id,
        "capacity": b.capacity,
        "pendingCount": b.pending_count,
        "oldestPendingAt": iso(b.oldest_pending_at),
        "lastSyncAt": iso(b.last_sync_at),
        "status": b.status.value,
    }


def offline_node_resource(n: OfflineNode) -> dict:
    return {
        "deviceCode": n.sensor_node_id,
        "lastSeenAt": iso(n.last_seen_at),
        "expectedSamplingIntervalSeconds": n.expected_sampling_interval,
        "detectedAt": iso(n.detected_at),
    }


def sensor_node_status_resource(item: dict) -> dict:
    profile: CalibrationProfile = item["profile"]
    return {
        "deviceCode": profile.sensor_node_id,
        "samplingIntervalSeconds": profile.sampling_interval.seconds,
        "lastSeenAt": iso(item["lastSeenAt"]),
        "status": "OFFLINE" if item["offline"] else "ONLINE",
    }
