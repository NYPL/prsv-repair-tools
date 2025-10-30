import time
from pathlib import Path
import argparse
import os
import json
import subprocess
import logging
import datetime
import requests
import re
import shutil
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
import repair_tools.prsv_api as prsvapi

def setup_logging(log_file: Path):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # file handler
    log_file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(str(log_file), mode='a')
    fh.setFormatter(log_file_formatter)
    logger.addHandler(fh)

    # console handler
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    ch = logging.StreamHandler()
    ch.setFormatter(console_formatter)
    logger.addHandler(ch)

    list_logger = logging.getLogger('list_logger')
    list_logger.setLevel(logging.INFO)

    basic_formatter = logging.Formatter('%(message)s')
    bh = logging.StreamHandler()
    bh.setFormatter(basic_formatter)
    list_logger.addHandler(bh)

    list_logger.propagate = False

    return logger, list_logger

################# Variables

config = configparser.ConfigParser()
SCRIPT_DIR = Path(__file__).parent
INI_PATH = SCRIPT_DIR / 'compare_sources.ini'

# fallback defaults
CACHE_PATH = Path.cwd() / "compare_sources_indexes" / "target_index.json"
SOURCE_CACHE_PATH = Path.cwd() / "compare_sources_indexes" / "source_index_reingest.json"
DELETION_LIST_PATH = Path.cwd() / "complete_reingest.txt"
LOG_PATH_DEFAULT = Path.cwd() / "compare_sources_logs_index"

if INI_PATH.exists():
    config.read(INI_PATH)
    if 'Paths' in config:
        paths = config['Paths']
        CACHE_PATH = Path(paths.get('CACHE_PATH', CACHE_PATH))
        SOURCE_CACHE_PATH = Path(paths.get('SOURCE_CACHE_PATH', SOURCE_CACHE_PATH))
        DELETION_LIST_PATH = Path(paths.get('DELETION_LIST_PATH', DELETION_LIST_PATH))
        LOG_PATH_DEFAULT = Path(paths.get('LOG_PATH', LOG_PATH_DEFAULT))
    else:
        logging.warning(f"INI file found at {INI_PATH}, but [Paths] section is missing. Using defaults.")
else:
    logging.warning(f"INI file not found at {INI_PATH}. Using defaults.")

NUM_THREADS = (os.cpu_count() - 2) if (os.cpu_count() - 2) > 0 else 1

move_count = 0
copy_count = 0
failed_dict = {}
skip_dict = {}

################# Parser
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
        help="""Complete path to HD directories to be searched for.""",
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
        choices=["test-ingest", "prod-ingest", "test-manage"],
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
    return parser.parse_args()
#################

def get_source_dirs(source_dir: Path, logger: logging.Logger) -> list[str]:
    # return {p.name for p in source_dir.iterdir() if p.is_dir()}
    logger.info("Getting source directory names...")
    # returning only AMI directories, should be modified for any DigArch names*******************************************
    # using string pattern instead of regex as rglob does not support it
    ami_id_pattern = "[0-9][0-9][0-9][0-9][0-9][0-9]"
    return {p.name for p in source_dir.rglob(ami_id_pattern) if p.is_dir()}
    # return {p.name for p in source_dir.rglob("*") if p.is_dir() and len(p.name) == 6 and p.name.isdigit()}

# why did I make this a whole function?
def is_six_digit_dir(name: str) -> bool:
    return len(name) == 6 and name.isdigit()

def find_matching_dirs(root: str, dirs: list[str]) -> dict:
    matches = {}

    for d in dirs:
        if is_six_digit_dir(d):
            full_path = os.path.join(root, d)
            if d not in matches:
                matches[d] = []
            matches[d].append(full_path)

    return matches

def index_target_dirs(target_dir: Path, logger: logging.Logger) -> dict:
    logger.info("Indexing target volume:")

    index = {}
    tasks = []

    with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
        for root, dirs, _ in os.walk(target_dir):
            logger.info(root)
            if any(is_six_digit_dir(d) for d in dirs):
                tasks.append(executor.submit(find_matching_dirs, root, dirs))

        for future in as_completed(tasks):
            result = future.result()
            for name, paths in result.items():
                if name not in index:
                    index[name] = []
                index[name].extend(paths)
    return index

def find_index(target_dir: Path,logger, cache_path: Path = CACHE_PATH) -> dict:
    if cache_path.exists():
        logger.info(f"Loading target volume index from: {cache_path}")
        with open(cache_path, "r") as f:
            return json.load(f)
    else:
        index = index_target_dirs(target_dir, logger)
        logger.info(f"Saving target volume index to: {cache_path}")
        with open(cache_path, "w") as f:
            json.dump(index, f, indent=2)
        return index
    
