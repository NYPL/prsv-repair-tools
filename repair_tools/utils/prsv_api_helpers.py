import re
import xml.etree.ElementTree as ET
import requests
import logging
from typing import Optional

def get_pkg_title(accesstoken: str, pkg_uuid: str) -> Optional[str]:
    get_so_url = f"https://nypl.preservica.com/api/entity/structural-objects/{pkg_uuid}"
    headers = {"Preservica-Access-Token": accesstoken, "Accept": "application/xml"}
    res = requests.get(get_so_url, headers=headers)
    if res.status_code != 200: return None
    root = ET.fromstring(res.text)
    version_search = re.search(r"v(\d+\.\d+)\}", root.tag)
    version = version_search.group(1) if version_search else "7.0"
    title = root.find(f".//{{http://preservica.com/XIP/v{version}}}Title")
    return title.text.strip() if title is not None else None

def _get_entity_xml(accesstoken: str, url: str, params: dict = None) -> Optional[ET.Element]:
    headers = {"Preservica-Access-Token": accesstoken, "accept": "application/xml;charset=UTF-8"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return ET.fromstring(response.text)
    except Exception as e:
        full_url = response.url if 'response' in locals() else url
        logging.getLogger(__name__).error(f"API request failed for URL {full_url}: {e}")
        return None

def get_so_children(accesstoken: str, uuid: str) -> list:
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{uuid}/children?max=1000"
    root = _get_entity_xml(accesstoken, url)
    if root is None:
        return []
        
    version_search = re.search(r"v(\d+\.\d+)\}", root.tag)
    version = version_search.group(1) if version_search else "7.0"
    
    children = []
    for child in root.findall(f".//{{http://preservica.com/EntityAPI/v{version}}}Children"):
        child_type = child.get('type')
        child_ref = child.get('ref')
        if child_type and child_ref:
             children.append({"type": child_type, "ref": child_ref})
             
    return children

def find_all_children(accesstoken: str, uuid: str) -> list:
    all_objects = []
    stack = [uuid]
    
    while stack:
        current_uuid = stack.pop()
        children = get_so_children(accesstoken, current_uuid)
        
        for child in children:
            all_objects.append(child)
            if child["type"] == "SO":
                stack.append(child["ref"])
                
    return all_objects

def get_preservica_objects(accesstoken: str, folder_uuid: str) -> list:
    children = find_all_children(accesstoken, folder_uuid)
    return [c for c in children if c['type'] == 'IO']
