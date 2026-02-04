# validate prsv ingests
import argparse
import os
import sys
import requests
import logging
import concurrent.futures
import time
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging
from repair_tools.utils.file_utils import _find_matching_dirs
from repair_tools.create_pkg_report import (
        find_all_children,
        get_representation_details,
        get_generation_details,
        get_bitstream_details,
        requests_retry_session
    )
import repair_tools.utils.prsv_api as prsvapi

IGNORED_FILES = {
    ".DS_Store",
    "premis-events.json",
    "thumbs.db",
    "manifest-md5.txt",
    "tagmanifest-md5.txt",
    "bagit.txt",
    "bag-info.txt"
}

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", "-s",
        required=True,
        type=Path,
        help="Path to the directory containing AMI packages (6-digit folders)."
    )
    parser.add_argument(
        "--destination", "-d",
        type=Path,
        help="Path to move valid packages to. (Top level directories containing multiple packages)"
    )
    parser.add_argument(
        "--credentials", "-c",
        required=True,
        default="prod-ingest",
        help="Credentials set name to use (default: prod-ingest)."
    )
    parser.add_argument(
        "--log_path", "-l",
        required=False,
        default=Path(f"logs/ingest_validation_{datetime.now().strftime('%Y%m%d_%H%M')}.log"),
        type=Path,
        help="Path to save the log file."
    )
    parser.add_argument(
        "--verbose", "-v",
        required=False,
        action='store_true',
        help="Print verbose output."
    )
    parser.add_argument(
        "--threads", "-t",
        required=False,
        type=int,
        default=5,
        help="Number of concurrent threads to use (default: 5)."
    )
    return parser.parse_args()

# def get_local_files(ami_path):
#     """Recursively finds all valid files within an AMI directory."""
#     files = set()
#     for file_path in ami_path.rglob("*"):
#         if file_path.is_file():
#             filename = file_path.name.lower()
#             if (filename not in IGNORED_FILES 
#                 and not filename.startswith(".") 
#                 and not filename.endswith(".old")):
#                 files.add(file_path.name)
#     return files