def find_source_index(source_dir: Path, logger, cache_path: Path = SOURCE_CACHE_PATH) -> dict:
    cache_path = Path(str(SOURCE_CACHE_PATH).replace("_reingest", f"_{source_dir.name}_reingest"))
    if cache_path.exists():
        logger.info(f"Loading source index from {cache_path}")
        with open(cache_path, "r") as f:
            return {key: Path(value) for key, value in json.load(f).items()}
    else:
        logger.info("Creating new source index.")
        index = {dir.name: dir for dir in source_dir.rglob("*") if dir.is_dir() and len(dir.name)==6 and dir.name.isdigit()}
        logger.info(f"Source index created with {len(index)} items")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({key: str(value) for key, value in index.items()}, f, indent=2)
        return index

############# Prsv API from export_metadata

def search_preservica_api(
    accesstoken: str, query_params: dict, parentuuid: str
) -> requests.Response:
    query = json.dumps(query_params)
    #search-within
    search_url = f"https://nypl.preservica.com/api/content/search-within?q={query}&parenthierarchy={parentuuid}&start=0&max=-1&metadata=''"
    search_headers = {
        "Preservica-Access-Token": accesstoken,
        "Content-Type": "application/xml;charset=UTF-8",
    }
    # logging.info("")
    search_response = requests.get(search_url, headers=search_headers, timeout=30)

    logging.info("")
    return search_response

def get_packages_uuids(
    accesstoken: str, pkg_id: str, parentuuid: str
) -> requests.Response:
    try: 
        col_id = re.search(r"(M\d+)_(ER|DI|EM)_\d+", pkg_id).group(1)
    except AttributeError:
        return None
    query_params = {
        "q": "",
        "fields": [
            {"name": "xip.title", "values": [pkg_id]},
            {"name": "spec.specCollectionID", "values": [col_id]},
        ],
    }
    return search_preservica_api(accesstoken, query_params, parentuuid)

def get_amipackages_uuids(
        accesstoken: str, pkg_id: str, parentuuid: str
) -> requests.Response:
    """get AMI uuids based on first 3 digits of AMI ID"""
    query_params = {
        "q": "",
        "fields": [
            {"name": "xip.title", "values": [f"{pkg_id}"]},
            {"name": "xip.identifier", "values": ["DigitizedAMIContainer"]}
        ]
    }
    return search_preservica_api(accesstoken, query_params, parentuuid)

def parse_structural_object_uuid(res: requests.Response) -> list:
    """function to parse json API response into a list of UUIDs"""
    uuid_ls = list()

    if res is None:
        return uuid_ls
    
    json_obj = json.loads(res.text)

    value = json_obj.get("value")
    if value and value.get("objectIds"):
        obj_ids = json_obj["value"]["objectIds"]
        for sdbso in obj_ids:
            uuid_ls.append(sdbso[-36:])

    return uuid_ls
    
############# COPY/MOVE FUNCTIONS

def copy_single_pkg(missing_dirs, source_index: dict, copy_dir: Path, logger: logging.Logger,): # change missing_dirs to dir_name for threading
    c_copy_count = 0
    c_failed_dict = {}
    if not missing_dirs:
        logger.warning("No missing directories to copy.")
        return c_copy_count, c_failed_dict
    
    for dir_name in sorted(missing_dirs):
        if dir_name in source_index:
            source_path = source_index[dir_name]
            if any(x in dir_name for x in ("Audio", "Film", "Video")):
                dest_path = Path(copy_dir / source_path.parent.name / dir_name)
            else:
                dest_path = Path(copy_dir / source_path.name / dir_name)
            dest_path.mkdir(parents=True, exist_ok=True)
            rsync_cmd = [
                "rsync",
                "-aP",
                f"{str(source_path)}/",
                f"{str(dest_path)}/"
            ]
            logger.info(f"Copying {source_path} to {dest_path} ...")
            try:
                subprocess.run(rsync_cmd, check=True, text=True)
                c_copy_count += 1
            except subprocess.CalledProcessError as e:
                logger.error(f"Error copying {dir_name}: {e.stderr}")
                c_failed_dict[dir_name] = e.stderr
                break 
    return c_copy_count, c_failed_dict




