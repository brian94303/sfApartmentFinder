"""
SF Apartment Finder - FastAPI Backend
Fetches rental listings near the OpenAI office from Redfin via RapidAPI.
"""

import os
import json
import time
import math
import hashlib
from pathlib import Path
from datetime import datetime, date
from typing import Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="SF Apartment Finder")

# --- Config ---
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
MAPS_API_KEY = os.getenv("MAPS_API_KEY", "")

# San Francisco anchor point (1515 3rd Street, Mission Bay)
OPENAI_OFFICE_LAT = 37.7693278
OPENAI_OFFICE_LNG = -122.3888868

# Search parameters
MAX_RADIUS_MILES = 1.5
PRICE_CAPS = {0: 3800, 1: 5500, 2: 6500}  # bedrooms -> max price
TARGET_START = date(2026, 5, 16)
TARGET_END = date(2026, 7, 15)

# For-sale parameters (used by /api/sale-listings and /buy page)
SALE_MAX_PRICE = 1_300_000
SALE_BEDROOMS = (1, 2)  # 1 or 2 bedrooms only
SALE_SENIOR_KEYWORDS = (
    "senior", "55+", "55 +", "age restricted", "age-restricted",
    "active adult", "age-qualified", "age qualified",
)

# Cache settings
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SECONDS = 3600  # 1 hour


# --- Utility Functions ---

def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def get_cache(key: str) -> Optional[dict]:
    """Read from file cache if not expired."""
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        if time.time() - data.get("_cached_at", 0) < CACHE_TTL_SECONDS:
            return data.get("payload")
    return None


def set_cache(key: str, payload: dict):
    """Write to file cache."""
    cache_file = CACHE_DIR / f"{key}.json"
    cache_file.write_text(json.dumps({
        "_cached_at": time.time(),
        "payload": payload,
    }))


async def fetch_zillow_listings(location: str = "San Francisco", page: int = 1) -> dict:
    """
    Fetch rental listings from Redfin via the RapidAPI Redfin endpoint
    (redfin-com-data.p.rapidapi.com).  Requires RAPIDAPI_KEY in the environment.

    The function name is kept for backwards compatibility — the data source
    is now Redfin, not Zillow.

    Returns a dict with:
        props:  list of normalized listing objects (passed to process_listings)
        source: one of "redfin_rapidapi" | "no_api_key" | "sample_fallback"
        error:  optional error message when source != "redfin_rapidapi"
    """
    cache_key = hashlib.md5(f"redfin_rapidapi_{location}_{page}".encode()).hexdigest()
    cached = get_cache(cache_key)
    if cached:
        return cached

    if not RAPIDAPI_KEY:
        # Don't cache — user may set the key and retry.
        return {
            "props": get_sample_listings(),
            "source": "no_api_key",
            "error": "RAPIDAPI_KEY is not set. Add it to your .env file to fetch live data.",
        }

    url = "https://redfin-com-data.p.rapidapi.com/property/search-rent"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "redfin-com-data.p.rapidapi.com",
    }
    params = {"location": location}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()

        api_data = response.json()
        raw_items = api_data.get("data", []) or []
        props = _transform_redfin_listings(raw_items)

        data = {"props": props, "source": "redfin_rapidapi"}
        set_cache(cache_key, data)
        return data

    except httpx.HTTPStatusError as e:
        msg = f"Redfin RapidAPI returned HTTP {e.response.status_code}"
        print(f"Redfin RapidAPI failed: {msg}")
        return {
            "props": get_sample_listings(),
            "source": "sample_fallback",
            "error": msg,
        }
    except Exception as e:
        print(f"Redfin RapidAPI failed: {e}")
        return {
            "props": get_sample_listings(),
            "source": "sample_fallback",
            "error": str(e),
        }


