import argparse
import logging
from pathlib import Path
import datetime

from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging

DELETION_LIST_PATH = Path("/Users/emileebuytkins/Documents/Buytkins_Programming/complete_reingest.txt")

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--credentials",
        type=str,
        required=True,
        help="which set of credentials to use",
        )
    
    parser.add_argument(
        "--pkgtitle",
        "-p",
        nargs='+',
        help="One or more titles of packages to find and move, separated by a space."
    )
    parser.add_argument(
        "--directory",
        "-d",
        type=Path,
        help="(Optional) Path to a directory containing multiple packages to move. Usually on _reingest folder after ingest validation"
    )
    parser.add_argument(
        "--use-file",
        action="store_true",
        help=f"Use the hardcoded package list from {DELETION_LIST_PATH.name}."
    )

    parser.add_argument(
        "--new-parent-ref",
        "-npf",
        required=True,
        help="The parentref of the new folder."
    )
    parser.add_argument(
        "--parent", 
        type=str,
        choices=["ingest", "digami", "digarch"],
        help="(Optional) Limit search to specific parent. If omitted, checks all."
    )
    parser.add_argument(
        "--logpath", 
        type=Path,
        help="Directory for log files."
    )
    return parser.parse_args()

def process_move_list(credentials: str, pkg_list: list, new_parent_ref: str, limit_parent: str = None, existing_logger=None):
    logger = existing_logger if existing_logger else logging.getLogger(__name__)
    
    api = PreservicaAPI(credentials)

    # define search locations
    search_locations = {
        "INGEST": api.ingest_uuid,
        "DigAMI": api.ami_uuid,
        "DigArch": api.digarch_uuid
    }

    if limit_parent:
        key_map = {"ingest": "INGEST", "digami": "DigAMI", "digarch": "DigArch"}
        if limit_parent in key_map:
            search_locations = {key_map[limit_parent]: search_locations[key_map[limit_parent]]}

    failed_moves = set()
    successful_moves = set()
    deletion_exists = set()

    logger.info(f"Starting move workflow for {len(pkg_list)} packages.")

    for pkg_title in pkg_list:
        logger.info(f"--- Processing: {pkg_title} ---")
        
        # 1. check already in deletion folder
        if api.fetch_uuid_by_title(pkg_title, new_parent_ref):
            logger.info(f"SKIPPED: '{pkg_title}' already in destination.")
            deletion_exists.add(pkg_title)
            continue

        # 2. search source folders
        found_uuid = None
        for name, parent_uuid in search_locations.items():
            if not parent_uuid: 
                continue # skip if uuid missing (ie. test env)
            
            found_uuid = api.fetch_uuid_by_title(pkg_title, parent_uuid)
            if found_uuid:
                logger.info(f"Found '{pkg_title}' in {name} ({found_uuid})")
                break
        
        # 3. Move or Log Failure
        if found_uuid:
            success = api.move_entity(found_uuid, new_parent_ref)
            if success:
                logger.info(f"MOVED: '{pkg_title}' moved successfully.")
                successful_moves.add(pkg_title)
            else:
                logger.error(f"FAILED: Could not move '{pkg_title}'.")
                failed_moves.add(pkg_title)
        else:
            logger.warning(f"NOT FOUND: '{pkg_title}' could not be found in source folders.")
            failed_moves.add(pkg_title)

    # Summary
    logger.info("\n--- MOVE SUMMARY ---")
    logger.info(f"Successful: {len(successful_moves)}")
    logger.info(f"Already in Dest: {len(deletion_exists)}")
    logger.info(f"Failed/Not Found: {len(failed_moves)}")
    
    if failed_moves:
        for pkg in sorted(list(failed_moves)):
            logger.info(f"- {pkg}")

def main():
    args = parse_args()

    log_path = args.logpath if args.logpath else Path.cwd()
    log_file = log_path / f"prsv_move_{datetime.datetime.now().strftime('%Y%m%d')}.log"
    logger, _ = setup_logging(log_file)

    if args.use_file:
        if not DELETION_LIST_PATH.exists():
            logger.error(f"File not found: {DELETION_LIST_PATH}")
            return
        logger.info(f"Using list from file: {DELETION_LIST_PATH.name}")
        pkg_list = [line for line in DELETION_LIST_PATH.read_text().splitlines() if line.strip()]
    elif args.directory:
        if not args.directory.is_dir():
            logger.error(f"Directory not found: {args.directory}")
            return
        logger.info(f"Getting package names from directory: {args.directory}")
        pkg_list = [item.name for item in args.directory.iterdir() if item.is_dir()]
    else:
        logger.info("Using list from command line.")
        pkg_list = args.pkgtitle

    # Run Logic
    process_move_list(
        credentials=args.credentials,
        pkg_list=pkg_list,
        new_parent_ref=args.new_parent_ref,
        limit_parent=args.parent,
        existing_logger=logger
    )

if __name__ == "__main__":
    main()