def get_local_files(ami_path):
    """Recursively finds all valid files within an AMI directory and returns a dictionary of {filename: filesize}."""
    files = {}
    
    for root, dirs, filenames in os.walk(ami_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in filenames:
            if name.startswith(".") or name.endswith(".old"):
                continue

            if name.lower() not in IGNORED_FILES:
                full_path = os.path.join(root, name)
                files[name] = os.path.getsize(full_path)
                
    return files

def get_preservica_objects(api, package_uuid, credentials_name, logger):
    """
    Uses functions from create_pkg_report.py to traverse the package and get filenames 
    and their file sizes.
    Returns:
        file_data: dict {filename: filesize}
        io_titles: set {io_title}
    """
    file_data = {}
    io_titles = set()
    
    session = requests_retry_session()

    version = None
    for attempt in range(3):
        try:
            version = prsvapi.find_apiversion(credentials_name)
            break # Success, exit loop
        except Exception as e:
            if attempt < 2:
                logger.warning(f"Attempt {attempt + 1} failed to determine API version: {e}. Retrying in 1s...")
                time.sleep(1)
            else:
                logger.warning(f"Final attempt failed. Could not determine API version, defaulting to 8.0: {e}")
            version = "8.0"

    namespaces = {
        'xip': f'http://preservica.com/XIP/v{version}',
        'entity': f'http://preservica.com/EntityAPI/v{version}'
    }

    # ignore child SOs list
    child_so_refs = [] 
    io_info_list = []
    
    try:
        find_all_children(api.token, version, package_uuid, child_so_refs, io_info_list, session, namespaces)
    except Exception as e:
        logger.error(f"Error traversing package children: {e}")
        return file_data, io_titles

    # iterate through IOs
    for io_info in io_info_list:
        io_ref = io_info['ref']
        # record IO Title 
        if io_info.get('title'):
            io_titles.add(io_info['title'])
        
        # get reps
        reps = get_representation_details(api.token, version, io_ref, session, namespaces)
        
        for rep in reps:
            co_refs = get_generation_details(api.token, version, io_ref, rep['type'], session, namespaces)
            
            for co_ref in co_refs:
                bitstream = get_bitstream_details(api.token, version, co_ref, session, namespaces)
                
                if bitstream and bitstream.get('filename'):
                    file_data[bitstream['filename']] = bitstream.get('filesize')
                else:
                    logger.warning(f"Found CO {co_ref} without a valid filename bitstream.")

    return file_data, io_titles

def validate_package(api, ami_id, ami_paths, credentials_name, logger, list_logger, args_verbose=False):
    """
    Validates a single AMI package.
    Checks:
    1. File existence (Local vs Preservica)
    2. 0 byte files in Preservica
    3. Filesize mismatch between Local and Preservica
    
    Returns: True if valid, False if invalid or skipped.
    """
    # duplicate local folders
    ami_path = Path(ami_paths[0])
    if len(ami_paths) > 1:
        logger.warning(f"Multiple local folders found for {ami_id}, checking content of: {ami_path}")

    logger.info(f"Checking package: {ami_id}")

    package_uuid = api.fetch_uuid_by_title(ami_id, api.ami_uuid)

    if not package_uuid:
        logger.error(f"FAILED: AMI ID {ami_id} NOT FOUND in Preservica.")
        return False

    # Local Files {filename: size}
    local_files = get_local_files(ami_path)
    if not local_files:
        logger.warning(f"Skipping {ami_id}: No local files found.")
        return False 

    # Preservica files {filename: size} & IO titles
    try:
        preservica_files, preservica_io_titles = get_preservica_objects(api, package_uuid, credentials_name, logger)
    except Exception as e:
        logger.error(f"Error retrieving files from Preservica for {ami_id}: {e}")
        return False

    # dict keys to sets for comparison
    local_filenames = set(local_files.keys())
    prsv_filenames = set(preservica_files.keys())

    # debug
    # print(f"Local files: {list(local_filenames)}")
    # print(f"Preservica files: {list(prsv_filenames)}")

    missing_from_preservica = local_filenames - prsv_filenames
    missing_from_local = prsv_filenames - local_filenames
    matching_files = local_filenames.intersection(prsv_filenames)

    # 1. CHECK MISSING FILES
    # filter missing files: see if they exist as IO Title
    truly_missing = set()
    found_as_io = set()

    for f in missing_from_preservica:
        if f in preservica_io_titles:
            found_as_io.add(f)
        else:
            truly_missing.add(f)
    
    # print(f"Truly missing: {truly_missing}")
    # print(f"Found as IO: {found_as_io}")
    # print(f"Matching files: {matching_files}")
    # print(f"Missing from Preservica: {missing_from_preservica}")
    # print(f"Missing from Local: {missing_from_local}")
    # print(f"Preservica Files: {preservica_files}")
    # print(f"Preservica IO Titles: {preservica_io_titles}")
    # print(f"Local Files: {local_files}")
    # sys.exit(1)

    # FAILURE: critical files missing (not as Bitstream OR IO)
    if truly_missing:
        logger.error(f"FAILED: {ami_id} exists, but is missing {len(truly_missing)} file(s) in Preservica.")
        logger.error(f"{ami_id} - Files found Locally but MISSING in Preservica:")
        for f in sorted(truly_missing):
            list_logger.info(f" - {f}")
        
        # log IO mismatches if they exist alongside failures
        if found_as_io:
            logger.warning(f"{ami_id} - {len(found_as_io)} files matched IO Titles but not Bitstream filenames:")
            for f in sorted(found_as_io):
                list_logger.info(f" ~ {f} (IO Title Match)")
        
        return False 

    # WARNING: IO Title Match (Bitstream mismatch)
    elif found_as_io:
        logger.warning(f"WARNING: {ami_id} - {len(found_as_io)} files matched IO Titles but NOT Bitstream filenames.")
        for f in sorted(found_as_io):
            list_logger.info(f" ~ {f} (IO Title Match Only)")
        
        return False

    # 2. CHECK FILE SIZES
    size_mismatch = []
    zero_byte_files = []

    for fname in matching_files:
        local_size = local_files[fname]
        prsv_size = preservica_files.get(fname)
        
        # debug
        # print(f"Comparing file sizes for {fname}: Local={local_size}, Preservica={prsv_size}")

        if prsv_size == 0:
            zero_byte_files.append(fname)
        elif local_size != prsv_size:
            size_mismatch.append((fname, local_size, prsv_size))

    if zero_byte_files:
        logger.error(f"FAILED: {ami_id} contains {len(zero_byte_files)} file(s) with 0 bytes in Preservica.")
        for f in sorted(zero_byte_files):
            list_logger.info(f" ! {f} (0 Bytes)")
        return False

    if size_mismatch:
        logger.error(f"FAILED: {ami_id} contains {len(size_mismatch)} file(s) with size mismatches.")
        for fname, l_size, p_size in sorted(size_mismatch):
            list_logger.info(f" ! {fname} | Local: {l_size} bytes | Prsv: {p_size} bytes")
        return False

    # WARNING: extra files in Preservica
    if missing_from_local:
        logger.warning(f"WARNING: {ami_id} has {len(missing_from_local)} extra file(s) in Preservica not found locally.")
        for f in sorted(missing_from_local):
            list_logger.info(f" + {f} (Extra in Preservica)")

    # SUCCESS
    logger.info(f"SUCCESS: {ami_id} fully validated ({len(local_files)} files).")

    if args_verbose:
        header = f"{'LOCAL':<60} {'PRESERVICA'}"
        separator = f"{'-'*60} {'-'*15}"
        
        output = [f"\n{ami_id}:", header, separator]
        for f in sorted(matching_files):
            size_str = f"{local_files[f]} bytes"
            output.append(f"{f:<60} --------> {f} ({size_str})")
        output.append("")
        
        list_logger.info("\n".join(output))
        
    return True 

def move_to_delete(source_path: Path, destination_path: Path):
    try:
        destination_path.mkdir(parents=True, exist_ok=True)
        target_path = destination_path / source_path.parent.name / source_path.name
        target_input = input(f"Move '{source_path}' to '{target_path}'? (press Enter to confirm)")
        if target_input.lower() not in ['', 'y', 'yes']:
            logging.info(f"'{source_path}' not moved to {target_path}.")
            return
        source_path.rename(target_path)
        logging.info(f"Moved '{source_path}' to '{target_path}'")
    except Exception as e:
        logging.error(f"Error moving '{source_path}' to '{destination_path}': {e}")

def main():
    args = parse_args()

    log_path = args.log_path
    logger, list_logger = setup_logging(log_path)
    
    try:
        api = PreservicaAPI(credentials=args.credentials)
    except Exception as e:
        logger.critical(f"Failed to authenticate with Preservica API: {e}")
        sys.exit(1)

    logger.info(f"Scanning source directory: {args.source}")
    ami_packages = {}
    
    for root, dirs, _ in os.walk(args.source):
        matches = _find_matching_dirs(root, dirs)
        for ami_id, paths in matches.items():
            if ami_id not in ami_packages:
                ami_packages[ami_id] = paths
            else:
                ami_packages[ami_id].extend(paths)

    if not ami_packages:
        logger.warning("No AMI packages found to process.")
        sys.exit(0)

    logger.info(f"Found {len(ami_packages)} AMI packages.")

    valid_ids = set()
    invalid_pkgs = []  # list of tuples: (ami_id, path)
    
    logger.info(f"Starting validation with {args.threads} threads...")

    # THREADING
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        future_to_ami = {
            executor.submit(
                validate_package, 
                api, 
                ami_id, 
                paths, 
                args.credentials, 
                logger, 
                list_logger, 
                args.verbose
            ): (ami_id, paths) 
            for ami_id, paths in ami_packages.items()
        }

        for future in concurrent.futures.as_completed(future_to_ami):
            ami_id, paths = future_to_ami[future]
            try:
                is_valid = future.result()
                if is_valid:
                    valid_ids.add(ami_id)
                else:
                    invalid_pkgs.append((ami_id, paths[0]))
            except Exception as e:
                logger.error(f"Exception generated for {ami_id}: {e}")
                if (ami_id, paths[0]) not in invalid_pkgs:
                    invalid_pkgs.append((ami_id, paths[0]))
    
    logger.info("Validation complete.")

    # SUMMARY
    list_logger.info("\n" + "="*60)
    list_logger.info(F"FINAL VALIDATION SUMMARY: {(args.source).name}")
    list_logger.info("="*60)

    list_logger.info(f"\nVALID PACKAGES ({len(valid_ids)}):")
    # for valid_id in sorted(valid_ids):
    #     list_logger.info(f" - {valid_id}")

    list_logger.info(f"\nINVALID PACKAGES ({len(invalid_pkgs)}):")
    for ami_id, path in invalid_pkgs:
        list_logger.info(f" - {ami_id} | Path: {path}")

    list_logger.info("\n" + "="*60)

    if args.destination and valid_ids:
        logger.info(f"\nMoving valid packages to destination: {args.destination}")
        for ami_id in valid_ids:
            source_paths = ami_packages[ami_id]
            for source_path in source_paths:
                move_to_delete(Path(source_path), args.destination)

    handlers = logger.handlers[:]
    for handler in handlers:
        handler.close()
        logger.removeHandler(handler)
    
    handlers_list = list_logger.handlers[:]
    for handler in handlers_list:
        handler.close()
        list_logger.removeHandler(handler)

    if log_path.exists():
        print(f"\nLog file generated at: {log_path.resolve()}")
        while True:
            response = input("Delete the generated log file? (y/n): ").strip().lower()
            if response == 'y':
                try:
                    log_path.unlink()
                    print(f"Deleted log file: {log_path}")
                except Exception as e:
                    print(f"Error deleting log file: {e}")
                break
            elif response == 'n':
                print("Log file kept.")
                break
            else:
                print("Invalid input. Please enter 'y' or 'n'.")


if __name__ == "__main__":
    main()