def _transform_redfin_listings(items: list) -> list:
    """
    Convert Redfin API listing items into the shape process_listings expects.

    Each Redfin item describes a building with a price/bed range.  We expand
    each building into one listing per bedroom count in the range, with the
    price and sqft linearly interpolated across that range.
    """
    out = []
    for item in items:
        hd = item.get("homeData", {}) or {}
        ext = item.get("rentalExtension", {}) or {}

        # Coordinates — nested centroid.centroid in the Redfin response.
        centroid = hd.get("addressInfo", {}).get("centroid", {}).get("centroid", {}) or {}
        lat = centroid.get("latitude")
        lng = centroid.get("longitude")
        if lat is None or lng is None:
            continue

        # Address
        addr_info = hd.get("addressInfo", {}) or {}
        street = addr_info.get("formattedStreetLine", "") or ""
        city = addr_info.get("city", "") or ""
        state = addr_info.get("state", "") or ""
        zipcode = addr_info.get("zip", "") or ""
        full_address = ", ".join(p for p in [street, city, f"{state} {zipcode}".strip()] if p)

        # Detail URL — Redfin returns a relative path under homeData.url
        rel_url = hd.get("url", "") or ""
        detail_url = f"https://www.redfin.com{rel_url}" if rel_url and not rel_url.startswith("http") else rel_url

        # Image — use the prebuilt static map URL Redfin returns
        img_src = hd.get("staticMapUrl", "") or ""

        # Building name (often empty for single-family rentals)
        property_name = ext.get("propertyName", "") or ""

        # Sqft range, baths, availability date
        sqft_range = ext.get("sqftRange") or {}
        sqft_min = sqft_range.get("min")
        sqft_max = sqft_range.get("max")

        bath_range = ext.get("bathRange") or {}
        bath = bath_range.get("min")

        date_available = ext.get("dateAvailable", "") or ""

        # Bed and price ranges — required
        bed_range = ext.get("bedRange") or {}
        price_range = ext.get("rentPriceRange") or {}
        bed_min_raw = bed_range.get("min")
        price_min_raw = price_range.get("min")
        if bed_min_raw is None or price_min_raw is None:
            continue

        bed_min = int(bed_min_raw)
        bed_max_raw = bed_range.get("max")
        bed_max = int(bed_max_raw) if bed_max_raw is not None else bed_min
        price_min = float(price_min_raw)
        price_max_raw = price_range.get("max")
        price_max = float(price_max_raw) if price_max_raw is not None else price_min

        # Emit one entry per bedroom count, interpolating price and sqft linearly.
        for beds in range(bed_min, bed_max + 1):
            span = bed_max - bed_min
            if span == 0:
                price = price_min
                sqft = sqft_min
            else:
                frac = (beds - bed_min) / span
                price = price_min + frac * (price_max - price_min)
                if sqft_min is not None and sqft_max is not None:
                    sqft = sqft_min + frac * (sqft_max - sqft_min)
                else:
                    sqft = sqft_min

            out.append({
                "zpid": f"{hd.get('propertyId', '')}_{beds}",
                "address": full_address,
                "buildingName": property_name,
                "latitude": float(lat),
                "longitude": float(lng),
                "price": price,
                "bedrooms": beds,
                "bathrooms": bath,
                "livingArea": int(sqft) if sqft is not None else None,
                "homeStatus": "FOR_RENT",
                "imgSrc": img_src,
                "detailUrl": detail_url,
                "dateAvailable": date_available,
            })

    return out


