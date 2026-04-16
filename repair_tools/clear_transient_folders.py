import argparse
import logging
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--type",
        choices=["ami", "digarch"],
        required=True,
        help="Transient folders to search in: AMI or DigArch."
    )
    parser.add_argument(
        "--tenant",
        choices=["prod", "dev"],
        required=True,
        help="Tenant to search for transient folders."
    )
    parser.add_argument(
        "--credentials",
        required=True,
        help="The credential set to use. Note: dependent on tenant."
    )
    parser.add_argument(
        "--deletion-ref",
        "-dpf",
        required=True,
        help="The UUID of the destination folder (Deletion Folder)."
    )
    parser.add_argument(
        "--creation-date",
        help="Filter packages created ON or BEFORE this date (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--package-name",
        help="Specific package name to move (e.g. 123456). Skips scanning transient folders."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Debug without moving packages."
    )
    parser.add_argument(
        "--logpath",
        type=Path,
        default=Path("logs"),
        help="Directory for log files."
    )
    return parser.parse_args()

def get_xml(url: str, token: str) -> ET.Element:
    headers = {
        "Preservica-Access-Token": token,
        "Accept": "application/xml"
    }
    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        
        xml_text = res.text
        xml_text = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
        xml_text = re.sub(r'\sxmlns:xip="[^"]+"', '', xml_text, count=1)
        xml_text = re.sub(r'xip:', '', xml_text)
        
        return ET.fromstring(xml_text)
    except Exception as e:
        logging.error(f"Failed to fetch XML from {url}: {e}")
        return None

def check_date_criteria(api: PreservicaAPI, uuid: str, cutoff_date_str: str) -> bool:
    if not cutoff_date_str:
        return True 

    try:
        cutoff_date = datetime.strptime(cutoff_date_str, "%Y-%m-%d").date()
    except ValueError:
        logging.error(f"Invalid date format: {cutoff_date_str}.")
        return False

    url = f"{api.ENTITY_URL}/structural-objects/{uuid}/event-actions?start=0&max=100"
    root = get_xml(url, api.token)
    
    if root is None:
        return False

    creation_date_str = None
    
    # EventAction(command_create) > Event(Ingest) > Date
    for action in root.findall(".//EventAction"):
        if action.get("commandType") == "command_create":
            event = action.find("Event")
            if event is not None and event.get("type") == "Ingest":
                date_node = event.find("Date")
                if date_node is not None:
                    creation_date_str = date_node.text
                    break
    
    if not creation_date_str:
        for action in root.findall(".//EventAction"):
             if action.get("commandType") == "command_create":
                 date_node = action.find("Date")
                 if date_node is not None:
                     creation_date_str = date_node.text
                     break

    if not creation_date_str:
        logging.warning(f"Could not find creation/ingest date for {uuid}. Skipping.")
        return False

    try:
        clean_date_str = creation_date_str.replace("Z", "+00:00")
        pkg_datetime = datetime.fromisoformat(clean_date_str)
        pkg_date = pkg_datetime.date()
        
        if pkg_date <= cutoff_date:
            # logging.info(f"  TO BE MOVED: Package {pkg_date} <= {cutoff_date}")
            return True
        else:
            # logging.info(f"  SKIPPED: Package {pkg_date} > {cutoff_date}") # current ingest
            return False
            
    except ValueError as e:
        logging.error(f"Error parsing API date '{creation_date_str}': {e}")
        return False

def get_children_with_titles(api: PreservicaAPI, parent_uuid: str) -> dict:
    results = {}
    start = 0
    max_results = 100
    
    while True:
        url = f"{api.ENTITY_URL}/structural-objects/{parent_uuid}/children?start={start}&max={max_results}"
        root = get_xml(url, api.token)
        
        if root is None:
            break
            
        children = root.findall(".//Child")
        if not children:
            break
            
        found_on_page = 0
        for child in children:
            if child.get("type") == "SO":
                ref = child.get("ref")
                title = child.get("title")
                if ref and title:
                    results[ref] = title
            found_on_page += 1
            
        if found_on_page < max_results:
            break
            
        start += max_results
        
    return results

