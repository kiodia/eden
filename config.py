'''
Config of the Eden FastAPI with a simple dictionary and .env for the secret keys

Same pattern as the events project: run mode is resolved from the OS
(Windows -> TESTING for local dev, Linux -> PROD for the VPS) and can be
forced with the MODE environment variable.

Interactive Documentation:

FastAPI automatically generates interactive documentation:

    Swagger UI: http://localhost:8000/docs

    ReDoc: http://localhost:8000/redoc

@author: vankomme
'''

import os
import platform
import logging
import logging.handlers as handlers
import sys

# Supported run modes:
#   TESTING -> local development on Windows
#   PROD    -> deployment on the Linux VPS
TESTING = "TESTING"
PROD = "PROD"

# global flags (runtime configuration, populated by init_config)
flags = {}


def resolve_mode():
    """Resolve the run mode (TESTING or PROD).

    An explicit ``MODE`` environment variable always wins, so the mode can be
    forced on either OS (e.g. running TESTING on the Linux box). When it is not
    set the mode is inferred from the OS: Windows -> TESTING (local dev),
    Linux -> PROD (the VPS).
    """
    mode = os.getenv("MODE", "").strip().upper()
    if mode in (TESTING, PROD):
        return mode

    system = platform.system()
    if system == "Windows":
        return TESTING
    elif system == "Linux":
        return PROD
    raise Exception(
        f"Error, unknown OS '{system}' not supported. Set MODE=TESTING|PROD to override."
    )


def init_config():

    mode = resolve_mode()
    flags['mode'] = mode

    if mode == TESTING:
        # Local development on Windows
        flags['log_file'] = r'C:\temp\python_eden.log'
        flags['log_level'] = logging.DEBUG
        flags['debug'] = True
    else:
        # PROD: deployment on the Linux VPS
        flags['log_file'] = "/home/angel/logs/python_eden.log"
        flags['log_level'] = logging.INFO
        flags['debug'] = False

    # Make sure the log directory exists before the file handler opens it,
    # so a fresh Windows/Linux box does not crash on first start.
    log_dir = os.path.dirname(flags['log_file'])
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # Configure logging once at module level
    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        level=flags['log_level'],
        datefmt='%d-%m-%Y %H:%M:%S',
        handlers=[
            handlers.RotatingFileHandler(flags['log_file'], maxBytes=1048576, backupCount=7),
            logging.StreamHandler(sys.stdout)
        ],
        force=True   # <--- important
    )

    log = logging.getLogger(__name__)
    log.info(f"Configured for {platform.system()} in {mode} mode (log file: {flags['log_file']})")
    return mode
