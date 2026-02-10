import argparse
import logging
import requests
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging
from repair_tools.utils.prsv_creds import Credentials

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--uuid",
        required=True,
        help="UUID of the SO to delete, ie. deletion folder"
    )
    parser.add_argument(
        "--credentials",
        required=True,
        help="The credential set to use."
    )
    parser.add_argument(
        "--comment",
        type=str,
        help="Comment to attach to the deletion action, ie. 'transient folder cleanup 2025-02-10'"
    )
    parser.add_argument(
        "--logpath",
        type=Path,
        default=Path("logs"),
        help="Directory for log files."
    )
    return parser.parse_args()

def get_username(credential_set: str) -> str:
    try:
        creds = Credentials()
        user, _, _ = creds.get_credentials(credential_set)
        return user
    except Exception as e:
        logging.error(f"Could not retrieve username for credentials '{credential_set}': {e}")
        return "unknown_user"

def delete_structural_object(api: PreservicaAPI, uuid: str, user: str, comment: str) -> str:
    url = f"{api.ENTITY_URL}/structural-objects/{uuid}"
    
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<DeletionAction>
    <Submission>
        <User>{user}</User>
        <Comment>{comment}</Comment>
    </Submission>
    <Approval>
        <Approved>true</Approved>
        <Comment>{comment}</Comment>
    </Approval>
</DeletionAction>"""

    headers = {
        "Preservica-Access-Token": api.token,
        "Content-Type": "application/xml;charset=UTF-8",
        "accept": "text/plain;charset=UTF-8"
    }

    logging.info(f"Sending DELETE request for SO: {uuid}")
    
    try:
        response = requests.delete(url, headers=headers, data=payload)
        
        if response.status_code in [200, 202]:
            progress_token = response.text.strip()
            logging.info(f"Deletion requested. Progress Token: {progress_token}")
            return progress_token
        else:
            logging.error(f"Delete request failed. Status: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return None
    except requests.RequestException as e:
        logging.error(f"Request Error during delete: {e}")
        return None

def monitor_progress(api: PreservicaAPI, progress_token: str) -> str:
    base_url = "https://nypl.preservica.com/api/processmonitor/progress"
    url = f"{base_url}/{progress_token}?includeErrors=true"
    
    headers = {
        "Preservica-Access-Token": api.token,
        "accept": "application/xml;charset=UTF-8"
    }

    logging.info(f"Deletion request now requires secondary approval: {progress_token}...")

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        xml_text = response.text.replace('xmlns="http://status.preservica.com"', '') 
        root = ET.fromstring(xml_text)
        status_node = root.find("Status")
        
        if status_node is not None:
            status = status_node.text
            logging.info(f"Current Status: {status}")
            
            if status == "PENDING": 
                return "PENDING"
            elif status == "COMPLETED":
                return "COMPLETED"
            elif status == "FAILED":
                error_node = root.find("Errors")
                if error_node is not None:
                    logging.error(f"Deletion Errors: {ET.tostring(error_node, encoding='unicode')}")
                return "FAILED"
            else:
                return status
        else:
            logging.warning("Could not parse Status from response.")
            return "UNKNOWN"

    except requests.RequestException as e:
        logging.error(f"Monitor request failed: {e}")
        return "ERROR"
    except ET.ParseError:
        logging.error(f"Failed to parse monitor XML: {response.text}")
        return "ERROR"

def main():
    args = parse_args()
    logger, _ = setup_logging(args.logpath / "delete_so.log")

    logger.info(f"Starting Delete SO for uuid: {args.uuid}")

    try:
        api = PreservicaAPI(args.credentials)
    except Exception as e:
        logger.critical(f"Failed to authenticate: {e}")
        sys.exit(1)

    user_email = get_username(args.credentials)
    # debug
    logger.info(f"Submitting deletion as user: {user_email}")

    progress_token = delete_structural_object(api, args.uuid, user_email, args.comment)

    if progress_token:
        final_status = monitor_progress(api, progress_token)
        
        logger.info("="*60)
        if final_status == "PENDING":
            logger.warning(f"Deletion is still pending and requires secondary approval. Progress Token: {progress_token}")
        elif final_status == "COMPLETED":
            logger.info(f"SUCCESS: {args.uuid} deleted successfully.")
        else:
            logger.error(f"FAILURE: Deletion failed with status: {final_status}")
    else:
        logger.error("Failed delete request, no progress token returned.")

if __name__ == "__main__":
    main()