import logging
import json
import re
import requests
import time
import csv
import xml.etree.ElementTree as ET
from repair_tools.utils import prsv_api as prsvapi

logger = logging.getLogger(__name__)

class PreservicaAPI:
    BASE_URL = "https://nypl.preservica.com/api"
    WORKFLOW_URL = "https://nypl.preservica.com/sdb/rest/workflow/instances"
    CONTENT_URL = f"{BASE_URL}/content"
    ENTITY_URL = f"{BASE_URL}/entity"
    SLEEP_DURATION = 2

    def __init__(self, credentials: str):
        self.token = prsvapi.get_token(credentials)
        self.credentials_name = credentials # store for refresh
        
        if "test" in credentials:
            self.digarch_uuid = "c0b9b47a-5552-4277-874e-092b3cc53af6"
            self.ami_uuid = ""
            self.ingest_uuid = ""
        else:
            self.digarch_uuid = "e80315bc-42f5-44da-807f-446f78621c08"
            self.ami_uuid = "183a74b5-7247-4fb2-8184-959366bc0cbc"
            self.ingest_uuid = "380c6d78-0a8a-4843-b472-2199ba7fad72"
        
        self.headers = {
            "Preservica-Access-Token": self.token,
            "Content-Type": "application/xml;charset=UTF-8",
        }

    def _search_within(self, query_params: dict, parent_uuid: str, metadata: str = "''", max_results: str = "-1") -> requests.Response:
        query = json.dumps(query_params)
        
        url = (
            f"{self.CONTENT_URL}/search-within"
            f"?q={requests.utils.quote(query)}"
            f"&parenthierarchy={parent_uuid}"
            f"&start=0"
            f"&max={max_results}"
            f"&metadata={metadata}"
        )
        
        for attempt in range(3):
            try:
                res = requests.get(url, headers=self.headers, timeout=30)
                
                if res.status_code == 401:
                    logger.warning("Refreshing expired token...")
                    self._refresh_token()
                    continue 

                res.raise_for_status()
                return res
            except requests.RequestException as e:
                logger.warning(f"Request failed (attempt {attempt+1}): {e}")
                time.sleep(self.SLEEP_DURATION)
        
        logger.error(f"Search failed after 3 attempts for parent {parent_uuid}")
        return None

    def _parse_uuids(self, res: requests.Response) -> list:
        if res is None:
            return []
        
        try:
            json_obj = res.json()
            value = json_obj.get("value")
            if value and value.get("objectIds"):
                return [obj_id[-36:] for obj_id in json_obj["value"]["objectIds"]]
        except json.JSONDecodeError:
            logger.error(f"Failed to parse response: {res.text}")
            return []
        return []

    def _parse_first_uuid(self, res: requests.Response) -> str | None:
        if not res: return None
        try:
            data = res.json()
            if data.get("success") and data["value"]["totalHits"] > 0:
                metadata = data.get("value", {}).get("metadata", [])
                if metadata and metadata[0]:
                    return metadata[0][0].get("value")
                
                if data["value"].get("objectIds"):
                    return data["value"]["objectIds"][0][-36:]
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse UUID: {e}")
        return None

    def check_package_exists(self, pkg_id: str) -> bool:
        if pkg_id.startswith("M"):
            parent_uuid = self.digarch_uuid
            try:
                col_id = re.search(r"(M\d+)_(ER|DI|EM)_\d+", pkg_id).group(1)
            except AttributeError:
                logger.warning(f"Could not parse collection ID from {pkg_id}. Skipping.")
                return False
            query_params = {
                "q": "",
                "fields": [
                    {"name": "xip.title", "values": [pkg_id]},
                    {"name": "spec.specCollectionID", "values": [col_id]},
                ],
            }
        else:
            parent_uuid = self.ami_uuid
            query_params = {
                "q": "",
                "fields": [
                    {"name": "xip.title", "values": [f"{pkg_id}"]},
                    {"name": "xip.identifier", "values": ["DigitizedAMIContainer"]}
                ]
            }
        
        try:
            response = self._search_within(query_params, parent_uuid)
            uuids = self._parse_uuids(response)
        except Exception as e:
            logger.warning(f"Error checking {pkg_id}, retrying in {self.SLEEP_DURATION}s: {e}")
            time.sleep(self.SLEEP_DURATION)
            try:
                response = self._search_within(query_params, parent_uuid)
                uuids = self._parse_uuids(response)
            except Exception as e:
                logger.error(f"Failed to check {pkg_id} on retry: {e}")
                return False

        return bool(uuids)

    def fetch_uuid_by_title(self, pkg_title: str, parent_uuid: str) -> str | None:
        query_params = {"q": "", "fields": [{"name": "xip.title", "values": [pkg_title]}]}
        response = self._search_within(query_params, parent_uuid, metadata="id", max_results="10")
        return self._parse_first_uuid(response)

    def move_entity(self, entity_uuid: str, new_parent_uuid: str) -> bool:
        url = f"{self.ENTITY_URL}/structural-objects/{entity_uuid}/parent-ref"
        headers = self.headers.copy()
        headers["Content-Type"] = "text/plain"
        headers["accept"] = "text/plain;charset=UTF-8"

        try:
            response = requests.put(url, headers=headers, data=new_parent_uuid.strip())
            if response.status_code == 202:
                return True
            elif response.status_code == 401:
                self._refresh_token()
                headers["Preservica-Access-Token"] = self.token
                response = requests.put(url, headers=headers, data=new_parent_uuid.strip())
                return response.status_code == 202
                
            logger.error(f"FAILED to move {entity_uuid}. Status: {response.status_code}, Response: {response.text}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed during move: {e}")
            return False

    def get_wf_instances(self, state: str, from_date: str = None, to_date: str = None):
        ns = {'wf': 'http://workflow.preservica.com'}
        start = 0
        max_results = 100 
        total_count = None

        while total_count is None or start < total_count:
            params = {
                "state": state,
                "type": "Ingest",
                "start": str(start),
                "max": str(max_results),
                "includeActiveSteps": "false",
                "includeStepInputs": "false",
                "includeStartInputs": "false",
                "includeOutputs": "false",
                "latestFirst": "false"
            }

            if from_date: params["from"] = from_date
            if to_date: params["to"] = to_date

            response = None
            for attempt in range(3):
                try:
                    res = requests.get(self.WORKFLOW_URL, headers=self.headers, params=params, timeout=30)
                    if res.status_code == 401:
                        logger.warning("Refreshing token...")
                        self._refresh_token()
                        continue 
                    res.raise_for_status()
                    response = res
                    break
                except requests.RequestException as e:
                    logger.warning(f"Workflow request failed (attempt {attempt+1}): {e}")
                    time.sleep(self.SLEEP_DURATION)
            
            if not response:
                logger.error("Failed to retrieve workflows.")
                break

            try:
                root = ET.fromstring(response.content)
                
                if total_count is None:
                    total_node = root.find('wf:TotalCount', ns)
                    total_count = int(total_node.text) if total_node is not None else 0
                    logger.info(f"Total workflows found: {total_count}")

                instances = root.findall('wf:WorkflowInstance', ns)
                if not instances:
                    break

                for inst in instances:
                    row_data = {}
                    for child in inst:
                        tag_name = child.tag.split('}')[-1]
                        text_content = ""
                        
                        if len(child) > 0:
                            sub_data = []
                            for sub in child:
                                sub_tag = sub.tag.split('}')[-1]
                                sub_val = sub.text if sub.text else ""
                                sub_data.append(f"{sub_tag}: {sub_val}")
                            text_content = " | ".join(sub_data)
                        else:
                            text_content = child.text if child.text else ""

                        if tag_name in row_data:
                            row_data[tag_name] += f"; {text_content}"
                        else:
                            row_data[tag_name] = text_content
                    
                    yield row_data
                
                start += max_results
                time.sleep(0.5)
                
            except ET.ParseError as e:
                logger.error(f"Failed to parse XML: {e}")
                break

    def _refresh_token(self):
        self.token = prsvapi.get_token(self.credentials_name)
        self.headers["Preservica-Access-Token"] = self.token

#######################################################################################
