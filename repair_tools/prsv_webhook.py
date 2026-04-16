from flask import Flask, request, jsonify, abort, render_template_string
import hmac
import hashlib
import os
import json
import sqlite3
import pytz
import xml.etree.ElementTree as ET
import requests
import logging
import threading
import shutil
import tempfile
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import repair_tools.utils.prsv_api as prsvapi
import repair_tools.cli as prsvcli

# create app
app = Flask(__name__)

def get_webhook_secret():
    # first check for environment variable, then look for the token file in the project root
    # token_file = os.environ.get("WEBHOOK_TOKEN_FILE", "/Users/emileebuytkins/Documents/Buytkins_Programming/repair-tools/prod-ingest.token.file")
    # try:
    #     with open(token_file, "r") as f:
    #         secret = f.readline().strip()
    #         return secret
    # except Exception as e:
        # fallback to the hardcoded value if file is missing, though this may cause 401s
    # the webhook secret is static
    return "1776187381.344287"

WEBHOOK_SECRET = get_webhook_secret()
DB_FILE = Path.cwd() / "databases/webhook_events.db"

# --- SETUP LOGGER ---
def setup_logging(log_file: Path):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # clear existing handlers
    if logger.hasHandlers():
        logger.handlers.clear()

    # file handler
    log_file_formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(str(log_file), mode='a')
    fh.setFormatter(log_file_formatter)
    logger.addHandler(fh)

    # console handler
    console_formatter = logging.Formatter('%(threadName)s - %(levelname)s - %(message)s')
    ch = logging.StreamHandler()
    ch.setFormatter(console_formatter)
    logger.addHandler(ch)

def parse_args():
    parser = prsvcli.Parser()
    parser.add_argument(
        "--host",
        type=str,
        required=False,
        default='0.0.0.0',
        help="Host IP to bind the server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=False,
        default=5000,
        help='Port to bind the server (default: 5000)',
    )
    parser.add_argument(
        "--container-db",
        type=str,
        required=False,
        help="Path to the secondary container database (optional)"
    )
    parser.add_argument(
        "--retry-containers",
        action="store_true",
        help="Retry looking up container names for existing events with 'N/A' values"
    )
    return parser.parse_args()

def compute_hmac(secret, message):
    return hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT UNIQUE, 
            trigger TEXT NOT NULL,
            event_json TEXT NOT NULL,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        c.execute("ALTER TABLE events ADD COLUMN CO_md5 TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE events ADD COLUMN package_title TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE events ADD COLUMN container_name TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def insert_event(trigger, event_json, event_key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO events (event_key, trigger, event_json) VALUES (?, ?, ?)",
        (event_key, trigger, event_json)
    )
    conn.commit()
    conn.close()

def get_events():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT event_json, received_at, CO_md5, package_title, container_name FROM events ORDER BY received_at DESC"
    )
    rows = c.fetchall()
    conn.close()
    
    all_events = []
    for event_json, received_at, CO_md5, pkg_title, cont_name in rows:
        event = json.loads(event_json)
        event["_received_at"] = received_at
        event["package_title"] = pkg_title or "N/A"
        event["container_name"] = cont_name or "N/A"
        # if CO_md5:
        #     try:
        #         event["CO_md5"] = json.loads(CO_md5)
        #     except:
        #         event["CO_md5"] = CO_md5
        # else:
        #     event["CO_md5"] = "Not yet processed"
        pass
        all_events.append(event)
    return all_events

def update_bitstream_results(event_key, results_json):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE events SET CO_md5 = ? WHERE event_key = ?",
        (results_json, event_key)
    )
    conn.commit()
    conn.close()

def update_event_metadata(event_key, package_title, container_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE events SET package_title = ?, container_name = ? WHERE event_key = ?",
        (package_title, container_name, event_key)
    )
    conn.commit()
    conn.close()