def _parse_price(price_str: str) -> Optional[float]:
    """Parse price strings like '$1,245+', '$2,200/mo', 'Call' etc."""
    if not price_str or price_str.lower() in ("call", "contact", ""):
        return None
    cleaned = price_str.replace("$", "").replace(",", "").replace("+", "").replace("/mo", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def get_sample_listings() -> list:
    """
    Realistic sample rental listings near SF.
    Used as fallback when Zillow API is unavailable.
    Based on actual SLO rental market data.
    """
    return [
        {"zpid": "s1", "address": "55 Broad St, San Luis Obispo, CA 93405", "latitude": 35.2805, "longitude": -120.6594, "price": 1950, "bedrooms": 1, "bathrooms": 1, "livingArea": 620, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s2", "address": "1468 Slack St, San Luis Obispo, CA 93405", "latitude": 35.2892, "longitude": -120.6685, "price": 2100, "bedrooms": 1, "bathrooms": 1, "livingArea": 680, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s3", "address": "773 Foothill Blvd, San Luis Obispo, CA 93405", "latitude": 35.3032, "longitude": -120.6571, "price": 1800, "bedrooms": 0, "bathrooms": 1, "livingArea": 450, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s4", "address": "81 Palomar Ave, San Luis Obispo, CA 93405", "latitude": 35.2971, "longitude": -120.6543, "price": 2200, "bedrooms": 1, "bathrooms": 1, "livingArea": 700, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s5", "address": "1240 Fredericks St, San Luis Obispo, CA 93405", "latitude": 35.2955, "longitude": -120.6688, "price": 3200, "bedrooms": 2, "bathrooms": 2, "livingArea": 950, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s6", "address": "21 Stenner Creek Rd, San Luis Obispo, CA 93405", "latitude": 35.3065, "longitude": -120.6520, "price": 1650, "bedrooms": 0, "bathrooms": 1, "livingArea": 400, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s7", "address": "1600 Santa Rosa St, San Luis Obispo, CA 93401", "latitude": 35.2870, "longitude": -120.6613, "price": 2400, "bedrooms": 1, "bathrooms": 1, "livingArea": 750, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s8", "address": "61 N Broad St, San Luis Obispo, CA 93405", "latitude": 35.2830, "longitude": -120.6610, "price": 3800, "bedrooms": 2, "bathrooms": 2, "livingArea": 1050, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s9", "address": "150 N Chorro St, San Luis Obispo, CA 93405", "latitude": 35.2825, "longitude": -120.6570, "price": 2050, "bedrooms": 1, "bathrooms": 1, "livingArea": 660, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s10", "address": "3000 Calle Malva, San Luis Obispo, CA 93401", "latitude": 35.3120, "longitude": -120.6750, "price": 2800, "bedrooms": 2, "bathrooms": 1, "livingArea": 880, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s11", "address": "490 Foothill Blvd, San Luis Obispo, CA 93405", "latitude": 35.2988, "longitude": -120.6558, "price": 1900, "bedrooms": 0, "bathrooms": 1, "livingArea": 480, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s12", "address": "795 Buchon St, San Luis Obispo, CA 93401", "latitude": 35.2755, "longitude": -120.6620, "price": 2300, "bedrooms": 1, "bathrooms": 1, "livingArea": 720, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s13", "address": "1300 Bond St, San Luis Obispo, CA 93401", "latitude": 35.2910, "longitude": -120.6710, "price": 4200, "bedrooms": 2, "bathrooms": 2, "livingArea": 1100, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s14", "address": "1925 Augusta St, San Luis Obispo, CA 93401", "latitude": 35.2862, "longitude": -120.6450, "price": 3400, "bedrooms": 2, "bathrooms": 1, "livingArea": 920, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s15", "address": "3700 Sacramento Dr, San Luis Obispo, CA 93401", "latitude": 35.2670, "longitude": -120.6820, "price": 2600, "bedrooms": 2, "bathrooms": 1, "livingArea": 850, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s16", "address": "450 Kentucky St, San Luis Obispo, CA 93405", "latitude": 35.2945, "longitude": -120.6612, "price": 1750, "bedrooms": 0, "bathrooms": 1, "livingArea": 420, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s17", "address": "201 Murray Ave, San Luis Obispo, CA 93405", "latitude": 35.3005, "longitude": -120.6640, "price": 2150, "bedrooms": 1, "bathrooms": 1, "livingArea": 690, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
        {"zpid": "s18", "address": "22 Hathway Ave, San Luis Obispo, CA 93405", "latitude": 35.2980, "longitude": -120.6575, "price": 4400, "bedrooms": 2, "bathrooms": 2, "livingArea": 1080, "homeStatus": "FOR_RENT", "imgSrc": "", "detailUrl": ""},
    ]


def parse_bedrooms(prop: dict) -> int:
    """Extract bedroom count from a property object."""
    beds = prop.get("bedrooms")
    if beds is not None:
        try:
            return int(beds)
        except (ValueError, TypeError):
            pass
    # Try to infer from livingArea or property description
    unit_str = str(prop.get("hdpData", {}).get("homeInfo", {}).get("bedrooms", ""))
    if unit_str:
        try:
            return int(unit_str)
        except (ValueError, TypeError):
            pass
    return -1  # Unknown


def check_price(price: float, bedrooms: int) -> bool:
    """Check if price is within cap for the bedroom count."""
    if bedrooms in PRICE_CAPS:
        return price <= PRICE_CAPS[bedrooms]
    # For 3+ bedrooms, no specific cap defined, include them
    return True


def check_location(lat: float, lng: float) -> tuple[bool, float]:
    """Check if within 1.5 mile radius. Returns (passes, distance_miles)."""
    dist = haversine_miles(OPENAI_OFFICE_LAT, OPENAI_OFFICE_LNG, lat, lng)
    return dist <= MAX_RADIUS_MILES, round(dist, 2)


def check_date_availability(prop: dict) -> bool:
    """
    Check if property is available in the Jul 1 - Sep 5, 2026 window.
    Since Zillow rarely provides exact lease start dates, we accept
    properties listed as available / for rent.
    If a date field exists, we verify it falls within range.
    """
    # Check various date fields
    for date_field in ["dateAvailable", "availableFrom", "date_available"]:
        val = prop.get(date_field) or prop.get("hdpData", {}).get("homeInfo", {}).get(date_field)
        if val:
            try:
                avail_date = datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
                return avail_date <= TARGET_END
            except (ValueError, TypeError):
                pass

    # If listing is currently active / for rent, assume available
    status = str(prop.get("statusType", "") or prop.get("homeStatus", "")).upper()
    if "RENT" in status or "AVAILABLE" in status or "ACTIVE" in status:
        return True

    # Default: include it (most rental listings are currently available)
    return True


def check_date_not_before_july(prop: dict) -> bool:
    """
    Exclude listings whose known availability date is AFTER the move-in
    window ends (TARGET_END).  Listings available earlier in the window
    (or already available now) are fine — the tenant can take a later
    start date.  Listings without a date are included.
    """
    for date_field in ["dateAvailable", "availableFrom", "date_available"]:
        val = prop.get(date_field) or prop.get("hdpData", {}).get("homeInfo", {}).get(date_field)
        if val:
            try:
                avail_date = datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
                if avail_date > TARGET_END:
                    return False
            except (ValueError, TypeError):
                pass
    return True


def process_listings(raw_data: dict) -> list[dict]:
    """Process raw Zillow API response into filtered, scored listings."""
    results = []
    props = raw_data.get("props", [])

    # Also check under different response keys
    if not props:
        props = raw_data.get("results", [])
    if not props:
        search_results = raw_data.get("searchResults", {})
        if isinstance(search_results, dict):
            props = search_results.get("listResults", [])
    if not props:
        # Try flat property list
        if isinstance(raw_data, list):
            props = raw_data

    for prop in props:
        try:
            # Extract core fields
            lat = prop.get("latitude") or prop.get("lat")
            lng = prop.get("longitude") or prop.get("lng") or prop.get("long")
            price = prop.get("price")
            address = prop.get("address") or prop.get("streetAddress", "Unknown")

            # Handle price formats
            if isinstance(price, str):
                price = price.replace("$", "").replace(",", "").replace("/mo", "").replace("+", "")
                try:
                    price = float(price)
                except ValueError:
                    continue
            elif isinstance(price, (int, float)):
                price = float(price)
            else:
                # Try unformattedPrice
                price = prop.get("unformattedPrice") or prop.get("rentZestimate")
                if price is None:
                    continue
                price = float(price)

            if not lat or not lng:
                continue

            lat = float(lat)
            lng = float(lng)
            bedrooms = parse_bedrooms(prop)
            if bedrooms < 0:
                bedrooms = 1  # Default assumption

            # --- Hard exclusions ---
            # 0. Absolute max price — anything over $6000 is excluded regardless
            if price > 6000:
                continue

            # 1. Distance > 40 min walk (2.0 miles at 3 mph)
            _, distance = check_location(lat, lng)
            if distance > 2.0:
                continue

            # 2. Price exceeds cap by more than 20%
            #    Studio < $2640, 1BR < $3000, 2BR < $5400, 3+BR < $6000
            if bedrooms in PRICE_CAPS and price > PRICE_CAPS[bedrooms] * 1.2:
                continue
            elif bedrooms not in PRICE_CAPS and price > 6000:
                continue

            # 3. Lease starts before July (if date info available)
            if not check_date_not_before_july(prop):
                continue

            # --- Scoring ---
            location_ok = distance <= MAX_RADIUS_MILES
            price_ok = check_price(price, bedrooms)
            date_ok = check_date_availability(prop)

            criteria_met = sum([location_ok, price_ok, date_ok])
            if criteria_met < 2:
                continue  # Skip if fewer than 2 criteria met

            # Determine marker color
            if criteria_met == 3:
                marker = "green"
            else:
                marker = "orange"

            # Build address string
            if isinstance(address, dict):
                address = f"{address.get('streetAddress', '')} {address.get('city', '')}, {address.get('state', '')} {address.get('zipcode', '')}".strip()

            # Include building name if available
            building_name = prop.get("buildingName", "")
            display_address = str(address)
            if building_name and building_name not in display_address:
                display_address = f"{building_name} — {display_address}"

            listing = {
                "id": prop.get("zpid") or prop.get("id") or hash(f"{lat}{lng}{price}"),
                "address": display_address,
                "price": int(price),
                "bedrooms": bedrooms,
                "bathrooms": prop.get("bathrooms") or prop.get("baths"),
                "sqft": prop.get("livingArea") or prop.get("area"),
                "lat": lat,
                "lng": lng,
                "distance_miles": distance,
                "image": prop.get("imgSrc") or prop.get("image") or prop.get("thumbnailUrl"),
                "url": prop.get("detailUrl") or prop.get("url"),
                "marker": marker,
                "criteria_met": criteria_met,
                "location_ok": location_ok,
                "price_ok": price_ok,
                "date_ok": date_ok,
            }

            # Build Zillow URL if relative
            if listing["url"] and not listing["url"].startswith("http"):
                listing["url"] = f"https://www.zillow.com{listing['url']}"

            results.append(listing)

        except (TypeError, ValueError, KeyError):
            continue

    # Sort: green first, then by distance
    results.sort(key=lambda x: (0 if x["marker"] == "green" else 1, x["distance_miles"]))
    return results


# --- For-sale (Redfin /property/search) ---

async def fetch_sale_listings(location: str = "San Francisco") -> dict:
    """
    Fetch for-sale apartment listings from Redfin via RapidAPI.

    Calls /property/search (the generic search endpoint, which defaults to
    for-sale listings).  Returns a dict with `raw_homes` for downstream
    processing and a `source` / optional `error` field for the UI.
    """
    cache_key = hashlib.md5(f"redfin_sale_{location}".encode()).hexdigest()
    cached = get_cache(cache_key)
    if cached:
        return cached

    if not RAPIDAPI_KEY:
        return {
            "raw_homes": [],
            "source": "no_api_key",
            "error": "RAPIDAPI_KEY is not set. Add it to your .env file to fetch live data.",
        }

    url = "https://redfin-com-data.p.rapidapi.com/property/search"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "redfin-com-data.p.rapidapi.com",
    }
    params = {"location": location}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()

        api_data = response.json()
        homes = (api_data.get("data") or {}).get("homes", []) or []
        data = {"raw_homes": homes, "source": "redfin_rapidapi"}
        set_cache(cache_key, data)
        return data

    except httpx.HTTPStatusError as e:
        msg = f"Redfin RapidAPI returned HTTP {e.response.status_code}"
        print(f"Redfin sale RapidAPI failed: {msg}")
        return {"raw_homes": [], "source": "error", "error": msg}
    except Exception as e:
        print(f"Redfin sale RapidAPI failed: {e}")
        return {"raw_homes": [], "source": "error", "error": str(e)}


def process_sale_listings(raw_data: dict) -> list[dict]:
    """
    Filter Redfin /property/search results to apartments matching:
      - 1 or 2 bedrooms (SALE_BEDROOMS)
      - price under SALE_MAX_PRICE
      - within MAX_RADIUS_MILES of the OpenAI Office anchor
      - not senior housing (listingRemarks does not contain SALE_SENIOR_KEYWORDS)
    """
    results = []
    homes = raw_data.get("raw_homes", []) or []

    for h in homes:
        try:
            beds = h.get("beds")
            if beds is None or int(beds) not in SALE_BEDROOMS:
                continue

            price_val = (h.get("price") or {}).get("value")
            if price_val is None or float(price_val) >= SALE_MAX_PRICE:
                continue
            price = float(price_val)

            latlong = (h.get("latLong") or {}).get("value") or {}
            lat = latlong.get("latitude")
            lng = latlong.get("longitude")
            if lat is None or lng is None:
                continue
            lat, lng = float(lat), float(lng)

            within_radius, distance = check_location(lat, lng)
            if not within_radius:
                continue

            remarks = (h.get("listingRemarks") or "").lower()
            if any(k in remarks for k in SALE_SENIOR_KEYWORDS):
                continue

            # Address fields — Redfin wraps several of these in {level, value} dicts.
            def _unwrap(v):
                if isinstance(v, dict):
                    return v.get("value", "") or ""
                return v or ""

            street = _unwrap(h.get("streetLine"))  # already includes unit number
            city = h.get("city", "") or ""
            state = h.get("state", "") or ""
            zipcode = _unwrap(h.get("postalCode"))
            full_address = ", ".join(
                p for p in [street, city, f"{state} {zipcode}".strip()] if p
            )

            # Detail URL — usually a relative path under homeData; here it's flat
            rel_url = h.get("url") or ""
            detail_url = (
                f"https://www.redfin.com{rel_url}"
                if rel_url and not rel_url.startswith("http")
                else rel_url
            )

            # Primary photo
            photos = ((h.get("photos") or {}).get("items") or [])
            img = photos[0] if photos else ""

            # Sqft / baths
            sqft = (h.get("sqFt") or {}).get("value")
            baths = h.get("baths")

            results.append({
                "id": h.get("listingId") or h.get("propertyId") or hash(f"{lat}{lng}{price}"),
                "address": full_address,
                "price": int(price),
                "bedrooms": int(beds),
                "bathrooms": baths,
                "sqft": int(sqft) if sqft else None,
                "lat": lat,
                "lng": lng,
                "distance_miles": round(distance, 2),
                "image": img,
                "url": detail_url,
                "marker": "green",
                "criteria_met": 3,
                "location_ok": True,
                "price_ok": True,
                "date_ok": True,
                "neighborhood": (h.get("location") or {}).get("value", ""),
                "year_built": next(
                    (kf["description"].replace("Built ", "")
                     for kf in (h.get("keyFacts") or [])
                     if isinstance(kf, dict) and str(kf.get("description", "")).startswith("Built ")),
                    "",
                ),
            })
        except (TypeError, ValueError, KeyError):
            continue

    results.sort(key=lambda x: x["price"])
    return results


# --- API Routes ---

@app.get("/api/config")
async def get_config():
    """Return Maps API key and building coordinates for frontend."""
    return {
        "mapsApiKey": MAPS_API_KEY,
        "openaiOffice": {"lat": OPENAI_OFFICE_LAT, "lng": OPENAI_OFFICE_LNG},
        "radiusMiles": MAX_RADIUS_MILES,
        "priceCaps": PRICE_CAPS,
    }


@app.get("/api/listings")
async def get_listings(page: int = Query(1, ge=1, le=5)):
    """Fetch and return filtered apartment listings."""
    try:
        raw_data = await fetch_zillow_listings(page=page)
        listings = process_listings(raw_data)
        source = raw_data.get("source", "zillow_rapidapi")
        response_body = {
            "listings": listings,
            "total": len(listings),
            "page": page,
            "source": source,
            "openaiOffice": {"lat": OPENAI_OFFICE_LAT, "lng": OPENAI_OFFICE_LNG},
            "filters": {
                "radiusMiles": MAX_RADIUS_MILES,
                "priceCaps": PRICE_CAPS,
                "dateRange": f"{TARGET_START} to {TARGET_END}",
            },
        }
        if "error" in raw_data:
            response_body["error"] = raw_data["error"]
        return response_body
    except httpx.HTTPStatusError as e:
        return JSONResponse(
            status_code=e.response.status_code,
            content={"error": f"Zillow API error: {e.response.status_code}", "detail": str(e)},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )


@app.get("/api/sale-listings")
async def get_sale_listings():
    """Fetch and return filtered for-sale apartment listings."""
    try:
        raw_data = await fetch_sale_listings()
        listings = process_sale_listings(raw_data)
        response_body = {
            "listings": listings,
            "total": len(listings),
            "source": raw_data.get("source", "redfin_rapidapi"),
            "openaiOffice": {"lat": OPENAI_OFFICE_LAT, "lng": OPENAI_OFFICE_LNG},
            "filters": {
                "radiusMiles": MAX_RADIUS_MILES,
                "maxPrice": SALE_MAX_PRICE,
                "bedrooms": list(SALE_BEDROOMS),
                "excludesSeniorHousing": True,
            },
        }
        if "error" in raw_data:
            response_body["error"] = raw_data["error"]
        return response_body
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/cache/clear")
async def clear_cache():
    """Clear the file cache."""
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        count += 1
    return {"cleared": count}


# --- Static files & SPA ---
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/buy")
async def serve_buy():
    return FileResponse("static/buy.html")
