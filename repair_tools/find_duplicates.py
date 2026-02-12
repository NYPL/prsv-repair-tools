# original
import argparse
import logging
import json
import requests
import concurrent.futures
import xml.etree.ElementTree as ET
from datetime import datetime

from repair_tools.archive.create_pkg_report import requests_retry_session
from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging
from repair_tools.utils import prsv_api

logger = logging.getLogger(__name__)

MAX_THREADS = 8

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--package", 
        "-p", 
        required=False,
        help="Specific package title to check. If omitted, scans the entire parent folder."
        )
    parser.add_argument(
        "--prefix",
        required=False,
        help="Filter scan to packages starting with this prefix (e.g., '123')."
        )
    parser.add_argument(
        "--deletion-parent", 
        "-dp", 
        required=False,
        help="Deletion Folder UUID (Required if --move is set)")
    parser.add_argument(
        "--credentials", 
        required=True
        )
    parser.add_argument(
        "--parent", 
        type=str, 
        choices=["digarch", "ami"], 
        default="ami", 
        required=True,
        help="Parent hierarchy context"
        )
    parser.add_argument(
        "--move", 
        action="store_true",
        help="If set, actually moves the duplicates. Otherwise runs in dry-run mode."
        )
    
    args = parser.parse_args()

    if args.move and not args.deletion_parent:
        parser.error("The --move argument requires --deletion-parent to be specified.")

    return args

def parse_iso_date(date_str):
    try:
        if date_str.endswith('Z'):
            date_str = date_str.replace('Z', '+00:00')
        return datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        logger.warning(f"Could not parse date: {date_str}")
        return datetime.min

def check_structural_object(api: PreservicaAPI, uuid: str, session) -> bool:
    url = f"{api.ENTITY_URL}/structural-objects/{uuid}"
    
    try:
        res = session.get(url, headers=api.headers, timeout=30)
        
        if res.status_code == 401:
            api._refresh_token()
            res = session.get(url, headers=api.headers, timeout=30)
            
        if res.status_code == 404:
            return False
            
        res.raise_for_status()
        
        root = ET.fromstring(res.text)
        version = prsv_api.find_apiversion(api.credentials_name)
        namespaces = {'xip': f'http://preservica.com/XIP/v{version}'}
        
        title_elem = root.find(".//xip:Title", namespaces)
        # double checking sub folders didn't slip in
        if title_elem is not None and title_elem.text:
            title = title_elem.text
            if "_metadata" in title or "_contents" in title or "_media" in title:
                return False
        else:
            for elem in root.iter():
                if elem.tag.endswith("Title") and elem.text:
                    if "_metadata" in elem.text or "_contents" in elem.text or "_media" in elem.text:
                        return False
                    return True
        return True

    except Exception as e:
        logger.warning(f"Error validating structural object {uuid}: {e}")
        return False

def fetch_ingest_date(api: PreservicaAPI, uuid: str, session) -> str | None:
    url = f"{api.ENTITY_URL}/structural-objects/{uuid}/event-actions?start=0&max=100"
    
    try:
        res = session.get(url, headers=api.headers, timeout=30)
        
        if res.status_code == 401:
            api._refresh_token()
            res = session.get(url, headers=api.headers, timeout=30)
        
        if res.status_code == 404:
            return None
            
        res.raise_for_status()
        
        root = ET.fromstring(res.text)
        version = prsv_api.find_apiversion(api.credentials_name)
        namespaces = {'xip': f'http://preservica.com/XIP/v{version}'}
        
        for action in root.findall(".//xip:EventAction", namespaces):
            if action.get("commandType") == "command_create":
                event = action.find("xip:Event", namespaces)
                if event is not None and event.get("type") == "Ingest":
                    date_elem = event.find("xip:Date", namespaces)
                    if date_elem is not None:
                        return date_elem.text

        for action in root:
            if action.tag.endswith("EventAction") and action.get("commandType") == "command_create":
                for event in action:
                    if event.tag.endswith("Event") and event.get("type") == "Ingest":
                        for child in event:
                            if child.tag.endswith("Date"):
                                return child.text

    except Exception as e:
        logger.warning(f"Failed to fetch event actions for {uuid}: {e}")
    
    return None

