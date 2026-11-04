"""Gunicorn entry point: gunicorn wsgi:app, with one worker (see deploy/encore.service).

One worker, because the queue, the background jobs and the search results the page picks from
live in memory. Many threads instead, so a slow MusicBrainz call never holds up the page.
"""
import logging

from encore.web import create_app, start_workers

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
app = create_app()
start_workers(app)
