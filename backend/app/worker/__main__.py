"""``python -m app.worker`` (``make worker``). One process, one engine, one loop."""

import logging
import signal

from app.core.config import get_settings, require_object_store_settings
from app.core.db import create_app_engine
from app.worker.runner import Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    require_object_store_settings(settings)
    engine = create_app_engine(production=True)
    worker = Worker(engine)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop())
    try:
        worker.run_forever()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