def move_single_pkg(move_dirs, source_index: dict, ingest_dir: Path, logger: logging.Logger, rsync_mode): # change missing_dirs to dir_name for threading
    """Moves directories found in Preservica from source to target directory using rsync."""
    m_move_count = 0
    m_failed_dict = {}
    m_skip_dict = {}
    #############
    if not move_dirs:
        logger.warning("No missing directories to copy.")
        return m_move_count, m_failed_dict, m_skip_dict
    
    for dir_name in sorted(move_dirs):
        if dir_name in source_index:
            source_path = source_index[dir_name]
            if any(x in source_path.name for x in ("Audio", "Film", "Video")):
                dest_path = Path(ingest_dir / source_path.parent.name / dir_name)
            else:
                dest_path = Path(ingest_dir / dir_name)
            dest_path.mkdir(parents=True, exist_ok=True)
            rsync_cmd = [
                "rsync",
                "-aP",
                "--remove-source-files",
                f"{str(source_path)}/",
                f"{str(dest_path)}/"
            ]
            logger.info(f"Moving {source_path} to {dest_path}")
            try:
                if rsync_mode == True:
                    subprocess.run(rsync_cmd, check=True, text=True)
                    shutil.rmtree(source_path)
                else:
                    shutil.move(str(source_path), str(dest_path))
                m_move_count += 1
            except subprocess.CalledProcessError as e:
                logger.error(f"Error moving {dir_name}: {e.stderr}")
                m_failed_dict[dir_name] = e.stderr
                continue
            except FileNotFoundError:
                # previous run removed directory
                logger.warning(f"Directory already removed: {source_path}")
                m_skip_dict[source_path] = "Directory already removed"
                continue
            except FileExistsError:
                logging.warning(f"Package '{source_path.name}' already exists, skipping.")
                m_skip_dict[source_path] = "Directory already exists in target"
                continue
            except OSError as e:
                # directory not empty
                logger.error(f"Could not remove {source_path}, dir may not be empty: {e}")
                m_failed_dict[dir_name] = str(e)
                continue
    return m_move_count, m_failed_dict, m_skip_dict

def get_source_index(single_dir: Path):
    dir_index = {dir.name: dir for dir in single_dir.rglob("*") if dir.is_dir() and len(dir.name) == 6 and dir.name.isdigit()} 
    return dir_index
#############

