import sys
import argparse
import logging
import os
import configparser
import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import repair_tools.prsv_deletion_move as prsv_deletion_move
import repair_tools.utils.file_utils as file_utils
from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging

#############################
SCRIPT_DIR = Path(__file__).parent
INI_PATH = SCRIPT_DIR / 'compare_sources.ini'
NUM_THREADS = (os.cpu_count() - 2) if (os.cpu_count() - 2) > 0 else 1
#############################

def load_config(ini_path: Path) -> configparser.ConfigParser:
    """Loads configuration from the .ini file."""
    config = configparser.ConfigParser()
    if not ini_path.exists():
        logging.warning(f"INI file not found at {ini_path}. Using fallback defaults.")
        # fallback defaults
        config['Paths'] = {
            'CACHE_PATH': 'compare_sources_indexes/target_index.json',
            'SOURCE_CACHE_PATH': 'compare_sources_indexes/source_index_reingest.json',
            'DELETION_LIST_PATH': 'complete_reingest.txt',
            'LOG_PATH': 'compare_sources_logs_index'
        }
    else:
        config.read(ini_path)
    return config

#############################
CONFIG = load_config(INI_PATH)
PATHS = CONFIG['Paths']
#############################
CACHE_PATH = Path(PATHS.get('CACHE_PATH', 'compare_sources_indexes/target_index.json'))
SOURCE_CACHE_PATH = Path(PATHS.get('SOURCE_CACHE_PATH', 'compare_sources_indexes/source_index_reingest.json'))
DELETION_LIST_PATH = Path(PATHS.get('DELETION_LIST_PATH', 'complete_reingest.txt'))
LOG_PATH_DEFAULT = Path(PATHS.get('LOG_PATH', 'compare_sources_logs_index'))
#############################

def extant_dir(p: str) -> Path:
    path = Path(p)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"{path} is not a directory")
    return path

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--source",
        "-s",
        type=extant_dir,
        required=True,
        help="""Complete path to bags to be searched for.""",
        )
    parser.add_argument(
        "--target",
        "-t",
        type=extant_dir,
        help="""Complete path to volume to be searched in.""",
        )
    parser.add_argument(
        "--copydir",
        "-cd",
        type=extant_dir,
        help="""Complete path to directory missing pacakges will be copied to.""",
        )
    parser.add_argument(
        "--movedir",
        "-md",
        type=extant_dir,
        help="""Complete path to directory where packages will be moved.""",
        )
    parser.add_argument(
        "--credentials",
        type=str,
        required=True,
        help="which set of credentials to use",
        )
    parser.add_argument(
        "--prsvcheck",
        action="store_true",
        help="Flag to only check Preservica, not the target volume",
        )
    parser.add_argument(
        "--mvingested",
        action="store_true",
        help="Flag to move ingested pacakges rather than missing",
        )
    parser.add_argument(
        "--srcindex",
        '-si',
        type=Path,
        help="Path cached source index",
        )
    parser.add_argument(
        "--target-index",
        '-ti',
        type=Path,
        help="Path cached target index",
        )
    parser.add_argument(
        "--checklist",
        "-cl",
        type=Path,
        help="Path to reingest_list to check packages against list only",
        )
    parser.add_argument(
        "--update_checklist",
        "-ucl",
        action="store_true",
        help="Flag to update reingest_list with missing packages",
        )
    parser.add_argument(
        "--rsync",
        "-rs",
        action="store_true",
        help="Flag to run rsync copy/move commands instead of shutil",
        )
    parser.add_argument(
        "--logpath",
        "-lp",
        type=extant_dir,
        required=True,
        help="""base path to directory where log file and target directory index will be created.""",
        )
    parser.add_argument(
        "--deletion-parent-ref", 
        "-dpr", 
        type=str,
        help="UUID of the folder to move missing packages for Preservica deletion workflow."
    )
    return parser.parse_args()