def fetch_search_page_worker(api, query, parent_uuid, start, max_results):
    url = (
        f"{api.CONTENT_URL}/search-within"
        f"?q={requests.utils.quote(query)}"
        f"&parenthierarchy={parent_uuid}"
        f"&start={start}"
        f"&max={max_results}"
        f"&metadata=xip.title"
    )
    
    found_items = []
    try:
        with requests_retry_session() as session:
            res = session.get(url, headers=api.headers, timeout=30)
            
            if res.status_code == 401:
                api._refresh_token()
                # Get fresh headers with new token
                headers = api.headers.copy()
                res = session.get(url, headers=headers, timeout=30)
            
            res.raise_for_status()
            data = res.json()
            
            if data.get("success"):
                value = data.get("value", {})
                object_ids = value.get("objectIds", [])
                metadata = value.get("metadata", [])
                
                for i, uuid_url in enumerate(object_ids):
                    uuid = uuid_url[-36:]
                    title = None
                    if i < len(metadata) and metadata[i]:
                        title = metadata[i][0].get("value")
                    
                    if title:
                        found_items.append((title, uuid))
                        
    except Exception as e:
        logger.warning(f"Failed to fetch page starting at {start}: {e}")
        
    return found_items

def process_item_worker(api, uuid, session):
    if not check_structural_object(api, uuid, session):
        return None
    
    date_val = fetch_ingest_date(api, uuid, session)
    if not date_val:
        return None
        
    return {
        "uuid": uuid,
        "date_str": date_val,
        "date_obj": parse_iso_date(date_val)
    }

def get_uuids_for_title(api: PreservicaAPI, pkg_title: str, parent_uuid: str, session) -> list:
    # Restrict single-package search to SOs as well
    query_params = {
        "q": "", 
        "fields": [
            {"name": "xip.title", "values": [pkg_title]},
            {"name": "xip.document_type", "values": ["SO"]}
        ]
    }
    query = json.dumps(query_params)
    
    url = (
        f"{api.CONTENT_URL}/search-within"
        f"?q={requests.utils.quote(query)}"
        f"&parenthierarchy={parent_uuid}"
        f"&start=0"
        f"&max=100"
        f"&metadata=''" 
    )

    uuids = []
    try:
        res = session.get(url, headers=api.headers, timeout=30)
        if res.status_code == 401:
            api._refresh_token()
            res = session.get(url, headers=api.headers, timeout=30)
        res.raise_for_status()
        data = res.json()

        if data.get("success") and data.get("value", {}).get("totalHits", 0) > 0:
            value = data["value"]
            object_ids = value.get("objectIds", [])
            uuids = [u[-36:] for u in object_ids]
                    
    except Exception as e:
        logger.error(f"Error searching for {pkg_title}: {e}")

    return uuids

def scan_parent_for_duplicates(api: PreservicaAPI, parent_uuid: str, prefix: str = None, session=None) -> dict:
    logger.info(f"Scanning parent {parent_uuid}.")
    
    title_map = {}
    
    # Build Query: Always filter by SO, optionally add Prefix
    fields = [{"name": "xip.document_type", "values": ["SO"]}]
    
    if prefix:
        fields.append({"name": "xip.title", "values": [f"{prefix}*"]})
        # logger.info(f"Filter: Prefix='{prefix}*', Type='SO'")
    # else:
        # logger.info(f"Filter: Type='SO' (All)")

    query = json.dumps({"q": "", "fields": fields})
    
    max_results = 100
    
    base_url = (
        f"{api.CONTENT_URL}/search-within"
        f"?q={requests.utils.quote(query)}"
        f"&parenthierarchy={parent_uuid}"
        f"&start=0"
        f"&max=1"
        f"&metadata=xip.title"
        )
    
    sess = session if session else requests.Session()
        
    try:
        res = sess.get(base_url, headers=api.headers, timeout=30)
        if res.status_code == 401:
            api._refresh_token()
            res = sess.get(base_url, headers=api.headers, timeout=30)
        res.raise_for_status()
        data = res.json()
        total_hits = data.get("value", {}).get("totalHits", 0)
        logger.info(f"Total SOs to scan: {total_hits}")
    except Exception as e:
        logger.error(f"Failed initial scan request: {e}")
        return {}

    if total_hits == 0:
        return {}

    offsets = range(0, total_hits, max_results)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        future_to_offset = {
            executor.submit(fetch_search_page_worker, api, query, parent_uuid, start, max_results): start 
            for start in offsets
        }
        
        completed_count = 0
        for future in concurrent.futures.as_completed(future_to_offset):
            results = future.result()
            for title, uuid in results:
                
                if "_metadata" in title or "_contents" in title or "_media" in title:
                    continue

                if title not in title_map:
                    title_map[title] = []
                title_map[title].append(uuid)
            
            completed_count += 1
            if completed_count % 10 == 0:
                logger.info(f"Scanned {completed_count * max_results}/{total_hits} items...")

    duplicates = {t: uuids for t, uuids in title_map.items() if len(uuids) > 1}
    return duplicates

