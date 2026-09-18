"""
Local Lead Finder - Flask Backend

Scans the Google Places API for nearby businesses that do NOT have a website,
so web developers can pitch their services to local lead generation.

Leads are saved to MongoDB (database: lead_finder_db, collection: saved_leads).

Environment variables:
    GOOGLE_PLACES_API_KEY=<your-gcp-api-key>
    MONGO_URI=<mongodb connection string>   (optional, defaults to local Mongo)

Run:
    set GOOGLE_PLACES_API_KEY=<your-gcp-api-key>
    python app.py
"""

import csv
import io
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS
from pymongo import MongoClient

# Load GOOGLE_PLACES_API_KEY / MONGO_URI from the local .env file if present.
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# Fields we ask Google to return for every candidate place.
DETAILS_FIELDS = (
    "name,formatted_address,international_phone_number,"
    "geometry,website,url,rating"
)

DEFAULT_RADIUS_M = 3000
MAX_RADIUS_M = 50000      # Google hard limit
MAX_PAGES = 3             # Google returns up to 20 results per page
PAGE_DELAY_S = 2.0        # Google requires a short delay between pagination calls
HTTP_TIMEOUT_S = 10
MAX_HTTP_RETRIES = 2      # Retry transient network failures per request

# ---------------------------------------------------------------------------
# MongoDB setup
# ---------------------------------------------------------------------------

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = "lead_finder_db"
MONGO_COLLECTION_NAME = "saved_leads"

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo_client[MONGO_DB_NAME]
saved_leads_collection = db[MONGO_COLLECTION_NAME]

# Enforce the "one business per place_id" rule at the database level too.
try:
    saved_leads_collection.create_index("place_id", unique=True)
except Exception as exc:
    print(f"[WARN] Could not create unique index on place_id: {exc}")

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def _serialize_lead(doc):
    """Convert a MongoDB document into a JSON-safe dict (stringify _id)."""
    doc = dict(doc)
    doc["_id"] = str(doc["_id"])
    return doc


# ---------------------------------------------------------------------------
# Google Places API helpers
# ---------------------------------------------------------------------------

def _get_with_retry(session, url, params):
    """Perform a GET with a couple of retries on transient network failures."""
    last_error = None
    for attempt in range(MAX_HTTP_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=HTTP_TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout as exc:
            last_error = exc
            time.sleep(1)
        except requests.exceptions.ConnectionError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def fetch_nearby_places(lat, lng, keyword, radius, session):
    """Step 1: Nearby Search -> list of candidate place_ids."""
    places, seen = [], set()

    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "language": "en",
        "key": GOOGLE_PLACES_API_KEY,
    }
    if keyword:
        params["keyword"] = keyword

    for _ in range(MAX_PAGES):
        data = _get_with_retry(session, NEARBY_SEARCH_URL, params)

        status = data.get("status")
        if status != "OK":
            msg = data.get("error_message") or data.get("status") or "Unknown error"
            raise RuntimeError(f"Google Nearby Search failed ({status}): {msg}")

        for place in data.get("results", []):
            pid = place.get("place_id")
            if pid and pid not in seen:
                seen.add(pid)
                places.append(place)

        token = data.get("next_page_token")
        if not token:
            break

        params["pagetoken"] = token
        time.sleep(PAGE_DELAY_S)

    return places


