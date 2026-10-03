import os
from dataclasses import dataclass, field


@dataclass
class Config:
    database_path: str = field(default_factory=lambda: os.getenv("EDGE_DATABASE_PATH", "edge.db"))
    gateway_id: str = field(default_factory=lambda: os.getenv("EDGE_GATEWAY_ID", "gateway-001"))
    cloud_base_url: str = field(default_factory=lambda: os.getenv("MACHINEGUARD_API_URL", ""))
    device_credential: str = field(default_factory=lambda: os.getenv("EDGE_DEVICE_CREDENTIAL", ""))
    buffer_capacity: int = field(default_factory=lambda: int(os.getenv("EDGE_BUFFER_CAPACITY", "10000")))
    sync_batch_size: int = field(default_factory=lambda: int(os.getenv("EDGE_SYNC_BATCH_SIZE", "100")))
    default_sampling_interval: int = field(default_factory=lambda: int(os.getenv("EDGE_SAMPLING_INTERVAL", "60")))
    temperature_range: tuple = (-40.0, 80.0)
    humidity_range: tuple = (0.0, 100.0)
    availability_check_seconds: int = field(default_factory=lambda: int(os.getenv("EDGE_AVAILABILITY_CHECK_SECONDS", "30")))
    enable_availability_loop: bool = True