def lookup_container_name(db_path, package_title):
    if not db_path or not package_title or package_title == "N/A":
        return None
    
    temp_db = None
    try:
        # Create a temporary copy to avoid issues if the original is being replaced/overwritten
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tf:
            temp_db = tf.name
        
        shutil.copy2(db_path, temp_db)
        
        # Connect to the copy
        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        # Search for package_title in container_name column using LIKE
        query = "SELECT container_name FROM workflows WHERE container_name LIKE ?"
        c.execute(query, (f"%{package_title}%",))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logging.getLogger(__name__).error(f"Error querying container DB: {e}")
        return None
    finally:
        # Clean up the temporary copy
        if temp_db and os.path.exists(temp_db):
            try:
                os.remove(temp_db)
            except:
                pass

def fetch_and_update_metadata(entity_ref, credentials_to_use, event_key, container_db_path):
    logger = logging.getLogger(__name__)
    try:
        accesstoken = prsvapi.get_token(credential_set=credentials_to_use)
        uuid = entity_ref.split('/')[-1]
        
        # 1. Get package title
        title = get_pkg_title(accesstoken, uuid)
        if not title:
            title = "Title Not Found"
            
        # 2. Lookup container name
        container_name = lookup_container_name(container_db_path, title)
        
        # 3. Update primary database
        update_event_metadata(event_key, title, container_name)
        logger.info(f"Updated metadata for {event_key}: Title={title}, Container={container_name}")
        
    except Exception as e:
        logger.error(f"Error in metadata fetch for {entity_ref}: {e}")

def retry_container_lookups(container_db_path, credentials_to_use="prod-ingest"):
    logger = logging.getLogger(__name__)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Find records with N/A or empty container_name
    c.execute("SELECT event_key, event_json, package_title FROM events WHERE (container_name IS NULL OR container_name = 'N/A')")
    rows = c.fetchall()
    conn.close()

    if not rows:
        logger.info("No records found with missing container names to retry.")
        return

    logger.info(f"Retrying container lookups for {len(rows)} records...")
    accesstoken = None

    for event_key, event_json, pkg_title in rows:
        title = pkg_title
        if title == "N/A" or not title:
            # Need to fetch title from Preservica
            try:
                data = json.loads(event_json)
                if data.get('events'):
                    entity_ref = data['events'][0].get('entityRef')
                    if entity_ref:
                        if not accesstoken:
                            accesstoken = prsvapi.get_token(credential_set=credentials_to_use)
                        uuid = entity_ref.split('/')[-1]
                        title = get_pkg_title(accesstoken, uuid)
            except Exception as e:
                logger.error(f"Could not fetch title for retry on {event_key}: {e}")
        
        if title and title != "N/A":
            container_name = lookup_container_name(container_db_path, title)
            if container_name or (pkg_title != title):
                update_event_metadata(event_key, title, container_name)
                logger.info(f"Refreshed metadata for {event_key}: Title={title}, Container={container_name}")

