"""Käynnistys: python3 -m arkisto"""
import logging
import os
import signal
import sys
import threading

from . import config, db
from .recorder import Manager
from .server import Server


def main():
    logging.basicConfig(
        level=os.environ.get("ARKISTO_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("arkisto")
    db.init()
    manager = Manager()
    manager.start()
    server = Server((config.HOST, config.PORT), manager)
    log.info("Stream-arkisto käynnissä: http://%s:%s  (data: %s)", config.HOST, config.PORT, config.DATA_DIR)

    def stop(signum, frame):
        log.info("Sammutetaan...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        manager.shutdown()
        log.info("Sammutettu")


if __name__ == "__main__":
    main()
