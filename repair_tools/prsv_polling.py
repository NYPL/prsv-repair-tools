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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

import repair_tools.utils.prsv_api as prsvapi
import repair_tools.cli as prsvcli

# create app
app = Flask(__name__)

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
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval in minutes (default: 5)"
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=24,
        help="Hours to look back on the first run if no previous state exists (default: 24)"
    )
    return parser.parse_args()

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
    # Track the last successful poll time
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
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

def get_last_polled_at():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'last_polled_at'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_last_polled_at(timestamp_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_polled_at', ?)", (timestamp_str,))
    conn.commit()
    conn.close()

def insert_event(trigger, event_json, event_key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Use INSERT OR IGNORE to prevent duplicates during lookbacks
    c.execute(
        "INSERT OR IGNORE INTO events (event_key, trigger, event_json) VALUES (?, ?, ?)",
        (event_key, trigger, event_json)
    )
    was_inserted = conn.total_changes > 0
    conn.commit()
    conn.close()
    return was_inserted

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
        all_events.append(event)
    return all_events

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
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tf:
            temp_db = tf.name
        shutil.copy2(db_path, temp_db)
        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        query = "SELECT container_name FROM workflows WHERE container_name LIKE ?"
        c.execute(query, (f"%{package_title}%",))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logging.getLogger(__name__).error(f"Error querying container DB: {e}")
        return None
    finally:
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
        title = get_pkg_title(accesstoken, uuid)
        if not title:
            title = "Title Not Found"
        container_name = lookup_container_name(container_db_path, title)
        update_event_metadata(event_key, title, container_name)
        logger.info(f"Updated metadata for {event_key}: Title={title}, Container={container_name}")
    except Exception as e:
        logger.error(f"Error in metadata fetch for {entity_ref}: {e}")

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

def fetch_latest_event_action(accesstoken, uuid, version):
    """
    Calls /event-actions for an SO and returns the most recent relevant event (trigger, date).
    """
    logger = logging.getLogger(__name__)
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{uuid}/event-actions?start=0&max=100"
    namespaces = {
        'entity': f'http://preservica.com/EntityAPI/v{version}',
        'xip': f'http://preservica.com/XIP/v{version}'
    }
    
    root = _get_entity_xml(accesstoken, url)
    if root is None:
        return "UNKNOWN", None

    actions = root.findall('.//xip:EventAction', namespaces)
    if not actions:
        return "NO_ACTIONS", None
    
    # Sort actions by date descending to find the latest
    # In the XML, <xip:Date> is a child of EventAction or Event
    # The user example has <xip:Date> directly under EventAction and also inside Event.
    # We'll look for the latest one.
    
    latest_action = None
    latest_date = None
    
    for action in actions:
        date_elem = action.find('xip:Date', namespaces)
        if date_elem is not None and date_elem.text:
            try:
                dt = datetime.fromisoformat(date_elem.text.replace('Z', '+00:00'))
                if latest_date is None or dt > latest_date:
                    latest_date = dt
                    latest_action = action
            except:
                pass
                
    if latest_action is not None:
        cmd_type = latest_action.get('commandType', 'UNKNOWN')
        # If commandType is 'command_create' or 'AddFragment' and it's an Ingest event, 
        # we might want to call it INGEST.
        event_elem = latest_action.find('xip:Event', namespaces)
        if event_elem is not None:
            event_type = event_elem.get('type', '')
            if event_type:
                return event_type.upper(), latest_date.isoformat()
        
        return cmd_type.upper(), latest_date.isoformat()
        
    return "UNKNOWN", None

def poll_preservica(credentials, interval_mins, lookback_hours, container_db):
    logger = logging.getLogger(__name__)
    logger.info(f"Starting polling loop. Interval: {interval_mins}m, Initial Lookback: {lookback_hours}h")

    while True:
        try:
            last_polled = get_last_polled_at()
            if not last_polled:
                # First run: go back by lookback_hours
                since_dt = datetime.now(pytz.utc) - timedelta(hours=lookback_hours)
                since_ts = since_dt.strftime('%Y-%m-%dT%H:%M:%S.000+0000')
            else:
                since_ts = last_polled
                # Normalize old formats (like Z) to the new required .000+0000 format
                if 'Z' in since_ts or '.' not in since_ts:
                    try:
                        clean_ts = since_ts.replace('Z', '+00:00')
                        dt = datetime.fromisoformat(clean_ts)
                        since_ts = dt.strftime('%Y-%m-%dT%H:%M:%S.000+0000')
                    except:
                        pass

            logger.info(f"Polling Preservica for updates since {since_ts}...")
            accesstoken = prsvapi.get_token(credential_set=credentials)
            version = prsvapi.find_apiversion(credential_set=credentials)
            
            # Use the correct endpoint and parameter name 'date'
            url = f"https://nypl.preservica.com/api/entity/entities/updated-since"
            params = {'date': since_ts, 'start': 0, 'max': 100}
            namespaces = {'entity': f'http://preservica.com/EntityAPI/v{version}'}
            root = _get_entity_xml(accesstoken, url, params=params)
            
            new_poll_time = datetime.now(pytz.utc).strftime('%Y-%m-%dT%H:%M:%S.000+0000')

            if root is not None:
                entities = root.findall('.//entity:Entity', namespaces)
                logger.info(f"Found {len(entities)} updated entities.")
                
                for entity in entities:
                    etype = entity.get('type')
                    eref = entity.get('ref')
                    title = entity.get('title', '')
                    
                    # Filtering: Structural Objects (SO) with numeric titles ONLY
                    if etype == 'SO' and re.match(r'^\d+$', title):
                        uuid = eref
                        
                        # Call second endpoint to get event details
                        trigger, event_date = fetch_latest_event_action(accesstoken, uuid, version)
                        
                        if not event_date:
                            event_date = datetime.now(pytz.utc).isoformat()
                            
                        event_key = f"POLL_{uuid}_{event_date}"
                        
                        # Create event JSON
                        event_data = {
                            "trigger": trigger,
                            "subscriptionId": "POLLING_TASK",
                            "timestamp": event_date,
                            "events": [{"entityRef": f"https://nypl.preservica.com/api/entity/structural-objects/{uuid}"}]
                        }
                        
                        # Insert into DB
                        inserted = insert_event(trigger, json.dumps(event_data), event_key)
                        
                        if inserted:
                            logger.info(f"New Ingest Discovered: {title} (Trigger: {trigger})")
                            # Start metadata fetch
                            meta_thread = threading.Thread(
                                target=fetch_and_update_metadata,
                                args=(event_data['events'][0]['entityRef'], credentials, event_key, container_db)
                            )
                            meta_thread.start()
            
            set_last_polled_at(new_poll_time)
            
        except Exception as e:
            logger.error(f"Error during polling cycle: {e}")

        logger.info(f"Polling cycle complete. Sleeping for {interval_mins} minutes...")
        time.sleep(interval_mins * 60)

# --- DASHBOARD CODE ---

DASHBOARD_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>Ingest Dashboard (Polling Mode)</title>
  <meta http-equiv="refresh" content="60">
  <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.css">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 2em; background-color: #f8f9fa; color: #212529; }
    h1 { color: #28a745; }
    .status-badge { font-size: 0.8em; padding: 2px 6px; border-radius: 4px; background: #eee; color: #666; margin-left: 10px; }
    table.dataTable thead th { background-color: #e9ecef; }
  </style>
</head>
<body>
  <h1>Ingest Events <span class="status-badge">Polling Mode</span></h1>
  {% if not events %}
    <p>No packages discovered yet.</p>
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
              <td data-order="{{ item.get(header + '_sort', '') }}">{{ item.get(header, 'N/A') }}</td>
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
    credentials = "prod-ingest"
    accesstoken = prsvapi.get_token(credential_set=credentials)
    events = get_events()
    utc_zone, est_zone = pytz.utc, pytz.timezone('America/New_York')

    processed_events = []
    for item in events:
        flat_item = {
            '_received_at': str(item.get('_received_at')),
            'package_title': item.get('package_title', 'N/A'),
            'trigger': item.get('trigger'),
            'container_name': item.get('container_name', 'N/A')
        }

        if flat_item['_received_at']:
            try:
                flat_item['_received_at_sort'] = flat_item['_received_at']
                utc_dt = datetime.fromisoformat(flat_item['_received_at'].replace('Z', '+00:00'))
                est_dt = utc_dt.astimezone(est_zone)
                flat_item['_received_at'] = est_dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')
            except Exception as e: 
                pass

        if item.get('events'):
            inner_event = item['events'][0]
            entity_ref = inner_event.get('entityRef')
            flat_item['entityRef'] = entity_ref
        
        processed_events.append(flat_item)

    all_keys = set()
    for item in processed_events: all_keys.update(item.keys())
    header_order = ['_received_at', 'package_title', 'container_name', 'trigger', 'entityRef']
    exclude_list = {'_received_at_sort'}
    headers = [h for h in all_keys if h.lower() not in exclude_list]
    headers = sorted(headers, key=lambda x: (header_order.index(x) if x in header_order else len(header_order), x))

    return render_template_string(DASHBOARD_TEMPLATE, headers=headers, events=processed_events)

def main():
    log_path = Path.cwd() / "webhook_logs"
    log_path.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=Path(log_path / f"polling_prsv_{datetime.now().strftime('%Y%m%d')}_{datetime.now().strftime('%H%M')}.log"))
    
    args = parse_args()
    app.config['CONTAINER_DB'] = args.container_db
    init_db()
    
    # Start the polling thread
    polling_thread = threading.Thread(
        target=poll_preservica,
        args=("prod-ingest", args.interval, args.lookback, args.container_db),
        daemon=True
    )
    polling_thread.start()

    print(f"Starting dashboard on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port)

if __name__ == '__main__':
    main()
