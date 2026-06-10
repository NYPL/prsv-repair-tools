import os
import json
import sqlite3
import pytz
import logging
import tempfile
import shutil
from datetime import datetime
from flask import render_template_string

def init_db(db_file):
    os.makedirs(os.path.dirname(db_file), exist_ok=True)
    conn = sqlite3.connect(db_file)
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

def get_last_polled_at(db_file):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'last_polled_at'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_last_polled_at(db_file, timestamp_str):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_polled_at', ?)", (timestamp_str,))
    conn.commit()
    conn.close()

def insert_event(db_file, trigger, event_json, event_key):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    # Use INSERT OR IGNORE to prevent duplicates
    c.execute(
        "INSERT OR IGNORE INTO events (event_key, trigger, event_json) VALUES (?, ?, ?)",
        (event_key, trigger, event_json)
    )
    was_inserted = conn.total_changes > 0
    conn.commit()
    conn.close()
    return was_inserted

def get_pending_events(db_file):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT event_key, event_json FROM events WHERE trigger = 'PENDING'")
    rows = c.fetchall()
    conn.close()
    return rows

def get_events(db_file):
    conn = sqlite3.connect(db_file)
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

def update_event_metadata(db_file, event_key, package_title, container_name):
    conn = sqlite3.connect(db_file)
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

DASHBOARD_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>Ingest Dashboard</title>
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
  <h1>Ingest Events <span class="status-badge">{{ mode }}</span></h1>
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

def render_dashboard(events, mode="Polling Mode"):
    utc_zone, est_zone = pytz.utc, pytz.timezone('America/New_York')

    processed_events = []
    for item in events:
        flat_item = {
            '_received_at': str(item.get('_received_at')),
            'package_title': item.get('package_title', 'N/A'),
            'trigger': item.get('trigger'),
            'container_name': item.get('container_name', 'N/A')
        }

        if flat_item['_received_at'] and flat_item['_received_at'] != 'None':
            try:
                flat_item['_received_at_sort'] = flat_item['_received_at']
                dt_str = flat_item['_received_at'].replace('Z', '+00:00')
                utc_dt = datetime.fromisoformat(dt_str)
                if utc_dt.tzinfo is None:
                    utc_dt = utc_dt.replace(tzinfo=pytz.utc)
                est_dt = utc_dt.astimezone(est_zone)
                flat_item['_received_at'] = est_dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')
            except Exception: 
                pass

        if item.get('events'):
            inner_event = item['events'][0]
            entity_ref = inner_event.get('entityRef')
            if entity_ref:
                flat_item['entityRef'] = entity_ref.split('/')[-1]
        
        processed_events.append(flat_item)

    all_keys = set()
    for item in processed_events: all_keys.update(item.keys())
    header_order = ['_received_at', 'package_title', 'container_name', 'trigger', 'entityRef']
    exclude_list = {'_received_at_sort'}
    headers = [h for h in all_keys if h.lower() not in exclude_list]
    headers = sorted(headers, key=lambda x: (header_order.index(x) if x in header_order else len(header_order), x))

    return render_template_string(DASHBOARD_TEMPLATE, headers=headers, events=processed_events, mode=mode)

def fetch_and_update_metadata(entity_ref, credentials_to_use, event_key, container_db_path):
    import repair_tools.utils.prsv_api as prsvapi
    import repair_tools.utils.prsv_api_helpers as prsvapi_helpers
    logger = logging.getLogger(__name__)
    try:
        accesstoken = prsvapi.get_token(credential_set=credentials_to_use)
        uuid = entity_ref.split('/')[-1]
        title = prsvapi_helpers.get_pkg_title(accesstoken, uuid)
        if not title:
            title = "Title Not Found"
        container_name = lookup_container_name(container_db_path, title)
        update_event_metadata("databases/webhook_events.db", event_key, title, container_name)
        logger.info(f"Updated metadata for {event_key}: Title={title}, Container={container_name}")
    except Exception as e:
        logger.error(f"Error in metadata fetch for {entity_ref}: {e}")

