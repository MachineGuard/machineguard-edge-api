import os
import math
import re
from ipaddress import ip_address, IPv6Address
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Config:
    database_path: str = field(default_factory=lambda: os.getenv("EDGE_DATABASE_PATH", "edge.db"))
    gateway_id: str = field(default_factory=lambda: os.getenv("EDGE_GATEWAY_ID", "gateway-001"))
    cloud_base_url: str = field(default_factory=lambda: os.getenv("MACHINEGUARD_API_URL", ""))
    device_credential: str = field(default_factory=lambda: os.getenv("EDGE_DEVICE_CREDENTIAL", ""), repr=False)
    admin_key: str = field(default_factory=lambda: os.getenv("EDGE_ADMIN_KEY", ""), repr=False)
    admin_id: str = field(default_factory=lambda: os.getenv("EDGE_ADMIN_ID", "local-admin"))
    admin_allowed_ips: tuple = field(default_factory=lambda: tuple(
        value.strip() for value in os.getenv('EDGE_ADMIN_ALLOWED_IPS', '').split(',') if value.strip()))
    bind_host: str = field(default_factory=lambda: os.getenv("EDGE_BIND_HOST", "127.0.0.1"))
    max_content_bytes: int = field(default_factory=lambda: int(os.getenv("EDGE_MAX_CONTENT_BYTES", "65536")))
    max_temperature_offset: float = field(default_factory=lambda: float(os.getenv("EDGE_MAX_TEMPERATURE_OFFSET", "10")))
    max_humidity_offset: float = field(default_factory=lambda: float(os.getenv("EDGE_MAX_HUMIDITY_OFFSET", "20")))
    max_future_seconds: int = field(default_factory=lambda: int(os.getenv('EDGE_MAX_FUTURE_SECONDS', '60')))
    max_reading_age_days: int = field(default_factory=lambda: int(os.getenv('EDGE_MAX_READING_AGE_DAYS', '30')))
    min_captured_at: str = field(default_factory=lambda: os.getenv('EDGE_MIN_CAPTURED_AT', '2026-01-01T00:00:00Z'))
    buffer_capacity: int = field(default_factory=lambda: int(os.getenv("EDGE_BUFFER_CAPACITY", "10000")))
    sync_batch_size: int = field(default_factory=lambda: int(os.getenv("EDGE_SYNC_BATCH_SIZE", "100")))
    default_sampling_interval: int = field(default_factory=lambda: int(os.getenv("EDGE_SAMPLING_INTERVAL", "60")))
    temperature_range: tuple = field(default_factory=lambda: (
        float(os.getenv("EDGE_TEMPERATURE_MIN", "-40")), float(os.getenv("EDGE_TEMPERATURE_MAX", "80"))))
    humidity_range: tuple = field(default_factory=lambda: (
        float(os.getenv("EDGE_HUMIDITY_MIN", "0")), float(os.getenv("EDGE_HUMIDITY_MAX", "100"))))
    availability_check_seconds: int = field(default_factory=lambda: int(os.getenv("EDGE_AVAILABILITY_CHECK_SECONDS", "30")))
    enable_availability_loop: bool = True

    def validate(self) -> None:
        if self.admin_key == 'replace-with-at-least-32-random-bytes':
            raise ValueError('EDGE_ADMIN_KEY must not use the published example value')
        if not 32 <= len(self.admin_key.encode('utf-8')) <= 4096:
            raise ValueError('EDGE_ADMIN_KEY must contain 32 to 4096 bytes')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.admin_id):
            raise ValueError('EDGE_ADMIN_ID must be a stable administrative identifier')
        try:
            allowed = tuple(_canonical_ip(value) for value in self.admin_allowed_ips)
            if any(value.is_unspecified or value.is_multicast for value in allowed):
                raise ValueError('Not an administrative client address')
        except ValueError:
            raise ValueError('EDGE_ADMIN_ALLOWED_IPS must contain explicit IP addresses, not networks or wildcards') from None
        self.admin_allowed_ips = tuple(str(value) for value in allowed)
        if not self.bind_host.strip() or self.max_content_bytes < 1:
            raise ValueError('EDGE_BIND_HOST and EDGE_MAX_CONTENT_BYTES must be valid')
        if not 1 <= self.sync_batch_size <= 500 or self.default_sampling_interval < 1:
            raise ValueError('Invalid sampling interval or EDGE_SYNC_BATCH_SIZE (1..500)')
        if self.max_future_seconds < 0 or self.max_reading_age_days <= 0:
            raise ValueError('Capture time limits must be nonnegative (age must be positive)')
        try:
            timedelta(seconds=self.max_future_seconds)
            timedelta(days=self.max_reading_age_days)
            self.minimum_capture
        except (ValueError, OverflowError, TypeError):
            raise ValueError('Invalid Edge capture time configuration') from None
        limits = (self.max_temperature_offset, self.max_humidity_offset)
        if any(not math.isfinite(v) or v < 0 for v in limits):
            raise ValueError('Calibration offset limits must be finite and nonnegative')
        for bounds in (self.temperature_range, self.humidity_range):
            if len(bounds) != 2 or not all(math.isfinite(v) for v in bounds) or bounds[0] >= bounds[1]:
                raise ValueError('Physical ranges must have finite increasing bounds')

    def allows_admin_address(self, remote_addr) -> bool:
        try:
            address = _canonical_ip(remote_addr)
        except ValueError:
            return False
        return address.is_loopback or str(address) in self.admin_allowed_ips

    @property
    def minimum_capture(self) -> datetime:
        value = datetime.fromisoformat(self.min_captured_at.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError('EDGE_MIN_CAPTURED_AT requires an explicit timezone')
        return value.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_ip(value):
    address = ip_address(value)
    return address.ipv4_mapped or address if isinstance(address, IPv6Address) else address