def main():
    args = parse_args()
    SLEEP_DURATION = 2
    
    target_cache_path = args.target_index if args.target_index else CACHE_PATH 
    source_cache_path = args.srcindex if args.srcindex else SOURCE_CACHE_PATH
    deletion_list_path = args.checklist if args.checklist else DELETION_LIST_PATH

    log_path = args.logpath if args.logpath else LOG_PATH_DEFAULT
    log_path.mkdir(parents=True, exist_ok=True)
    
    logger, list_logger = setup_logging(log_file=Path(log_path / f"compare_mounted_volumes_{datetime.datetime.now().strftime('%Y%m%d')}.log"))
    
    source_dirs = []
    source_index = {}

    if args.checklist:
        logger.info(f"Reading package names from file: {deletion_list_path}")
        if not deletion_list_path.exists():
            logger.error(f"Deletion list file not found at: {deletion_list_path}")
            raise SystemExit("Exiting: File not found.")
        source_dirs = [line.strip() for line in deletion_list_path.read_text().splitlines() if line.strip()]
        logger.info(f"Found {len(source_dirs)} package names to check from the list.")
    elif args.source:
        logger.info(f"Scanning source directory: {args.source}")
        source_dir = Path(args.source)
        if args.srcindex:
            source_index = find_source_index(source_dir, logger, cache_path=source_cache_path)
        else:
            logger.info("Creating temp source index...")
            source_index = {dir.name: dir for dir in source_dir.rglob("*") if dir.is_dir() and len(dir.name) == 6 and dir.name.isdigit()}
            logger.info(f"Source index created with {len(source_index)} entries.")
        source_dirs = list(source_index.keys())
    else:
        logger.error("You must provide a --source directory or use the --check-list flag.")
        raise SystemExit("No source specified.")
    
    target_index = {}
    if not args.prsvcheck:
        if args.target:
            target_dir = Path(args.target)
            target_index = find_index(target_dir, logger, cache_path=target_cache_path)
        else:
            logger.error("When not using --prsvcheck you must provide --target argument")
            raise SystemExit("Missing --target")

    copy_dir = Path(args.copydir) if args.copydir else None
    move_dir = Path(args.movedir) if args.movedir else None

    accesstoken = prsvapi.get_token(args.credentials)

    if "test" in args.credentials:
        digarch_uuid = "c0b9b47a-5552-4277-874e-092b3cc53af6"
    else:
        digarch_uuid = "e80315bc-42f5-44da-807f-446f78621c08"
        ami_uuid = "183a74b5-7247-4fb2-8184-959366bc0cbc"

    logger.info(f"Checking {len(source_dirs)} packages against Preservica...")

    missing_dirs = []
    index_uuids = []
    prsv_uuids = []

    for dir in sorted(source_dirs):
        try:
            if dir.startswith("M"):
                res = get_packages_uuids(accesstoken, dir, digarch_uuid)
            else:
                res = get_amipackages_uuids(accesstoken, dir, ami_uuid)
            find_prsv_pkg = parse_structural_object_uuid(res)
        except Exception as e:
            logging.warning(f"Error reaching prsv API, retrying {dir} in {SLEEP_DURATION} sec: {e}")
            time.sleep(SLEEP_DURATION)
            if dir.startswith("M"):
                res = get_packages_uuids(accesstoken, dir, digarch_uuid)
            else:
                res = get_amipackages_uuids(accesstoken, dir, ami_uuid)
            find_prsv_pkg = parse_structural_object_uuid(res)

        if find_prsv_pkg == []: 
            if dir not in target_index:
                missing_dirs.append(dir)
                logger.info(f"{dir} not found in Preservica or target directory.\n")
            else:
                logger.info(f"{dir} not found in Preservica, found in target directory.\n")
                index_uuids.append(dir)
        else:
            logger.info(f"{dir} found in Preservica.\n")
            prsv_uuids.append(dir)

    print(" --- COMPARE SUMMARY --- ")
    logger.info(f"\nTotal packages checked: {len(source_dirs)}\nFound in Preservica: {len(prsv_uuids)}\nFound in target: {len(index_uuids)}\nMissing: {len(missing_dirs)}\n")

    current_deletion_list = [line for line in deletion_list_path.read_text().splitlines() if line.strip()]
    updated_list = [pkg for pkg in current_deletion_list if pkg not in prsv_uuids]
    final_list = sorted(list(set(updated_list + missing_dirs)))
    
    logger.info(f"Updating {deletion_list_path.name}...")
    logger.info(f"Removed {len(current_deletion_list) - len(updated_list)} already ingested packages from the list.")
    logger.info(f"Adding {len(final_list) - len(updated_list)} new missing packages to the list.")

    with open(deletion_list_path, "w") as f:
        for pkg in final_list:
            f.write(f"{pkg}\n")
            
    logger.info("\n Missing Packages:")
    if args.checklist:
        for name in final_list:
            list_logger.info(name)
    else:
        for name in missing_dirs:
            list_logger.info(name)

    if not args.checklist:
        failed_items = {}
        skipped_items = {}
        successful_copies = 0
        successful_moves = 0

        if args.copydir:
            print(f"Copying {len(missing_dirs)} packages.")
            with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
                futures = {
                    executor.submit(copy_single_pkg, [dir_name], source_index, copy_dir, logger): 
                    dir_name for dir_name in sorted(missing_dirs)
                }
                for future in sorted(as_completed(futures)):
                    dir_name = futures[future]
                    try:
                        copy_count, failed_dict = future.result()
                        successful_copies += copy_count
                        failed_items.update(failed_dict)
                    except Exception as e:
                        logger.error(f"Error during copying {dir_name}: {e}")
            logger.info(f"{successful_copies} packages copied successfully.\n{len(failed_items)} packages failed to copy.\n {failed_items if failed_items else ''}")
        
        if args.movedir:
            r_mode = True if args.rsync else False

            move_list = prsv_uuids if args.mvingested else missing_dirs
            print(f"Moving {len(move_list)} packages.")
            with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
                futures = {
                    executor.submit(move_single_pkg, [dir_name], source_index, move_dir, logger, r_mode): 
                    dir_name for dir_name in sorted(move_list)
                }
                for future in as_completed(futures):
                    dir_name = futures[future]
                    try:
                        move_count, failed_dict, skip_dict = future.result()
                        successful_moves += move_count
                        failed_items.update(failed_dict)
                        skipped_items.update(skip_dict)
                    except Exception as e:
                        logger.error(f"Error during moving {dir_name}: {e}")

        if args.copydir or args.movedir:
            print(" --- COPY / MOVE SUMMARY --- ")
            print(f"Copied: {successful_copies}, Moved: {successful_moves}, Failed: {len(failed_items)}, Skipped: {len(skipped_items)}")
    elif args.copydir or args.movedir:
        logger.warning("Copy and Move operations are ignored when using the --check-list flag.")

if __name__ == "__main__":
    main()