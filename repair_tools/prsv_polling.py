# pyrefly: ignore [missing-import]
from flask import Flask
import json
import sqlite3
import pytz
import logging
import threading
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import repair_tools.utils.prsv_api as prsvapi
import repair_tools.utils.cli as prsvcli
import repair_tools.utils.db_utils as db_utils
import repair_tools.utils.prsv_api_helpers as prsvapi_helpers
from repair_tools.utils.logger_setup import setup_logging

# create app
app = Flask(__name__)

DB_FILE = Path.cwd() / "databases/webhook_events.db"

# --- SETUP LOGGER ---
# Imported from logger_setup

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

# DB and API helpers imported from utils

def fetch_latest_event_action(accesstoken, uuid, version):
    """
    Calls /event-actions for an SO and returns 'MOVED' if a Moved action occurred,
    otherwise returns 'PENDING'.
    """
    logger = logging.getLogger(__name__)
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{uuid}/event-actions?start=0&max=100"
    namespaces = {
        'entity': f'http://preservica.com/EntityAPI/v{version}',
        'xip': f'http://preservica.com/XIP/v{version}'
    }
    
    root = prsvapi_helpers._get_entity_xml(accesstoken, url)
    if root is None:
        return "PENDING", None

    actions = root.findall('.//xip:EventAction', namespaces)
    if not actions:
        return "PENDING", None
    
    latest_date = None
    moved_date = None
    
    for action in actions:
        date_elem = action.find('xip:Date', namespaces)
        dt = None
        if date_elem is not None and date_elem.text:
            try:
                dt = datetime.fromisoformat(date_elem.text.replace('Z', '+00:00'))
                if latest_date is None or dt > latest_date:
                    latest_date = dt
            except:
                pass
                
        if action.get('commandType') == 'Moved':
            if dt:
                moved_date = dt
            else:
                moved_date = datetime.now(pytz.utc)
                
    if moved_date:
        return "MOVED", moved_date.isoformat()
        
    return "PENDING", latest_date.isoformat() if latest_date else None

def poll_preservica(credentials, interval_mins, lookback_hours, container_db):
    logger = logging.getLogger(__name__)
    logger.info(f"Starting polling loop. Interval: {interval_mins}m, Initial Lookback: {lookback_hours}h")

    while True:
        try:
            accesstoken = prsvapi.get_token(credential_set=credentials)
            version = prsvapi.find_apiversion(credential_set=credentials)

            # 1. Re-check PENDING items
            pending_rows = db_utils.get_pending_events(DB_FILE)
            if pending_rows:
                # logger.info(f"Re-checking {len(pending_rows)} PENDING items...")
                for key, json_str in pending_rows:
                    try:
                        data = json.loads(json_str)
                        uuid = data['events'][0]['entityRef'].split('/')[-1]
                        trigger, event_date = fetch_latest_event_action(accesstoken, uuid, version)
                        
                        if trigger == 'MOVED':
                            if not event_date:
                                event_date = datetime.now(pytz.utc).isoformat()
                                
                            # logger.info(f"PENDING item {uuid} has now MOVED!")
                            data['trigger'] = 'MOVED'
                            data['timestamp'] = event_date
                            
                            conn = sqlite3.connect(DB_FILE)
                            c = conn.cursor()
                            c.execute("UPDATE events SET trigger = ?, event_json = ? WHERE event_key = ?", ('MOVED', json.dumps(data), key))
                            conn.commit()
                            conn.close()
                    except Exception as e:
                        logger.error(f"Error checking pending item {key}: {e}")

            # 2. Discover new items
            last_polled = db_utils.get_last_polled_at(DB_FILE)
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
            
            # Use the correct endpoint and parameter name 'date'
            url = f"https://nypl.preservica.com/api/entity/entities/updated-since"
            params = {'date': since_ts, 'start': 0, 'max': 100}
            namespaces = {'entity': f'http://preservica.com/EntityAPI/v{version}'}
            root = prsvapi_helpers._get_entity_xml(accesstoken, url, params=params)
            
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
                        
                        # PREVENT DUPLICATES: Check if tracked previously
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute("SELECT 1 FROM events WHERE event_key LIKE ?", (f"POLL_{uuid}%",))
                        exists = bool(c.fetchone())
                        conn.close()

                        if exists:
                            continue
                        
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
                            "events": [{"entityRef": uuid}]
                        }
                        
                        # Insert into DB
                        inserted = db_utils.insert_event(DB_FILE, trigger, json.dumps(event_data), event_key)
                        
                        if inserted:
                            logger.info(f"New Ingest Discovered: {title} (Trigger: {trigger})")
                            # Start metadata fetch
                            meta_thread = threading.Thread(
                                target=db_utils.fetch_and_update_metadata,
                                args=(uuid, credentials, event_key, container_db)
                            )
                            meta_thread.start()
            
            db_utils.set_last_polled_at(DB_FILE, new_poll_time)
            
        except Exception as e:
            logger.error(f"Error during polling cycle: {e}")

        logger.info(f"Polling cycle complete. Sleeping for {interval_mins} minutes...")
        time.sleep(interval_mins * 60)

# --- DASHBOARD CODE ---
@app.route('/dashboard')
def dashboard():
    events = db_utils.get_events(DB_FILE)
    return db_utils.render_dashboard(events, mode="Polling Mode")

def main():
    log_path = Path.cwd() / "webhook_logs"
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = Path(log_path / f"polling_prsv_{datetime.now().strftime('%Y%m%d')}_{datetime.now().strftime('%H%M')}.log")
    setup_logging(log_file, log_format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s', console_format='%(threadName)s - %(levelname)s - %(message)s')
    
    args = parse_args()
    app.config['CONTAINER_DB'] = args.container_db
    db_utils.init_db(DB_FILE)
    
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
