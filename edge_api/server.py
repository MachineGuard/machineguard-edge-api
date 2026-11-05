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
        try:
            self.server=create_server(self.app,host=self.config.bind_host,port=self.config.port,
                                      threads=self.config.wsgi_threads,asyncore_loop_timeout=.2)
        except Exception:
            self.app.config['services'].runtime.stop()
            raise

    def run(self):
        try:
            self.server.run()
        finally:
            self.close()

    def close(self):
        with self._lock:
            if self._closed:return
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
