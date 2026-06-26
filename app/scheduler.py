import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from app.uspto_importer import run_uspto_import

logger = logging.getLogger("scheduler")

# Initialize BackgroundScheduler
scheduler = BackgroundScheduler()

def trigger_manual_import():
    """Triggers the USPTO import asynchronously in a separate background thread."""
    thread = threading.Thread(target=run_uspto_import)
    thread.daemon = True
    thread.start()
    logger.info("Manual USPTO import thread started.")

def start_scheduler():
    """Starts the cron scheduler for automated weekly syncs."""
    # Schedule weekly USPTO import: every Sunday at 02:00 AM
    scheduler.add_job(
        run_uspto_import,
        'cron',
        day_of_week='sun',
        hour=2,
        minute=0,
        id='uspto_weekly_import',
        replace_existing=True
    )
    
    # Start scheduler if not already running
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started successfully.")
