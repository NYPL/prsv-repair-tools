import logging
import sys
from pathlib import Path

def setup_logging(log_file: Path):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # prevents duplicate handlers
    if logger.hasHandlers():
        logger.handlers.clear()

    # logfile handler
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(str(log_file), mode='a')
    fh.setFormatter(log_file_formatter)
    logger.addHandler(fh)

    # console handler
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(console_formatter)
    logger.addHandler(ch)

    # list logger (for clean package lists)
    list_logger = logging.getLogger('list_logger')
    list_logger.setLevel(logging.INFO)
    
    if list_logger.hasHandlers():
        list_logger.handlers.clear()

    basic_formatter = logging.Formatter('%(message)s')
    bh = logging.StreamHandler(sys.stdout)
    bh.setFormatter(basic_formatter)
    list_logger.addHandler(bh)
    
    list_logger.propagate = False

    return logger, list_logger