def _get_entity_xml(accesstoken: str, url: str) -> Optional[ET.Element]:
    headers = {
        "Preservica-Access-Token": accesstoken,
        "accept": "application/xml;charset=UTF-8"
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return ET.fromstring(response.text)
    except Exception as e:
        logging.getLogger(__name__).error(f"API request failed for URL {url}: {e}")
        return None

def get_so_children(accesstoken: str, parent_uuid: str, version: str, namespaces: dict) -> list:
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{parent_uuid}/children?start=0&max=100"
    root = _get_entity_xml(accesstoken, url)
    children_data = []
    if root is not None:
        for child in root.findall('.//entity:Child', namespaces):
            children_data.append({
                'ref': child.get('ref'),
                'type': child.get('type')
            })
    return children_data

def find_all_children(accesstoken: str, parent_uuid: str, version: str, io_refs: list, namespaces: dict):
    children = get_so_children(accesstoken, parent_uuid, version, namespaces)
    for child in children:
        if child['type'] == 'IO':
            io_refs.append(child['ref'])
        elif child['type'] == 'SO':
            find_all_children(accesstoken, child['ref'], version, io_refs, namespaces)

def get_bitstreams_for_io(accesstoken: str, io_ref: str, version: str, namespaces: dict) -> list:
    # 1. Get representations
    rep_url = f"https://nypl.preservica.com/api/entity/information-objects/{io_ref}/representations"
    rep_root = _get_entity_xml(accesstoken, rep_url)
    bitstream_data = []
    
    if rep_root is None:
        return bitstream_data

    for rep in rep_root.findall('.//entity:Representation', namespaces):
        rep_type = rep.get('type')
        # 2. Get COs for each representation
        co_url = f"https://nypl.preservica.com/api/entity/information-objects/{io_ref}/representations/{rep_type}"
        co_root = _get_entity_xml(accesstoken, co_url)
        if co_root is None:
            continue
            
        co_refs = []
        for co in co_root.findall('.//xip:ContentObjects/xip:ContentObject', namespaces):
            if co.text: co_refs.append(co.text.strip())
        for co in co_root.findall('.//entity:ContentObjects/entity:ContentObject', namespaces):
            if co.get('ref'): co_refs.append(co.get('ref'))
            
        for co_ref in co_refs:
            # 3. Get generations for each CO
            gen_url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations"
            gen_list_root = _get_entity_xml(accesstoken, gen_url)
            if gen_list_root is None:
                continue
                
            for gen_elem in gen_list_root.findall('.//entity:Generation', namespaces):
                gen_id = gen_elem.text.split('/')[-1]
                # 4. Get bitstreams for each generation
                bs_list_url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations/{gen_id}"
                bs_list_root = _get_entity_xml(accesstoken, bs_list_url)
                if bs_list_root is None:
                    continue
                    
                for bs_elem in bs_list_root.findall('.//entity:Bitstream', namespaces):
                    bs_url = bs_elem.text
                    bs_root = _get_entity_xml(accesstoken, bs_url)
                    if bs_root is None:
                        continue
                        
                    filename = bs_root.find('.//xip:Filename', namespaces)
                    md5_val = "N/A"
                    for fixity in bs_root.findall('.//xip:Fixity', namespaces):
                        alg = fixity.find('xip:FixityAlgorithmRef', namespaces)
                        val = fixity.find('xip:FixityValue', namespaces)
                        if alg is not None and alg.text == 'MD5':
                            md5_val = val.text
                            break
                    
                    bitstream_data.append({
                        'filename': filename.text if filename is not None else 'Unknown',
                        'md5': md5_val,
                        'co_ref': co_ref
                    })
    return bitstream_data

def crawl_for_bitstreams(entity_ref, credentials_to_use, event_key, update_callback):
    """Background task to discover all bitstreams and their MD5s."""
    logger = logging.getLogger(__name__)
    logger.info(f"Starting crawl for {entity_ref}")
    
    try:
        accesstoken = prsvapi.get_token(credential_set=credentials_to_use)
        version = prsvapi.find_apiversion(credential_set=credentials_to_use)
        namespaces = {
            'xip': f'http://preservica.com/XIP/v{version}',
            'entity': f'http://preservica.com/EntityAPI/v{version}'
        }
        
        io_refs = []
        # Check if entity is SO or IO
        if "structural-objects" in entity_ref or len(entity_ref) == 36:
            # Assume it might be an SO or a bare UUID which we treat as SO first
            so_url = f"https://nypl.preservica.com/api/entity/structural-objects/{entity_ref.split('/')[-1]}"
            so_root = _get_entity_xml(accesstoken, so_url)
            if so_root is not None:
                find_all_children(accesstoken, entity_ref.split('/')[-1], version, io_refs, namespaces)
            else:
                # Try as IO
                io_url = f"https://nypl.preservica.com/api/entity/information-objects/{entity_ref.split('/')[-1]}"
                io_root = _get_entity_xml(accesstoken, io_url)
                if io_root is not None:
                    io_refs.append(entity_ref.split('/')[-1])
        
        results = {}
        for io_ref in io_refs:
            bitstreams = get_bitstreams_for_io(accesstoken, io_ref, version, namespaces)
            for bs in bitstreams:
                results[bs['filename']] = bs['md5']
        
        if not results:
            logger.warning(f"No bitstreams found for {entity_ref}")
            update_callback(event_key, "No bitstreams found")
        else:
            update_callback(event_key, json.dumps(results))
            logger.info(f"Crawl complete for {entity_ref}. Found {len(results)} bitstreams.")
            
    except Exception as e:
        logger.error(f"Error during crawl for {entity_ref}: {e}")
        update_callback(event_key, f"Error: {str(e)}")

# --- ROUTES ---

@app.route('/preservica-webhook', methods=['POST'])
def preservica_webhook():
    secret = WEBHOOK_SECRET
    challenge_code = request.args.get('challengeCode')
    if challenge_code:
        challenge_response = compute_hmac(secret, challenge_code)
        return jsonify({
            "challengeCode": challenge_code,
            "challengeResponse": challenge_response
        })

    signature_header = request.headers.get('Preservica-Signature')
    if not signature_header:
        logging.getLogger(__name__).warning("Preservica-Signature header missing from request")
        abort(401, description="Signature header missing")

    raw_data = request.get_data(as_text=True)
    expected_signature = compute_hmac(
        secret,
        "preservica-webhook-auth" + raw_data
    )

    if not hmac.compare_digest(signature_header, expected_signature):
        logging.getLogger(__name__).error(f"Signature mismatch. Received: {signature_header}, Expected: {expected_signature}")
        # during troubleshooting, it can be helpful to see the secret used (commented out for security)
        # logging.getLogger(__name__).debug(f"Secret used: {secret}")
        abort(401, description="Invalid signature")

    data = request.get_json(silent=True)
    if not data:
        logging.getLogger(__name__).error("Could not parse JSON body from Preservica POST")
        return "Invalid JSON", 400
        
    trigger = data.get("trigger")
    event_key = f"{data.get('subscriptionId','')}_{data.get('timestamp','')}"
    
    if trigger in ("MOVED", "INGEST_FAILED"):
        insert_event(trigger, json.dumps(data), event_key)

    if data.get('events'):
        entity_ref = data['events'][0].get('entityRef')
        if entity_ref:
            credentials_to_use = "prod-ingest"
            # print(f"Starting background crawl for {entity_ref}")
            # crawler_thread = threading.Thread(
            #     target=crawl_for_bitstreams, 
            #     args=(entity_ref, credentials_to_use, event_key, update_bitstream_results),
            # )
            # crawler_thread.start()
            
            # Start background metadata and container lookup
            container_db = app.config.get('CONTAINER_DB')
            meta_thread = threading.Thread(
                target=fetch_and_update_metadata,
                args=(entity_ref, credentials_to_use, event_key, container_db)
            )
            meta_thread.start()
            pass

    return "OK", 200

def get_pkg_title(accesstoken: str, pkg_uuid: str) -> str:
    get_so_url = f"https://nypl.preservica.com/api/entity/structural-objects/{pkg_uuid}"
    headers = {"Preservica-Access-Token": accesstoken, "Accept": "application/xml"}
    res = requests.get(get_so_url, headers=headers)
    if res.status_code != 200: return None
    root = ET.fromstring(res.text)
    version_search = re.search(r"v(\d+\.\d+)\}", root.tag)
    version = version_search.group(1) if version_search else "7.0"
    title = root.find(f".//{{http://preservica.com/XIP/v{version}}}Title")
    return title.text.strip() if title is not None else None

title_cache = {}

DASHBOARD_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>Ingest Dashboard</title>
  <meta http-equiv="refresh" content="60">
  <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.css">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 2em; background-color: #f8f9fa; color: #212529; }
    h1 { color: #007bff; }
    table.dataTable thead th { background-color: #e9ecef; }
    .md5-cell { font-family: monospace; font-size: 0.85em; white-space: pre-wrap; word-break: break-all; max-width: 300px; }
  </style>
</head>
<body>
  <h1>Ingest Events</h1>
  {% if not events %}
    <p>No events received yet.</p>
  {% else %}
    <table id="webhook_table" class="display">
      <thead>
        <tr>
          {% for header in headers %}
            <th>{{ header.replace('_', ' ').title() }}</th>
          {% endfor %}
        </tr>
      </thead>
      <tbody>
        {% for item in events %}
          <tr>
            {% for header in headers %}
              <td data-order="{{ item.get(header + '_sort', '') }}" class="{{ 'md5-cell' if header == 'CO_md5' else '' }}">{{ item.get(header, 'N/A') }}</td>
            {% endfor %}
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endif %}
  <script type="text/javascript" charset="utf8" src="https://code.jquery.com/jquery-3.7.0.js"></script>
  <script type="text/javascript" charset="utf8" src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.js"></script>
  <script>
    $(document).ready(function() {
        $('#webhook_table').DataTable({ "order": [[0, "desc"]], "pageLength": 25 });
    });
  </script>
</body>
</html>
"""

@app.route('/dashboard')
def dashboard():
    global title_cache
    credentials = "prod-ingest"
    accesstoken = prsvapi.get_token(credential_set=credentials)
    events = get_events()
    utc_zone, est_zone = pytz.utc, pytz.timezone('America/New_York')

    processed_events = []
    for item in events:
        flat_item = {
            '_received_at': str(item.get('_received_at')),
            'package_title': item.get('package_title', 'N/A'),
            # 'tenant': item.get('tenant'),
            'trigger': item.get('trigger'),
            'container_name': item.get('container_name', 'N/A')
        }
        # co_md5 = item.get('CO_md5', "Processing")
        # flat_item['CO_md5'] = json.dumps(co_md5, indent=2) if isinstance(co_md5, dict) else co_md5
        pass

        if flat_item['_received_at']:
            try:
                # Store original timestamp for precise chronological sorting
                flat_item['_received_at_sort'] = flat_item['_received_at']
                utc_dt = datetime.fromisoformat(flat_item['_received_at'].split('.')[0])
                est_dt = utc_zone.localize(utc_dt).astimezone(est_zone)
                flat_item['_received_at'] = est_dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')
            except: pass

        if item.get('events'):
            inner_event = item['events'][0]
            entity_ref = inner_event.get('entityRef')
            flat_item['entityRef'] = entity_ref
            if inner_event.get('identifiers'):
                for ident in inner_event['identifiers']:
                    flat_item.update(ident)
            
            if entity_ref:
                uuid = entity_ref.split('/')[-1]
                # Fallback to cache if not already populated in DB (for older events)
                if flat_item['package_title'] == 'N/A':
                    if uuid not in title_cache:
                        title_cache[uuid] = get_pkg_title(accesstoken, uuid)
                    flat_item['package_title'] = title_cache[uuid] or "Title Not Found"
        
        processed_events.append(flat_item)

    all_keys = set()
    for item in processed_events: all_keys.update(item.keys())
    header_order = [
        '_received_at', 'package_title', 'container_name', 'trigger',
        'entityRef', 'BatchId'
    ]
    # exclude headers as requested (case-insensitive)
    exclude_list = {'co_md5', 'comd5', 'socategory', 'tenant', '_received_at_sort'}
    headers = [h for h in all_keys if h.lower() not in exclude_list]
    headers = sorted(headers, key=lambda x: (header_order.index(x) if x in header_order else len(header_order), x))

    return render_template_string(DASHBOARD_TEMPLATE, headers=headers, events=processed_events)

def main():
    log_path = Path.cwd() / "webhook_logs"
    log_path.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=Path(log_path / f"webhook_prsv_{datetime.now().strftime('%Y%m%d')}_{datetime.now().strftime('%H%M')}.log"))
    
    args = parse_args()
    app.config['CONTAINER_DB'] = args.container_db
    init_db()
    
    if args.retry_containers:
        if not args.container_db:
            print("Error: --container-db is required when using --retry-containers")
        else:
            retry_container_lookups(args.container_db)

    print(f"Starting server on {args.host}:{args.port}")
    if args.container_db:
        print(f"Using container database: {args.container_db}")
    app.run(host=args.host, port=args.port)

if __name__ == '__main__':
    main()
