"""Development server: python -m encore (from the repo root). The service runs gunicorn instead."""
import logging

from .config import PORT
from .web import create_app, start_workers

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
app = create_app()
start_workers(app)
app.run(host="0.0.0.0", port=PORT, threaded=True)
