"""One stoppable runtime per process; the database lease also excludes other processes."""
import atexit
import logging
import threading

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
        self.availability_thread = None
        self.secondary_thread = None

    def start(self):
        global _runtime
        _runtime = self
        config = self.services.config
        if not (config.enable_delivery_worker or config.enable_availability_loop or self.services.thingspeak.code=='READY'):
            return
        self.services.sync.cancelled = self.stopped.is_set
        self.services.thingspeak.cancelled = self.stopped.is_set
        if self.services.thingspeak.code=='READY':
            self.secondary_thread=threading.Thread(target=self._secondary_loop,name='thingspeak-delivery',daemon=True)
            self.secondary_thread.start()
        if config.enable_delivery_worker:
            self.thread = threading.Thread(target=self._loop,name='edge-delivery',daemon=True)
            self.thread.start()
        if config.enable_availability_loop:
            self.availability_thread = threading.Thread(target=self._availability_loop,name='node-availability',daemon=True)
            self.availability_thread.start()
        atexit.register(self.stop)

    def stop(self):
        self.stopped.set()
        for thread in (self.thread,self.availability_thread,self.secondary_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=12)
                if thread.is_alive():
                    raise RuntimeError('Edge worker did not stop; database cannot be replaced')

    def _secondary_loop(self):
        while not self.stopped.wait(self.services.config.thingspeak_poll_seconds):
            try:self.services.thingspeak.cycle()
            except Exception:
                logger.warning('Secondary cycle failed; durable work retained for review/recovery')
            finally:
                if not database.is_closed():database.close()

    def _loop(self):
        config = self.services.config
        while not self.stopped.wait(config.delivery_poll_seconds):
            try:
                if config.enable_delivery_worker:
                    self.services.sync.handle(SyncLocalBufferCommand(config.sync_batch_size),force=False)
            except Exception:
                logger.warning('Edge background cycle failed; durable pending readings retained')
            finally:
                if not database.is_closed():
                    database.close()

    def _availability_loop(self):
        while not self.stopped.wait(self.services.config.availability_check_seconds):
            try:
                self.services.detect_offline.handle()
            except Exception:
                logger.warning('Edge availability cycle failed')
            finally:
                if not database.is_closed():
                    database.close()
