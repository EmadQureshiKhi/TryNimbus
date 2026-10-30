"""Entry point: `python -m nimbus` (or the `nimbus` script)."""
from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

from nimbus import config


def _setup_logging() -> None:
    logs = config.data_dir("logs")
    handlers: list[logging.Handler] = [RotatingFileHandler(logs / "nimbus.log", maxBytes=2_000_000, backupCount=3),
                                       logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    for noisy in ("httpx", "httpcore", "openai", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Native crashes (Qt, CoreAudio callbacks) bypass logging: dump their Python stacks to a file.
    faulthandler.enable(open(logs / "crash.log", "a"), all_threads=True)  # noqa: SIM115 - lives as long as the process
    log = logging.getLogger("nimbus")
    sys.excepthook = lambda kind, exc, tb: log.critical("uncaught exception", exc_info=(kind, exc, tb))


def main() -> None:
    _setup_logging()
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from nimbus.app import NimbusApp

    qt = QApplication(sys.argv)
    qt.setApplicationName("Nimbus")
    qt.setQuitOnLastWindowClosed(False)
    app = NimbusApp(qt)
    app.start()
    log = logging.getLogger("nimbus")
    log.info("Nimbus started (pid %s)", os.getpid())
    qt.aboutToQuit.connect(lambda: log.info("Nimbus quitting"))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: (log.info("Nimbus received SIGTERM"), app.quit()))
    tick = QTimer()  # let Python see Ctrl+C while Qt's loop runs
    tick.start(250)
    tick.timeout.connect(lambda: None)
    code = qt.exec()
    log.info("Nimbus exited with code %s", code)
    sys.exit(code)


if __name__ == "__main__":
    main()