#############################
def main():
    args = parse_args()

    log_base_path = args.logpath if args.logpath else LOG_PATH_DEFAULT
    log_file = log_base_path / f"compare_sources_{datetime.datetime.now().strftime('%Y%m%d')}.log"
    logger, list_logger = setup_logging(log_file)
    
    target_cache_path = args.target_index if args.target_index else CACHE_PATH
    source_cache_path = args.srcindex if args.srcindex else Path(SOURCE_CACHE_PATH / f"{args.source.parent.name}_{args.source.name}_index.json")
    deletion_list_path = args.checklist if args.checklist else DELETION_LIST_PATH

    # 1. find source directories
    source_dirs = []
    source_index = {}
    if args.checklist:
        logger.info(f"Reading package names from file: {deletion_list_path}")
        if not deletion_list_path.exists():
            logger.error(f"Deletion list file not found at: {deletion_list_path}")
            sys.exit("Exiting: File not found.")
        source_dirs = [line.strip() for line in deletion_list_path.read_text().splitlines() if line.strip()]
        logger.info(f"Found {len(source_dirs)} package names to check from the list.")
    elif args.source:
        logger.info(f"Scanning source directory: {args.source}")
        source_index = file_utils.load_or_create_source_index(args.source, source_cache_path, True)
        source_dirs = list(source_index.keys())
        logger.info(f"Found {len(source_dirs)} packages in source.")
    else:
        logger.error("You must provide a --source directory or use the --check-list flag.")
        sys.exit("No source specified.")

    # 2. load or create target index
    target_index = {}
    if not args.prsvcheck:
        if not args.target:
            logger.error("When not using --prsvcheck you must provide --target argument")
            sys.exit("Missing --target")
        
        target_index = file_utils.load_or_create_target_index(args.target, target_cache_path, NUM_THREADS)

    # 4. search preservica
    api = PreservicaAPI(args.credentials)
    
    logger.info(f"Checking {len(source_dirs)} packages against Preservica...")
    missing_dirs = []
    index_uuids = []
    prsv_uuids = []

    for dir_name in sorted(source_dirs):
        if api.check_package_exists(dir_name):
            logger.info(f"{dir_name} found in Preservica.\n")
            prsv_uuids.append(dir_name)
        else:
            if dir_name not in target_index:
                missing_dirs.append(dir_name)
                logger.info(f"{dir_name} not found in Preservica or target directory.\n")
            else:
                logger.info(f"{dir_name} not found in Preservica, found in target directory.\n")
                index_uuids.append(dir_name)

    # 4.5. run prsv_deletion_move (optional)
    if args.deletion_parent_ref:
        if missing_dirs:
            logger.info(f"Starting Preservica deletion workflow for {len(missing_dirs)} missing packages...")

            prsv_deletion_move.process_move_list(
                credentials=args.credentials,
                pkg_list=missing_dirs,
                new_parent_ref=args.deletion_parent_ref,
                existing_logger=logger
            )
        else:
            logger.info("No missing packages. Skipping deletion workflow move.")

    # 5. update checklist if flag used
    final_list = missing_dirs
    if args.update_checklist:
        logger.info(f"Updating {deletion_list_path.name}...")
        current_list = [line for line in deletion_list_path.read_text().splitlines() if line.strip()]
        updated_list = [pkg for pkg in current_list if pkg not in prsv_uuids]
        final_list = sorted(list(set(updated_list + missing_dirs)))
        
        logger.info(f"Removed {len(current_list) - len(updated_list)} already ingested packages.")
        logger.info(f"Adding {len(final_list) - len(updated_list)} new missing packages.")

        with open(deletion_list_path, "w") as f:
            f.write("\n".join(final_list) + "\n")
            
    logger.info("\n Missing Packages:")
    list_to_log = final_list if args.checklist else missing_dirs
    for name in list_to_log:
        list_logger.info(name)

    # 6. copy/move
    if args.checklist and (args.copydir or args.movedir):
        logger.warning("Copy/Move operations are ignored when using --check-list.")
        sys.exit()

    successful_ops = 0
    failed_items = {}
    skipped_items = {}

    if args.copydir:
        logger.info(f"Copying {len(missing_dirs)} missing packages to {args.copydir}...")
        with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            futures = {}
            for dir_name in sorted(missing_dirs):
                if dir_name not in source_index:
                    logger.warning(f"Cannot copy {dir_name}, not in source index. Skipping.")
                    continue
                source_path = source_index[dir_name]
                dest_path = file_utils.build_destination_path(dir_name, source_path, args.copydir)
                futures[executor.submit(file_utils.copy_package, source_path, dest_path)] = dir_name

            for future in as_completed(futures):
                dir_name = futures[future]
                try:
                    status, message = future.result()
                    if status == "copied":
                        successful_ops += 1
                    elif status == "failed":
                        failed_items[dir_name] = message
                except Exception as e:
                    logger.error(f"Error processing copy for {dir_name}: {e}")
                    failed_items[dir_name] = str(e)

    if args.movedir:
        move_list = prsv_uuids if args.mvingested else missing_dirs
        logger.info(f"Preparing to move {len(move_list)} packages to {args.movedir}...")
        
        if not move_list:
            logger.info("No packages to move.")
        else:
            with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
                futures = {}
                for dir_name in sorted(move_list):
                    if dir_name not in source_index:
                        logger.warning(f"Cannot move {dir_name}, not in source index. Skipping.")
                        continue
                    source_path = source_index[dir_name]
                    dest_path = file_utils.build_destination_path(dir_name, source_path, args.movedir)
                    futures[executor.submit(file_utils.move_package, source_path, dest_path, args.rsync)] = dir_name

                for future in as_completed(futures):
                    dir_name = futures[future]
                    try:
                        status, message = future.result()
                        if status == "moved":
                            successful_ops += 1
                        elif status == "failed":
                            failed_items[dir_name] = message
                        elif status == "skipped":
                            skipped_items[dir_name] = message
                    except Exception as e:
                        logger.error(f"Error processing move for {dir_name}: {e}")
                        failed_items[dir_name] = str(e)
            
                try:
                    if not any(args.source.iterdir()):
                        logger.info(f"Source directory is now empty: {args.source}")
                        remove_input = input("Remove empty source directory? (y/n): ")
                        if remove_input.lower() == 'y':
                            args.source.rmdir()
                            logger.info(f"Removed empty source directory: {args.source}")
                    else:
                        logger.info(f"Source directory is not empty: {args.source}")
                except Exception as e:
                    logger.error(f"Could not verify if source is empty: {e}")
    
    print("\n --- COMPARE SUMMARY --- ")
    logger.info(f"Total packages checked: {len(source_dirs)}")
    logger.info(f"Found in Preservica: {len(prsv_uuids)}")
    logger.info(f"Found in target only: {len(index_uuids)}")
    logger.info(f"Missing from all: {len(missing_dirs)}\n")

    if args.copydir or args.movedir:
        print("\n --- OPERATION SUMMARY --- ")
        print(f"Successful: {successful_ops}")
        print(f"Skipped: {len(skipped_items)}")
        print(f"Failed: {len(failed_items)}")
        if failed_items:
            logger.error("Failed items:")
            for item, reason in failed_items.items():
                logger.error(f" - {item}: {reason}")

    if not missing_dirs and not failed_items:
        print(f"All packages found in Preservica or target directory. Deleting index cache files and log file.")
        try:
            if target_cache_path.exists():
                target_cache_path.unlink()
            if source_cache_path.exists():
                source_cache_path.unlink()
            if log_file.exists():
                log_file.unlink()
            print("Cache files and log file deleted successfully.")
        except Exception as e:
            print(f"Error deleting cache or log files: {e}") 
    elif len(missing_dirs) == successful_ops and not failed_items:
        print("All missing packages processed successfully.")
        try:
            if target_cache_path.exists():
                target_cache_path.unlink()
            if source_cache_path.exists():
                source_cache_path.unlink()
            if log_file.exists():
                log_file.unlink()
            print("Cache files and log file deleted successfully.")
        except Exception as e:
            print(f"Error deleting cache or log files: {e}")
    else:
        delete_log_index = input("Some packages are still missing or failed operations. Delete cache files? (y/n): ")
        if delete_log_index.lower() == 'y':
            try:
                if target_cache_path.exists():
                    target_cache_path.unlink()
                if source_cache_path.exists():
                    source_cache_path.unlink()
                print("Cache files deleted successfully.")
            except Exception as e:
                print(f"Error deleting cache files: {e}")
            try:
                if log_file.exists():
                    log_file.unlink()
                print("Log file deleted successfully.")
            except Exception as e:
                print(f"Error deleting log file: {e}")
        else:
            print("Cache files and log file retained for review.")



if __name__ == "__main__":
    main()
