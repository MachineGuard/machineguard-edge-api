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
    enable_delivery_worker: bool = True
    delivery_poll_seconds: float = field(default_factory=lambda: float(os.getenv('EDGE_DELIVERY_POLL_SECONDS', '1')))
    delivery_backoff_seconds: float = field(default_factory=lambda: float(os.getenv('EDGE_DELIVERY_BACKOFF_SECONDS', '2')))
    delivery_max_backoff_seconds: float = field(default_factory=lambda: float(os.getenv('EDGE_DELIVERY_MAX_BACKOFF_SECONDS', '60')))
    delivery_max_requests: int = field(default_factory=lambda: int(os.getenv('EDGE_DELIVERY_MAX_REQUESTS', '16')))
    delivery_max_depth: int = field(default_factory=lambda: int(os.getenv('EDGE_DELIVERY_MAX_DEPTH', '9')))
    delivery_http_timeout_seconds: float = field(default_factory=lambda: float(os.getenv('EDGE_DELIVERY_HTTP_TIMEOUT_SECONDS', '5')))
    delivery_lease_seconds: float = field(default_factory=lambda: float(os.getenv('EDGE_DELIVERY_LEASE_SECONDS', '30')))
    node_pause_seconds: float = field(default_factory=lambda: float(os.getenv('EDGE_NODE_PAUSE_SECONDS', '60')))
    offline_grace_ratio: float = field(default_factory=lambda: float(os.getenv('EDGE_OFFLINE_GRACE_RATIO', '0.25')))
    device_credential_expires_at: str = field(default_factory=lambda: os.getenv('EDGE_DEVICE_CREDENTIAL_EXPIRES_AT',''))
    credential_warning_seconds: int = field(default_factory=lambda: int(os.getenv('EDGE_CREDENTIAL_WARNING_SECONDS','604800')))
    port: int = field(default_factory=lambda: int(os.getenv('EDGE_PORT','5000')))
    wsgi_threads: int = field(default_factory=lambda: int(os.getenv('EDGE_WSGI_THREADS','4')))
    thingspeak_enabled: bool = field(default_factory=lambda: os.getenv('THINGSPEAK_ENABLED','false').lower()=='true')
    thingspeak_device_code: str = field(default_factory=lambda: os.getenv('THINGSPEAK_DEVICE_CODE',''))
    thingspeak_channel_id: str = field(default_factory=lambda: os.getenv('THINGSPEAK_CHANNEL_ID',''))
    thingspeak_write_key: str = field(default_factory=lambda: os.getenv('THINGSPEAK_WRITE_KEY',''),repr=False)
    thingspeak_window_seconds: int = field(default_factory=lambda: os.getenv('THINGSPEAK_WINDOW_SECONDS','20'))
    thingspeak_capacity: int = field(default_factory=lambda: os.getenv('THINGSPEAK_CAPACITY','1000'))
    thingspeak_max_samples: int = field(default_factory=lambda: os.getenv('THINGSPEAK_MAX_SAMPLES','500'))
    thingspeak_scan_size: int = field(default_factory=lambda: os.getenv('THINGSPEAK_SCAN_SIZE','100'))
    thingspeak_poll_seconds: float = field(default_factory=lambda: os.getenv('THINGSPEAK_POLL_SECONDS','1'))
    thingspeak_backoff_seconds: float = field(default_factory=lambda: os.getenv('THINGSPEAK_BACKOFF_SECONDS','20'))
    thingspeak_max_backoff_seconds: float = field(default_factory=lambda: os.getenv('THINGSPEAK_MAX_BACKOFF_SECONDS','300'))
    thingspeak_http_seconds: float = field(default_factory=lambda: os.getenv('THINGSPEAK_HTTP_SECONDS','5'))

    def thingspeak_configuration(self):
        # Invalid optional configuration disables only the secondary integration.
        if not self.thingspeak_enabled:return 'DISABLED'
        if not all((self.thingspeak_device_code,self.thingspeak_channel_id,self.thingspeak_write_key)):
            return 'MISSING_CONFIGURATION'
        try:
            for name,minimum,maximum,convert in [
                ('window_seconds',20,86400,int),('capacity',1,10000,int),('max_samples',1,500,int),
                ('scan_size',1,500,int),('poll_seconds',.1,60,float),('backoff_seconds',20,86400,float),
                ('max_backoff_seconds',20,86400,float),('http_seconds',.2,5,float)]:
                value=convert(getattr(self,'thingspeak_'+name))
                if not math.isfinite(value) or not minimum<=value<=maximum:raise ValueError()
                setattr(self,'thingspeak_'+name,value)
            if self.thingspeak_max_backoff_seconds<self.thingspeak_backoff_seconds:raise ValueError()
            if not re.fullmatch(r'[1-9][0-9]{0,19}',self.thingspeak_channel_id):raise ValueError()
            if not 1<=len(self.thingspeak_device_code)<=100 or not self.thingspeak_device_code.isprintable():raise ValueError()
            if not 16<=len(self.thingspeak_write_key)<=256 or any(ord(c)<33 or ord(c)>126 for c in self.thingspeak_write_key):raise ValueError()
        except (ValueError,TypeError,OverflowError):return 'INVALID_CONFIGURATION'
        return 'READY'

    def validate(self) -> None:
        if not 1 <= self.port <= 65535 or not 1 <= self.wsgi_threads <= 32 or not 1 <= self.credential_warning_seconds <= 31622400:
            raise ValueError('Invalid Edge server or credential warning configuration')
        try:
            self.credential_expiration
        except (ValueError,OverflowError,TypeError):
            raise ValueError('EDGE_DEVICE_CREDENTIAL_EXPIRES_AT requires an ISO-8601 date with explicit timezone') from None
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
        if self.max_future_seconds > 86400 or self.max_reading_age_days > 36600:
            raise ValueError('Edge capture time limits exceed supported age/time bounds')
        positive = (self.delivery_poll_seconds,self.delivery_backoff_seconds,self.delivery_max_backoff_seconds,
                    self.node_pause_seconds,self.delivery_http_timeout_seconds,self.delivery_lease_seconds)
        if (any(not math.isfinite(v) or v <= 0 for v in positive)
                or self.delivery_max_backoff_seconds < self.delivery_backoff_seconds
                or not 1 <= self.delivery_max_requests <= 64 or not 1 <= self.delivery_max_depth <= 9
                or not math.isfinite(self.offline_grace_ratio) or not 0 <= self.offline_grace_ratio <= 2):
            raise ValueError('Invalid delivery or offline tolerance configuration')
        if not self.delivery_http_timeout_seconds <= 5 or not 2*self.delivery_http_timeout_seconds+1 <= self.delivery_lease_seconds <= 300:
            raise ValueError('Delivery lease must exceed HTTP timeouts and remain at most 300 seconds')
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

    @property
    def credential_expiration(self):
        if not self.device_credential_expires_at:
            return None
        value=datetime.fromisoformat(self.device_credential_expires_at.replace('Z','+00:00'))
        if value.tzinfo is None:
            raise ValueError('Credential expiry requires a timezone')
        return value.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_ip(value):
    address = ip_address(value)
    return address.ipv4_mapped or address if isinstance(address, IPv6Address) else address
