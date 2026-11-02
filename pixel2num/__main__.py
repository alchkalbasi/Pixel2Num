import fcntl
import logging
import signal
import threading
from pathlib import Path

from .bot import Bot
from .config import Config


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    # One worker per state directory. Linux/Docker is the supported runtime.
    with open(Path(config.data_dir) / "bot.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        bot = Bot(config)
        logging.info("Bot connected; polling direct messages")
        try:
            while not stop.is_set():
                try:
                    bot.run_once()
                except Exception as error:
                    logging.error("Polling cycle failed (%s); will retry", type(error).__name__)
                stop.wait(config.poll_seconds)
        finally:
            bot.db.close()


if __name__ == "__main__":
    main()