def resolve_duplicate_uuids(api: PreservicaAPI, title: str, uuids: list, deletion_uuid: str, move_duplicate: bool, session) -> list:
    if len(uuids) < 2:
        return None
    
    logger.info(f"Checking {len(uuids)} potential duplicates for '{title}'.")
    
    valid_items = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        future_to_uuid = {
            executor.submit(process_item_worker, api, uuid, session): uuid 
            for uuid in uuids
        }
        
        for future in concurrent.futures.as_completed(future_to_uuid):
            result = future.result()
            if result:
                valid_items.append(result)

    if len(valid_items) < 2:
        logger.info(f"Skipping '{title}': Not enough valid Structural Objects/Dates found.")
        return None

    logger.info(f"Resolving {len(valid_items)} valid copies for '{title}'...")

    valid_items.sort(key=lambda x: x['date_obj'], reverse=True)

    keep = valid_items[0]
    remove = valid_items[1:]
    
    if move_duplicate:
        logger.info(f"KEEPING: {keep['uuid']} (Ingested: {keep['date_str']})")
        
        for item in remove:
            logger.info(f"MOVING DUPLICATE: {item['uuid']} (Ingested: {item['date_str']}) -> {deletion_uuid}")
            success = api.move_entity(item['uuid'], deletion_uuid)
            if success:
                logger.info(f"Successfully moved {item['uuid']}")
            else:
                logger.error(f"Failed to move {item['uuid']}")
    else:
        logger.info(f"[DRY RUN] Would KEEP {keep['uuid']} ({keep['date_str']})")
        for item in remove:
            logger.info(f"[DRY RUN] Would MOVE {item['uuid']} ({item['date_str']})")
            
    return valid_items

def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO)
    
    api = PreservicaAPI(args.credentials)
    session = requests_retry_session()
    
    parent_uuid = api.ami_uuid if args.parent == "ami" else api.digarch_uuid
    
    # { title: [item_dict, ...] }
    summary_results = {}
    
    if args.package:
        logger.info(f"Checking single package: {args.package}")
        uuids = get_uuids_for_title(api, args.package, parent_uuid, session)
        if len(uuids) > 1:
            items = resolve_duplicate_uuids(api, args.package, uuids, args.deletion_parent, args.move, session)
            if items:
                summary_results[args.package] = items
        else:
            logger.info("No duplicates found.")
            
    else:
        duplicates_map = scan_parent_for_duplicates(api, parent_uuid, args.prefix, session)
        
        if not duplicates_map:
            logger.info("Scan complete. No duplicates found.")
        else:
            logger.info(f"Scan complete. Found {len(duplicates_map)} titles with duplicates.")
            
            for title, uuids in duplicates_map.items():
                try:
                    items = resolve_duplicate_uuids(api, title, uuids, args.deletion_parent, args.move, session)
                    if items:
                        summary_results[title] = items
                except Exception as e:
                    logger.error(f"Error processing duplicates for '{title}': {e}")

    # --- SUMMARY ---
    if summary_results:
        print("\nDuplicates Summary:")
        for title, items in summary_results.items():
            # Format: Title (Count): date1, date2
            dates = [i['date_str'] for i in items]
            print(f"{title} ({len(items)}): {', '.join(dates)}")
    else:
        print("\nNo valid duplicates resolved.")

if __name__ == "__main__":
    main()