def main():
    args = parse_args()
    logger, _ = setup_logging(args.logpath / f"clear_transient_{args.type}.log")

    api = PreservicaAPI(args.credentials)
    if args.tenant == "prod":
        if args.type == "ami":
            # AMI25_1
            holding_uuid = "82d97497-7883-4e9f-87b6-f2ab6895f157"
            holding_title = "AMI25_1"
            transient_folder_regex = re.compile(r"^AMI\d{3}$")
            package_regex = re.compile(r"^\d{6}$")
        else:
            # DigArch
            holding_uuid = "edce9abe-1c08-4c85-abf7-6cf100196a6d"
            holding_title = "DigArch"
            transient_folder_regex = re.compile(r"^DIGARCH\d{3}$", re.IGNORECASE)
            package_regex = re.compile(r"^M\d+_((ER)|(DI)|(EM))_\d+.*$")
    else:
        if args.type == "ami":
            #AMI_Ingest
            holding_uuid = "a1df33ab-9069-4a87-af7f-470a3b3de33f"
            holding_title = "AMI_Ingest(DEV)"
            transient_folder_regex = re.compile(r"^AMI\d{3}$")
            package_regex = re.compile(r"^\d{6}$")
        else:
            # DigArch_Ingest
            holding_uuid = "9ad66df6-a1a4-4989-9c10-d0d8c4dcaa63"
            holding_title = "DigArch Ingest(DEV)"
            transient_folder_regex = re.compile(r"^DIGARCH\d{3}$", re.IGNORECASE)
            package_regex = re.compile(r"^M\d+_((ER)|(DI)|(EM))_\d+.*$")

    packages_to_move = {} # uuid: title

    if args.package_name:
        logger.info(f"Searching for package '{args.package_name}' in {holding_title}...")
        found_uuid = api.fetch_uuid_by_title(args.package_name, holding_uuid)
        
        if found_uuid:
            if args.creation_date:
                if check_date_criteria(api, found_uuid, args.creation_date):
                    packages_to_move[found_uuid] = args.package_name
            else:
                packages_to_move[found_uuid] = args.package_name
                logger.info(f"  Found: {args.package_name} ({found_uuid})")
        else:
            logger.error(f"Package '{args.package_name}' not found.")
            return
            
    else:
        logger.info("Searching for transient folders...")
        holding_children = get_children_with_titles(api, holding_uuid)
        
        transient_folders = {}
        for uuid, title in holding_children.items():
            if transient_folder_regex.match(title):
                transient_folders[uuid] = title

        logger.info(f"Found {len(transient_folders)} transient folders.")

        for t_uuid, t_title in transient_folders.items():
            logger.info(f"Checking {t_title}")
            t_children = get_children_with_titles(api, t_uuid)
            
            count_in_folder = 0
            for p_uuid, p_title in t_children.items():
                if package_regex.match(p_title):
                    if args.creation_date:
                        if not check_date_criteria(api, p_uuid, args.creation_date):
                            continue

                    packages_to_move[p_uuid] = p_title
                    count_in_folder += 1
                    # logger.info(f"  Found: {p_title}")
            
            if count_in_folder > 0:
                logger.info(f"  > {count_in_folder} of {len(t_children.items())} matching packages found in {t_title}: {len(packages_to_move)} total.")

    if not packages_to_move:
        logger.info("No matching packages found to move.")
        return

    logger.info("="*60)
    logger.info(f"Found {len(packages_to_move)} total packages to delete.")
    logger.info("="*60)

    success_count = 0
    fail_count = 0

    for p_uuid, p_title in packages_to_move.items():
        if args.debug:
            logger.info(f"[DEBUG] Would move '{p_title}' ({p_uuid}) to deletion folder: {args.deletion_ref}")
            success_count += 1
        else:
            logger.info(f"Moving '{p_title}'")
            if api.move_entity(p_uuid, args.deletion_ref):
                # logger.info(f"SUCCESS: Moved '{p_title}'")
                success_count += 1
            else:
                logger.error(f"FAILURE: Could not move '{p_title}'")
                fail_count += 1

    logger.info("="*60)
    logger.info("SUMMARY")
    logger.info(f"Total Found: {len(packages_to_move)}")
    logger.info(f"Moved:       {success_count}")
    logger.info(f"Failed:      {fail_count}")

if __name__ == "__main__":
    main()