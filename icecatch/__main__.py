"""Entry point: python3 -m icecatch"""
import logging
import os
import signal
import sys
import threading

from . import __version__, config, db
from .recorder import Manager
from .server import Server


def main():
    logging.basicConfig(
        level=os.environ.get("ICECATCH_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("icecatch")
    db.init()
    manager = Manager()
    manager.start()
    server = Server((config.HOST, config.PORT), manager)
    log.info("IceCatch %s running: http://%s:%s  (data: %s)",
             __version__, config.HOST, config.PORT, config.DATA_DIR)

    def stop(signum, frame):
        log.info("Shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        manager.shutdown()
        log.info("Stopped")


if __name__ == "__main__":
    main()
