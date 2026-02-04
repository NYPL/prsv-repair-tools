import sys
import argparse
import shutil
import os
import subprocess
import json
import pandas as pd
import logging
from pathlib import Path
import repair_tools.path_tools.search_index as search_index
import repair_tools.create_manifest as cm
import repair_tools.create_bag_strucutre as cbs

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        type=Path,
        help="Path to the input CSV file containing AMI IDs and filenames."
    )
    parser.add_argument(
        "--index_file",
        type=Path,
        help="Path to the MGDC_index.json file."
    )
    parser.add_argument(
        "--sym_base_dir",
        type=Path,
        help="The base directory where all new 'sym_bags' will be created."
    )
    parser.add_argument(
        "--prepend_path",
        type=Path,
        help="The root path to prepend to filepaths found in the index (e.g., /source/path/MGDC)."
    )
    parser.add_argument(
        "--md5",
        action='store_true',
        help="Generate real md5 checksums for the manifest (default == placeholder zeros)."
    )
    return parser.parse_args()

# 0.5 load mgdc index
def load_index(index_file: Path):
    try:
        with open(index_file, 'r', encoding='utf-8') as f:
            index_data = json.load(f)
            return index_data
    except FileNotFoundError:
        logging.error(f"Index file not found at '{index_file}'")
    except json.JSONDecodeError:
        logging.error(f"Could not decode json from '{index_file}'")
    except Exception as e:
        logging.error(f"An error occurred while reading '{index_file}': {e}")
    return None

# 2. find pm/sc filepath
def find_filepath(index_data, *filenames_to_try):
    for filename in filenames_to_try:
        if isinstance(filename, str) and filename.strip():
            match_files = search_index.search_tree_for_key(index_data, filename)
            
            if match_files:
                logging.info(f"    Found match for '{filename}'.")
                if len(match_files) > 1:
                    logging.warning(f"    Multiple files found for '{filename}'. Using the first match.")
                return match_files[0] 
            else:
                logging.warning(f"    File '{filename}' not found.")
    logging.info("    No matches found after trying all provided filenames.")
    return None

# 3. create symlink based on pm and sc filepaths
def create_symlink(mgdc_path: Path, sym_base_path: Path, nypl_filename: str = None):
    sym_path = sym_base_path / (nypl_filename if nypl_filename else mgdc_path.name)
    logging.info(f"  Creating symlink: {sym_path} -> {mgdc_path}")
    sym_command = [
        "ln",
        "-s",
        str(mgdc_path),
        str(sym_path)
    ]

    try:
        subprocess.run(sym_command, check=True, capture_output=True, text=True)
        logging.info(f"    Created symlink: {sym_path} -> {mgdc_path}")
        return sym_path
    except subprocess.CalledProcessError as e:
        logging.error(f"    ERROR: Symlink creation is not supported on this platform or by this user account.")
    return None

# 5. create pm json in symbag
def create_json(filename: str, ami_id: str, sym_bag_path: Path):
    if not (isinstance(filename, str) and filename.strip()):
        logging.error(f"    Invalid or empty filename provided for AMI ID '{ami_id}'. Cannot create json.")
        return
        
    try:
        filename_parts = str(filename).split("_")
        division_code = filename_parts[0].lower()
        file_type = filename_parts[1].lower()
    except IndexError:
        logging.error(f"    ERROR: Filename '{filename}' does not conform to expected 'div_type_...' convention.")
        return

    data_structure = {
        "asset": {
            "referenceFilename": filename
        },
        "bibliographic": {
            "primaryID": ami_id,
            "cmsItemID": ami_id,
            "divisionCode": division_code
        }
    }
    
    output_filepath = None
    if file_type == "pm":
        output_filepath = sym_bag_path / "data" / "PreservationMasters" / (Path(filename).stem + ".json")
    elif file_type == "sc":
        output_filepath = sym_bag_path / "data" / "ServiceCopies" / (Path(filename).stem + ".json")
    else:
        logging.error(f"    ERROR: Filename '{filename}' type '{file_type}' is not 'pm' or 'sc'.")
        return
    
    try:
        output_filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(output_filepath, 'w', encoding='utf-8') as json_file:
            json.dump(data_structure, json_file, indent=4)
        logging.info(f"    Successfully created new json file at '{output_filepath}'")

    except IOError as e:
        logging.error(f"    ERROR: Unable to write to file '{output_filepath}': {e}")
    except Exception as e:
        logging.error(f"    ERROR: Could not create JSON file at '{output_filepath}': {e}")


def _find_and_link_file(
    index_data: dict, 
    filenames_to_try,
    sym_target_dir: Path,
    nypl_filename: str,
    source_root: Path,
    file_type: str  # "PM" or "SC" for logging
):
    logging.info(f"  Searching for {file_type} file...")
    
    # alt filenames with .mov ext
    alt_filenames = []
    for f in filenames_to_try:
        if isinstance(f, str) and f.strip() and '.' in f:
            alt_filenames.append(Path(f).stem + ".mov")
            
    # original and alt filenames
    all_filenames = filenames_to_try + alt_filenames
    
    relative_path_str = find_filepath(index_data, *all_filenames)
    
    if relative_path_str is None:
        logging.warning(f"    No {file_type} file found after trying: {all_filenames}")
        return None
    
    source_file_path = source_root / relative_path_str
    if not source_file_path.exists():
        logging.error(f"    Index points to missing {file_type} file: '{source_file_path}'")
        return None
        
    logging.info(f"    Found {file_type} file: '{source_file_path}'")
    return create_symlink(source_file_path, sym_target_dir, nypl_filename)

