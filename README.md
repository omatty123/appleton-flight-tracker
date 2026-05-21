# Appleton Flight Tracker

A local website for watching aircraft near Appleton, Wisconsin and collecting observations over time.

## Run it

```bash
cd /Users/wegehaum/Documents/New\ project/appleton-flight-tracker
python3 server.py
```

Then open [http://127.0.0.1:8787](http://127.0.0.1:8787).

## Configure it

```bash
cp .env.example .env
```

Edit `.env` and set `HOME_LAT` / `HOME_LON` to your actual house coordinates if you want precise overhead tracking. Keep `.env` private.

OpenSky now recommends API clients with OAuth2 client credentials. The app supports:

```bash
OPENSKY_CLIENT_ID=...
OPENSKY_CLIENT_SECRET=...
```

If those are blank, it will attempt anonymous OpenSky requests.

## Route and Aircraft Enrichment

The article you shared uses ADSBDB for the fields OpenSky does not provide. This app does the same by calling:

- `https://api.adsbdb.com/v0/aircraft/{MODE_S}?callsign={CALLSIGN}`

Those responses are cached in SQLite so refreshes do not repeatedly hit ADSBDB. You can turn enrichment off with:

```bash
ADSBDB_ENABLED=0
```

## Data

Observations are stored in SQLite at `data/flights.sqlite3` by default. Each poll stores aircraft position, speed, altitude, heading, vertical rate, distance from home, and whether it was inside the overhead radius.

OpenSky live state vectors do not include a reliable live destination field. Destination and origin are enriched from ADSBDB when its callsign database has a match.

## Phone Notifications

The app can send phone pings through [ntfy](https://ntfy.sh/). Install the ntfy app, subscribe to a long private topic name, then add the same topic to `.env`:

```bash
NOTIFY_ENABLED=1
NTFY_TOPIC=appleton-flights-your-long-random-topic
```

Use the dashboard's **Test** button to confirm the phone receives a push.

Default alert rules:

- Look-up alert: aircraft is at least 300 mph, at least 10,000 ft, within 25 miles, and heading roughly toward home or already overhead.
- Anomaly alert: emergency squawk `7500`, `7600`, or `7700`.
- Anomaly alert: at least 180 mph, below 2,500 ft, and within 10 miles.

Each aircraft/rule is cooled down for 30 minutes by default to avoid repeat pings. Recent alert attempts are stored in SQLite in `notification_events`.

## Notes

- The map uses Leaflet with OpenStreetMap tiles.
- The backend uses only Python standard-library modules.
- Keep the server running if you want to collect long-term patterns.
- OpenSky API docs: https://openskynetwork.github.io/opensky-api/rest.html
