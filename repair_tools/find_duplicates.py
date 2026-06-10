from repair_tools.utils.format_utils import print_standard_summary
import argparse
import logging
import json
import requests
import sys
from pathlib import Path
import concurrent.futures
import xml.etree.ElementTree as ET
from datetime import datetime

from repair_tools.utils import prsv_api_helpers 
from repair_tools.archive.create_pkg_report import requests_retry_session
from repair_tools.utils.preservica_search_parse import PreservicaAPI
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
        help="Flag to actually move the duplicates. Otherwise runs in dry-run mode."
        )
    parser.add_argument(
        "--compare", 
        action="store_true",
        required=True, # temporary restriction for EB use
        help="Flag to perform a detailed comparison of contents and checksums between duplicates."
        )
    parser.add_argument(
        "--logpath", 
        "-l",
        required=False,
        type=Path,
        help="Path to save the log file. Defaults to logs/find_duplicates_YYYYMMDD_HHMM.log"
        )
    
    args = parser.parse_args()

    if args.move and not args.deletion_parent:
        parser.error("The --move argument requires --deletion-parent to be specified.")

    return args

def setup_logger(log_path: Path):
    log_file = Path(log_path/f"find_duplicates_{datetime.now().strftime('%Y%m%d')}.log") if log_path else Path(f"logs/find_duplicates_{datetime.now().strftime('%Y%m%d')}.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    fh = logging.FileHandler(str(log_file), mode='a')
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(fh)
    
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
    root_logger.addHandler(ch)
    
    return log_file

def force_token_refresh(api: PreservicaAPI):
    logger.info("401 Unauthorized encountered. Forcing token refresh...")
    token_file = Path(f"{api.credentials_name}.token.file")
    if token_file.exists():
        token_file.unlink()
    
    api._refresh_token()

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
            force_token_refresh(api)
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
        logger.warning(f"Error parsing structural object {uuid}: {e}")
        return False

def fetch_ingest_date(api: PreservicaAPI, uuid: str, session) -> str | None:
    url = f"{api.ENTITY_URL}/structural-objects/{uuid}/event-actions?start=0&max=100"
    
    try:
        res = session.get(url, headers=api.headers, timeout=30)
        
        if res.status_code == 401:
            force_token_refresh(api)
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
        logger.warning(f"Failed to get ingest date for {uuid}: {e}")
    
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
                force_token_refresh(api)
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
        logger.warning(f"Failed to get object metadata at interval {start}: {e}")
        
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
            force_token_refresh(api)
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
    
    fields = [{"name": "xip.document_type", "values": ["SO"]}]
    
    if prefix:
        fields.append({"name": "xip.title", "values": [f"{prefix}*"]})

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
            force_token_refresh(api)
            res = sess.get(base_url, headers=api.headers, timeout=30)
        res.raise_for_status()
        data = res.json()
        total_hits = data.get("value", {}).get("totalHits", 0)
        logger.info(f"Total SOs to scan: {total_hits}")
    except Exception as e:
        logger.error(f"Failed SO scan: {e}")
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
                # added INFO:repair_tools fluff to keep progress bar in place
                sys.stderr.write(f"\rINFO:repair_tools.find_duplicates:Scanned {completed_count * max_results}/{total_hits} items...")
                sys.stderr.flush()

    sys.stderr.write("\n")
    sys.stderr.flush()

    duplicates = {t: uuids for t, uuids in title_map.items() if len(uuids) > 1}
    return duplicates

def normalize_filename(fname: str) -> str:
    """Normalizes filenames so .m4a equivalents map to .mp4 for comparison."""
    if fname.lower().endswith('.m4a'):
        return fname[:-4] + '.mp4'
    return fname

def compare_duplicate_contents(api: PreservicaAPI, title: str, valid_items: list, session):
    logger.info(f"[{title}] Running detailed content comparison for {len(valid_items)} packages...")
    
    uuid_contents = {}
    normalized_all = set()
    
    for item in valid_items:
        uuid = item['uuid']
        logger.info(f"Getting objects for UUID: {uuid} (Ingested: {item['date_str']})")
        contents = prsv_api_helpers.get_preservica_objects(api, uuid, session)
        uuid_contents[uuid] = contents
        for fname in contents.keys():
            normalized_all.add(normalize_filename(fname))
        
    if not normalized_all:
        logger.warning(f"No files found in any of the duplicate packages for '{title}'.")
        valid_items.sort(key=lambda x: x['date_obj'], reverse=True)
        return "RESOLVABLE", valid_items[0], valid_items[1:]

    header_uuids = " | ".join([f"{u[:8]}..." for u in uuid_contents.keys()])
    header = f"{'FILENAME':<55} | UUID:{header_uuids}"
    logger.info(header)
    logger.info("-" * len(header))
    
    has_conflicts = False
    
    for norm_fname in sorted(normalized_all):
        row = [f"{norm_fname:<55}"]
        file_hashes = []
        file_sizes = []
        actual_names = []
        
        for item in valid_items:
            uuid = item['uuid']
            
            actual_fname = None
            for fname in uuid_contents[uuid].keys():
                if normalize_filename(fname) == norm_fname:
                    actual_fname = fname
                    break
                    
            if actual_fname:
                f_info = uuid_contents[uuid][actual_fname]
                md5 = f_info['md5']
                size = f_info['size']
                file_hashes.append(md5)
                file_sizes.append(size)
                actual_names.append(actual_fname)
                
                # ext = actual_fname.split('.')[-1] if '.' in actual_fname else ''
                hash_display = f"{md5[:6] if md5 else 'NO MD5'}"
                row.append(f" {hash_display:<8}")
            else:
                row.append(f" {'MISSING':<7} ")
                file_hashes.append(None)
                file_sizes.append(None)
                actual_names.append(None)
        
        present_hashes = [h for h in file_hashes if h is not None]
        present_sizes = [s for s in file_sizes if s is not None]
        present_actual_names = [n for n in actual_names if n is not None]
        
        types_present = set(n.lower().split('.')[-1] for n in present_actual_names)
        is_mixed_mp4_m4a = types_present.issubset({'mp4', 'm4a'}) and len(types_present) > 1
        
        if len(present_hashes) < len(valid_items):
            row.append(" < MISSING IN SOME")
        elif is_mixed_mp4_m4a:
            row.append(" < MATCH (MP4/M4A ==)")
        elif (len(set(present_hashes)) > 1 and not is_mixed_mp4_m4a) or len(set(present_sizes)) > 1:
            has_conflicts = True
            row.append(" < CONFLICT")
        else:
            row.append(" < MATCH")
            
        logger.info(" |".join(row))
        
    complete_items = []
    incomplete_items = []
    for item in valid_items:
        uuid = item['uuid']
        pkg_norm_names = {normalize_filename(f) for f in uuid_contents[uuid].keys()}
        if pkg_norm_names == normalized_all:
            complete_items.append(item)
        else:
            incomplete_items.append(item)

    if has_conflicts:
        logger.warning(f"-> WARNING: Content mismatches (different hashes/sizes) found for '{title}'! Flagging for review.")
        logger.info("-" * 40 + "\n")
        return "REVIEW", None, None
        
    if len(complete_items) == 0:
        logger.warning(f"-> WARNING: All packages have missing file sets for '{title}'! Flagging for review.")
        logger.info("-" * 40 + "\n")
        return "REVIEW", None, None
        
    if len(complete_items) > 0 and len(incomplete_items) > 0:
        logger.info(f"-> WARNING: Found incomplete packages. Keeping the newest complete package.")
    else:
        logger.info(f"-> SUCCESS: All duplicate packages for '{title}' contain identical files and checksums.")
        
    logger.info("-" * 40 + "\n")
    
    complete_items.sort(key=lambda x: x['date_obj'], reverse=True)
    keep_item = complete_items[0]
    
    remove_items = complete_items[1:] + incomplete_items
    
    return "RESOLVABLE", keep_item, remove_items

def resolve_duplicate_uuids(api: PreservicaAPI, title: str, uuids: list, deletion_uuid: str, move_duplicate: bool, compare: bool, session) -> dict:
    if len(uuids) < 2:
        return None
    
    logger.info(f"Checking {len(uuids)} possible duplicates for '{title}'.")
    
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
        logger.info(f"Skipping '{title}': Not enough valid SOs/ingest dates found.")
        return None

    logger.info(f"Resolving {len(valid_items)} valid copies for '{title}'...")

    if compare:
        status, keep, remove = compare_duplicate_contents(api, title, valid_items, session)
        if status == "REVIEW":
            return {"status": "REVIEW", "items": valid_items}
    else:
        valid_items.sort(key=lambda x: x['date_obj'], reverse=True)
        keep = valid_items[0]
        remove = valid_items[1:]
        status = "RESOLVABLE"

    if status == "RESOLVABLE":
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
                
        return {"status": "RESOLVED", "keep": keep, "remove": remove}

def main():
    args = parse_args()
    
    # Initialize our logger configuration function
    log_file = setup_logger(args.logpath)

    logger.info(f"Starting find_duplicates. All logs will be saved to: {log_file}")
    
    api = PreservicaAPI(args.credentials)
    session = requests_retry_session()
    
    parent_uuid = api.ami_uuid if args.parent == "ami" else api.digarch_uuid
    
    # { "RESOLVED": { title: { "keep": item, "remove": [items] } }, "REVIEW": { title: [items] } }
    summary_results = {"RESOLVED": {}, "REVIEW": {}}
    
    if args.package:
        logger.info(f"Checking package: {args.package}")
        uuids = get_uuids_for_title(api, args.package, parent_uuid, session)
        if len(uuids) > 1:
            res = resolve_duplicate_uuids(api, args.package, uuids, args.deletion_parent, args.move, args.compare, session)
            if res:
                if res["status"] == "REVIEW":
                    summary_results["REVIEW"][args.package] = res["items"]
                else:
                    summary_results["RESOLVED"][args.package] = res
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
                    res = resolve_duplicate_uuids(api, title, uuids, args.deletion_parent, args.move, args.compare, session)
                    if res:
                        if res["status"] == "REVIEW":
                            summary_results["REVIEW"][title] = res["items"]
                        else:
                            summary_results["RESOLVED"][title] = res
                except Exception as e:
                    logger.error(f"Error processing duplicates for '{title}': {e}")

    summary = {
        "Resolved (or Dry-Run)": len(summary_results['RESOLVED']),
        "Flagged for Review":    len(summary_results['REVIEW']),
    }
    if summary_results['RESOLVED']:
        summary["Resolved Packages"] = [
            f"{title}: kept {data['keep']['date_str']}, removed {', '.join(i['date_str'] for i in data['remove'])}"
            for title, data in summary_results['RESOLVED'].items()
        ]
    if summary_results['REVIEW']:
        summary["Review Packages"] = [
            f"{title} ({len(items)}): {', '.join(i['date_str'] for i in items)}"
            for title, items in summary_results['REVIEW'].items()
        ]
    if not summary_results['RESOLVED'] and not summary_results['REVIEW']:
        summary["Status"] = "No valid duplicates resolved."
    print_standard_summary("Duplicates Summary", summary, logger=logger)

if __name__ == "__main__":
    main()