def _process_json_file(
    index_data: dict,
    json_filename: str,
    ami_id: str,
    sym_bag_path: Path,
    sym_target_dir: Path,
    source_root: Path
):
    logging.info(f"  Processing json file '{json_filename}'...")
    json_file_path_str = find_filepath(index_data, json_filename)
    
    if json_file_path_str:
        source_json_path = source_root / json_file_path_str
        try:
            if not source_json_path.exists():
                 raise FileNotFoundError(f"Index points to missing json file: {source_json_path}")
                 
            shutil.copy2(source_json_path, sym_target_dir)
            logging.info(f"    Copied json file '{source_json_path}' to '{sym_target_dir}'")
        except Exception as e:
            logging.error(f"    Could not copy json file '{source_json_path}': {e}")
            logging.warning(f"    Falling back to creating new json for '{json_filename}'.")
            create_json(json_filename, ami_id, sym_bag_path)
    else:
        logging.warning(f"    json file '{json_filename}' not found in index. Creating new json.")
        create_json(json_filename, ami_id, sym_bag_path)

# --- Main Execution ---

def main():
    args = parse_args()

    # args.index_file = Path("/Users/emileebuytkins/Documents/Buytkins_Programming/MGDC_index.json")
    logging.info(f"Loading index from '{args.index_file}'...")
    mgdc_index = load_index(args.index_file)

    if mgdc_index is None:
        logging.critical("Unable to load index. Exiting.")
        return

    prepend_path = Path("/source/lpasync/MGDC")
    base_dir = Path("/Volumes/lpasync/MGDC/sym_bags")

    # args.csv_file = Path("/Users/emileebuytkins/Downloads/mgdc_filenames.csv")
    logging.info(f"Loading CSV from '{args.csv_file}'...")
    try:
        df = pd.read_csv(args.csv_file, dtype=str).fillna('') # Read all as string, fill NaN with empty string
    except FileNotFoundError:
        logging.critical(f"ERROR: CSV file not found at '{args.csv_file}'. Exiting.")
        return
    except Exception as e:
        logging.critical(f"ERROR: Could not read CSV file. {e}. Exiting.")
        return

    found_sc_files = []
    found_pm_files = []
    missing_pm_files = []
    missing_sc_files = []
    ami_ids_processed = []

    for row in df.itertuples(index=False):
        ami_id = row.ami_id
        if not (isinstance(ami_id, str) and ami_id.strip()):
            logging.warning(f"Skipping row {row.Index if hasattr(row, 'Index') else ''} due to missing ami_id.")
            continue
            
        ami_ids_processed.append(ami_id)
        logging.info(f"--- Processing AMI ID: {ami_id} ---")

        # 1. create bag structure
        try:
            sym_bag_path, sym_pm_path, sym_sc_path = cbs.create_dir_structure(args.symlink_base_dir, str(ami_id))
        except Exception as e:
            logging.error(f"Failed to create directory structure for {ami_id}: {e}. Skipping this AMI.")
            continue
            
        manifest_paths = []

        # 2. find pm file, create symlink
        pm_symlink = _find_and_link_file(
            mgdc_index,
            [row.pm_legacy_filename, row.nypl_pm_filename],
            sym_pm_path,
            row.nypl_pm_filename,
            args.source_files_root,
            "PM"
        )
        if pm_symlink:
            manifest_paths.append(pm_symlink)
            found_pm_files.append(ami_id)
        else:
            missing_pm_files.append(ami_id)
        
        # 3. find sc file, create symlink
        sc_symlink = _find_and_link_file(
            mgdc_index,
            [row.sc_legacy_filename, row.nypl_sc_filename],
            sym_sc_path,
            row.nypl_sc_filename,
            args.source_files_root,
            "SC"
        )
        if sc_symlink:
            manifest_paths.append(sc_symlink)
            found_sc_files.append(ami_id)
        else:
            missing_sc_files.append(ami_id)
        
        # 4. find / create pm json
        _process_json_file(mgdc_index, row.nypl_pm_json, ami_id, sym_bag_path, sym_pm_path, args.prepend_path)
       
        # 5. find / create sc json
        _process_json_file(mgdc_index, row.nypl_sc_json, ami_id, sym_bag_path, sym_pm_path, args.prepend_path)

        # 6. Create manifest
        logging.info(f"  Creating manifest for '{sym_bag_path}'...")
        cm.create_manifest(sym_bag_path, manifest_paths, generate_md5=args.md5)

        #----------debug break (remove for production)
        break

    logging.info(f"Total AMI IDs processed: {len(ami_ids_processed)}")
    logging.info(f"Found pm files for {len(found_pm_files)} AMI IDs.")
    logging.info(f"Found sc files for {len(found_sc_files)} AMI IDs.")
    logging.info(f"Missing pm files for {len(missing_pm_files)} AMI IDs.")
    logging.info(f"Missing sc files for {len(missing_sc_files)} AMI IDs.")




if __name__ == "__main__":
    main()