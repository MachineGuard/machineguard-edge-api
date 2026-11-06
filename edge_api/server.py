"""Operational WSGI entry point: python -m edge_api.server."""
import threading

from waitress import create_server, wasyncore
from . import create_app
from .config import Config
from .infrastructure.models import database


class EdgeServer:
    def __init__(self, config=None):
        self.config=config or Config()
        self.app=create_app(self.config)
        self._lock=threading.Lock()
        self._closed=False
        self._stopping=threading.Event()
        self._running=False
        try:
            self.server=create_server(self.app,host=self.config.bind_host,port=self.config.port,
                                      threads=self.config.wsgi_threads,asyncore_loop_timeout=.2)
        except Exception:
            self.app.config['services'].runtime.stop()
            raise

    def run(self):
        with self._lock:
            if self._closed:return
            self._running=True
        try:
            # Close sockets on the polling thread, after select returns (Windows-safe).
            while not self._stopping.is_set():
                channels=self.server._map if hasattr(self.server,'_map') else self.server.map
                wasyncore.loop(timeout=.2,map=channels,count=1)
        finally:
            with self._lock:self._running=False
            self.close()

    def close(self):
        self._stopping.set()
        with self._lock:
            if self._closed or self._running:return
            self._closed=True
            try:
                self.app.config['services'].runtime.stop()
            finally:
                # Waitress 3.0.2 single-socket close does not stop its task dispatcher.
                self.server.task_dispatcher.shutdown(timeout=12)
                if hasattr(self.server,'_map'):
                    wasyncore.close_all(self.server._map)
                else:
                    self.server.close()
                if not database.is_closed():database.close()


def main():
    EdgeServer().run()


if __name__=='__main__':
    main()