def fetch_place_details(place_id, session):
    """Step 2: Place Details -> check for the 'website' property."""
    params = {
        "place_id": place_id,
        "fields": DETAILS_FIELDS,
        "language": "en",
        "key": GOOGLE_PLACES_API_KEY,
    }

    data = _get_with_retry(session, PLACE_DETAILS_URL, params)

    if data.get("status") != "OK":
        return None
    return data.get("result") or {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the single-page frontend."""
    return send_from_directory(".", "index.html")


@app.route("/api/health", methods=["GET"])
def health():
    mongo_ok = True
    try:
        mongo_client.admin.command("ping")
    except Exception:
        mongo_ok = False

    return jsonify({
        "status": "ok",
        "api_key_configured": bool(GOOGLE_PLACES_API_KEY),
        "mongo_connected": mongo_ok,
    })


@app.route("/api/leads", methods=["GET"])
def get_leads():
    """Return local businesses that are missing a website."""
    if not GOOGLE_PLACES_API_KEY:
        return jsonify({
            "error": (
                "GOOGLE_PLACES_API_KEY is not set. Set the environment variable "
                "before starting the server, e.g. on Windows: "
                "set GOOGLE_PLACES_API_KEY=your_key_here"
            )
        }), 500

    # --- Validate query parameters -----------------------------------------
    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except (TypeError, ValueError):
        return jsonify({
            "error": "lat and lng are required query parameters and must be numbers."
        }), 400

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return jsonify({"error": "lat or lng is out of range."}), 400

    keyword = (request.args.get("keyword") or "").strip()
    try:
        radius = int(request.args.get("radius", DEFAULT_RADIUS_M))
    except (TypeError, ValueError):
        radius = DEFAULT_RADIUS_M
    radius = max(1, min(radius, MAX_RADIUS_M))

    # --- Query Google Places ------------------------------------------------
    session = requests.Session()
    try:
        places = fetch_nearby_places(lat, lng, keyword, radius, session)
    except requests.exceptions.RequestException as exc:
        return jsonify({
            "error": f"Network error contacting Google Places API: {exc}"
        }), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    # --- Filter: keep only businesses WITHOUT a website ---------------------
    leads, scanned = [], 0
    for place in places:
        scanned += 1
        try:
            details = fetch_place_details(place["place_id"], session)
        except (requests.exceptions.RequestException, RuntimeError):
            continue  # skip this one, keep scanning the rest

        if not details:
            continue

        if details.get("website"):
            continue  # has a website -> not a lead

        location = (details.get("geometry") or {}).get("location") or {}
        leads.append({
            "name": details.get("name") or place.get("name") or "Unknown business",
            "address": (details.get("formatted_address")
                        or place.get("vicinity") or "N/A"),
            "phone": details.get("international_phone_number") or "",
            "latitude": location.get("lat"),
            "longitude": location.get("lng"),
            "place_id": place["place_id"],
        })

    return jsonify({
        "count": len(leads),
        "scanned": scanned,
        "leads": leads,
    })


@app.route("/api/save-lead", methods=["POST"])
def save_lead():
    """Insert a lead into MongoDB, skipping duplicates via unique place_id."""
    data = request.get_json(silent=True) or {}
    place_id = (data.get("place_id") or "").strip()
    if not place_id:
        return jsonify({"error": "place_id is required to save a lead."}), 400

    lead = {
        "place_id": place_id,
        "name": data.get("name") or "Unknown business",
        "address": data.get("address") or "N/A",
        "phone": data.get("phone") or "",
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        result = saved_leads_collection.update_one(
            {"place_id": place_id},
            {"$setOnInsert": lead},
            upsert=True,
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to save lead: {exc}"}), 500

    if result.upserted_id is not None:
        return jsonify({"success": True, "saved": True, "message": "Lead saved."}), 201

    return jsonify({"success": True, "saved": False, "message": "Lead already saved."}), 200


@app.route("/api/saved-leads", methods=["GET"])
def get_saved_leads():
    """Return every saved lead from MongoDB (newest first)."""
    try:
        cursor = saved_leads_collection.find().sort("saved_at", -1)
        leads = [_serialize_lead(doc) for doc in cursor]
    except Exception as exc:
        return jsonify({"error": f"Failed to load saved leads: {exc}"}), 500

    return jsonify({"count": len(leads), "leads": leads})


@app.route("/api/export-csv", methods=["GET"])
def export_csv():
    """Stream every saved lead as a downloadable CSV file."""
    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([
            "name", "place_id", "address", "phone", "latitude", "longitude", "saved_at"
        ])
        yield buffer.getvalue()

        for doc in saved_leads_collection.find().sort("saved_at", -1):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([
                doc.get("name", ""),
                doc.get("place_id", ""),
                doc.get("address", ""),
                doc.get("phone", ""),
                doc.get("latitude", ""),
                doc.get("longitude", ""),
                doc.get("saved_at", ""),
            ])
            yield buffer.getvalue()
        buffer.close()

    return Response(
        generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=saved_leads.csv",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not GOOGLE_PLACES_API_KEY:
        print("\n[WARNING] GOOGLE_PLACES_API_KEY is not set.")
        print("          Set it first, e.g.:  set GOOGLE_PLACES_API_KEY=your_key_here\n")

    try:
        mongo_client.admin.command("ping")
        print(f"[INFO] Connected to MongoDB at {MONGO_URI}")
    except Exception:
        print(f"\n[WARNING] Could not connect to MongoDB at {MONGO_URI}")
        print("          Make sure MongoDB is running locally, or set MONGO_URI.\n")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)