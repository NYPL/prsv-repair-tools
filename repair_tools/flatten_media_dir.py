import argparse
import logging
import shutil
import datetime
from pathlib import Path
from repair_tools.utils.logger_setup import setup_logging

# Setup basic logging to catch early errors before file logging is initialized
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(
        description="Flatten media directories by moving contents of 'Video', 'Film', or 'Audio' subdirectories up one level."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        nargs="+",
        help="Path(s) to directory/directories to process (e.g. /Volumes/amip/pass/0_linting/*)",
    )
    return parser.parse_args()

def get_size(path: Path) -> int:
    """Get the size of a file or directory in bytes."""
    if path.is_file():
        return path.stat().st_size
    elif path.is_dir():
        return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    return 0

def flatten_directory(directory: Path) -> dict:
    """
    Flattens the directory and returns a status dict tracking issues.
    """
    stats = {"duplicates": 0, "errors": 0}
    
    if not directory.is_dir():
        logger.warning(f"Skipping {directory}, not a directory.")
        stats["errors"] += 1
        return stats

    target_subs = {"Video", "Film", "Audio", "CD", "Data", "DVD", "VCD"}
    
    for sub_name in target_subs:
        sub_path = directory / sub_name
        if sub_path.is_dir():
            logger.info(f"Processing {sub_path}...")
            
            for item in sub_path.iterdir():
                dest = directory / item.name
                
                if dest.exists():
                    if item.name == ".DS_Store":
                        item.unlink()
                        logger.info(f"Deleted duplicate {item.name}")
                        continue
                    
                    stats["duplicates"] += 1
                    item_size = get_size(item)
                    dest_size = get_size(dest)
                    
                    if item_size > dest_size:
                        logger.info(f"Duplicate found: {item.name}. Source is larger ({item_size} bytes) than destination ({dest_size} bytes). Deleting destination and replacing.")
                        try:
                            if dest.is_dir():
                                shutil.rmtree(dest)
                            else:
                                dest.unlink()
                        except Exception as e:
                            logger.error(f"Failed to delete existing destination {dest}: {e}")
                            stats["errors"] += 1
                            continue
                    else:
                        logger.info(f"Duplicate found: {item.name}. Destination is larger or equal ({dest_size} bytes) than source ({item_size} bytes). Deleting source.")
                        try:
                            if item.is_dir():
                                shutil.rmtree(item)
                            else:
                                item.unlink()
                        except Exception as e:
                            logger.error(f"Failed to delete source duplicate {item}: {e}")
                            stats["errors"] += 1
                        continue
                        
                try:
                    shutil.move(str(item), str(dest))
                    logger.info(f"Moved {item.name} to {directory.resolve()}")
                except Exception as e:
                    logger.error(f"Failed to move {item} to {dest}: {e}")
                    stats["errors"] += 1
            
            # Confirm empty and delete
            try:
                # Check for remaining items (including hidden ones)
                remaining = list(sub_path.iterdir())
                if remaining:
                    logger.warning(f"Directory {sub_path} is not empty after moving contents. Remaining: {[f.name for f in remaining]}. Not deleting.")
                    stats["errors"] += 1
                else:
                    sub_path.rmdir()
                    logger.info(f"Deleted empty directory: {sub_path.resolve()}")
            except Exception as e:
                logger.error(f"Failed to handle directory cleanup for {sub_path}: {e}")
                stats["errors"] += 1
    
    return stats

def main():
    args = parse_args()
    
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"flatten_media_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    global logger
    logger, _ = setup_logging(log_file)
    
    total_stats = {"duplicates": 0, "errors": 0}
    
    for directory in args.directory:
        dir_stats = flatten_directory(directory)
        total_stats["duplicates"] += dir_stats["duplicates"]
        total_stats["errors"] += dir_stats["errors"]

    if total_stats["duplicates"] == 0 and total_stats["errors"] == 0:
        logger.info("Run successful with no duplicates or errors. Deleting log file.")
        logging.shutdown()
        log_file.unlink(missing_ok=True)
    else:
        logger.info(f"Run completed with {total_stats['duplicates']} duplicates and {total_stats['errors']} errors. Log file preserved at {log_file}")

if __name__ == "__main__":
    main()
