"""One stoppable runtime per process; the database lease also excludes other processes."""
import atexit
import logging
import threading
import time

from ..domain.commands import SyncLocalBufferCommand
from ..infrastructure.models import database

_runtime = None
logger = logging.getLogger(__name__)


def stop_previous_runtime():
    if _runtime is not None:
        _runtime.stop()


class EdgeRuntime:
    def __init__(self, services):
        self.services = services
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        global _runtime
        _runtime = self
        config = self.services.config
        if not (config.enable_delivery_worker or config.enable_availability_loop):
            return
        self.services.sync.cancelled = self.stopped.is_set
        self.thread = threading.Thread(target=self._loop,name='edge-delivery',daemon=True)
        self.thread.start()
        atexit.register(self.stop)

    def stop(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                raise RuntimeError('Edge worker did not stop; database cannot be replaced')

    def _loop(self):
        config = self.services.config
        next_availability = time.monotonic()+config.availability_check_seconds
        while not self.stopped.wait(config.delivery_poll_seconds):
            try:
                if config.enable_delivery_worker:
                    self.services.sync.handle(SyncLocalBufferCommand(config.sync_batch_size),force=False)
                if config.enable_availability_loop and time.monotonic() >= next_availability:
                    self.services.detect_offline.handle()
                    next_availability = time.monotonic()+config.availability_check_seconds
            except Exception:
                logger.warning('Edge background cycle failed; durable pending readings retained')
            finally:
                if not database.is_closed():
                    database.close()
