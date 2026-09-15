#!/usr/bin/env python3
"""
dashboard.py

Web dashboard for the Earthquake Monitor + Shaking Estimator.

Runs as a SEPARATE process alongside earthquake_monitor_with_shaking.py --
this app only READS from the same SQLite database and shake map image
folder the monitor writes to. It never runs detection, notifications, or
GMPE computations itself, so it can be started/stopped/restarted
independently of the live monitor without affecting it.

REQUIREMENTS
------------
    pip install flask folium
    (or: conda install -c conda-forge flask folium)

USAGE
-----
    conda activate openquake_conda
    python dashboard.py

Then open http://localhost:5001 in a browser. Runs alongside the main
monitor -- start both in separate terminals (or separate Docker
containers sharing the same mounted volumes).
"""

import os
import sqlite3
import time
from datetime import datetime, timezone

from flask import Flask, render_template_string, send_from_directory, abort, request
import folium

import config

app = Flask(__name__)

DB_PATH = config.HISTORY_DB_PATH
MAPS_DIR = config.SHAKEMAP_OUTPUT_DIR


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def magnitude_color(mag):
    """Rough color scale for map markers, matching typical earthquake
    magnitude color conventions."""
    if mag is None:
        return "gray"
    if mag < 3:
        return "green"
    if mag < 4.5:
        return "blue"
    if mag < 5.5:
        return "orange"
    if mag < 7:
        return "red"
    return "darkred"


