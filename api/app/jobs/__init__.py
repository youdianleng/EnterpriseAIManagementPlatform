"""Scheduled work.

Each module here is a command first: `python -m app.jobs.<name>` runs one pass and
exits, which is what a cron entry or a systemd timer calls. Anything these modules
decide lives in `app/domain/`; what is here is assembly, the run loop and the
logging, which is all a worker is allowed to be.
"""