# ============================================================================
# HTML templates (inline for a single-file app -- fine for a small
# internal dashboard; move to templates/ files if this grows)
# ============================================================================

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Earthquake Monitor Dashboard</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: -apple-system, sans-serif; margin: 0; padding: 20px;
              background: #f5f5f5; color: #222; }
        h1 { font-size: 1.5em; margin-bottom: 4px; }
        .subtitle { color: #666; margin-bottom: 20px; font-size: 0.9em; }
        .filter-bar { background: white; border-radius: 8px; padding: 14px 16px;
                    margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.1);
                    display: flex; gap: 20px; align-items: center; flex-wrap: wrap;
                    font-size: 0.9em; }
        .filter-bar label { color: #555; margin-right: 6px; }
        .filter-bar select, .filter-bar input[type=number] {
            padding: 5px 8px; border: 1px solid #ddd; border-radius: 5px; font-size: 0.95em; }
        .filter-bar input[type=number] { width: 70px; }
        .filter-bar button { padding: 6px 16px; border: none; border-radius: 5px;
                            background: #2563eb; color: white; cursor: pointer; font-size: 0.9em; }
        .filter-bar button:hover { background: #1d4ed8; }
        .filter-bar .checkbox-group { display: flex; align-items: center; gap: 6px; }
        .map-container { margin-bottom: 24px; border-radius: 8px; overflow: hidden;
                        box-shadow: 0 1px 4px rgba(0,0,0,0.15); }
        table { width: 100%; border-collapse: collapse; background: white;
              border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
        th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #fafafa; font-size: 0.85em; text-transform: uppercase;
            color: #888; letter-spacing: 0.03em; }
        tr:hover { background: #f9f9f9; cursor: pointer; }
        a { color: inherit; text-decoration: none; display: block; }
        .mag-badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
                    color: white; font-weight: 600; font-size: 0.85em; }
        .caution { color: #b45309; font-size: 0.85em; }
        .below-threshold { color: #aaa; font-size: 0.85em; font-style: italic; }
        .test-badge { display: inline-block; padding: 1px 6px; border-radius: 8px;
                     background: #ddd; color: #555; font-size: 0.75em; margin-left: 6px; }
    </style>
</head>
<body>
    <h1>🌍 Earthquake Monitor Dashboard</h1>
    <p class="subtitle">{{ shown_count }} of {{ total_events }} events shown &middot; auto-refreshes every 60s
       &middot; this reports events that have already occurred, it does not predict earthquakes</p>

    <form class="filter-bar" method="get" action="/">
        <div>
            <label for="days">Time period:</label>
            <select name="days" id="days">
                <option value="1" {% if days == 1 %}selected{% endif %}>Last 24 hours</option>
                <option value="7" {% if days == 7 %}selected{% endif %}>Last 7 days</option>
                <option value="30" {% if days == 30 %}selected{% endif %}>Last 30 days</option>
                <option value="0" {% if days == 0 %}selected{% endif %}>All time</option>
            </select>
        </div>
        <div>
            <label for="min_mag">Min magnitude:</label>
            <input type="number" step="0.1" name="min_mag" id="min_mag" value="{{ min_mag }}">
        </div>
        <div class="checkbox-group">
            <input type="checkbox" name="include_test" id="include_test" value="1"
                  {% if include_test %}checked{% endif %}>
            <label for="include_test">Include test events</label>
        </div>
        <button type="submit">Apply</button>
    </form>

    <div class="map-container">
        {{ map_html | safe }}
    </div>

    <table>
        <tr>
            <th>Magnitude</th>
            <th>Place</th>
            <th>Time (UTC)</th>
            <th>Region</th>
        </tr>
        {% for ev in events %}
        <tr onclick="window.location='/event/{{ ev.id }}'">
            <td><span class="mag-badge" style="background:{{ mag_color(ev.mag) }}">M{{ ev.mag }}</span></td>
            <td>{{ ev.place }}{% if 'TEST' in (ev.sources or '') %}<span class="test-badge">TEST</span>{% endif %}</td>
            <td>{{ ev.time_str }}</td>
            <td>
                {% if ev.region_name %}
                    {{ ev.region_name }}{% if ev.caution_note %} <span class="caution">⚠</span>{% endif %}
                {% else %}
                    <span class="below-threshold">{{ ev.region_display }}</span>
                {% endif %}
            </td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

EVENT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>M{{ event.mag }} {{ event.place }} - Earthquake Monitor</title>
    <meta charset="utf-8">
    <style>
        body { font-family: -apple-system, sans-serif; margin: 0; padding: 20px;
              background: #f5f5f5; color: #222; max-width: 900px; margin: 0 auto; }
        a.back { color: #666; text-decoration: none; font-size: 0.9em; }
        h1 { font-size: 1.4em; margin: 12px 0 4px; }
        .meta { color: #666; margin-bottom: 16px; }
        .caution-box { background: #fff7ed; border: 1px solid #fdba74; border-radius: 8px;
                     padding: 12px 16px; margin-bottom: 20px; color: #9a3412; }
        table { width: 100%; border-collapse: collapse; background: white;
              border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.1);
              margin-bottom: 24px; }
        th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #fafafa; font-size: 0.85em; text-transform: uppercase; color: #888; }
        .maps { display: flex; flex-direction: column; gap: 20px; }
        .maps img { width: 100%; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.15); }
        .map-label { font-weight: 600; margin-bottom: 6px; color: #444; }
    </style>
</head>
<body>
    <a class="back" href="/">&larr; Back to dashboard</a>
    <h1>M{{ event.mag }} &mdash; {{ event.place }}</h1>
    <div class="meta">
        {{ event.time_str }} UTC &middot; {{ "%.4f"|format(event.lat) }}, {{ "%.4f"|format(event.lon) }}
        &middot; depth {{ event.depth }} km &middot; sources: {{ event.sources }}
    </div>

    {% if event.region_name %}
    <p><strong>Region:</strong> {{ event.region_name }} ({{ event.tectonic_type }})
       &middot; <strong>Model:</strong> {{ event.gmpe_name }}</p>
    {% endif %}

    {% if event.caution_note %}
    <div class="caution-box">⚠ <strong>Caution:</strong> {{ event.caution_note }}</div>
    {% endif %}

    {% if shaking_results %}
    <table>
        <tr><th>Target</th><th>Distance</th><th>Vs30</th><th>PGA</th><th>MMI</th></tr>
        {% for r in shaking_results %}
        <tr>
            <td>{{ r.target_name }}</td>
            <td>{{ "%.0f"|format(r.distance_km) }} km</td>
            <td>{{ "%.0f"|format(r.vs30) }} m/s</td>
            <td>{{ "%.3f"|format(r.pga_percent_g) }} %g</td>
            <td>{{ r.mmi }} &mdash; {{ r.mmi_description }}</td>
        </tr>
        {% endfor %}
    </table>
    {% endif %}

    <div class="maps">
        {% if event.regional_map_path %}
        <div>
            <div class="map-label">Regional</div>
            <img src="/mapimage/{{ event.id }}/regional">
        </div>
        {% endif %}
        {% if event.street_map_path %}
        <div>
            <div class="map-label">Street</div>
            <img src="/mapimage/{{ event.id }}/street">
        </div>
        {% endif %}
        {% if event.zoom_street_map_path %}
        <div>
            <div class="map-label">Zoomed Street</div>
            <img src="/mapimage/{{ event.id }}/zoom_street">
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


# ============================================================================
# Routes
# ============================================================================

@app.route("/")
def index():
    # Read filters from the query string, with sensible defaults:
    # last 7 days, magnitude 0+, test events excluded by default.
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    try:
        min_mag = float(request.args.get("min_mag", 0))
    except ValueError:
        min_mag = 0
    include_test = request.args.get("include_test") == "1"

    conn = get_db_connection()
    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    where_clauses = ["mag >= ?"]
    params = [min_mag]

    if days > 0:
        cutoff = time.time() - days * 86400
        where_clauses.append("event_time >= ?")
        params.append(cutoff)

    if not include_test:
        # Exclude rows where 'TEST' appears anywhere in the sources field
        # (test events are logged with sources = "TEST", see run_test_map
        # in earthquake_monitor_with_shaking.py)
        where_clauses.append("(sources IS NULL OR sources NOT LIKE '%TEST%')")

    where_sql = " AND ".join(where_clauses)
    rows = conn.execute(f"""
        SELECT * FROM events WHERE {where_sql}
        ORDER BY event_time DESC LIMIT 500
    """, params).fetchall()
    conn.close()

    events = []
    for row in rows:
        d = dict(row)
        d["time_str"] = datetime.fromtimestamp(
            d["event_time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        # Region/GMPE only get computed for events meeting
        # SHAKING_MAGNITUDE_THRESHOLD (see earthquake_monitor_with_shaking.py) --
        # make that explicit here instead of showing an unexplained blank.
        if d["region_name"]:
            d["region_display"] = d["region_name"]
        elif d["mag"] is not None and d["mag"] < config.SHAKING_MAGNITUDE_THRESHOLD:
            d["region_display"] = (f"(below M{config.SHAKING_MAGNITUDE_THRESHOLD:g} "
                                  f"threshold -- no shaking estimate run)")
        else:
            d["region_display"] = "-"
        events.append(d)

    # Build the Folium map
    # Note: OpenStreetMap, not CartoDB -- CartoDB's tiles now require an
    # API key (same issue hit earlier with contextily). OSM's usage-policy
    # blocking mainly targets automated server-side scraping; Folium tiles
    # load directly in the user's own browser via Leaflet.js, which is
    # genuine browser traffic, so this should work fine here.
    fmap = folium.Map(location=[20, 0], zoom_start=2, tiles="OpenStreetMap")

    for i, ev in enumerate(events):
        if ev["lat"] is None or ev["lon"] is None:
            continue
        popup_html = (f"<b>M{ev['mag']}</b> {ev['place']}<br>"
                      f"{ev['time_str']} UTC<br>"
                      f"<a href='/event/{ev['id']}'>View details</a>")

        if i == 0:
            # Most recent event (events are already sorted DESC by time) --
            # make it visually distinct and auto-open its popup, so it's
            # immediately visible without scrolling through the table.
            folium.Marker(
                location=[ev["lat"], ev["lon"]],
                icon=folium.Icon(color="black", icon="star", prefix="fa"),
                popup=folium.Popup(
                    f"<b>⭐ Latest event</b><br>{popup_html}",
                    max_width=250, show=True),
            ).add_to(fmap)
        else:
            folium.CircleMarker(
                location=[ev["lat"], ev["lon"]],
                radius=4 + (ev["mag"] or 0),
                color=magnitude_color(ev["mag"]),
                fill=True,
                fill_opacity=0.7,
                popup=folium.Popup(popup_html, max_width=250),
            ).add_to(fmap)

    map_html = fmap._repr_html_()

    return render_template_string(
        INDEX_TEMPLATE, events=events, total_events=total_events,
        shown_count=len(events), map_html=map_html, mag_color=magnitude_color,
        days=days, min_mag=min_mag, include_test=include_test)


@app.route("/event/<int:event_id>")
def event_detail(event_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404)

    event = dict(row)
    event["time_str"] = datetime.fromtimestamp(
        event["event_time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    shaking_results = conn.execute("""
        SELECT * FROM shaking_estimates WHERE event_id=? ORDER BY distance_km ASC
    """, (event_id,)).fetchall()
    conn.close()

    return render_template_string(
        EVENT_TEMPLATE, event=event,
        shaking_results=[dict(r) for r in shaking_results])


@app.route("/mapimage/<int:event_id>/<map_type>")
def map_image(event_id, map_type):
    """Serve a shake map PNG for a given event. map_type is one of
    'regional', 'street', 'zoom_street'."""
    column = {
        "regional": "regional_map_path",
        "street": "street_map_path",
        "zoom_street": "zoom_street_map_path",
    }.get(map_type)
    if column is None:
        abort(404)

    conn = get_db_connection()
    row = conn.execute(f"SELECT {column} FROM events WHERE id=?",
                       (event_id,)).fetchone()
    conn.close()

    if row is None or row[0] is None:
        abort(404)

    full_path = row[0]
    directory = os.path.dirname(full_path) or "."
    filename = os.path.basename(full_path)
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"WARNING: {DB_PATH} doesn't exist yet -- run the main "
              f"monitor at least once first (or use --test-map) so "
              f"there's data to display.")
    print(f"Dashboard starting -- reading from {DB_PATH}")
    print("Open http://localhost:5001 in your browser")
    app.run(host="0.0.0.0", port=5001, debug=False)