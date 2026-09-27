from flask import Flask, request, jsonify, render_template_string
import requests
import webbrowser
import threading
import time


app = Flask(__name__)


# ============================================================
# SHARED HTTP HELPER: retry-with-backoff + short-lived cache
# ============================================================
# Open-Meteo's free tier can return 429 (Too Many Requests) under
# shared-IP load (e.g. on Render's free plan). A quick retry clears
# most transient throttles, and caching identical lookups for a few
# minutes cuts down how often we hit the API at all.

_response_cache = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _cache_key(url, params):
    # Round coordinates to ~100m precision so nearby repeat requests
    # (e.g. the same visitor's browser retrying) share a cache entry.
    rounded = {}
    for k, v in (params or {}).items():
        if k in ("latitude", "longitude") and isinstance(v, (int, float)):
            rounded[k] = round(v, 3)
        else:
            rounded[k] = v
    return url + "?" + "&".join(f"{k}={rounded[k]}" for k in sorted(rounded))


def get_json_with_retry(url, params=None, timeout=10, retries=2, backoff_seconds=1.5):
    """GET a JSON endpoint with a short cache and retry-with-backoff on 429s."""
    key = _cache_key(url, params)
    cached = _response_cache.get(key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL_SECONDS:
        return cached["data"]

    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code == 429 and attempt < retries:
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            _response_cache[key] = {"data": data, "ts": time.time()}
            return data
        except requests.exceptions.HTTPError as error:
            last_error = error
            if response is not None and response.status_code == 429 and attempt < retries:
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            raise
        except Exception as error:
            last_error = error
            raise
    if last_error:
        raise last_error
    return None


def get_air_quality(latitude, longitude):
    """Fetch current AQI and particulate matter for wellness advisories."""
    try:
        url = "https://air-quality-api.open-meteo.com/v1/air-quality"
        params = {"latitude": latitude, "longitude": longitude, "current": "us_aqi,pm2_5,pm10", "timezone": "auto"}
        data = get_json_with_retry(url, params=params, timeout=10)
        current = data.get("current", {})
        return {"aqi": current.get("us_aqi"), "pm2_5": current.get("pm2_5"), "pm10": current.get("pm10")}
    except Exception as error:
        print("Air Quality API Error:", error)
        return {"aqi": None, "pm2_5": None, "pm10": None}


# ============================================================
# WEATHER
# ============================================================

def get_weather(city):

    try:

        # City name -> Latitude / Longitude
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"

        geo_params = {
            "name": city,
            "count": 1,
            "language": "en",
            "format": "json"
        }

        geo_data = get_json_with_retry(
            geo_url,
            params=geo_params,
            timeout=10
        )

        if "results" not in geo_data or not geo_data["results"]:
            return None

        place = geo_data["results"][0]

        latitude = place["latitude"]
        longitude = place["longitude"]
        city_name = place["name"]
        country = place.get("country", "")

        # Get current weather
        weather_url = "https://api.open-meteo.com/v1/forecast"

        weather_params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,"
                "relative_humidity_2m,"
                "apparent_temperature,"
                "precipitation,"
                "weather_code,"
                "wind_speed_10m,"
                "surface_pressure,"
                "uv_index"
            ),
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "auto"
        }

        weather_data = get_json_with_retry(
            weather_url,
            params=weather_params,
            timeout=10
        )
        current = weather_data["current"]

        weather_codes = {
            0: "Clear Sky ☀️",
            1: "Mainly Clear 🌤️",
            2: "Partly Cloudy ⛅",
            3: "Overcast ☁️",
            45: "Fog 🌫️",
            48: "Fog 🌫️",
            51: "Light Drizzle 🌦️",
            53: "Drizzle 🌦️",
            55: "Heavy Drizzle 🌧️",
            61: "Light Rain 🌧️",
            63: "Moderate Rain 🌧️",
            65: "Heavy Rain 🌧️",
            71: "Light Snow ❄️",
            73: "Snow ❄️",
            75: "Heavy Snow ❄️",
            80: "Rain Showers 🌦️",
            81: "Rain Showers 🌧️",
            82: "Heavy Rain Showers 🌧️",
            95: "Thunderstorm ⛈️",
            96: "Thunderstorm with Hail ⛈️",
            99: "Heavy Thunderstorm ⛈️"
        }

        code = current.get("weather_code", 0)
        air = get_air_quality(latitude, longitude)

        return {
            "city": city_name,
            "country": country,
            "temperature": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "feels_like": current.get("apparent_temperature"),
            "rainfall": current.get("precipitation"),
            "wind": current.get("wind_speed_10m"),
            "pressure": current.get("surface_pressure"),
            "uv_index": current.get("uv_index"),
            "aqi": air.get("aqi"),
            "pm2_5": air.get("pm2_5"),
            "pm10": air.get("pm10"),
            "condition": weather_codes.get(code, "Unknown")
        }

    except Exception as error:

        print("Weather API Error:", error)

        return None


# ============================================================
# LOCATION / ROUTE WEATHER
# ============================================================

def get_weather_by_coordinates(latitude, longitude, label="Current Location"):
    """Fetch current weather for exact browser/route coordinates."""
    try:
        weather_url = "https://api.open-meteo.com/v1/forecast"
        weather_params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,"
                "relative_humidity_2m,"
                "apparent_temperature,"
                "precipitation,"
                "weather_code,"
                "wind_speed_10m,"
                "surface_pressure,"
                "uv_index"
            ),
            "hourly": "precipitation_probability",
            "forecast_hours": 1,
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "auto"
        }

        data = get_json_with_retry(weather_url, params=weather_params, timeout=10)
        current = data["current"]

        weather_codes = {
            0: "Clear Sky ☀️",
            1: "Mainly Clear 🌤️",
            2: "Partly Cloudy ⛅",
            3: "Overcast ☁️",
            45: "Fog 🌫️",
            48: "Fog 🌫️",
            51: "Light Drizzle 🌦️",
            53: "Drizzle 🌦️",
            55: "Heavy Drizzle 🌧️",
            61: "Light Rain 🌧️",
            63: "Moderate Rain 🌧️",
            65: "Heavy Rain 🌧️",
            71: "Light Snow ❄️",
            73: "Snow ❄️",
            75: "Heavy Snow ❄️",
            80: "Rain Showers 🌦️",
            81: "Rain Showers 🌧️",
            82: "Heavy Rain Showers 🌧️",
            95: "Thunderstorm ⛈️",
            96: "Thunderstorm with Hail ⛈️",
            99: "Heavy Thunderstorm ⛈️"
        }

        code = current.get("weather_code", 0)
        temperature = current.get("temperature_2m")
        precipitation_probability = 0

        try:
            precipitation_probability = (
                data.get("hourly", {})
                .get("precipitation_probability", [0])[0] or 0
            )
        except (IndexError, TypeError):
            precipitation_probability = 0

        air = get_air_quality(latitude, longitude)

        # UI theme:
        # rain/snow/storm always wins; otherwise use temperature + sky condition.
        if code in [51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99]:
            theme = "rain"
        elif code in [71, 73, 75]:
            theme = "winter"
        elif temperature is not None and temperature <= 18:
            theme = "winter"
        elif code in [0, 1] and temperature is not None and temperature >= 30:
            theme = "summer"
        elif code in [0, 1]:
            theme = "sunny"
        else:
            theme = "cloudy"

        return {
            "label": label,
            "latitude": latitude,
            "longitude": longitude,
            "temperature": temperature,
            "humidity": current.get("relative_humidity_2m"),
            "feels_like": current.get("apparent_temperature"),
            "rainfall": current.get("precipitation"),
            "rain_probability": precipitation_probability,
            "wind": current.get("wind_speed_10m"),
            "pressure": current.get("surface_pressure"),
            "uv_index": current.get("uv_index"),
            "aqi": air.get("aqi"),
            "pm2_5": air.get("pm2_5"),
            "pm10": air.get("pm10"),
            "condition": weather_codes.get(code, "Unknown"),
            "weather_code": code,
            "theme": theme
        }

    except Exception as error:
        print("Coordinate Weather API Error:", error)
        return None


# Major Indian city coordinates are kept as a safety net so a common city
# name can never accidentally resolve to a same-named place in another state/country.
INDIA_CITY_COORDS = {
    "lucknow": (26.8467, 80.9462),
    "gorakhpur": (26.7606, 83.3732),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "kanpur": (26.4499, 80.3319),
    "varanasi": (25.3176, 82.9739),
    "prayagraj": (25.4358, 81.8463),
    "allahabad": (25.4358, 81.8463),
    "ayodhya": (26.7922, 82.1998),
    "agra": (27.1767, 78.0081),
    "jaipur": (26.9124, 75.7873),
    "patna": (25.5941, 85.1376),
    "noida": (28.5355, 77.3910),
    "gurgaon": (28.4595, 77.0266),
    "gurugram": (28.4595, 77.0266),
    "dehradun": (30.3165, 78.0322),
    "chandigarh": (30.7333, 76.7794),
    "amritsar": (31.6340, 74.8723),
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "pune": (18.5204, 73.8567),
    "mumbai": (19.0760, 72.8777),
    "kolkata": (22.5726, 88.3639),
    "chennai": (13.0827, 80.2707),
    "bhopal": (23.2599, 77.4126),
    "indore": (22.7196, 75.8577),
    "meerut": (28.9845, 77.7064),
    "bareilly": (28.3670, 79.4304),
    "surat": (21.1702, 72.8311),
    "ahmedabad": (23.0225, 72.5714),
    "nagpur": (21.1458, 79.0882),
    "nashik": (19.9975, 73.7898),
}

def geocode_destination(destination):
    """Resolve an Indian destination accurately, preferring a known city coordinate
    and otherwise using country-restricted geocoding. This avoids wrong same-name
    locations such as a different Gorakhpur.
    """
    clean = " ".join(str(destination).strip().lower().split())

    # Exact common-city match first.
    if clean in INDIA_CITY_COORDS:
        lat, lon = INDIA_CITY_COORDS[clean]
        return {
            "name": clean.title(),
            "country": "India",
            "latitude": lat,
            "longitude": lon
        }

    try:
        # Open-Meteo supports country_code in the search request.
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        geo_params = {
            "name": destination,
            "count": 10,
            "language": "en",
            "format": "json",
            "country_code": "IN"
        }

        data = get_json_with_retry(geo_url, params=geo_params, timeout=10)

        results = data.get("results") or []
        if not results:
            return None

        # Prefer India + populated place and an exact-ish name match.
        target = clean.replace(", india", "").strip()
        results.sort(key=lambda x: (
            0 if str(x.get("name", "")).strip().lower() == target else 1,
            0 if str(x.get("country_code", "")).upper() == "IN" else 1,
            0 if x.get("population") else 1
        ))
        place = results[0]

        return {
            "name": place.get("name", destination.title()),
            "country": place.get("country", "India"),
            "latitude": float(place["latitude"]),
            "longitude": float(place["longitude"])
        }

    except Exception as error:
        print("Destination Geocoding Error:", error)
        return None


def get_google_route(origin_lat, origin_lon, destination_place):
    """Use Google Directions when GOOGLE_MAPS_API_KEY is configured.
    This is the provider to use when the route must match Google Maps.
    Returns None when no key is configured or the API cannot be used.
    """
    import os

    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return None

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{destination_place['latitude']},{destination_place['longitude']}",
        "mode": "driving",
        "language": "en-IN",
        "region": "in",
        "alternatives": "true",
        "key": api_key
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "OK" or not data.get("routes"):
        print("Google Directions API:", data.get("status"), data.get("error_message", ""))
        return None

    # Pick the shortest driving route, matching the normal Google Maps default
    # behaviour more closely than simply taking the first arbitrary alternative.
    route = min(data["routes"], key=lambda r: r.get("legs", [{}])[0].get("distance", {}).get("value", 10**18))
    leg = route["legs"][0]

    # Decode Google's overview polyline without external packages.
    def decode_polyline(polyline):
        coords = []
        index = lat = lng = 0
        while index < len(polyline):
            shift = result = 0
            while True:
                b = ord(polyline[index]) - 63; index += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            dlat = ~(result >> 1) if result & 1 else result >> 1
            lat += dlat
            shift = result = 0
            while True:
                b = ord(polyline[index]) - 63; index += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            dlng = ~(result >> 1) if result & 1 else result >> 1
            lng += dlng
            coords.append([lng / 1e5, lat / 1e5])
        return coords

    geometry = decode_polyline(route.get("overview_polyline", {}).get("points", ""))
    steps = []
    for step in leg.get("steps", []):
        start = step.get("start_location", {})
        steps.append({
            "instruction": step.get("html_instructions", "Continue").replace("<b>", "").replace("</b>", "").replace("<div style=\"font-size:0.9em\">", " ").replace("</div>", ""),
            "road": step.get("name", "Road"),
            "type": step.get("maneuver", "continue"),
            "modifier": "",
            "distance_m": int(step.get("distance", {}).get("value", 0)),
            "duration_s": int(step.get("duration", {}).get("value", 0)),
            "latitude": start.get("lat"),
            "longitude": start.get("lng")
        })

    return {
        "distance_km": round(leg.get("distance", {}).get("value", 0) / 1000, 1),
        "duration_min": round(leg.get("duration", {}).get("value", 0) / 60),
        "geometry": geometry,
        "steps": steps,
        "provider": "Google Maps Directions"
    }


def get_route_weather(origin_lat, origin_lon, destination):
    """
    Build a real road route with OSRM and sample weather at several
    points along that route. This gives a practical 'weather on the way'
    view instead of checking only the origin and destination.
    """
    place = geocode_destination(destination)
    if place is None:
        return None, "Destination not found."

    try:
        # If a Google Maps key is configured, use Google for the route/distance/turns.
        # Otherwise fall back to OSRM. The UI also provides a one-click Google Maps
        # navigation link so the user can always switch to Google's live navigation.
        google_route = get_google_route(origin_lat, origin_lon, place)

        if google_route:
            coordinates = google_route["geometry"]
            route = None
            route_data = None
        else:
            route_url = (
                "https://router.project-osrm.org/route/v1/driving/"
                f"{origin_lon},{origin_lat};"
                f"{place['longitude']},{place['latitude']}"
            )
            route_params = {
                "overview": "full",
                "geometries": "geojson",
                "steps": "true",
                "annotations": "true"
            }

            response = requests.get(route_url, params=route_params, timeout=20)
            response.raise_for_status()
            route_data = response.json()

            if route_data.get("code") != "Ok" or not route_data.get("routes"):
                return None, "A road route could not be found for this destination."

            route = route_data["routes"][0]
            coordinates = route["geometry"]["coordinates"]

        # Show real place names along the route instead of generic "Checkpoint 1" labels.
        # Sample the road by route position, then reverse-geocode each sample to the
        # nearest locality (town/city/village).
        checkpoint_count = min(8, max(5, len(coordinates)))
        indexes = [
            round(i * (len(coordinates) - 1) / (checkpoint_count - 1))
            for i in range(checkpoint_count)
        ]

        def reverse_route_location(lat, lon):
            try:
                reverse_url = "https://nominatim.openstreetmap.org/reverse"
                reverse_params = {
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": 10,
                    "addressdetails": 1,
                    "accept-language": "en"
                }
                headers = {"User-Agent": "AgriWeather-AI/1.0 route-weather"}
                r = requests.get(reverse_url, params=reverse_params, headers=headers, timeout=10)
                r.raise_for_status()
                address = (r.json() or {}).get("address", {})
                locality = (
                    address.get("city")
                    or address.get("town")
                    or address.get("municipality")
                    or address.get("village")
                    or address.get("county")
                    or address.get("state_district")
                )
                state = address.get("state", "")
                if locality and state and state.lower() not in locality.lower():
                    return f"{locality}, {state}"
                return locality or "Route location"
            except Exception as reverse_error:
                print("Reverse geocoding error:", reverse_error)
                return "Route location"

        checkpoints = []
        for index, point_index in enumerate(indexes):
            lon, lat = coordinates[point_index]

            if index == 0:
                label = "Current Location"
            elif index == checkpoint_count - 1:
                label = place["name"]
            else:
                label = reverse_route_location(lat, lon)

            weather = get_weather_by_coordinates(lat, lon, label)
            if weather:
                weather["route_index"] = point_index
                weather["location"] = label
                checkpoints.append(weather)

        if not checkpoints:
            return None, "Weather data could not be loaded for the route."

        # Convert provider maneuvers into simple turn-by-turn guidance for the UI.
        steps = []
        if google_route:
            steps = google_route["steps"]
        else:
            for step in route.get("legs", [{}])[0].get("steps", []):
                maneuver = step.get("maneuver", {}) or {}
                location = maneuver.get("location", [])
                if len(location) < 2:
                    continue

                mtype = (maneuver.get("type") or "").lower()
                modifier = (maneuver.get("modifier") or "").replace("-", " ").lower()
                road_name = (step.get("name") or "Unnamed road").strip()
                instruction_map = {
                    "depart": "Start driving",
                    "arrive": "You have arrived",
                    "roundabout": "Enter the roundabout",
                    "rotary": "Enter the rotary",
                    "merge": "Merge",
                    "fork": "Take the fork",
                    "on ramp": "Take the ramp",
                    "off ramp": "Take the exit ramp",
                    "new name": "Continue",
                    "continue": "Continue",
                    "turn": "Turn"
                }
                if mtype in ("turn", "continue", "new name", "merge", "fork") and modifier:
                    action = {
                        "left": "Turn left",
                        "right": "Turn right",
                        "straight": "Continue straight",
                        "slight left": "Keep slightly left",
                        "slight right": "Keep slightly right",
                        "sharp left": "Turn sharp left",
                        "sharp right": "Turn sharp right",
                        "uturn": "Make a U-turn"
                    }.get(modifier, instruction_map.get(mtype, "Continue"))
                else:
                    action = instruction_map.get(mtype, "Continue")

                if road_name and road_name.lower() != "unnamed road" and mtype not in ("arrive",):
                    text = f"{action} onto {road_name}"
                else:
                    text = action

                steps.append({
                    "instruction": text,
                    "road": road_name,
                    "type": mtype,
                    "modifier": modifier,
                    "distance_m": round(step.get("distance", 0)),
                    "duration_s": round(step.get("duration", 0)),
                    "latitude": location[1],
                    "longitude": location[0]
                })

        return {
            "destination": place,
            "origin": {"latitude": origin_lat, "longitude": origin_lon, "name": "Current Location"},
            "distance_km": google_route["distance_km"] if google_route else round(route.get("distance", 0) / 1000, 1),
            "duration_min": google_route["duration_min"] if google_route else round(route.get("duration", 0) / 60),
            "geometry": google_route["geometry"] if google_route else route.get("geometry", {}).get("coordinates", []),
            "checkpoints": checkpoints,
            "steps": steps,
            "provider": google_route["provider"] if google_route else "OpenStreetMap / OSRM",
            "google_maps_url": f"https://www.google.com/maps/dir/?api=1&origin={origin_lat},{origin_lon}&destination={place['latitude']},{place['longitude']}&travelmode=driving"
        }, None

    except Exception as error:
        print("Route Weather Error:", error)
        return None, "Route weather service is temporarily unavailable."


# ============================================================
# LANGUAGE + CITY UNDERSTANDING
# ============================================================

def detect_language(text):
    """
    Detect the user's preferred response language.

    - Devanagari -> Hindi
    - Common Roman-Hindi/Hinglish words -> Hindi (Roman)
    - Otherwise -> English
    """
    raw = text.strip().lower()

    # Hindi script
    if any("\u0900" <= ch <= "\u097f" for ch in raw):
        return "hi"

    roman_hindi_words = {
        "ka", "ke", "ki", "ko", "me", "mein", "hai", "hain",
        "kya", "kaisa", "kaisi", "kaise", "batao", "bata",
        "mujhe", "mera", "meri", "mere", "aaj", "kal", "abhi",
        "waha", "yaha", "wala", "wali", "hoga", "hogi", "honge",
        "barish", "baarish", "garmi", "thand", "mausam", "hawa",
        "paani", "kitna", "kitni", "kab", "kyu", "kyon",
        "chahiye", "btao", "btana", "dikhao", "dekhna"
    }

    words = set(raw.replace("?", " ").replace(".", " ").replace(",", " ").split())
    if len(words.intersection(roman_hindi_words)) >= 1:
        return "hi-roman"

    return "en"


def city_aliases():
    return {
        # Common Indian city short forms / chat abbreviations
        "gkp": "gorakhpur",
        "gorakhpur": "gorakhpur",
        "lko": "lucknow",
        "lk": "lucknow",
        "dli": "delhi",
        "del": "delhi",
        "ndls": "new delhi",
        "mum": "mumbai",
        "bom": "mumbai",
        "blr": "bengaluru",
        "blr city": "bengaluru",
        "hyd": "hyderabad",
        "ccu": "kolkata",
        "kol": "kolkata",
        "chn": "chennai",
        "maa": "chennai",
        "pune": "pune",
        "pnq": "pune",
        "jpr": "jaipur",
        "jai": "jaipur",
        "agr": "agra",
        "knp": "kanpur",
        "vns": "varanasi",
        "nd": "new delhi",
        "noida": "noida",
        "ggn": "gurugram",
        "gurgaon": "gurgaon"
    }


def find_city(text):
    known_cities = [
        "new delhi", "lucknow", "delhi", "mumbai", "kanpur", "agra",
        "varanasi", "gorakhpur", "prayagraj", "allahabad", "jaipur",
        "patna", "noida", "gurgaon", "gurugram", "bengaluru",
        "bangalore", "hyderabad", "pune", "kolkata", "chennai",
        "bhopal", "indore", "meerut", "bareilly", "dehradun",
        "chandigarh", "amritsar", "surat", "ahmedabad", "nagpur",
        "nashik"
    ]

    raw = text.lower().strip()
    aliases = city_aliases()

    # Exact shorthand: "gkp", "lko", "mum", etc.
    if raw in aliases:
        return aliases[raw]

    # Shorthand embedded in a short sentence: "gkp weather", "gkp ka weather"
    for alias, city in aliases.items():
        if len(alias) >= 3 and f" {alias} " in f" {raw} ":
            return city

    # Prefer longer names first so "new delhi" is checked before "delhi".
    for city in sorted(known_cities, key=len, reverse=True):
        if city in raw:
            return city

    phrases = [
        "temperature in ", "weather in ", "humidity in ",
        "rainfall in ", "rain in ", "wind in ", "weather of ",
        "temperature of ", "climate of ", "weather for ",
        "temperature at ", "weather at "
    ]

    for phrase in phrases:
        if phrase in raw:
            city = raw.split(phrase, 1)[1].strip()
            city = city.replace("?", "").replace(".", "").replace("!", "")
            city = city.strip()

            # If the extracted value is a shorthand, expand it.
            return aliases.get(city, city)

    # Hindi / Hinglish patterns
    hindi_patterns = [
        " ka weather", " ki weather", " ka mausam", " mein weather",
        " me weather", " me mausam", " mein mausam", " ka temperature",
        " me temperature", " mein temperature", " mein barish",
        " me barish", " ka haal"
    ]

    for pattern in hindi_patterns:
        if pattern in raw:
            candidate = raw.split(pattern, 1)[0].strip()
            candidate = candidate.replace("weather", "").strip()
            candidate = candidate.replace("mausam", "").strip()

            if candidate in aliases:
                return aliases[candidate]

            if candidate:
                return candidate

    # A short unknown word can itself be a city, e.g. "ayodhya"
    if len(raw.split()) <= 2:
        candidate = raw.strip(" ?!.,")
        if candidate in aliases:
            return aliases[candidate]
        if candidate and candidate not in {
            "weather", "mausam", "temperature", "rain", "barish",
            "humidity", "wind", "pressure", "hello", "hi", "hey"
        }:
            return candidate

    return None


# ============================================================
# RESPONSE HELPERS
# ============================================================

def response_language(message):
    return detect_language(message)


def say_weather(weather, lang):
    city = weather["city"]
    condition = weather["condition"]
    temp = weather["temperature"]
    feels = weather["feels_like"]
    humidity = weather["humidity"]
    rainfall = weather["rainfall"]
    wind = weather["wind"]
    pressure = weather["pressure"]

    if lang == "hi":
        return (
            f"🌦️ {city} ka current weather:\n\n"
            f"☁️ Mausam: {condition}\n"
            f"🌡️ Temperature: {temp}°C\n"
            f"🌡️ Feels like: {feels}°C\n"
            f"💧 Humidity: {humidity}%\n"
            f"🌧️ Rainfall: {rainfall} mm\n"
            f"💨 Wind: {wind} km/h\n"
            f"🧭 Pressure: {pressure} hPa"
        )

    if lang == "hi-roman":
        return (
            f"🌦️ {city} ka current weather:\n\n"
            f"☁️ Mausam: {condition}\n"
            f"🌡️ Temperature: {temp}°C\n"
            f"🌡️ Feels like: {feels}°C\n"
            f"💧 Humidity: {humidity}%\n"
            f"🌧️ Rainfall: {rainfall} mm\n"
            f"💨 Wind: {wind} km/h\n"
            f"🧭 Pressure: {pressure} hPa"
        )

    return (
        f"🌦️ Current weather in {city}, {weather['country']}\n\n"
        f"☁️ Condition: {condition}\n"
        f"🌡️ Temperature: {temp}°C\n"
        f"🌡️ Feels like: {feels}°C\n"
        f"💧 Humidity: {humidity}%\n"
        f"🌧️ Rainfall: {rainfall} mm\n"
        f"💨 Wind: {wind} km/h\n"
        f"🧭 Pressure: {pressure} hPa"
    )


# ============================================================
# AI RESPONSE
# ============================================================

def generate_response(message):

    text = message.lower().strip()
    lang = response_language(message)

    # --------------------------------------------------------
    # VERY SHORT CITY INPUT
    # "gkp", "lko", "mum" etc. -> directly understand as weather
    # --------------------------------------------------------

    city = find_city(text)

    if city and len(text.split()) <= 3 and not any(
        word in text for word in [
            "weather", "temperature", "rain", "rainfall",
            "humidity", "wind", "pressure", "farming",
            "agriculture", "crop", "irrigation"
        ]
    ):
        weather = get_weather(city)

        if weather is None:
            if lang == "hi":
                return f"⚠️ {city.title()} ka weather data abhi nahi mil pa raha hai."
            if lang == "hi-roman":
                return f"⚠️ {city.title()} ka weather data abhi nahi mil pa raha hai."
            return f"⚠️ I couldn't get weather data for {city.title()}."

        return say_weather(weather, lang)

    # --------------------------------------------------------
    # GREETING
    # --------------------------------------------------------

    if text in ["hi", "hello", "hey", "hii", "hlo", "namaste", "नमस्ते"]:

        if lang == "hi":
            return "नमस्ते! 👋 मैं WeatherGPT हूँ। आप मौसम के बारे में कुछ भी पूछ सकते हैं।"

        if lang == "hi-roman":
            return "Namaste! 👋 Main WeatherGPT hoon. Aap weather ke baare mein kuch bhi pooch sakte ho."

        return (
            "Hello! 👋 I am WeatherGPT. "
            "Ask me about weather, temperature, rainfall, humidity, wind or agriculture."
        )

    # --------------------------------------------------------
    # NAME
    # --------------------------------------------------------

    if "your name" in text or "who are you" in text or "naam kya" in text or "kaun ho" in text:

        if lang == "hi":
            return "मैं WeatherGPT हूँ 🤖🌾। मैं real-time weather और agriculture information देता हूँ।"

        if lang == "hi-roman":
            return "Main WeatherGPT hoon 🤖🌾. Main real-time weather aur agriculture information deta hoon."

        return (
            "I am WeatherGPT 🤖🌾. "
            "I provide real-time weather information and agriculture recommendations."
        )

    # --------------------------------------------------------
    # THANKS
    # --------------------------------------------------------

    if "thank" in text or "thanks" in text or "dhanyavaad" in text or "shukriya" in text:

        if lang == "hi":
            return "आपका स्वागत है! 🌱"

        if lang == "hi-roman":
            return "You're welcome! 🌱"

        return "You're welcome! 🌱"

    # --------------------------------------------------------
    # BYE
    # --------------------------------------------------------

    if "bye" in text or "goodbye" in text or "milte hain" in text:

        if lang == "hi":
            return "अलविदा! 🌾 आपका दिन शुभ हो।"

        if lang == "hi-roman":
            return "Bye! 🌾 Aapka din achha rahe."

        return "Goodbye! 🌾 Have a great day."

    # --------------------------------------------------------
    # WEATHER QUESTION
    # --------------------------------------------------------

    weather_words = [
        "weather", "temperature", "rain", "rainfall", "humidity",
        "wind", "pressure", "mausam", "barish", "baarish",
        "garmi", "thand", "hawa", "taapman", "मौसम", "बारिश",
        "तापमान", "नमी", "हवा"
    ]

    is_weather_question = any(word in text for word in weather_words)

    if is_weather_question:

        if city is None:

            if lang == "hi":
                return "🌦️ Bilkul! Bas city ka naam bata dijiye, jaise: Lucknow ka weather kaisa hai?"

            if lang == "hi-roman":
                return "🌦️ Bilkul! Bas city ka naam batao, jaise: Lucknow ka weather kaisa hai?"

            return "🌦️ Sure! Please tell me the city name, for example: What's the weather in Lucknow?"

        weather = get_weather(city)

        if weather is None:

            if lang in ["hi", "hi-roman"]:
                return f"⚠️ {city.title()} ka weather data nahi mil pa raha. City name check karke try karo."

            return f"⚠️ I couldn't get weather data for {city.title()}. Please check the city name or internet connection."

        # Temperature
        if "temperature" in text or "taapman" in text or "गर्मी" in text or "तापमान" in text:

            if lang == "hi":
                return f"🌡️ {weather['city']} में अभी temperature {weather['temperature']}°C है। Feels like {weather['feels_like']}°C है।"

            if lang == "hi-roman":
                return f"🌡️ {weather['city']} mein abhi temperature {weather['temperature']}°C hai. Feels like {weather['feels_like']}°C hai."

            return (
                f"🌡️ The current temperature in {weather['city']} is "
                f"{weather['temperature']}°C. It feels like {weather['feels_like']}°C."
            )

        # Humidity
        if "humidity" in text or "nami" in text or "नमी" in text:

            if lang == "hi":
                return f"💧 {weather['city']} में current humidity {weather['humidity']}% है।"

            if lang == "hi-roman":
                return f"💧 {weather['city']} mein current humidity {weather['humidity']}% hai."

            return f"💧 The current humidity in {weather['city']} is {weather['humidity']}%."

        # Rain
        if "rain" in text or "rainfall" in text or "barish" in text or "baarish" in text or "बारिश" in text:

            if lang == "hi":
                return f"🌧️ {weather['city']} में rainfall {weather['rainfall']} mm है। Condition: {weather['condition']}।"

            if lang == "hi-roman":
                return f"🌧️ {weather['city']} mein rainfall {weather['rainfall']} mm hai. Condition: {weather['condition']}."

            return (
                f"🌧️ Current rainfall in {weather['city']} is "
                f"{weather['rainfall']} mm. Condition: {weather['condition']}."
            )

        # Wind
        if "wind" in text or "hawa" in text or "हवा" in text:

            if lang == "hi":
                return f"💨 {weather['city']} में हवा की speed {weather['wind']} km/h है।"

            if lang == "hi-roman":
                return f"💨 {weather['city']} mein hawa ki speed {weather['wind']} km/h hai."

            return f"💨 Current wind speed in {weather['city']} is {weather['wind']} km/h."

        # Pressure
        if "pressure" in text:

            if lang == "hi":
                return f"🧭 {weather['city']} में current pressure {weather['pressure']} hPa है।"

            if lang == "hi-roman":
                return f"🧭 {weather['city']} mein current pressure {weather['pressure']} hPa hai."

            return f"🧭 Current pressure in {weather['city']} is {weather['pressure']} hPa."

        return say_weather(weather, lang)

    # --------------------------------------------------------
    # AGRICULTURE
    # --------------------------------------------------------

    agriculture_words = [
        "agriculture", "farming", "farmer", "crop", "irrigation",
        "kheti", "kisaan", "fasal", "sinchai", "किसान", "खेती", "फसल"
    ]

    if any(word in text for word in agriculture_words):

        if city is None:

            if lang == "hi":
                return "🌱 मैं weather-based farming advice दे सकता हूँ। City का नाम बताइए।"

            if lang == "hi-roman":
                return "🌱 Main weather-based farming advice de sakta hoon. City ka naam batao."

            return "🌱 I can provide agriculture advice based on weather conditions. Tell me the city."

        weather = get_weather(city)

        if weather is None:
            if lang in ["hi", "hi-roman"]:
                return f"⚠️ {city.title()} ka weather data nahi mil pa raha."
            return f"⚠️ I couldn't get weather information for {city.title()}."

        temperature = weather["temperature"]
        humidity = weather["humidity"]
        rainfall = weather["rainfall"]

        if rainfall > 0:
            if lang in ["hi", "hi-roman"]:
                advice = "Abhi rainfall ho rahi hai. Unnecessary irrigation avoid karo."
            else:
                advice = "Rainfall is currently occurring. Avoid unnecessary irrigation."
        elif temperature >= 35:
            if lang in ["hi", "hi-roman"]:
                advice = "Temperature high hai. Soil moisture monitor karo aur soil dry ho to irrigation do."
            else:
                advice = "Temperature is high. Monitor soil moisture and irrigate if the soil is dry."
        elif humidity >= 80:
            if lang in ["hi", "hi-roman"]:
                advice = "Humidity high hai. Crops mein fungal diseases ko monitor karo."
            else:
                advice = "Humidity is high. Monitor crops for fungal diseases."
        else:
            if lang in ["hi", "hi-roman"]:
                advice = "Weather relatively normal hai. Soil moisture aur rainfall monitor karte raho."
            else:
                advice = "Current weather conditions look relatively normal. Continue monitoring soil moisture and rainfall."

        if lang == "hi":
            return (
                f"🌱 {weather['city']} के लिए Agriculture Advisory\n\n"
                f"🌡️ Temperature: {temperature}°C\n"
                f"💧 Humidity: {humidity}%\n"
                f"🌧️ Rainfall: {rainfall} mm\n\n"
                f"💡 Recommendation:\n{advice}"
            )

        if lang == "hi-roman":
            return (
                f"🌱 {weather['city']} ke liye Agriculture Advisory\n\n"
                f"🌡️ Temperature: {temperature}°C\n"
                f"💧 Humidity: {humidity}%\n"
                f"🌧️ Rainfall: {rainfall} mm\n\n"
                f"💡 Recommendation:\n{advice}"
            )

        return (
            f"🌱 Agriculture Advisory for {weather['city']}\n\n"
            f"🌡️ Temperature: {temperature}°C\n"
            f"💧 Humidity: {humidity}%\n"
            f"🌧️ Rainfall: {rainfall} mm\n\n"
            f"💡 Recommendation:\n{advice}"
        )

    # --------------------------------------------------------
    # DEFAULT
    # --------------------------------------------------------

    if lang == "hi":
        return (
            "🤖 मैं WeatherGPT हूँ। मैं real-time weather और agriculture में मदद कर सकता हूँ।\n\n"
            "उदाहरण:\n"
            "🌡️ Lucknow का temperature?\n"
            "🌦️ GKP का weather?\n"
            "💧 Mumbai में humidity कितनी है?\n"
            "🌱 Lucknow में farming advice दो।"
        )

    if lang == "hi-roman":
        return (
            "🤖 Main WeatherGPT hoon. Main real-time weather aur agriculture mein help kar sakta hoon.\n\n"
            "Examples:\n"
            "🌡️ Lucknow ka temperature?\n"
            "🌦️ GKP ka weather?\n"
            "💧 Mumbai mein humidity kitni hai?\n"
            "🌱 Lucknow mein farming advice do."
        )

    return (
        "🤖 I can help you with real-time weather and agriculture. "
        "Try asking:\n\n"
        "🌡️ What's the temperature in Lucknow?\n"
        "🌦️ What's the weather in Delhi?\n"
        "💧 What's the humidity in Mumbai?\n"
        "🌱 Give me farming advice for Lucknow."
    )


# ============================================================
# HTML
# ============================================================

HTML = """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>WeatherGPT</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">

<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>

<style>
/* Leaflet layout safety fallback */
.leaflet-pane,
.leaflet-tile,
.leaflet-marker-icon,
.leaflet-marker-shadow,
.leaflet-marker-pane,
.leaflet-overlay-pane,
.leaflet-shadow-pane,
.leaflet-tooltip-pane,
.leaflet-popup-pane {
    position: absolute;
}
.leaflet-tile {
    width: 256px;
    height: 256px;
    user-select: none;
    -webkit-user-drag: none;
}
.leaflet-container {
    overflow: hidden;
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {

    min-height: 100vh;

    font-family: Arial, sans-serif;

    color: white;

    background: #071014;

    overflow-x: hidden;

    transition: background 1s ease;
}

body.theme-summer {
    background:
        radial-gradient(circle at 78% 12%, rgba(255,205,80,0.38), transparent 22%),
        linear-gradient(135deg, #193d54, #1c6680 52%, #0b3040);
}

body.theme-sunny {
    background:
        radial-gradient(circle at 82% 12%, rgba(255,225,120,0.28), transparent 20%),
        linear-gradient(135deg, #102b3a, #285b70 55%, #0c2635);
}

body.theme-cloudy {
    background:
        linear-gradient(135deg, #18242c, #263942 55%, #111b22);
}

body.theme-rain {
    background:
        radial-gradient(circle at 50% 0%, rgba(70,150,190,0.16), transparent 35%),
        linear-gradient(135deg, #08151d, #0e2c3a 55%, #071018);
}

body.theme-winter {
    background:
        radial-gradient(circle at 50% 0%, rgba(215,240,255,0.22), transparent 35%),
        linear-gradient(135deg, #10202d, #345064 55%, #101c27);
}


/* BACKGROUND */

.background {

    position: fixed;

    inset: 0;

    overflow: hidden;

    z-index: -1;
}


.cloud {

    position: absolute;

    width: 240px;

    height: 70px;

    background:
        rgba(255,255,255,0.10);

    border-radius: 100px;

    animation:
        moveCloud 45s linear infinite;
}


.cloud:before {

    content: "";

    position: absolute;

    width: 110px;

    height: 110px;

    left: 35px;

    top: -55px;

    background:
        rgba(255,255,255,0.10);

    border-radius: 50%;
}


.cloud:after {

    content: "";

    position: absolute;

    width: 130px;

    height: 130px;

    right: 25px;

    top: -70px;

    background:
        rgba(255,255,255,0.10);

    border-radius: 50%;
}


.cloud.one {

    top: 15%;

    left: -280px;
}


.cloud.two {

    top: 45%;

    left: -350px;

    transform: scale(0.7);

    animation-duration: 55s;
}


.cloud.three {

    top: 70%;

    left: -350px;

    transform: scale(0.5);

    animation-duration: 65s;
}


@keyframes moveCloud {

    from {
        transform: translateX(0);
    }

    to {
        transform: translateX(140vw);
    }
}


/* RAIN */

.rain {

    position: absolute;

    width: 2px;

    height: 60px;

    background:
        linear-gradient(
            transparent,
            rgba(180,230,255,0.7)
        );

    animation:
        fall linear infinite;
}


@keyframes fall {

    from {
        transform: translateY(-100px);
    }

    to {
        transform: translateY(110vh);
    }
}



/* DYNAMIC WEATHER BACKGROUND */

.background {
    pointer-events: none;
}

.sun-glow {
    position: absolute;
    width: 360px;
    height: 360px;
    right: -80px;
    top: -110px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(255,218,104,0.42), rgba(255,190,60,0.12) 38%, transparent 70%);
    opacity: 0;
    transition: opacity 1s ease;
}

.sun {
    position: absolute;
    width: 115px;
    height: 115px;
    right: 42px;
    top: 42px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #fff7c2, #ffd45a 55%, #ffad32);
    box-shadow: 0 0 70px rgba(255,210,90,0.35);
    opacity: 0;
    transition: opacity 1s ease;
}

.snow-layer {
    position: absolute;
    inset: 0;
    opacity: 0;
    background-image:
        radial-gradient(circle, rgba(255,255,255,0.85) 0 2px, transparent 2.5px),
        radial-gradient(circle, rgba(220,245,255,0.65) 0 1.5px, transparent 2px);
    background-size: 90px 90px, 55px 55px;
    animation: snowFall 8s linear infinite;
    transition: opacity 1s ease;
}

@keyframes snowFall {
    from { background-position: 0 -100px, 20px -60px; }
    to { background-position: 40px 110vh, -30px 110vh; }
}

body.theme-summer .sun,
body.theme-summer .sun-glow,
body.theme-sunny .sun,
body.theme-sunny .sun-glow {
    opacity: 1;
}

body.theme-rain .rain {
    opacity: 0.75;
}

body:not(.theme-rain) .rain {
    opacity: 0 !important;
}

body.theme-winter .snow-layer {
    opacity: 0.9;
}

body.theme-winter .cloud {
    opacity: 0.45;
}

/* HEADER */

header {
    position: sticky;
    top: 0;
    z-index: 20;
    padding: 16px 6%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: rgba(5, 8, 12, 0.78);
    backdrop-filter: blur(22px) saturate(140%);
    box-shadow: 0 10px 35px rgba(0,0,0,0.22);
}


.logo {

    font-size: 23px;

    font-weight: bold;
}


.status {

    color: #a5ffe9;

    font-size: 13px;
}


/* MAIN */

.container {

    width: min(950px, 94%);

    margin: 35px auto;
}


.hero {

    text-align: center;

    margin-bottom: 25px;
}


.hero h1 {

    font-size:
        clamp(32px, 6vw, 58px);

    margin-bottom: 10px;
}


.hero p {

    color:
        rgba(255,255,255,0.7);

    font-size: 16px;
}


/* CARD */

.card {

    background:
        rgba(255,255,255,0.10);

    border:
        1px solid rgba(255,255,255,0.20);

    backdrop-filter: blur(18px);

    border-radius: 28px;

    padding: 25px;

    box-shadow:
        0 25px 70px rgba(0,0,0,0.3);
}


/* CHAT */

.chat {

    height: 430px;

    overflow-y: auto;

    padding: 10px;
}


.message {

    display: flex;

    margin: 16px 0;
}


.message.ai {

    justify-content: flex-start;
}


.message.user {

    justify-content: flex-end;
}


.avatar {

    font-size: 25px;

    margin-right: 10px;
}


.bubble {

    max-width: 78%;

    padding: 15px 19px;

    border-radius: 20px;

    line-height: 1.55;

    white-space: pre-line;
}


.ai .bubble {

    background:
        rgba(0,180,190,0.25);

    border:
        1px solid rgba(100,240,255,0.2);
}


.user .bubble {

    background:
        rgba(255,255,255,0.17);
}


/* INPUT */

.input-row {

    display: flex;

    gap: 10px;

    margin-top: 18px;
}


input {

    flex: 1;

    min-width: 0;

    padding: 17px 20px;

    border-radius: 50px;

    border:
        1px solid rgba(255,255,255,0.2);

    background:
        rgba(0,0,0,0.20);

    color: white;

    font-size: 16px;

    outline: none;
}


input::placeholder {

    color:
        rgba(255,255,255,0.5);
}


button {

    border: none;

    cursor: pointer;

    transition: 0.2s;
}


button:hover {

    transform: scale(1.04);
}


.mic {

    width: 56px;

    height: 56px;

    border-radius: 50%;

    background: #50d5c7;

    font-size: 22px;
}


.send {

    width: 90px;

    border-radius: 50px;

    background: #50d5c7;

    font-size: 15px;

    font-weight: bold;
}


.listening {

    display: none;

    text-align: center;

    color: #a5ffe9;

    margin-top: 12px;
}



/* ROUTE WEATHER */

.route-card {
    margin-top: 22px;
    padding: 24px;
    border: 1px solid rgba(255,255,255,0.16);
    border-radius: 24px;
    background: rgba(255,255,255,0.075);
    backdrop-filter: blur(18px);
    box-shadow: 0 20px 55px rgba(0,0,0,0.25);
}

.route-head {
    display: flex;
    justify-content: space-between;
    gap: 18px;
    align-items: flex-start;
}

.route-kicker {
    color: #8fdff0;
    letter-spacing: 3px;
    font-size: 11px;
    margin-bottom: 8px;
}

.route-head h2 {
    font-size: 24px;
    margin-bottom: 7px;
}

.route-head p {
    color: rgba(255,255,255,0.62);
    line-height: 1.5;
    font-size: 14px;
    max-width: 650px;
}

.route-badge {
    border: 1px solid rgba(90,230,205,0.35);
    color: #78f1da;
    border-radius: 999px;
    padding: 7px 11px;
    font-size: 10px;
    letter-spacing: 1.5px;
    white-space: nowrap;
}

.route-form {
    display: flex;
    gap: 10px;
    margin-top: 18px;
}

.route-form input {
    flex: 1;
}

.route-btn {
    min-height: 54px;
    padding: 0 20px;
    border-radius: 50px;
    background: #50d5c7;
    color: #062021;
    font-weight: 700;
    font-size: 14px;
}

.route-note {
    margin-top: 10px;
    color: rgba(255,255,255,0.48);
    font-size: 12px;
}

.route-result {
    margin-top: 18px;
}

.route-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 15px;
}

.route-stat {
    flex: 1 1 150px;
    padding: 13px 15px;
    border-radius: 15px;
    background: rgba(0,0,0,0.18);
    border: 1px solid rgba(255,255,255,0.09);
}

.route-stat span {
    display: block;
    color: rgba(255,255,255,0.48);
    font-size: 10px;
    letter-spacing: 1.5px;
    margin-bottom: 5px;
}

.route-stat strong {
    font-size: 17px;
}

.route-provider-row {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:12px;
    flex-wrap:wrap;
    margin:10px 0 14px;
    color:rgba(255,255,255,0.55);
    font-size:11px;
}
.google-nav-btn {
    display:inline-flex;
    align-items:center;
    min-height:40px;
    padding:9px 14px;
    border-radius:12px;
    background:#4285f4;
    color:#fff;
    text-decoration:none;
    font-weight:700;
}
.google-nav-btn:hover { filter:brightness(1.08); }

.route-points {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
    gap: 9px;
}

.route-point {
    padding: 14px;
    border-radius: 17px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.09);
}

.route-point .point-name {
    color: #9dd9e8;
    font-size: 11px;
    margin-bottom: 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.route-point .point-temp {
    font-size: 25px;
    font-weight: 700;
}

.route-point .point-condition {
    color: rgba(255,255,255,0.7);
    font-size: 12px;
    line-height: 1.35;
    margin-top: 5px;
}

.route-point .point-meta {
    color: rgba(255,255,255,0.48);
    font-size: 11px;
    margin-top: 8px;
}

.route-error {
    padding: 13px 15px;
    border-radius: 14px;
    background: rgba(255,80,80,0.08);
    border: 1px solid rgba(255,120,120,0.15);
    color: #ffd2d2;
}

@media(max-width: 650px) {
    .route-head {
        flex-direction: column;
    }

    .route-form {
        flex-direction: column;
    }

    .route-btn {
        width: 100%;
    }
}

/* ROUTE MAP + TURN-BY-TURN NAVIGATION */
.route-map-wrap {
    margin-top: 18px;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 20px;
    overflow: hidden;
    background: rgba(0,0,0,0.18);
}

#routeMap {
    width: 100%;
    height: 470px;
    background: #0b141a;
    position: relative;
}
#routeMap .leaflet-tile {
    image-rendering: auto;
}
.map-loading {
    position:absolute;
    z-index:1000;
    left:50%;
    top:50%;
    transform:translate(-50%,-50%);
    padding:10px 14px;
    border-radius:999px;
    background:rgba(7,14,19,.88);
    border:1px solid rgba(255,255,255,.12);
    color:rgba(255,255,255,.82);
    font-size:12px;
    pointer-events:none;
    box-shadow:0 10px 30px rgba(0,0,0,.28);
}
.map-loading.hidden { display:none; }

.map-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
    padding: 11px 13px;
    background: rgba(4,10,14,0.72);
    color: rgba(255,255,255,0.68);
    font-size: 11px;
}

.legend-item { display:flex; align-items:center; gap:6px; }
.legend-dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.legend-rain { background:#4db8ff; }
.legend-sun { background:#ffd34e; }
.legend-cloud { background:#aab8c4; }
.legend-winter { background:#e9f7ff; }

.navigation-panel {
    margin-top: 14px;
    padding: 15px;
    border-radius: 18px;
    background: rgba(0,0,0,0.18);
    border: 1px solid rgba(255,255,255,0.09);
}

.nav-current {
    display:flex;
    align-items:center;
    gap:12px;
    margin-bottom: 12px;
}

.nav-arrow {
    width: 46px;
    height: 46px;
    border-radius: 14px;
    display:grid;
    place-items:center;
    background: rgba(80,213,199,0.16);
    border:1px solid rgba(80,213,199,0.25);
    font-size:22px;
}

.nav-current strong { display:block; font-size:16px; }
.nav-current span { display:block; color:rgba(255,255,255,0.5); font-size:11px; margin-top:3px; }

.nav-actions { display:flex; gap:9px; flex-wrap:wrap; }
.nav-start, .nav-stop {
    min-height:44px;
    padding:0 16px;
    border-radius:999px;
    font-weight:700;
}
.nav-start { background:#50d5c7; color:#062021; }
.nav-stop { background:rgba(255,255,255,0.10); color:white; border:1px solid rgba(255,255,255,0.14); }
.nav-start:disabled, .nav-stop:disabled { opacity:0.45; cursor:not-allowed; transform:none; }

.steps-list {
    margin-top: 12px;
    display:grid;
    gap:7px;
    max-height: 280px;
    overflow:auto;
}

.step-item {
    display:grid;
    grid-template-columns:34px 1fr auto;
    gap:10px;
    align-items:center;
    padding:10px;
    border-radius:13px;
    background:rgba(255,255,255,0.045);
    border:1px solid rgba(255,255,255,0.06);
}
.step-item.active {
    background:rgba(80,213,199,0.12);
    border-color:rgba(80,213,199,0.30);
}
.step-icon { font-size:18px; text-align:center; }
.step-text { font-size:12px; line-height:1.4; }
.step-distance { color:rgba(255,255,255,0.45); font-size:10px; white-space:nowrap; }

.leaflet-control-zoom a,
.leaflet-control-layers {
    background: rgba(10,14,19,0.92) !important;
    color: #eef4f8 !important;
    border-color: rgba(255,255,255,0.12) !important;
}
.leaflet-control-zoom a:hover {
    background: rgba(35,45,54,0.96) !important;
}
.leaflet-control-layers-expanded {
    padding: 8px 10px !important;
    border-radius: 12px !important;
    box-shadow: 0 12px 35px rgba(0,0,0,0.35) !important;
}
.leaflet-popup-content-wrapper,
.leaflet-popup-tip {
    background: #10161d;
    color: #eef4f8;
}
.leaflet-popup-content {
    line-height: 1.55;
}
.leaflet-control-attribution { font-size:9px !important; }

@media(max-width: 650px) {
    #routeMap { height: 360px; }
    .step-item { grid-template-columns:30px 1fr; }
    .step-distance { grid-column:2; }
}

/* QUICK BUTTONS */

.quick {

    display: flex;

    flex-wrap: wrap;

    gap: 10px;

    margin-top: 18px;
}


.quick button {

    padding: 10px 15px;

    border-radius: 30px;

    background:
        rgba(255,255,255,0.13);

    color: white;
}


.footer {

    text-align: center;

    margin-top: 20px;

    color:
        rgba(255,255,255,0.45);

    font-size: 13px;
}


@media(max-width: 650px) {

    .input-row {

        flex-wrap: wrap;
    }

    input {

        flex-basis: 100%;
    }

    .send {

        flex: 1;
    }

}


/* ============================================================
   PROFESSIONAL UI POLISH
   ============================================================ */
:root {
    --ui-bg: #07131a;
    --ui-panel: rgba(10, 24, 31, 0.86);
    --ui-panel-2: rgba(14, 31, 39, 0.92);
    --ui-border: rgba(147, 231, 218, 0.14);
    --ui-muted: #91a7b1;
    --ui-text: #f4fbfd;
    --ui-accent: #54dec5;
    --ui-accent-2: #43b7ff;
}

body {
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--ui-bg);
    color: var(--ui-text);
}

body::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    background:
        radial-gradient(circle at 8% 0%, rgba(84,222,197,.10), transparent 28%),
        radial-gradient(circle at 92% 20%, rgba(67,183,255,.08), transparent 26%);
    z-index: -2;
}

header {
    padding: 14px clamp(18px, 5vw, 72px);
    background: rgba(5, 15, 20, .88);
    border-bottom: 1px solid rgba(255,255,255,.07);
}
.logo {
    letter-spacing: -.4px;
    font-weight: 800;
}
.status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 7px 11px;
    border: 1px solid rgba(84,222,197,.20);
    border-radius: 999px;
    background: rgba(84,222,197,.07);
    color: #a9f8e9;
}
.status::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--ui-accent);
    box-shadow: 0 0 12px rgba(84,222,197,.8);
}
.container {
    width: min(1120px, 92%);
    margin: 42px auto 55px;
}
.hero {
    text-align: left;
    padding: 18px 4px 25px;
}
.hero h1 {
    max-width: 760px;
    font-size: clamp(38px, 6vw, 68px);
    line-height: .98;
    letter-spacing: -2.8px;
    background: linear-gradient(100deg, #fff, #a9f8e9 48%, #8ad2ff);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}
.hero p {
    max-width: 700px;
    margin-top: 14px;
    color: #91a7b1;
    font-size: 16px;
}
.card, .route-card {
    background: linear-gradient(145deg, rgba(17,37,46,.94), rgba(7,20,27,.94));
    border: 1px solid var(--ui-border);
    box-shadow: 0 24px 80px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.025);
}
.card { padding: 26px; border-radius: 24px; }
.chat { height: 430px; padding: 12px 8px; scrollbar-width: thin; scrollbar-color: rgba(84,222,197,.35) transparent; }
.message { margin: 13px 0; }
.bubble { border-radius: 16px; padding: 13px 16px; box-shadow: 0 10px 24px rgba(0,0,0,.12); }
.ai .bubble { background: rgba(31, 111, 115, .22); border-color: rgba(84,222,197,.15); }
.user .bubble { background: rgba(255,255,255,.065); border: 1px solid rgba(255,255,255,.07); }
input {
    border-radius: 14px;
    background: rgba(3,12,17,.72);
    border: 1px solid rgba(255,255,255,.10);
}
input:focus {
    border-color: rgba(84,222,197,.48);
    box-shadow: 0 0 0 4px rgba(84,222,197,.07);
}
.mic, .send, .route-btn, .nav-start {
    background: linear-gradient(135deg, #62ead2, #39b9a7);
    box-shadow: 0 10px 25px rgba(54,190,168,.18);
}
.mic { width: 54px; height: 54px; }
.send { width: 92px; }
.quick button {
    background: rgba(255,255,255,.045);
    border: 1px solid rgba(255,255,255,.08);
    color: #cbd9de;
}
.quick button:hover {
    background: rgba(84,222,197,.09);
    border-color: rgba(84,222,197,.24);
}
.route-card { margin-top: 24px; padding: 27px; border-radius: 24px; }
.route-kicker { color: #61e1cb; font-weight: 700; }
.route-badge { background: rgba(84,222,197,.06); }
.route-stat, .route-point, .navigation-panel {
    background: rgba(255,255,255,.035);
    border-color: rgba(255,255,255,.075);
}
.route-stat strong { color: #f3fbfd; }
.google-nav-btn {
    background: rgba(67,183,255,.12);
    border: 1px solid rgba(67,183,255,.24);
    color: #9edcff;
}
.route-map-wrap {
    position: relative;
    margin-top: 20px;
    border: 1px solid rgba(84,222,197,.15);
    border-radius: 22px;
    box-shadow: 0 22px 55px rgba(0,0,0,.30);
}
#routeMap {
    height: 500px;
    min-height: 360px;
}
.map-legend {
    background: rgba(5,13,18,.92);
    border-top: 1px solid rgba(255,255,255,.07);
    padding: 12px 15px;
}
.map-mode-badge {
    margin: 12px;
    padding: 8px 11px;
    display: flex;
    align-items: center;
    gap: 8px;
    border-radius: 999px;
    color: #eafcff;
    background: rgba(5,15,20,.84);
    border: 1px solid rgba(255,255,255,.13);
    backdrop-filter: blur(12px);
    box-shadow: 0 8px 24px rgba(0,0,0,.25);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.2px;
}
.map-live-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: #54dec5; box-shadow: 0 0 10px #54dec5;
}
.custom-map-marker-wrap { background: transparent !important; border: 0 !important; }
.custom-map-marker {
    width: 38px; height: 38px; border-radius: 50%;
    display: grid; place-items: center;
    color: white; font-size: 14px;
    border: 3px solid rgba(255,255,255,.92);
    box-shadow: 0 6px 20px rgba(0,0,0,.45);
}
.custom-map-marker.current { background: #14b89b; }
.custom-map-marker.destination { background: #e45757; }
.weather-popup .popup-temp { font-size: 22px; font-weight: 800; margin-top: 5px; }
.weather-popup .popup-meta { opacity: .68; margin-top: 4px; font-size: 11px; }
.leaflet-container { font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #09151b; }
.leaflet-control-layers { min-width: 145px; }
.leaflet-control-layers label { margin: 4px 0; }
.leaflet-popup-content-wrapper { border-radius: 14px; box-shadow: 0 12px 30px rgba(0,0,0,.38); }
.leaflet-popup-tip { box-shadow: 2px 2px 5px rgba(0,0,0,.18); }

@media(max-width: 650px) {
    .container { width: 94%; margin-top: 25px; }
    .hero { text-align: left; }
    .hero h1 { letter-spacing: -1.6px; }
    .card, .route-card { padding: 18px; border-radius: 19px; }
    #routeMap { height: 390px; min-height: 320px; }
}

/* ============================================================
   WEATHERGPT DASHBOARD UI
   Inspired by the supplied reference: editorial hero,
   live-weather panel, metric strip and forecast rail.
   Existing chat + route functionality remains below.
   ============================================================ */

.dashboard-shell {
    width: min(1240px, 94%);
    margin: 28px auto 64px;
}

.topbar-actions {
    display: flex;
    align-items: center;
    gap: 10px;
}

.location-btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-height: 38px;
    padding: 0 13px;
    border-radius: 999px;
    color: #d9f8f2;
    background: rgba(255,255,255,.045);
    border: 1px solid rgba(255,255,255,.10);
    font-size: 12px;
}

.location-btn:hover {
    background: rgba(84,222,197,.10);
    border-color: rgba(84,222,197,.25);
}

.dashboard-hero {
    display: grid;
    grid-template-columns: minmax(0, 1.18fr) minmax(360px, .82fr);
    gap: 22px;
    align-items: stretch;
}

.hero-copy {
    padding: 34px 8px 30px 4px;
}

.hero-eyebrow {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: #78e5d1;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 2.4px;
    margin-bottom: 15px;
}

.hero-eyebrow::before {
    content: "";
    width: 24px;
    height: 1px;
    background: #54dec5;
}

.dashboard-hero h1 {
    max-width: 760px;
    margin: 0;
    font-size: clamp(48px, 7vw, 82px);
    line-height: .91;
    letter-spacing: -4.5px;
    font-weight: 850;
    color: #f7fbfc;
}

.dashboard-hero h1 span {
    color: #62ddcb;
}

.hero-copy p {
    max-width: 610px;
    margin: 19px 0 22px;
    color: #8fa5ae;
    font-size: 15px;
    line-height: 1.6;
}

.explore-bar {
    display: flex;
    align-items: center;
    gap: 9px;
    max-width: 610px;
    padding: 7px;
    border: 1px solid rgba(255,255,255,.12);
    border-radius: 17px;
    background: rgba(7,20,27,.76);
    box-shadow: 0 16px 40px rgba(0,0,0,.22);
}

.explore-bar .search-icon {
    width: 42px;
    display: grid;
    place-items: center;
    color: #7c9ba5;
    font-size: 15px;
}

#dashboardCity {
    flex: 1;
    min-width: 0;
    padding: 12px 3px;
    border: 0;
    outline: 0;
    background: transparent;
    box-shadow: none;
    border-radius: 0;
    font-size: 14px;
}

#dashboardCity:focus {
    box-shadow: none;
    border: 0;
}

.explore-btn {
    min-height: 43px;
    padding: 0 20px;
    border-radius: 12px;
    color: #09201f;
    background: linear-gradient(135deg, #f5e5a8, #d9c778);
    font-weight: 800;
    font-size: 12px;
    box-shadow: 0 8px 25px rgba(221,199,120,.15);
}

.explore-btn:hover {
    filter: brightness(1.05);
}

.hero-links {
    display: flex;
    gap: 18px;
    flex-wrap: wrap;
    margin-top: 14px;
    color: #708993;
    font-size: 11px;
}

.hero-links span {
    cursor: pointer;
}

.hero-links span:hover {
    color: #c7e8e2;
}

.current-weather-card {
    position: relative;
    min-height: 330px;
    padding: 25px;
    overflow: hidden;
    border-radius: 26px;
    border: 1px solid rgba(255,255,255,.12);
    background:
        radial-gradient(circle at 75% 24%, rgba(55,128,255,.30), transparent 38%),
        linear-gradient(145deg, rgba(27,53,74,.96), rgba(11,22,34,.98));
    box-shadow: 0 26px 70px rgba(0,0,0,.32);
}

.current-weather-card::after {
    content: "";
    position: absolute;
    width: 260px;
    height: 260px;
    right: -120px;
    bottom: -120px;
    border-radius: 50%;
    background: rgba(49,126,255,.10);
    filter: blur(3px);
}

.current-top {
    position: relative;
    z-index: 1;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
}

.current-place {
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.4px;
    color: #d7e5ea;
    text-transform: uppercase;
}

.current-time {
    color: #78909b;
    font-size: 10px;
}

.weather-main {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: 20px;
    margin-top: 35px;
}

.weather-icon-large {
    width: 96px;
    height: 96px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    background: rgba(255,199,75,.11);
    box-shadow: 0 0 60px rgba(255,194,63,.12);
    font-size: 54px;
}

.weather-temp {
    font-size: 62px;
    line-height: 1;
    font-weight: 800;
    letter-spacing: -3px;
}

.weather-condition {
    margin-top: 8px;
    color: #a9bbc2;
    font-size: 13px;
}

.weather-feels {
    margin-top: 4px;
    color: #6f8791;
    font-size: 11px;
}

.weather-meta {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 34px;
}

.weather-meta-item {
    padding-top: 12px;
    border-top: 1px solid rgba(255,255,255,.10);
}

.weather-meta-item small {
    display: block;
    color: #6f8791;
    font-size: 9px;
    margin-bottom: 5px;
}

.weather-meta-item strong {
    font-size: 13px;
    color: #e9f5f7;
}

.metric-strip {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    margin-top: 14px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,.10);
    border-radius: 19px;
    background: rgba(7,19,26,.76);
    box-shadow: 0 18px 50px rgba(0,0,0,.20);
}

.metric-item {
    padding: 17px 19px;
    border-right: 1px solid rgba(255,255,255,.08);
}

.metric-item:last-child {
    border-right: 0;
}

.metric-label {
    display: flex;
    align-items: center;
    gap: 7px;
    color: #718891;
    font-size: 9px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
}

.metric-value {
    margin-top: 8px;
    color: #e8f4f5;
    font-size: 17px;
    font-weight: 750;
}

.metric-line {
    height: 2px;
    margin-top: 10px;
    border-radius: 99px;
    background: linear-gradient(90deg, rgba(84,222,197,.65), rgba(84,222,197,.05));
}

.forecast-section {
    margin-top: 32px;
}

.section-title-row {
    display: flex;
    justify-content: space-between;
    align-items: end;
    gap: 15px;
    margin-bottom: 13px;
}

.section-kicker {
    color: #54dec5;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 2px;
}

.section-title {
    margin-top: 5px;
    color: #eef8fa;
    font-size: 23px;
    letter-spacing: -.6px;
}

.section-note {
    color: #637b85;
    font-size: 10px;
}

.forecast-rail {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 10px;
}

.forecast-card {
    padding: 16px;
    border-radius: 16px;
    border: 1px solid rgba(255,255,255,.08);
    background: rgba(13,30,38,.78);
}

.forecast-card.active {
    border-color: rgba(84,222,197,.30);
    background: linear-gradient(145deg, rgba(24,64,67,.72), rgba(11,28,35,.84));
}

.forecast-time {
    color: #728993;
    font-size: 10px;
}

.forecast-icon {
    margin: 15px 0 10px;
    font-size: 24px;
}

.forecast-temp {
    color: #edf8fa;
    font-size: 19px;
    font-weight: 800;
}

.forecast-meta {
    margin-top: 5px;
    color: #758c95;
    font-size: 9px;
    line-height: 1.5;
}

.weathergpt-section {
    margin-top: 34px;
}

.weathergpt-label {
    margin-bottom: 11px;
    color: #69828c;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 2px;
}

.dashboard-chat-card {
    margin-top: 0 !important;
}

@media(max-width: 900px) {
    .dashboard-hero {
        grid-template-columns: 1fr;
    }
    .current-weather-card {
        min-height: 290px;
    }
    .metric-strip {
        grid-template-columns: repeat(2, 1fr);
    }
    .metric-item:nth-child(2) {
        border-right: 0;
    }
    .metric-item:nth-child(-n+2) {
        border-bottom: 1px solid rgba(255,255,255,.08);
    }
    .forecast-rail {
        grid-template-columns: repeat(3, 1fr);
    }
}

@media(max-width: 650px) {
    .dashboard-shell {
        width: 94%;
        margin-top: 18px;
    }
    .hero-copy {
        padding: 18px 2px;
    }
    .dashboard-hero h1 {
        font-size: clamp(43px, 14vw, 62px);
        letter-spacing: -3px;
    }
    .explore-bar {
        align-items: stretch;
    }
    .explore-btn {
        padding: 0 14px;
    }
    .weather-main {
        margin-top: 25px;
    }
    .weather-icon-large {
        width: 74px;
        height: 74px;
        font-size: 40px;
    }
    .weather-temp {
        font-size: 48px;
    }
    .metric-strip {
        grid-template-columns: 1fr 1fr;
    }
    .forecast-rail {
        grid-template-columns: repeat(2, 1fr);
    }
}

/* =========================================================
   SMART GEAR + WEATHER HEALTH
   ========================================================= */

.smart-tools-card,
.health-card {
    margin-top: 24px;
    border: 1px solid rgba(100, 220, 205, 0.16);
    background:
        linear-gradient(145deg, rgba(13, 30, 38, 0.96), rgba(7, 18, 24, 0.98));
    border-radius: 26px;
    padding: 26px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.22);
}

.smart-tools-head,
.health-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 18px;
    margin-bottom: 20px;
}

.smart-kicker,
.health-kicker {
    color: #57dbc8;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin-bottom: 7px;
}

.smart-tools-head h2,
.health-head h2 {
    font-size: 25px;
    line-height: 1.15;
    margin-bottom: 7px;
}

.smart-tools-head p,
.health-head p {
    color: #91a8b2;
    line-height: 1.6;
    font-size: 14px;
    max-width: 760px;
}

.smart-status,
.health-status {
    border: 1px solid rgba(82,224,196,0.2);
    background: rgba(82,224,196,0.08);
    color: #9cecdf;
    border-radius: 999px;
    padding: 9px 13px;
    font-size: 11px;
    font-weight: 800;
    white-space: nowrap;
}

.gear-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
}

.gear-card {
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(17, 34, 42, 0.82);
    border-radius: 20px;
    padding: 18px;
    min-height: 165px;
    transition: transform .2s ease, border-color .2s ease, background .2s ease;
}

.gear-card:hover {
    transform: translateY(-3px);
    border-color: rgba(82,224,196,0.28);
    background: rgba(21, 42, 51, 0.92);
}

.gear-icon {
    font-size: 31px;
    margin-bottom: 13px;
}

.gear-card h3 {
    font-size: 17px;
    margin-bottom: 7px;
}

.gear-card p {
    color: #91a8b2;
    line-height: 1.45;
    font-size: 13px;
}

.gear-tag {
    display: inline-flex;
    margin-top: 13px;
    border-radius: 999px;
    padding: 5px 9px;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .06em;
    text-transform: uppercase;
    background: rgba(82,224,196,.09);
    color: #7fe4d3;
}

.gear-card.hidden {
    display: none;
}

.no-gear {
    border: 1px dashed rgba(255,255,255,.11);
    border-radius: 18px;
    padding: 18px;
    color: #9db0b8;
    background: rgba(255,255,255,.025);
}

.health-grid {
    display: grid;
    grid-template-columns: 0.95fr 1.35fr;
    gap: 18px;
}

.health-profile,
.health-report {
    border: 1px solid rgba(255,255,255,.07);
    border-radius: 20px;
    padding: 18px;
    background: rgba(4, 15, 20, .55);
}

.health-profile h3,
.health-report h3 {
    font-size: 16px;
    margin-bottom: 15px;
}

.health-form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 11px;
}

.health-field label {
    display: block;
    color: #8fa5ae;
    font-size: 11px;
    font-weight: 700;
    margin-bottom: 6px;
}

.health-field input,
.health-field select {
    width: 100%;
    border: 1px solid rgba(255,255,255,.09);
    background: #09171d;
    color: #edf7f8;
    border-radius: 12px;
    padding: 11px 12px;
    outline: none;
}

.health-field input:focus,
.health-field select:focus {
    border-color: rgba(82,224,196,.5);
    box-shadow: 0 0 0 3px rgba(82,224,196,.08);
}

.condition-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 13px 0;
}

.condition-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 10px;
    border: 1px solid rgba(255,255,255,.08);
    background: rgba(255,255,255,.025);
    color: #aabdc4;
    border-radius: 999px;
    font-size: 11px;
    cursor: pointer;
}

.condition-chip input {
    accent-color: #52e0c4;
}

.health-save {
    width: 100%;
    border: 0;
    border-radius: 12px;
    padding: 12px 14px;
    font-weight: 800;
    cursor: pointer;
    background: linear-gradient(135deg, #53dfc6, #35bfae);
    color: #061417;
}

.health-disclaimer {
    color: #718790;
    font-size: 10px;
    line-height: 1.5;
    margin-top: 11px;
}

.risk-banner {
    border-radius: 16px;
    padding: 14px;
    margin-bottom: 13px;
    border: 1px solid rgba(82,224,196,.18);
    background: rgba(82,224,196,.07);
}

.risk-banner.caution {
    border-color: rgba(255,198,91,.24);
    background: rgba(255,198,91,.07);
}

.risk-banner.high {
    border-color: rgba(255,106,106,.28);
    background: rgba(255,106,106,.07);
}

.risk-label {
    font-size: 10px;
    font-weight: 900;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: #7fe4d3;
}

.risk-banner.caution .risk-label {
    color: #ffd77d;
}

.risk-banner.high .risk-label {
    color: #ff9b9b;
}

.risk-banner strong {
    display: block;
    font-size: 18px;
    margin: 4px 0;
}

.risk-banner span {
    color: #9bb0b8;
    font-size: 12px;
    line-height: 1.5;
}

.health-alert-list {
    display: grid;
    gap: 9px;
    margin-bottom: 14px;
}

.health-alert {
    border-left: 3px solid #52e0c4;
    padding: 10px 12px;
    background: rgba(255,255,255,.025);
    border-radius: 0 12px 12px 0;
}

.health-alert.warning {
    border-left-color: #ffc65b;
}

.health-alert.danger {
    border-left-color: #ff7272;
}

.health-alert strong {
    display: block;
    margin-bottom: 3px;
    font-size: 13px;
}

.health-alert span {
    color: #94a9b1;
    font-size: 12px;
    line-height: 1.45;
}

.health-history {
    margin-top: 15px;
    border-top: 1px solid rgba(255,255,255,.07);
    padding-top: 14px;
}

.health-history-title {
    color: #8fa5ae;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .12em;
    text-transform: uppercase;
    margin-bottom: 9px;
}

.health-history-list {
    display: grid;
    gap: 7px;
}

.health-history-item {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 10px;
    border-radius: 10px;
    background: rgba(255,255,255,.025);
    color: #a7b8be;
    font-size: 11px;
}

.health-history-item strong {
    color: #edf7f8;
}


.product-card{overflow:hidden;border:1px solid rgba(255,255,255,.08);background:rgba(8,19,25,.82);border-radius:20px;min-height:300px;display:flex;flex-direction:column}
.product-image-wrap{height:150px;background:#f4f6f6;display:flex;align-items:center;justify-content:center;overflow:hidden}
.product-image-wrap img{width:100%;height:100%;object-fit:contain;display:block}
.product-body{padding:15px;display:flex;flex-direction:column;flex:1}.product-type{color:#6fe2d0;font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.product-name{font-size:15px;font-weight:800;line-height:1.3;margin:6px 0 8px}.product-price{font-size:20px;font-weight:900;color:#f4fbfb}.product-meta{color:#8399a2;font-size:10px;margin-top:3px}.product-buy{display:block;text-align:center;margin-top:auto;padding:10px 12px;border-radius:11px;text-decoration:none;font-weight:900;font-size:12px;background:linear-gradient(135deg,#53dfc6,#35bfae);color:#061417}.product-buy:hover{filter:brightness(1.06)}
.health-mini-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}.health-mini{padding:12px;border-radius:14px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.06)}.health-mini-label{color:#8198a1;font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.health-mini-value{display:block;font-size:16px;font-weight:900;margin-top:4px}.health-mini-note{color:#8399a2;font-size:10px;margin-top:3px}.symptom-title{color:#8fa5ae;font-size:11px;font-weight:800;margin-top:15px;margin-bottom:8px}.symptom-grid{display:flex;flex-wrap:wrap;gap:7px}.symptom-chip{padding:7px 9px;border-radius:999px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.025);color:#aabdc4;font-size:10px;cursor:pointer}.symptom-chip input{accent-color:#ff7272}.health-emergency{display:none;margin-bottom:12px;padding:12px 13px;border-radius:14px;background:rgba(255,77,77,.10);border:1px solid rgba(255,100,100,.28)}.health-emergency.show{display:block}.health-emergency strong{display:block;color:#ff9a9a;margin-bottom:3px}.health-emergency span{color:#d7b4b4;font-size:11px;line-height:1.45}.health-privacy{margin-top:10px;color:#637b84;font-size:9px;line-height:1.45}
@media (max-width:820px){.health-mini-grid{grid-template-columns:1fr}.product-image-wrap{height:135px}}

@media (max-width: 820px) {
    .gear-grid,
    .health-grid {
        grid-template-columns: 1fr;
    }

    .health-form-grid {
        grid-template-columns: 1fr;
    }

    .smart-tools-head,
    .health-head {
        flex-direction: column;
    }
}

/* =========================================================
   PROFILE + EMERGENCY CONTACTS + WEATHER SAFETY MODE
   ========================================================= */
.profile-section-title{
    margin:18px 0 10px;
    color:#8fa5ae;
    font-size:10px;
    font-weight:900;
    letter-spacing:.12em;
    text-transform:uppercase;
}
.emergency-contacts{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:10px;
    margin-top:12px;
}
.contact-card{
    border:1px solid rgba(255,255,255,.07);
    background:rgba(255,255,255,.025);
    border-radius:14px;
    padding:12px;
}
.contact-card label{
    display:block;
    color:#8fa5ae;
    font-size:10px;
    font-weight:800;
    margin-bottom:5px;
}
.contact-card input{
    width:100%;
    box-sizing:border-box;
    border:1px solid rgba(255,255,255,.08);
    background:#09171d;
    color:#edf7f8;
    border-radius:10px;
    padding:9px 10px;
    outline:none;
    margin-bottom:7px;
}
.call-btn{
    display:flex;
    width:100%;
    justify-content:center;
    align-items:center;
    gap:6px;
    text-decoration:none;
    border-radius:10px;
    padding:9px 10px;
    background:rgba(82,224,196,.09);
    color:#7fe4d3;
    font-size:11px;
    font-weight:900;
}
.call-btn.disabled{
    opacity:.4;
    pointer-events:none;
}
.safety-card{
    margin-top:24px;
    border:1px solid rgba(255,91,91,.20);
    background:linear-gradient(145deg,rgba(35,15,19,.96),rgba(12,18,23,.98));
    border-radius:26px;
    padding:24px;
    box-shadow:0 20px 60px rgba(0,0,0,.22);
}
.safety-head{
    display:flex;
    justify-content:space-between;
    gap:16px;
    align-items:flex-start;
    margin-bottom:16px;
}
.safety-kicker{
    color:#ff9292;
    font-size:10px;
    font-weight:900;
    letter-spacing:.16em;
}
.safety-head h2{
    margin:6px 0;
    font-size:24px;
}
.safety-head p{
    color:#a9b8bd;
    font-size:13px;
    line-height:1.55;
    max-width:760px;
}
.safety-badge{
    display:none;
    border:1px solid rgba(255,91,91,.3);
    background:rgba(255,91,91,.08);
    color:#ffaaaa;
    border-radius:999px;
    padding:8px 12px;
    font-size:10px;
    font-weight:900;
    white-space:nowrap;
}
.safety-badge.show{display:block}
.safety-actions{
    display:flex;
    flex-wrap:wrap;
    gap:10px;
    margin-bottom:15px;
}
.safe-route-btn{
    border:0;
    border-radius:12px;
    padding:11px 15px;
    font-weight:900;
    cursor:pointer;
    background:linear-gradient(135deg,#ff7d7d,#e74f63);
    color:#fff;
}
.safe-route-btn:disabled{
    opacity:.5;
    cursor:not-allowed;
}
.safe-note{
    color:#7f959d;
    font-size:10px;
    line-height:1.5;
}
.safe-place-list{
    display:grid;
    gap:9px;
    margin-top:13px;
}
.safe-place{
    display:grid;
    grid-template-columns:auto 1fr auto;
    gap:12px;
    align-items:center;
    border:1px solid rgba(255,255,255,.07);
    background:rgba(255,255,255,.025);
    border-radius:14px;
    padding:12px;
}
.safe-place-icon{font-size:22px}
.safe-place strong{display:block;font-size:13px}
.safe-place span{display:block;color:#8fa5ae;font-size:10px;margin-top:3px}
.safe-place button{
    border:1px solid rgba(82,224,196,.18);
    background:rgba(82,224,196,.07);
    color:#83e4d4;
    border-radius:9px;
    padding:8px 10px;
    cursor:pointer;
    font-size:10px;
    font-weight:900;
}
.safety-map-wrap{
    margin-top:14px;
    border-radius:18px;
    overflow:hidden;
    border:1px solid rgba(255,255,255,.08);
}
#safetyMap{
    height:360px;
    width:100%;
}
.safety-disclaimer{
    margin-top:12px;
    color:#71868e;
    font-size:10px;
    line-height:1.55;
}
@media(max-width:700px){
    .emergency-contacts{grid-template-columns:1fr}
    .safety-head{flex-direction:column}
    .safe-place{grid-template-columns:auto 1fr}
    .safe-place button{grid-column:2}
}

</style>

</head>


<body>


<div class="background" id="weatherBackground" aria-hidden="true">
    <div class="sun-glow"></div>
    <div class="sun"></div>

    <div class="cloud one"></div>
    <div class="cloud two"></div>
    <div class="cloud three"></div>

    <div class="snow-layer"></div>
</div>


<header>

    <div class="logo">
        🌾 WeatherGPT
    </div>

    <div class="topbar-actions">
        <button id="useLocationBtn" class="location-btn" type="button">
            ⌖ Use my location
        </button>
        <div class="status">
            ● AI Online
        </div>
    </div>

</header>


<div class="dashboard-shell">

    <section class="dashboard-hero">

        <div class="hero-copy">
            <div class="hero-eyebrow">WEATHER INTELLIGENCE</div>

            <h1>Read the sky.<br><span>Before it changes.</span></h1>

            <p>
                A cinematic weather assistant that turns live conditions
                into practical decisions — for travel, farming and everyday planning.
            </p>

            <div class="explore-bar">
                <div class="search-icon">⌕</div>
                <input id="dashboardCity" type="text"
                       placeholder="Search a city — e.g. Lucknow"
                       autocomplete="off">
                <button id="dashboardExplore" class="explore-btn" type="button">
                    Explore
                </button>
            </div>

            <div class="hero-links">
                <span onclick="dashboardQuick('Lucknow')">Current weather</span>
                <span onclick="dashboardQuick('Delhi')">Rain forecast</span>
                <span onclick="dashboardQuick('Mumbai')">Travel weather</span>
            </div>
        </div>

        <aside class="current-weather-card">
            <div class="current-top">
                <div id="currentPlace" class="current-place">CURRENT LOCATION</div>
                <div id="currentTime" class="current-time">Live</div>
            </div>

            <div class="weather-main">
                <div id="currentWeatherIcon" class="weather-icon-large">☀️</div>
                <div>
                    <div id="currentTemp" class="weather-temp">--°</div>
                    <div id="currentCondition" class="weather-condition">Getting your weather…</div>
                    <div id="currentFeels" class="weather-feels">Feels like --°</div>
                </div>
            </div>

            <div class="weather-meta">
                <div class="weather-meta-item">
                    <small>HUMIDITY</small>
                    <strong id="currentHumidity">--%</strong>
                </div>
                <div class="weather-meta-item">
                    <small>WIND</small>
                    <strong id="currentWind">-- km/h</strong>
                </div>
                <div class="weather-meta-item">
                    <small>PRESSURE</small>
                    <strong id="currentPressure">-- hPa</strong>
                </div>
            </div>
        </aside>

    </section>

    <section class="metric-strip">
        <div class="metric-item">
            <div class="metric-label">◌ Humidity</div>
            <div id="metricHumidity" class="metric-value">--%</div>
            <div class="metric-line"></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">◒ Wind</div>
            <div id="metricWind" class="metric-value">-- km/h</div>
            <div class="metric-line"></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">● Rain chance</div>
            <div id="metricRain" class="metric-value">--%</div>
            <div class="metric-line"></div>
        </div>
        <div class="metric-item">
            <div class="metric-label">⌁ Pressure</div>
            <div id="metricPressure" class="metric-value">-- hPa</div>
            <div class="metric-line"></div>
        </div>
    </section>

    <section class="forecast-section">
        <div class="section-title-row">
            <div>
                <div class="section-kicker">WEATHER SNAPSHOT</div>
                <div class="section-title">The conditions, decoded.</div>
            </div>
            <div class="section-note">Live data • updates with location</div>
        </div>

        <div class="forecast-rail">
            <div class="forecast-card active">
                <div class="forecast-time">NOW</div>
                <div id="forecastIcon1" class="forecast-icon">☀️</div>
                <div id="forecastTemp1" class="forecast-temp">--°</div>
                <div id="forecastMeta1" class="forecast-meta">Waiting for location</div>
            </div>
            <div class="forecast-card">
                <div class="forecast-time">FEELS LIKE</div>
                <div class="forecast-icon">🌡️</div>
                <div id="forecastTemp2" class="forecast-temp">--°</div>
                <div class="forecast-meta">Apparent temperature</div>
            </div>
            <div class="forecast-card">
                <div class="forecast-time">RAIN</div>
                <div class="forecast-icon">🌧️</div>
                <div id="forecastTemp3" class="forecast-temp">--%</div>
                <div class="forecast-meta">Current precipitation probability</div>
            </div>
            <div class="forecast-card">
                <div class="forecast-time">WIND</div>
                <div class="forecast-icon">💨</div>
                <div id="forecastTemp4" class="forecast-temp">--</div>
                <div class="forecast-meta">Kilometres per hour</div>
            </div>
            <div class="forecast-card">
                <div class="forecast-time">FARMING</div>
                <div class="forecast-icon">🌱</div>
                <div id="forecastTemp5" class="forecast-temp">Ready</div>
                <div class="forecast-meta">Ask WeatherGPT for crop advice</div>
            </div>
        </div>
    </section>

    <section class="weathergpt-section">
        <div class="weathergpt-label">ASK WEATHERGPT</div>

        <div class="card dashboard-chat-card">


        <div class="chat" id="chat">

            <div class="message ai">

                <div class="avatar">
                    🤖
                </div>

                <div class="bubble">
Hello! 👋

I am WeatherGPT — your AI weather, travel and wellness assistant.

Ask me:
🌡️ Temperature
🌧️ Rainfall
💧 Humidity
💨 Wind
🌱 Farming advice
                    </div>

            </div>

        </div>


        <div class="input-row">

            <input
                id="message"
                type="text"
                placeholder="Ask me about weather..."
                autocomplete="off"
            >

            <button
                id="mic"
                class="mic"
                title="Speak"
            >
                🎤
            </button>

            <button
                id="send"
                class="send"
            >
                Send
            </button>

        </div>


        <div
            id="listening"
            class="listening"
        >
            🎙️ Listening...
        </div>


        <div class="quick">

            <button onclick="quickAsk('What is the temperature in Lucknow?')">
                🌡️ Lucknow
            </button>

            <button onclick="quickAsk('What is the weather in Delhi?')">
                🌦️ Delhi
            </button>

            <button onclick="quickAsk('What is the rainfall in Mumbai?')">
                🌧️ Mumbai
            </button>

            <button onclick="quickAsk('What is the humidity in Lucknow?')">
                💧 Humidity
            </button>

            <button onclick="quickAsk('Give me farming advice for Lucknow')">
                🌱 Farming
            </button>

        </div>


    </div>



    <div class="route-card">
        <div class="route-head">
            <div>
                <div class="route-kicker">TRAVEL WEATHER</div>
                <h2>🧭 Weather on My Route</h2>
                <p>Set a destination and see how the weather changes from your current location to the destination.</p>
            </div>
            <div class="route-badge">LIVE</div>
        </div>

        <div class="route-form">
            <input id="destination" type="text" placeholder="Enter destination — e.g. Lucknow, Delhi" autocomplete="off">
            <button id="routeButton" class="route-btn" type="button">Check Route Weather</button>
        </div>

        <div class="route-note" id="routeNote">
            📍 Your browser location is used as the starting point. Location permission may be required.
        </div>

        <div id="routeResult" class="route-result" aria-live="polite"></div>

        <div id="routeMapWrap" class="route-map-wrap" hidden>
            <div id="routeMap" aria-label="Road route map"></div>
            <div class="map-legend">
                <span class="legend-item"><i class="legend-dot legend-rain"></i> Rain / Storm</span>
                <span class="legend-item"><i class="legend-dot legend-sun"></i> Sunny / Hot</span>
                <span class="legend-item"><i class="legend-dot legend-cloud"></i> Cloudy</span>
                <span class="legend-item"><i class="legend-dot legend-winter"></i> Cold / Winter</span>
            </div>
        </div>

        <div id="navigationPanel" class="navigation-panel" hidden aria-live="polite">
            <div class="nav-current">
                <div id="navArrow" class="nav-arrow">🧭</div>
                <div>
                    <strong id="navInstruction">Ready for navigation</strong>
                    <span id="navSubtext">Press Start Navigation to begin turn-by-turn guidance.</span>
                </div>
            </div>
            <div class="nav-actions">
                <button id="startNav" class="nav-start" type="button">▶ Start Navigation</button>
                <button id="stopNav" class="nav-stop" type="button" disabled>■ Stop</button>
            </div>
            <div id="stepsList" class="steps-list"></div>
        </div>
    </div>

    <!-- =====================================================
         WEATHER EMERGENCY / SAFE PLACE NAVIGATION
         ===================================================== -->
    <div class="safety-card" id="safetyCard">
        <div class="safety-head">
            <div>
                <div class="safety-kicker">WEATHER SAFETY MODE</div>
                <h2>🚨 Emergency Weather Route</h2>
                <p id="safetySummary">
                    WeatherGPT checks your current weather conditions and can show nearby safer public places on the map when severe weather risk is detected.
                </p>
            </div>
            <div id="safetyBadge" class="safety-badge">SAFETY MODE</div>
        </div>

        <div class="safety-actions">
            <button id="findSafePlaceBtn" class="safe-route-btn" type="button">
                🛡️ Find Safe Place & Navigate
            </button>
            <a id="emergencyFamilyCall" class="call-btn disabled" href="#">📞 Family</a>
            <a id="emergencyHospitalCall" class="call-btn disabled" href="#">🏥 Hospital</a>
        </div>

        <div id="safePlaceList" class="safe-place-list"></div>

        <div class="safety-map-wrap">
            <div id="safetyMap" aria-label="Emergency safe place map"></div>
        </div>

        <div class="safety-disclaimer">
            Safety Mode is a weather-risk aid, not an official flood warning service. If local authorities issue an evacuation order, follow it immediately. The app does not guarantee that a selected place is flood-safe.
        </div>
    </div>

    <!-- =====================================================
         SMART WEATHER GEAR
         ===================================================== -->
    <div class="smart-tools-card" id="smartToolsCard">
        <div class="smart-tools-head">
            <div>
                <div class="smart-kicker">SMART WEATHER ACTION</div>
                <h2>☔ What should you carry?</h2>
                <p id="smartWeatherSummary">
                    WeatherGPT will suggest useful gear when rain, cold, heat or storms are detected.
                </p>
            </div>
            <div class="smart-status" id="smartGearStatus">WAITING FOR WEATHER</div>
        </div>

        <div id="gearGrid" class="gear-grid">
            <div class="no-gear" id="noGearMessage">
                Check your current location or a route to get personalized weather-based suggestions.
            </div>
        </div>
        <div style="margin-top:14px;display:flex;align-items:center;gap:9px;flex-wrap:wrap;">
            <span class="gear-tag">BUDGET PICKS</span>
            <span style="color:#7f959d;font-size:11px;">Affordable options selected for rain-ready travel.</span>
        </div>
        <div id="productGrid" class="gear-grid" style="margin-top:10px;"></div>
        <div class="health-privacy">Product images, names and prices are from checked retailer listings; prices and availability can change. Product suggestions are optional. “View & Buy” opens the retailer page.</div>
    </div>

    <!-- =====================================================
         WEATHER HEALTH REPORT
         ===================================================== -->
    <div class="health-card" id="healthCard">
        <div class="health-head">
            <div>
                <div class="health-kicker">WEATHER × WELLNESS</div>
                <h2>🩺 <span id="healthReportTitle">Personal Health Weather Report</span></h2>
                <p>
                    A weather-based safety advisory using your optional profile.
                    It does not diagnose illness or replace a doctor.
                </p>
            </div>
            <div class="health-status" id="healthStatus">PROFILE NOT SET</div>
        </div>

        <div class="health-grid">
            <div class="health-profile">
                <h3>Your optional profile</h3>

                <div class="health-form-grid">
                    <div class="health-field" style="grid-column:1/-1;">
                        <label for="healthName">Your name</label>
                        <input id="healthName" type="text" maxlength="80" placeholder="e.g. Akhil">
                    </div>
                    <div class="health-field"><label for="healthAge">Age</label><input id="healthAge" type="number" min="1" max="120" placeholder="e.g. 22"></div>
                    <div class="health-field"><label for="healthActivity">Outdoor activity</label><select id="healthActivity"><option value="low">Mostly indoors</option><option value="moderate">Moderate</option><option value="high">Frequent outdoor activity / gym</option></select></div>
                    <div class="health-field"><label for="healthHeight">Height (cm)</label><input id="healthHeight" type="number" min="50" max="250" placeholder="e.g. 180"></div>
                    <div class="health-field"><label for="healthWeight">Weight (kg)</label><input id="healthWeight" type="number" min="10" max="300" step="0.1" placeholder="e.g. 70"></div>
                </div>

                <div class="condition-list">
                    <label class="condition-chip"><input type="checkbox" value="asthma" class="health-condition"> Asthma</label>
                    <label class="condition-chip"><input type="checkbox" value="heart" class="health-condition"> Heart condition</label>
                    <label class="condition-chip"><input type="checkbox" value="hypertension" class="health-condition"> High BP</label>
                    <label class="condition-chip"><input type="checkbox" value="diabetes" class="health-condition"> Diabetes</label>
                    <label class="condition-chip"><input type="checkbox" value="heat_sensitive" class="health-condition"> Heat sensitive</label>
                    <label class="condition-chip"><input type="checkbox" value="cold_sensitive" class="health-condition"> Cold sensitive</label>
                    <label class="condition-chip"><input type="checkbox" value="heat_medication" class="health-condition"> Heat-sensitive medicines</label>
                </div>

                <div class="symptom-title">Optional: how are you feeling right now?</div>
                <div class="symptom-grid">
                    <label class="symptom-chip"><input type="checkbox" value="headache" class="health-symptom"> Headache</label>
                    <label class="symptom-chip"><input type="checkbox" value="dizziness" class="health-symptom"> Dizziness</label>
                    <label class="symptom-chip"><input type="checkbox" value="nausea" class="health-symptom"> Nausea</label>
                    <label class="symptom-chip"><input type="checkbox" value="weakness" class="health-symptom"> Weakness</label>
                    <label class="symptom-chip"><input type="checkbox" value="breathing" class="health-symptom"> Breathing difficulty</label>
                    <label class="symptom-chip"><input type="checkbox" value="chest_pain" class="health-symptom"> Chest pain</label>
                    <label class="symptom-chip"><input type="checkbox" value="fainting" class="health-symptom"> Fainting / confusion</label>
                </div>

                <div class="profile-section-title">Emergency contacts</div>
                <div class="emergency-contacts">
                    <div class="contact-card">
                        <label for="familyContactName">Family member</label>
                        <input id="familyContactName" type="text" maxlength="60" placeholder="Name">
                        <input id="familyContactPhone" type="tel" maxlength="20" placeholder="Phone number">
                        <a id="familyCallBtn" class="call-btn disabled" href="#">📞 One-tap family call</a>
                    </div>
                    <div class="contact-card">
                        <label for="hospitalContactName">Hospital / emergency</label>
                        <input id="hospitalContactName" type="text" maxlength="60" placeholder="Hospital / contact">
                        <input id="hospitalContactPhone" type="tel" maxlength="20" placeholder="Phone number">
                        <a id="hospitalCallBtn" class="call-btn disabled" href="#">🏥 One-tap hospital call</a>
                    </div>
                </div>

                <button class="health-save" id="saveHealthProfile" type="button">
                    Save My Health Preferences
                </button>

                <div class="health-disclaimer">
                    Only store information you are comfortable keeping in this browser.
                    WeatherGPT uses it for advisory personalization and does not send this
                    profile to the weather API.
                </div>
            </div>

            <div class="health-report">
                <h3>Early-warning report</h3>

                <div id="healthEmergency" class="health-emergency"><strong>🚨 Immediate attention may be needed</strong><span id="healthEmergencyText">Some reported symptoms can be serious. Seek urgent medical care rather than relying on this app.</span></div>

                <div class="health-mini-grid">
                    <div class="health-mini"><div class="health-mini-label">BMI</div><span class="health-mini-value" id="healthBMI">—</span><div class="health-mini-note" id="healthBMInote">Optional screening</div></div>
                    <div class="health-mini"><div class="health-mini-label">UV index</div><span class="health-mini-value" id="healthUV">—</span><div class="health-mini-note" id="healthUVnote">Sun exposure</div></div>
                    <div class="health-mini"><div class="health-mini-label">Air quality</div><span class="health-mini-value" id="healthAQI">—</span><div class="health-mini-note" id="healthAQInote">Current AQI</div></div>
                </div>

                <div id="healthRiskBanner" class="risk-banner">
                    <div class="risk-label">STATUS</div>
                    <strong id="healthRiskTitle">Waiting for weather</strong>
                    <span id="healthRiskText">
                        Check your current location or route weather to generate an advisory.
                    </span>
                </div>

                <div id="healthAlertList" class="health-alert-list"></div>

                <div class="health-history">
                    <div class="health-history-title">Recent weather-health checks</div>
                    <div id="healthHistoryList" class="health-history-list">
                        <div class="health-history-item">
                            <span>No checks saved yet.</span>
                            <strong>—</strong>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="footer">
        AI-Based Agriculture Weather Prediction System
    </div>


</div>

</div>


<script>


// ==========================================================
// ELEMENTS
// ==========================================================

const input =
    document.getElementById("message");

const sendButton =
    document.getElementById("send");

const micButton =
    document.getElementById("mic");

const chat =
    document.getElementById("chat");

const listening =
    document.getElementById("listening");


// ==========================================================
// ADD MESSAGE
// ==========================================================

function addMessage(text, type) {

    const message =
        document.createElement("div");

    message.className =
        "message " + type;


    if (type === "ai") {

        const avatar =
            document.createElement("div");

        avatar.className =
            "avatar";

        avatar.textContent =
            "🤖";


        const bubble =
            document.createElement("div");

        bubble.className =
            "bubble";

        bubble.textContent =
            text;


        message.appendChild(avatar);

        message.appendChild(bubble);

    } else {

        const bubble =
            document.createElement("div");

        bubble.className =
            "bubble";

        bubble.textContent =
            text;

        message.appendChild(bubble);
    }


    chat.appendChild(message);

    chat.scrollTop =
        chat.scrollHeight;
}


// ==========================================================
// SEND MESSAGE TO PYTHON
// ==========================================================

async function sendMessage() {

    const message =
        input.value.trim();


    if (!message) {
        return;
    }


    addMessage(
        message,
        "user"
    );


    input.value = "";


    addMessage(
        "Thinking... 🤔",
        "ai"
    );


    try {

        const response =
            await fetch(
                "/chat",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        message: message
                    })
                }
            );


        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(data.reply || data.error || "Server error");
        }


        // Remove thinking

        const aiMessages =
            document.querySelectorAll(
                ".message.ai"
            );


        if (aiMessages.length > 0) {

            const last =
                aiMessages[
                    aiMessages.length - 1
                ];


            if (
                last.innerText.includes(
                    "Thinking"
                )
            ) {

                last.remove();
            }
        }


        addMessage(
            data.reply,
            "ai"
        );


        speak(
            data.reply
        );


    } catch (error) {

        addMessage(
            "⚠️ Server error. Please try again.",
            "ai"
        );
    }
}


// ==========================================================
// TEXT TO SPEECH
// ==========================================================

function speak(text) {

    if (
        !("speechSynthesis" in window)
    ) {
        return;
    }


    window.speechSynthesis.cancel();


    const speech =
        new SpeechSynthesisUtterance(text);


    speech.lang =
        "en-IN";

    speech.rate =
        0.95;

    speech.pitch =
        1;


    window.speechSynthesis.speak(
        speech
    );
}


// ==========================================================
// VOICE RECOGNITION
// ==========================================================

const SpeechRecognition =
    window.SpeechRecognition ||
    window.webkitSpeechRecognition;


if (SpeechRecognition) {

    const recognition =
        new SpeechRecognition();


    recognition.lang =
        "en-IN";

    recognition.continuous =
        false;

    recognition.interimResults =
        false;


    micButton.addEventListener(
        "click",
        function () {

            try {

                recognition.start();

            } catch (error) {

                console.log(error);
            }

        }
    );


    recognition.onstart =
        function () {

            listening.style.display =
                "block";

            micButton.style.transform =
                "scale(1.1)";
        };


    recognition.onresult =
        function (event) {

            const text =
                event.results[0][0].transcript;


            input.value =
                text;


            listening.style.display =
                "none";


            micButton.style.transform =
                "scale(1)";


            sendMessage();
        };


    recognition.onerror =
        function (event) {

            console.log(
                "Voice error:",
                event.error
            );

            listening.style.display =
                "none";

            micButton.style.transform =
                "scale(1)";
        };


    recognition.onend =
        function () {

            listening.style.display =
                "none";

            micButton.style.transform =
                "scale(1)";
        };

}


// ==========================================================
// ENTER KEY
// ==========================================================

input.addEventListener(
    "keydown",
    function(event) {

        if (event.key === "Enter") {

            sendMessage();
        }

    }
);


// ==========================================================
// QUICK QUESTIONS
// ==========================================================

function quickAsk(question) {

    input.value =
        question;

    sendMessage();
}


// ==========================================================
// ===========================================================
// DYNAMIC WEATHER BACKGROUND
// ===========================================================

/* ==========================================================
   WEATHERGPT DASHBOARD
   ========================================================== */

const dashboardCity = document.getElementById("dashboardCity");
const dashboardExplore = document.getElementById("dashboardExplore");
const useLocationBtn = document.getElementById("useLocationBtn");

function dashboardWeatherIcon(code, theme) {
    if ([95, 96, 99].includes(Number(code))) return "⛈️";
    if ([51,53,55,61,63,65,80,81,82].includes(Number(code))) return "🌧️";
    if ([71,73,75].includes(Number(code))) return "❄️";
    if ([45,48].includes(Number(code))) return "🌫️";
    if (theme === "summer" || theme === "sunny") return "☀️";
    if (theme === "cloudy") return "☁️";
    return "🌤️";
}

function updateDashboardWeather(weather, placeLabel) {
    if (!weather) return;

    const icon = dashboardWeatherIcon(weather.weather_code, weather.theme);
    const city = placeLabel || weather.label || "Current Location";

    document.getElementById("currentPlace").textContent = String(city).toUpperCase();
    document.getElementById("currentTemp").textContent =
        `${Math.round(Number(weather.temperature ?? 0))}°`;
    document.getElementById("currentCondition").textContent =
        String(weather.condition || "Weather available");
    document.getElementById("currentFeels").textContent =
        `Feels like ${Math.round(Number(weather.feels_like ?? 0))}°`;

    document.getElementById("currentHumidity").textContent =
        `${weather.humidity ?? "--"}%`;
    document.getElementById("currentWind").textContent =
        `${weather.wind ?? "--"} km/h`;
    document.getElementById("currentPressure").textContent =
        `${weather.pressure ?? "--"} hPa`;

    document.getElementById("metricHumidity").textContent =
        `${weather.humidity ?? "--"}%`;
    document.getElementById("metricWind").textContent =
        `${weather.wind ?? "--"} km/h`;
    document.getElementById("metricRain").textContent =
        `${weather.rain_probability ?? "--"}%`;
    document.getElementById("metricPressure").textContent =
        `${weather.pressure ?? "--"} hPa`;

    document.getElementById("forecastIcon1").textContent = icon;
    document.getElementById("forecastTemp1").textContent =
        `${Math.round(Number(weather.temperature ?? 0))}°`;
    document.getElementById("forecastMeta1").textContent =
        String(weather.condition || "Live weather");

    document.getElementById("forecastTemp2").textContent =
        `${Math.round(Number(weather.feels_like ?? 0))}°`;
    document.getElementById("forecastTemp3").textContent =
        `${weather.rain_probability ?? "--"}%`;
    document.getElementById("forecastTemp4").textContent =
        `${weather.wind ?? "--"}`;

    document.getElementById("currentTime").textContent =
        new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});

    updateWeatherBackground(weather);
}

async function loadDashboardCity(city) {
    const clean = String(city || "").trim();
    if (!clean) return;

    dashboardExplore.disabled = true;
    dashboardExplore.textContent = "Loading…";

    try {
        input.value = `What's the weather in ${clean}?`;
        await sendMessage();
    } catch (error) {
        console.error("Dashboard weather error:", error);
    } finally {
        dashboardExplore.disabled = false;
        dashboardExplore.textContent = "Explore";
    }
}

function dashboardQuick(city) {
    dashboardCity.value = city;
    loadDashboardCity(city);
}

dashboardExplore.addEventListener("click", () => {
    loadDashboardCity(dashboardCity.value);
});

dashboardCity.addEventListener("keydown", event => {
    if (event.key === "Enter") loadDashboardCity(dashboardCity.value);
});

if (useLocationBtn) {
    useLocationBtn.addEventListener("click", () => {
        loadCurrentLocationWeather(true);
    });
}


const background = document.querySelector(".background");
const destinationInput = document.getElementById("destination");
const routeButton = document.getElementById("routeButton");
const routeResult = document.getElementById("routeResult");
const routeNote = document.getElementById("routeNote");

let currentLocation = null;
let routeData = null;
let routeMap = null;
let routeLayers = [];
let userMarker = null;
let navigationWatchId = null;
let activeStepIndex = 0;
let lastRerouteAt = 0;
let rerouting = false;


// ==========================================================
// WEATHER EMERGENCY / SAFE PLACE NAVIGATION
// ==========================================================

let safetyMap = null;
let safetyLayer = null;
let safetyLocationMarker = null;
let safetyAccuracyCircle = null;
let safetyLocationWatchId = null;
let safetyPlaces = [];

// Google-Maps-style live blue location dot for the Safety Map.
function updateSafetyLiveLocation(lat, lon, accuracy = null) {
    if (!safetyMap || typeof L === "undefined") return;

    const latLng = [lat, lon];

    if (!safetyLocationMarker) {
        // Outer soft blue pulse/accuracy circle.
        safetyAccuracyCircle = L.circle(latLng, {
            radius: Math.max(Number(accuracy) || 25, 20),
            color: "#4285F4",
            weight: 1,
            opacity: 0.28,
            fillColor: "#4285F4",
            fillOpacity: 0.12,
            interactive: false
        }).addTo(safetyMap);

        // Main blue GPS dot with white border.
        safetyLocationMarker = L.circleMarker(latLng, {
            radius: 9,
            color: "#ffffff",
            weight: 3,
            opacity: 1,
            fillColor: "#4285F4",
            fillOpacity: 1
        }).addTo(safetyMap).bindPopup(
            "📍 <b>Your live location</b><br><span style='opacity:.72'>GPS position</span>"
        );
    } else {
        safetyLocationMarker.setLatLng(latLng);

        if (safetyAccuracyCircle) {
            safetyAccuracyCircle.setLatLng(latLng);
            safetyAccuracyCircle.setRadius(Math.max(Number(accuracy) || 25, 20));
        }
    }
}

function startSafetyLiveLocation() {
    if (!navigator.geolocation) return;

    // Get the position immediately, then keep updating it while Safety Mode is open.
    navigator.geolocation.getCurrentPosition(
        position => {
            const lat = position.coords.latitude;
            const lon = position.coords.longitude;
            currentLocation = {
                latitude: lat,
                longitude: lon
            };

            updateSafetyLiveLocation(lat, lon, position.coords.accuracy);

            if (safetyMap) {
                safetyMap.setView([lat, lon], Math.max(safetyMap.getZoom() || 14, 14));
            }
        },
        error => console.warn("Safety live location:", error),
        {
            enableHighAccuracy: true,
            maximumAge: 5000,
            timeout: 15000
        }
    );

    if (safetyLocationWatchId !== null) return;

    safetyLocationWatchId = navigator.geolocation.watchPosition(
        position => {
            const lat = position.coords.latitude;
            const lon = position.coords.longitude;

            currentLocation = {
                latitude: lat,
                longitude: lon
            };

            updateSafetyLiveLocation(lat, lon, position.coords.accuracy);
        },
        error => console.warn("Safety live location watch:", error),
        {
            enableHighAccuracy: true,
            maximumAge: 5000,
            timeout: 15000
        }
    );
}

function isSevereWeatherRisk(weather) {
    if (!weather) return false;

    const code = Number(weather.weather_code);
    const rain = Number(weather.rainfall || 0);
    const probability = Number(weather.rain_probability || 0);

    // This is intentionally a WEATHER-RISK proxy, not an official flood forecast.
    return (
        [65, 82, 95, 96, 99].includes(code) ||
        rain >= 15 ||
        probability >= 80
    );
}

function setSafetyMode(weather) {
    if (!weather) return;

    const badge = document.getElementById("safetyBadge");
    const summary = document.getElementById("safetySummary");

    if (isSevereWeatherRisk(weather)) {
        badge?.classList.add("show");
        if (summary) {
            summary.textContent =
                "Severe rain/storm conditions or a high rain probability were detected. Check nearby safer public places and follow official local emergency instructions.";
        }
    } else {
        badge?.classList.remove("show");
        if (summary) {
            summary.textContent =
                "No severe weather trigger is currently detected. You can still manually check nearby hospitals, police stations and other public places.";
        }
    }
}

function initSafetyMap() {
    if (typeof L === "undefined") return;

    // The safety card exists on page load, so Leaflet must get a real
    // center/zoom and the map must be invalidated after the card is visible.
    if (!safetyMap) {
        safetyMap = L.map("safetyMap", {
            zoomControl: true,
            zoomSnap: 1,
            zoomDelta: 1,
            wheelPxPerZoomLevel: 100,
            preferCanvas: false,
            attributionControl: true,
            fadeAnimation: false,
            zoomAnimation: false,
            markerZoomAnimation: false
        });

        // Same high-quality satellite imagery used by the main Route Weather map.
        const satellite = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: 18,
                maxNativeZoom: 18,
                tileSize: 256,
                keepBuffer: 6,
                updateWhenIdle: true,
                updateWhenZooming: true,
                updateInterval: 150,
                crossOrigin: true,
                noWrap: true,
                attribution: "Tiles © Esri"
            }
        ).addTo(safetyMap);

        // Street layer remains available from the layer switcher.
        const streets = L.tileLayer(
            "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            {
                maxZoom: 19,
                keepBuffer: 3,
                attribution: "© OpenStreetMap contributors"
            }
        );

        // Optional Esri place labels, matching the main route map.
        const labels = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: 19,
                maxNativeZoom: 18,
                opacity: 0.9,
                keepBuffer: 3,
                attribution: "Esri reference"
            }
        );

        L.control.layers(
            { "Satellite": satellite, "Street": streets },
            { "Place labels": labels },
            { position: "topright", collapsed: false }
        ).addTo(safetyMap);

        // If imagery is blocked/rate-limited, automatically fall back to streets.
        let satelliteErrors = 0;
        satellite.on("tileerror", () => {
            satelliteErrors += 1;
            if (satelliteErrors >= 8 && !safetyMap.hasLayer(streets)) {
                streets.addTo(safetyMap);
            }
        });

        // Satellite loading badge.
        const loadingEl = L.DomUtil.create("div", "map-loading");
        loadingEl.textContent = "🛰️ Loading satellite imagery…";
        document.getElementById("safetyMap").appendChild(loadingEl);

        let loadedTiles = 0;
        const hideMapLoading = () => {
            loadedTiles += 1;
            if (loadedTiles >= 2) loadingEl.classList.add("hidden");
        };
        satellite.on("tileload", hideMapLoading);
        streets.on("tileload", hideMapLoading);
        satellite.on("load", () => loadingEl.classList.add("hidden"));

        const mapBadge = L.control({ position: "topleft" });
        mapBadge.onAdd = function() {
            const div = L.DomUtil.create("div", "map-mode-badge");
            div.innerHTML = '<span class="map-live-dot"></span><span>LIVE SAFETY • SATELLITE</span>';
            L.DomEvent.disableClickPropagation(div);
            return div;
        };
        mapBadge.addTo(safetyMap);

        // IMPORTANT: unlike the old version, always give the map a valid
        // initial center. Without this Leaflet can render controls but no tiles.
        if (currentLocation) {
            safetyMap.setView(
                [currentLocation.latitude, currentLocation.longitude],
                14
            );
        } else {
            // India fallback until browser GPS is available.
            safetyMap.setView([26.8467, 80.9462], 5);
        }
    }

    // Start the live blue GPS dot on the Safety Map.
    startSafetyLiveLocation();

    // The safety card may have been rendered after the map was created.
    // Force Leaflet to recalculate the container dimensions and request tiles.
    safetyMap.invalidateSize(true);

    if (currentLocation) {
        safetyMap.setView(
            [currentLocation.latitude, currentLocation.longitude],
            Math.max(safetyMap.getZoom() || 14, 12)
        );
    }

    setTimeout(() => {
        if (safetyMap) safetyMap.invalidateSize(true);
    }, 250);

    setTimeout(() => {
        if (safetyMap) safetyMap.invalidateSize(true);
    }, 900);
}

function renderSafetyPlaces(places) {
    const list = document.getElementById("safePlaceList");
    if (!list) return;

    if (!places.length) {
        list.innerHTML = `
            <div class="safe-note">
                No nearby public safety places were found. Follow local authority guidance and use your saved emergency contacts.
            </div>
        `;
        return;
    }

    list.innerHTML = places.map((place, index) => `
        <div class="safe-place">
            <div class="safe-place-icon">${place.icon || "🛡️"}</div>
            <div>
                <strong>${escapeHTML(place.name)}</strong>
                <span>${escapeHTML(place.type)} • approximately ${escapeHTML(String(place.distance_km))} km away</span>
            </div>
            <button type="button" onclick="navigateToSafePlace(${index})">Navigate</button>
        </div>
    `).join("");
}

function renderSafetyMap(places) {
    initSafetyMap();
    if (!safetyMap || !currentLocation) return;

    // Recalculate Leaflet's size before drawing markers/tiles.
    safetyMap.invalidateSize(true);
    safetyMap.setView(
        [currentLocation.latitude, currentLocation.longitude],
        14
    );

    if (safetyLayer) safetyMap.removeLayer(safetyLayer);
    safetyLayer = L.layerGroup().addTo(safetyMap);

    // Keep the live blue GPS dot separate from the safe-place marker layer.
    // It must stay visible even when safe-place markers are refreshed.
    updateSafetyLiveLocation(
        currentLocation.latitude,
        currentLocation.longitude,
        currentLocation.accuracy || 25
    );

    const bounds = [
        [currentLocation.latitude, currentLocation.longitude]
    ];

    places.forEach(place => {
        const marker = L.marker([place.latitude, place.longitude], {
            title: place.name
        }).addTo(safetyLayer);

        marker.bindPopup(`
            <strong>${escapeHTML(place.name)}</strong><br>
            ${escapeHTML(place.type)}<br>
            ${escapeHTML(String(place.distance_km))} km away
        `);

        bounds.push([place.latitude, place.longitude]);
    });

    safetyMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });

    // Recenter the live dot after fitting all safe places without losing the
    // selected safety-place overview.
    updateSafetyLiveLocation(
        currentLocation.latitude,
        currentLocation.longitude,
        currentLocation.accuracy || 25
    );
}

async function findSafePlaces() {
    if (!currentLocation) {
        await loadCurrentLocationWeather(true);
    }

    if (!currentLocation) {
        alert("Please allow location access first.");
        return;
    }

    const btn = document.getElementById("findSafePlaceBtn");
    if (btn) {
        btn.disabled = true;
        btn.textContent = "Finding nearby safe places…";
    }

    try {
        const response = await fetch("/safe-places", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({
                latitude: currentLocation.latitude,
                longitude: currentLocation.longitude
            })
        });

        const data = await response.json();

        if (!data.ok) {
            document.getElementById("safePlaceList").innerHTML =
                `<div class="safe-note">⚠️ ${escapeHTML(data.error || "Could not find safe places.")}</div>`;
            return;
        }

        safetyPlaces = data.places || [];
        renderSafetyPlaces(safetyPlaces);
        renderSafetyMap(safetyPlaces);

        if (safetyPlaces.length) {
            const first = safetyPlaces[0];
            const summary = document.getElementById("safetySummary");
            if (summary) {
                summary.textContent =
                    `Nearest public safety option: ${first.name} (${first.distance_km} km). Use Navigate to start the route.`;
            }
        }
    } catch (error) {
        console.error("Safe places:", error);
        document.getElementById("safePlaceList").innerHTML =
            `<div class="safe-note">⚠️ Safe-place service is temporarily unavailable.</div>`;
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = "🛡️ Find Safe Place & Navigate";
        }
    }
}

async function navigateToSafePlace(index) {
    const place = safetyPlaces[index];
    if (!place || !currentLocation) return;

    // First show the target on the safety map.
    initSafetyMap();
    if (safetyMap) {
        safetyMap.setView([place.latitude, place.longitude], 15);
    }

    // Reuse the app's existing route-weather/navigation system.
    const destinationInput = document.getElementById("destination");
    if (destinationInput) {
        destinationInput.value = place.name;
    }

    // The app's normal route flow will calculate the road route.
    const routeButton = document.getElementById("routeButton");
    if (routeButton) {
        routeButton.click();
    }

    // Also open a Google Maps driving route in a new tab as a live-navigation fallback.
    // This does not require a Google Maps API key.
    const googleUrl =
        `https://www.google.com/maps/dir/?api=1&origin=${currentLocation.latitude},${currentLocation.longitude}` +
        `&destination=${place.latitude},${place.longitude}&travelmode=driving`;

    window.open(googleUrl, "_blank", "noopener,noreferrer");
}

// ==========================================================
// SMART GEAR + HEALTH REPORT
// ==========================================================

let latestWeatherForHealth = null;


function updateHealthReportTitle(profile = getSavedHealthProfile()) {
    const el = document.getElementById("healthReportTitle");
    if (el) {
        el.textContent = profile.name
            ? `${profile.name}'s Health Weather Report`
            : "Personal Health Weather Report";
    }
}

function loadHealthProfile() {
    try {
        const saved = JSON.parse(localStorage.getItem("weatherGPTHealthProfile") || "null");
        if (!saved) return null;

        if (saved.name) document.getElementById("healthName").value = saved.name;
        if (saved.age) document.getElementById("healthAge").value = saved.age;
        if (saved.activity) document.getElementById("healthActivity").value = saved.activity;

        if (saved.familyName) document.getElementById("familyContactName").value = saved.familyName;
        if (saved.familyPhone) document.getElementById("familyContactPhone").value = saved.familyPhone;
        if (saved.hospitalName) document.getElementById("hospitalContactName").value = saved.hospitalName;
        if (saved.hospitalPhone) document.getElementById("hospitalContactPhone").value = saved.hospitalPhone;
        updateEmergencyCallLinks(saved);
        if (saved.height) document.getElementById("healthHeight").value = saved.height;
        if (saved.weight) document.getElementById("healthWeight").value = saved.weight;

        document.querySelectorAll(".health-condition").forEach(box => {
            box.checked = (saved.conditions || []).includes(box.value);
        });
        document.querySelectorAll(".health-symptom").forEach(box => {
            box.checked = (saved.symptoms || []).includes(box.value);
        });

        document.getElementById("healthStatus").textContent = "PROFILE ACTIVE";
        updateHealthReportTitle(saved);
        return saved;
    } catch (error) {
        console.warn("Health profile load error:", error);
        return null;
    }
}


function normalizePhone(phone) {
    return String(phone || "").replace(/[^\d+]/g, "");
}

function updateEmergencyCallLinks(profile = getSavedHealthProfile()) {
    const familyPhone = normalizePhone(profile.familyPhone);
    const hospitalPhone = normalizePhone(profile.hospitalPhone);

    const familyButtons = [
        document.getElementById("familyCallBtn"),
        document.getElementById("emergencyFamilyCall")
    ];
    const hospitalButtons = [
        document.getElementById("hospitalCallBtn"),
        document.getElementById("emergencyHospitalCall")
    ];

    familyButtons.forEach(btn => {
        if (!btn) return;
        if (familyPhone) {
            btn.href = `tel:${familyPhone}`;
            btn.classList.remove("disabled");
        } else {
            btn.href = "#";
            btn.classList.add("disabled");
        }
    });

    hospitalButtons.forEach(btn => {
        if (!btn) return;
        if (hospitalPhone) {
            btn.href = `tel:${hospitalPhone}`;
            btn.classList.remove("disabled");
        } else {
            btn.href = "#";
            btn.classList.add("disabled");
        }
    });
}

function saveHealthProfile() {
    const profile = {
        name: document.getElementById("healthName").value.trim(),
        age: document.getElementById("healthAge").value || "",
        activity: document.getElementById("healthActivity").value || "low",
        familyName: document.getElementById("familyContactName").value.trim(),
        familyPhone: document.getElementById("familyContactPhone").value.trim(),
        hospitalName: document.getElementById("hospitalContactName").value.trim(),
        hospitalPhone: document.getElementById("hospitalContactPhone").value.trim(),
        height: document.getElementById("healthHeight").value || "",
        weight: document.getElementById("healthWeight").value || "",
        conditions: Array.from(document.querySelectorAll(".health-condition:checked")).map(box => box.value),
        symptoms: Array.from(document.querySelectorAll(".health-symptom:checked")).map(box => box.value)
    };

    localStorage.setItem("weatherGPTHealthProfile", JSON.stringify(profile));
    document.getElementById("healthStatus").textContent = "PROFILE ACTIVE";
    updateHealthReportTitle(profile);
    updateEmergencyCallLinks(profile);

    if (latestWeatherForHealth) {
        updateHealthReport(latestWeatherForHealth);
    }
}

function weatherIsRainy(weather) {
    if (!weather) return false;

    const code = Number(weather.weather_code);
    const rainCodes = [51,53,55,61,63,65,80,81,82,95,96,99];

    return (
        rainCodes.includes(code) ||
        Number(weather.rainfall || 0) > 0 ||
        Number(weather.rain_probability || 0) >= 40
    );
}

function weatherIsStormy(weather) {
    const code = Number(weather?.weather_code);
    return [95,96,99].includes(code);
}

function productCatalog() {
    return {
        // Budget-friendly products checked from current Decathlon India listings.
        umbrella:{
            type:"BUDGET RAIN ESSENTIAL",
            name:"Decathlon Waterproof Umbrella 110cm",
            price:"₹499",
            meta:"Decathlon • budget pick • price checked Sep 2026",
            image:"https://contents.mediadecathlon.com/p1909027/536877eb5270d256040d449bd389b4c0/p1909027.jpg?f=1200x0&format=auto&quality=70",
            url:"https://www.decathlon.in/p/8519377/waterproof-umbrella-small-110cm-coverage-upf50-sun-protection-auto-open-red"
        },
        raincoat:{
            type:"BUDGET WATERPROOF",
            name:"QUECHUA Raincoat Lightweight Waterproof Jacket",
            price:"₹699",
            meta:"Decathlon • budget pick • price checked Sep 2026",
            image:"https://contents.mediadecathlon.com/p2644239/a8f5d5a6f3139d101ace0f0deaacca1c/p2644239.jpg?f=1200x0&format=auto&quality=70",
            url:"https://www.decathlon.in/p/8862313/men-half-zip-compact-rain-jacket-with-pouch-black"
        },
        poncho:{
            type:"BUDGET RAIN PROTECTION",
            name:"QUECHUA Lightweight Waterproof Rain Poncho",
            price:"₹799",
            meta:"Decathlon • budget pick • price checked Sep 2026",
            image:"https://contents.mediadecathlon.com/p2393309/966d9acbce8e559977cf3c42ce6b3177/p2393309.jpg?f=1200x0&format=auto&quality=70",
            url:"https://www.decathlon.in/p/8737843/rain-ponchos/adult-waterproof-poncho-for-0-10l-bag-pale-blue-mt50"
        },
        shoes:{
            type:"BUDGET WATER FOOTWEAR",
            name:"Decathlon Aquashoes 100",
            price:"₹499",
            meta:"Decathlon • water-friendly footwear • price checked Sep 2026",
            image:"https://contents.mediadecathlon.com/p2165479/fd62139a59aa55c8e1da35d26d317ec4/p2165479.jpg?f=1200x0&format=auto&quality=70",
            url:"https://www.decathlon.in/p/8330684/adult-breathable-clogs-slip-on-lightweight-shoes-grey"
        }
    };
}
function renderWeatherProducts(keys){
    const grid=document.getElementById("productGrid"); if(!grid)return;
    const catalog=productCatalog(); const unique=[...new Set(keys)];
    grid.innerHTML=unique.map(key=>{const p=catalog[key];if(!p)return"";return `<div class="product-card"><div class="product-image-wrap"><img src="${p.image}" alt="${escapeHTML(p.name)}" loading="lazy" onerror="this.style.display='none';this.parentElement.innerHTML='<span style="color:#7b8d93;font-size:12px">Product image unavailable</span>'"></div><div class="product-body"><div class="product-type">${escapeHTML(p.type)}</div><div class="product-name">${escapeHTML(p.name)}</div><div class="product-price">${escapeHTML(p.price)}</div><div class="product-meta">${escapeHTML(p.meta)}</div><a class="product-buy" href="${p.url}" target="_blank" rel="noopener">View & Buy ↗</a></div></div>`}).join("");
}
function buildGearSuggestions(weather,sourceLabel){
    const grid=document.getElementById("gearGrid"),status=document.getElementById("smartGearStatus"),summary=document.getElementById("smartWeatherSummary"); if(!grid||!weather)return;
    const cards=[],products=[],rainy=weatherIsRainy(weather),storm=weatherIsStormy(weather),temp=Number(weather.temperature),feels=Number(weather.feels_like),rainChance=Number(weather.rain_probability||0);
    if(rainy){products.push("umbrella","raincoat","shoes");cards.push({icon:"☂️",title:"Umbrella",text:`${sourceLabel}: rain is possible (${rainChance}% chance). Keep an umbrella with you.`,tag:"RAIN READY"},{icon:"🧥",title:"Raincoat",text:rainChance>=60||Number(weather.rainfall||0)>0?"Rain risk is significant. A waterproof raincoat will help keep you dry.":"A light rain jacket is useful if showers develop.",tag:"WATERPROOF"},{icon:"🥾",title:"Rain shoes",text:"Wet roads and puddles can make travel uncomfortable. Water-resistant footwear with good grip is recommended.",tag:"TRAVEL"});}
    if(storm)cards.push({icon:"⛈️",title:"Storm protection",text:"Thunderstorm conditions detected. Prefer sheltered travel and avoid exposed outdoor areas.",tag:"SAFETY"});
    if(Number.isFinite(feels)&&feels>=35)cards.push({icon:"💧",title:"Water bottle",text:"Feels-like temperature is high. Carry water and take cooling breaks.",tag:"HEAT CARE"});
    if(Number.isFinite(temp)&&temp<=12)cards.push({icon:"🧣",title:"Warm layer",text:"Cool conditions detected. Carry a warm layer, especially for longer outdoor travel.",tag:"COLD CARE"});
    if(!cards.length){grid.innerHTML=`<div class="no-gear"><strong>✓ No special weather gear needed right now.</strong><br>Current conditions look relatively comfortable for normal travel.</div>`;status.textContent="ALL CLEAR";summary.textContent=`${sourceLabel}: no strong gear alert detected from the current weather.`;}else{grid.innerHTML=cards.map(c=>`<div class="gear-card"><div class="gear-icon">${c.icon}</div><h3>${escapeHTML(c.title)}</h3><p>${escapeHTML(c.text)}</p><span class="gear-tag">${escapeHTML(c.tag)}</span></div>`).join("");status.textContent=rainy?"RAIN GEAR ADVISED":"WEATHER CARE";summary.textContent=`${sourceLabel}: WeatherGPT detected conditions where a few practical precautions may help.`;}
    renderWeatherProducts(products);
}

function getSavedHealthProfile() {
    try {
        return JSON.parse(localStorage.getItem("weatherGPTHealthProfile") || "null") || {
            age: "", activity: "low", height: "", weight: "", conditions: [], symptoms: []
        };
    } catch (_) {
        return { age: "", activity: "low", height: "", weight: "", conditions: [], symptoms: [] };
    }
}

function updateHealthHistory(entry) {
    try {
        const history = JSON.parse(localStorage.getItem("weatherGPTHealthHistory") || "[]");
        history.unshift(entry);
        localStorage.setItem("weatherGPTHealthHistory", JSON.stringify(history.slice(0, 7)));
        renderHealthHistory();
    } catch (error) {
        console.warn("Health history error:", error);
    }
}

function renderHealthHistory() {
    const list = document.getElementById("healthHistoryList");
    if (!list) return;

    let history = [];
    try {
        history = JSON.parse(localStorage.getItem("weatherGPTHealthHistory") || "[]");
    } catch (_) {}

    if (!history.length) {
        list.innerHTML = `
            <div class="health-history-item">
                <span>No checks saved yet.</span>
                <strong>—</strong>
            </div>
        `;
        return;
    }

    list.innerHTML = history.map(item => `
        <div class="health-history-item">
            <span><strong>${escapeHTML(item.location || "Weather check")}</strong> · ${escapeHTML(item.date || "")}</span>
            <strong>${escapeHTML(item.level || "Normal")}</strong>
        </div>
    `).join("");
}

function updateHealthReport(weather,sourceLabel="Current location"){
    if(!weather)return;latestWeatherForHealth=weather;
    const profile=getSavedHealthProfile(),alerts=[],temp=Number(weather.temperature),feels=Number(weather.feels_like),humidity=Number(weather.humidity),code=Number(weather.weather_code),uv=Number(weather.uv_index),aqi=Number(weather.aqi),age=Number(profile.age),height=Number(profile.height),weight=Number(profile.weight),activity=profile.activity||"low",conditions=profile.conditions||[],symptoms=profile.symptoms||[];
    let level="Normal",bannerClass="",title="Weather looks manageable",text="No strong weather-related health warning was detected from the available conditions.";
    let bmi=null;if(height>=50&&height<=250&&weight>=10&&weight<=300)bmi=weight/Math.pow(height/100,2);
    const bmiEl=document.getElementById("healthBMI"),bmiNote=document.getElementById("healthBMInote");if(bmiEl)bmiEl.textContent=bmi?bmi.toFixed(1):"—";if(bmiNote)bmiNote.textContent=!bmi?"Add height + weight":bmi<18.5?"Below usual adult screening range":bmi<25?"Usual adult screening range":bmi<30?"Above usual adult screening range":"Higher adult screening range";
    const uvEl=document.getElementById("healthUV"),uvNote=document.getElementById("healthUVnote");if(uvEl)uvEl.textContent=Number.isFinite(uv)?uv.toFixed(1):"—";if(uvNote)uvNote.textContent=uv>=8?"High • sun protection":uv>=6?"Moderate-high":"Lower exposure risk";
    const aqiEl=document.getElementById("healthAQI"),aqiNote=document.getElementById("healthAQInote");if(aqiEl)aqiEl.textContent=Number.isFinite(aqi)?Math.round(aqi):"—";if(aqiNote)aqiNote.textContent=aqi>=151?"Unhealthy":aqi>=101?"Sensitive groups caution":aqi>=51?"Moderate":"Good";
    const emergency=document.getElementById("healthEmergency"),emergencyText=document.getElementById("healthEmergencyText");const urgent=["chest_pain","breathing","fainting"].some(x=>symptoms.includes(x));if(urgent){emergency?.classList.add("show");if(emergencyText)emergencyText.textContent="Chest pain, significant breathing difficulty, fainting or confusion can be serious. Seek urgent medical care now rather than waiting for a weather prediction.";}else emergency?.classList.remove("show");
    if((Number.isFinite(feels)&&feels>=35)||(Number.isFinite(temp)&&temp>=38)){level="High caution";bannerClass="high";title="Heat-related risk is elevated";text="Hot conditions can contribute to dehydration and heat illness. Stay cool, hydrate appropriately and reduce prolonged outdoor exposure.";alerts.push({cls:"danger",title:"🔥 Heat alert",text:"Watch for heavy sweating, dizziness, headache, nausea, weakness or shortness of breath. If symptoms worsen, seek medical help."});}
    else if((Number.isFinite(feels)&&feels>=32)||(Number.isFinite(temp)&&temp>=35)){level="Caution";bannerClass="caution";title="Warm-weather caution";text="Keep hydrated and consider limiting long outdoor activity during the hottest part of the day.";alerts.push({cls:"warning",title:"💧 Hydration reminder",text:"Carry water and take breaks in a cooler or shaded place."});}
    if(activity==="high"&&((Number.isFinite(feels)&&feels>=32)||temp>=35))alerts.push({cls:"warning",title:"🏃 Outdoor activity risk",text:"Exercise and strenuous outdoor activity increase heat exposure. Prefer cooler hours and stop if you feel faint or weak."});
    if(age&&(age<5||age>=65)&&((Number.isFinite(feels)&&feels>=32)||temp>=35)){level=level==="High caution"?level:"Caution";bannerClass=bannerClass||"caution";alerts.push({cls:"warning",title:"👤 Age-related heat caution",text:"Young children and older adults can need extra heat precautions. Keep them cool, hydrated and monitored."});}
    if(Number.isFinite(temp)&&temp<=10){level=level==="High caution"?level:"Caution";bannerClass=bannerClass||"caution";title=title==="Weather looks manageable"?"Cold-weather caution":title;if(title==="Cold-weather caution")text="Cold exposure can become a health concern, especially when you are wet or outside for long periods.";alerts.push({cls:"warning",title:"🧣 Cold exposure",text:"Keep warm and dry. Persistent shivering, confusion, unusual tiredness or drowsiness can be warning signs of hypothermia and need prompt medical attention."});}
    if(weatherIsRainy(weather)&&Number.isFinite(temp)&&temp<=15)alerts.push({cls:"warning",title:"🌧️ Wet + cold",text:"Rain and cold can increase heat loss. Carry waterproof clothing and change out of wet clothes promptly."});
    if([95,96,99].includes(code)){level="High caution";bannerClass="high";title="Thunderstorm safety alert";text="Avoid exposed outdoor areas during thunderstorms and move to a safer sheltered location.";alerts.push({cls:"danger",title:"⛈️ Storm warning",text:"If thunder is present, postpone exposed outdoor activity and follow local weather/emergency guidance."});}
    if(Number.isFinite(uv)&&uv>=6){level=level==="High caution"?level:"Caution";bannerClass=bannerClass||"caution";alerts.push({cls:"warning",title:"☀️ UV exposure alert",text:"UV is elevated. Prefer shade, protective clothing and sunscreen according to the product label; reduce prolonged direct sun exposure."});}
    if(Number.isFinite(aqi)&&aqi>=151){level="High caution";bannerClass="high";alerts.push({cls:"danger",title:"🌫️ Air quality alert",text:"Air quality is unhealthy. Consider reducing prolonged outdoor exposure, especially if you have a respiratory or heart condition."});}
    else if(Number.isFinite(aqi)&&aqi>=101){level=level==="High caution"?level:"Caution";bannerClass=bannerClass||"caution";alerts.push({cls:"warning",title:"🌫️ Sensitive-group air alert",text:"Air quality may affect sensitive people. Consider reducing strenuous outdoor activity and follow local air-quality guidance."});}
    if(conditions.includes("heart")&&((Number.isFinite(feels)&&feels>=35)||temp>=38))alerts.push({cls:"warning",title:"❤️ Heart condition profile",text:"Hot weather can place extra stress on the body. Follow your clinician's heat plan and avoid prolonged heat exposure."});
    if(conditions.includes("hypertension")&&((Number.isFinite(feels)&&feels>=35)||temp>=38))alerts.push({cls:"warning",title:"🩺 High BP profile",text:"Heat can add physiological stress. Stay cool and follow your clinician's plan; do not change medicines based on this app."});
    if(conditions.includes("diabetes")&&((Number.isFinite(feels)&&feels>=32)||temp>=35))alerts.push({cls:"warning",title:"🧃 Diabetes + heat",text:"Hot weather can increase dehydration risk. Follow your clinician's diabetes plan and medication-storage instructions."});
    if(conditions.includes("heat_medication")&&((Number.isFinite(feels)&&feels>=32)||temp>=35))alerts.push({cls:"warning",title:"💊 Medication + heat",text:"Some medicines can change heat tolerance. Do not stop or change medication; check your clinician/pharmacist's hot-weather guidance."});
    if(conditions.includes("heat_sensitive")&&Number.isFinite(feels)&&feels>=32)alerts.push({cls:"warning",title:"🌡️ Heat sensitivity",text:"The current conditions may be harder to tolerate. Stay cool, hydrated and monitor how you feel."});
    if(conditions.includes("cold_sensitive")&&Number.isFinite(temp)&&temp<=15)alerts.push({cls:"warning",title:"❄️ Cold sensitivity",text:"Use warm, dry layers and limit prolonged exposure if the cold feels uncomfortable."});
    if(conditions.includes("asthma")&&(humidity>=80||weatherIsRainy(weather)||[45,48].includes(code)||aqi>=101))alerts.push({cls:"warning",title:"🫁 Asthma profile",text:"Weather and air quality can trigger symptoms for some people. Keep your prescribed plan available and follow your clinician's advice."});
    if(symptoms.some(x=>["headache","dizziness","nausea","weakness"].includes(x)))alerts.push({cls:"warning",title:"🩹 Symptom check-in",text:"These symptoms can occur with heat illness or other causes. Move to a cooler place if overheated, hydrate as appropriate, and seek medical help if symptoms are severe or worsening."});
    const banner=document.getElementById("healthRiskBanner");banner.className=`risk-banner ${bannerClass}`;document.getElementById("healthRiskTitle").textContent=title;document.getElementById("healthRiskText").textContent=text;
    const alertList=document.getElementById("healthAlertList");alertList.innerHTML=alerts.length?alerts.map(a=>`<div class="health-alert ${a.cls}"><strong>${escapeHTML(a.title)}</strong><span>${escapeHTML(a.text)}</span></div>`).join(""):`<div class="health-alert"><strong>✓ No major weather-health alert</strong><span>Continue normal precautions and check how you feel during outdoor activity.</span></div>`;
    updateHealthHistory({location:sourceLabel,date:new Date().toLocaleString(),level:level,aqi:Number.isFinite(aqi)?Math.round(aqi):null,uv:Number.isFinite(uv)?Number(uv.toFixed(1)):null,bmi:bmi?Number(bmi.toFixed(1)):null});
}

function updateSmartWeatherFeatures(weather, sourceLabel) {
    if (!weather) return;
    buildGearSuggestions(weather, sourceLabel);
    updateHealthReport(weather, sourceLabel);
    setSafetyMode(weather);
    initSafetyMap();
}


function setWeatherTheme(theme) {
    document.body.classList.remove(
        "theme-summer",
        "theme-sunny",
        "theme-cloudy",
        "theme-rain",
        "theme-winter"
    );

    const allowed = [
        "theme-summer",
        "theme-sunny",
        "theme-cloudy",
        "theme-rain",
        "theme-winter"
    ];

    const selected = "theme-" + theme;
    document.body.classList.add(
        allowed.includes(selected) ? selected : "theme-cloudy"
    );
}

function createRain() {
    if (!background) return;

    background.querySelectorAll(".rain").forEach(drop => drop.remove());

    for (let i = 0; i < 90; i++) {
        const drop = document.createElement("div");
        drop.className = "rain";

        drop.style.left = Math.random() * 100 + "%";
        drop.style.animationDuration =
            (0.55 + Math.random() * 1.25) + "s";
        drop.style.animationDelay =
            Math.random() * 2 + "s";
        drop.style.opacity =
            0.25 + Math.random() * 0.65;

        background.appendChild(drop);
    }
}

function updateWeatherBackground(weather) {
    if (!weather) return;

    setWeatherTheme(weather.theme);

    // Rain drops are created once; CSS decides whether they are visible.
    if (!background.querySelector(".rain")) {
        createRain();
    }
}

async function loadCurrentLocationWeather(fromButton = false) {
    if (!navigator.geolocation) {
        routeNote.textContent =
            "📍 Location is not supported by this browser. You can still ask the AI about any city.";
        return;
    }

    navigator.geolocation.getCurrentPosition(
        async position => {
            currentLocation = {
                latitude: position.coords.latitude,
                longitude: position.coords.longitude
            };

            routeNote.textContent =
                "📍 Current location detected. Enter a destination to check weather along the route.";

            try {
                const response = await fetch("/weather/location", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify(currentLocation)
                });

                const data = await response.json();

                if (data.ok) {
                    updateWeatherBackground(data.weather);
                    updateDashboardWeather(data.weather, "Current Location");
                    updateSmartWeatherFeatures(data.weather, "Current location");
                }
            } catch (error) {
                console.log("Current weather background error:", error);
            }
        },
        error => {
            console.log("Location permission:", error.message);
            routeNote.textContent =
                "📍 Allow location access to use Route Weather. You can still use the city weather chat.";
        },
        {
            enableHighAccuracy: true,
            timeout: 10000,
            maximumAge: 300000
        }
    );
}

function escapeHTML(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function weatherLineColor(theme) {
    return {
        rain: "#4db8ff",
        summer: "#ffd34e",
        sunny: "#f4c95d",
        cloudy: "#aab8c4",
        winter: "#e9f7ff"
    }[theme] || "#aab8c4";
}

function stepIcon(step) {
    const m = (step.modifier || "").toLowerCase();
    const t = (step.type || "").toLowerCase();
    if (t === "arrive") return "🏁";
    if (m.includes("left")) return "↰";
    if (m.includes("right")) return "↱";
    if (t === "roundabout" || t === "rotary") return "⟳";
    if (t === "merge" || t === "fork") return "⤴";
    return "↑";
}

function showNavigationSteps(steps) {
    const list = document.getElementById("stepsList");
    if (!list) return;
    list.innerHTML = (steps || []).map((step, index) => `
        <div class="step-item" data-step-index="${index}">
            <div class="step-icon">${stepIcon(step)}</div>
            <div class="step-text">${escapeHTML(step.instruction || "Continue")}</div>
            <div class="step-distance">${step.distance_m >= 1000 ? (step.distance_m/1000).toFixed(1)+" km" : Math.round(step.distance_m)+" m"}</div>
        </div>
    `).join("");
}

function clearRouteLayers() {
    routeLayers.forEach(layer => {
        try { routeMap.removeLayer(layer); } catch (_) {}
    });
    routeLayers = [];
    if (userMarker && routeMap) {
        try { routeMap.removeLayer(userMarker); } catch (_) {}
        userMarker = null;
    }
}

function renderRouteMap(route) {
    const wrap = document.getElementById("routeMapWrap");
    if (!wrap || !window.L) return;

    wrap.hidden = false;

    // Leaflet needs a real, visible container before it calculates its size.
    // Repeated invalidateSize calls also fix the common "map only partly visible"
    // problem when the map is rendered after a hidden section becomes visible.
    const ensureMapSize = () => {
        if (routeMap) {
            routeMap.invalidateSize(true);
            setTimeout(() => routeMap && routeMap.invalidateSize(true), 250);
            setTimeout(() => routeMap && routeMap.invalidateSize(true), 900);
        }
    };

    if (!routeMap) {
        routeMap = L.map("routeMap", {
            zoomControl: false,
            zoomSnap: 1,
            zoomDelta: 1,
            wheelPxPerZoomLevel: 100,
            preferCanvas: false,
            attributionControl: true,
            fadeAnimation: false,
            zoomAnimation: false,
            markerZoomAnimation: false
        });

        L.control.zoom({ position: "bottomright" }).addTo(routeMap);

        // High-quality satellite imagery.
        const satellite = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: 18,
                maxNativeZoom: 18,
                tileSize: 256,
                keepBuffer: 6,
                updateWhenIdle: true,
                updateWhenZooming: true,
                updateInterval: 150,
                crossOrigin: true,
                noWrap: true,
                attribution: "Tiles © Esri"
            }
        ).addTo(routeMap);

        // Clean street layer for situations where road names are easier to read.
        const streets = L.tileLayer(
            "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            {
                maxZoom: 19,
                keepBuffer: 3,
                attribution: "© OpenStreetMap contributors"
            }
        );

        // Road/place labels over the satellite layer.
        const labels = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: 19,
                maxNativeZoom: 18,
                opacity: 0.9,
                keepBuffer: 3,
                attribution: "Esri reference"
            }
        );

        // Keep the satellite imagery as the default. Labels are optional because
        // they are a second tile service and can create apparent gaps while loading.
        L.control.layers(
            { "Satellite": satellite, "Street": streets },
            { "Place labels": labels },
            { position: "topright", collapsed: false }
        ).addTo(routeMap);

        // If satellite tiles fail completely, keep the map usable with streets.
        let satelliteErrors = 0;
        satellite.on("tileerror", () => {
            satelliteErrors += 1;
            if (satelliteErrors >= 8 && !routeMap.hasLayer(streets)) {
                // Never leave the user with a broken/blank map if the imagery
                // provider is blocked or rate-limited on the current network.
                streets.addTo(routeMap);
                loadingEl.classList.add("hidden");
            }
        });

        // Small professional map badge.
        const loadingEl = L.DomUtil.create("div", "map-loading");
        loadingEl.textContent = "🛰️ Loading satellite imagery…";
        document.getElementById("routeMap").appendChild(loadingEl);

        let loadedTiles = 0;
        const hideMapLoading = () => {
            loadedTiles += 1;
            if (loadedTiles >= 2) loadingEl.classList.add("hidden");
        };
        satellite.on("tileload", hideMapLoading);
        streets.on("tileload", hideMapLoading);
        satellite.on("load", () => loadingEl.classList.add("hidden"));

        const mapBadge = L.control({ position: "topleft" });
        mapBadge.onAdd = function() {
            const div = L.DomUtil.create("div", "map-mode-badge");
            div.innerHTML = '<span class="map-live-dot"></span><span>LIVE ROUTE • SATELLITE</span>';
            L.DomEvent.disableClickPropagation(div);
            return div;
        };
        mapBadge.addTo(routeMap);
    }

    ensureMapSize();
    clearRouteLayers();

    const geometry = route.geometry || [];
    const latLngs = geometry
        .filter(p => Array.isArray(p) && p.length >= 2 && Number.isFinite(Number(p[0])) && Number.isFinite(Number(p[1])))
        .map(p => [Number(p[1]), Number(p[0])]);

    if (!latLngs.length) return;

    // Soft route shadow for contrast over satellite imagery.
    const routeShadow = L.polyline(latLngs, {
        color: "#061116",
        weight: 12,
        opacity: 0.72,
        lineCap: "round",
        lineJoin: "round"
    }).addTo(routeMap);

    const base = L.polyline(latLngs, {
        color: "#ffffff",
        weight: 8,
        opacity: 0.82,
        lineCap: "round",
        lineJoin: "round"
    }).addTo(routeMap);
    routeLayers.push(routeShadow, base);

    // Weather-colored route sections.
    const points = route.checkpoints || [];
    for (let i = 0; i < points.length - 1; i++) {
        const a = points[i];
        const b = points[i + 1];
        const startIndex = Math.max(0, Number(a.route_index || 0));
        const endIndex = Math.min(geometry.length - 1, Number(b.route_index || 0));
        if (endIndex <= startIndex) continue;

        const segment = geometry
            .slice(startIndex, endIndex + 1)
            .map(p => [Number(p[1]), Number(p[0])]);

        const line = L.polyline(segment, {
            color: weatherLineColor(a.theme),
            weight: 6,
            opacity: 0.98,
            lineCap: "round",
            lineJoin: "round"
        }).addTo(routeMap);
        routeLayers.push(line);
    }

    const start = route.origin || {};
    const destination = route.destination || {};
    const startLatLng = [Number(start.latitude), Number(start.longitude)];
    const endLatLng = [Number(destination.latitude), Number(destination.longitude)];

    // Exact GPS accuracy ring.
    const accuracyCircle = L.circle(startLatLng, {
        radius: 45,
        color: "#52e0c4",
        weight: 2,
        opacity: 0.9,
        fillColor: "#52e0c4",
        fillOpacity: 0.12
    }).addTo(routeMap);

    const startIcon = L.divIcon({
        className: "custom-map-marker-wrap",
        html: '<div class="custom-map-marker current"><span>●</span></div>',
        iconSize: [38, 38],
        iconAnchor: [19, 19]
    });

    const destinationIcon = L.divIcon({
        className: "custom-map-marker-wrap",
        html: '<div class="custom-map-marker destination"><span>★</span></div>',
        iconSize: [38, 38],
        iconAnchor: [19, 19]
    });

    const startMarker = L.marker(startLatLng, {
        title: "Exact current location",
        icon: startIcon,
        zIndexOffset: 1000
    }).addTo(routeMap).bindPopup("<b>📍 Current Location</b><br><span style='opacity:.7'>Exact GPS starting point</span>");

    const endMarker = L.marker(endLatLng, {
        title: destination.name || "Destination",
        icon: destinationIcon,
        zIndexOffset: 1000
    }).addTo(routeMap).bindPopup(`🏁 <b>${escapeHTML(destination.name || "Destination")}</b><br><span style='opacity:.7'>Route destination</span>`);

    routeLayers.push(accuracyCircle, startMarker, endMarker);

    // Weather checkpoints.
    points.forEach((point, index) => {
        const marker = L.circleMarker([point.latitude, point.longitude], {
            radius: index === 0 || index === points.length - 1 ? 7 : 6,
            color: "#ffffff",
            weight: 2,
            fillColor: weatherLineColor(point.theme),
            fillOpacity: 0.96
        }).addTo(routeMap);

        marker.bindPopup(
            `<div class="weather-popup"><strong>📍 ${escapeHTML(point.label || "Route location")}</strong>` +
            `<div class="popup-temp">${point.temperature ?? "--"}°C</div>` +
            `<div>${escapeHTML(point.condition || "Unknown")}</div>` +
            `<div class="popup-meta">🌧️ ${point.rain_probability ?? 0}% rain · 💨 ${point.wind ?? "--"} km/h</div></div>`
        );
        routeLayers.push(marker);
    });

    const bounds = L.latLngBounds(latLngs);
    routeMap.fitBounds(bounds, {
        paddingTopLeft: [30, 30],
        paddingBottomRight: [30, 30],
        maxZoom: 15
    });

    ensureMapSize();
}

function showRouteWeather(route) {
    routeData = route;
    const destination = route.destination || {};
    const checkpoints = route.checkpoints || [];

    const pointsHTML = checkpoints.map(point => `
        <div class="route-point">
            <div class="point-name">📍 ${escapeHTML(point.label || "Route location")}</div>
            <div class="point-temp">${point.temperature ?? "--"}°C</div>
            <div class="point-condition">${escapeHTML(point.condition || "Unknown")}</div>
            <div class="point-meta">
                🌧️ ${point.rain_probability ?? 0}% rain chance
                &nbsp; • &nbsp;
                💨 ${point.wind ?? "--"} km/h
            </div>
        </div>
    `).join("");

    routeResult.innerHTML = `
        <div class="route-summary">
            <div class="route-stat">
                <span>DESTINATION</span>
                <strong>${escapeHTML(destination.name || "Destination")}</strong>
            </div>
            <div class="route-stat">
                <span>ROAD DISTANCE</span>
                <strong>${route.distance_km} km</strong>
            </div>
            <div class="route-stat">
                <span>EST. DRIVE TIME</span>
                <strong>${route.duration_min} min</strong>
            </div>
        </div>
        <div class="route-provider-row">
            <span>🧭 Route: ${escapeHTML(route.provider || "OpenStreetMap / OSRM")}</span>
            <a class="google-nav-btn" href="${escapeHTML(route.google_maps_url || "#")}" target="_blank" rel="noopener noreferrer">🗺️ Open exact route in Google Maps</a>
        </div>
        <div class="route-points">${pointsHTML}</div>
    `;

    renderRouteMap(route);
    showNavigationSteps(route.steps || []);
    document.getElementById("navigationPanel").hidden = !(route.steps || []).length;

    const startWeather = checkpoints[0];
    const finalWeather = checkpoints[checkpoints.length - 1];

    if (startWeather) {
        updateSmartWeatherFeatures(
            startWeather,
            "Current location"
        );
    }

    if (finalWeather) {
        updateWeatherBackground(finalWeather);
        updateSmartWeatherFeatures(
            finalWeather,
            `Destination: ${destination.name || "Destination"}`
        );
    }
}

function distanceMeters(lat1, lon1, lat2, lon2) {
    const R = 6371000;
    const toRad = value => value * Math.PI / 180;
    const dLat = toRad(lat2 - lat1);
    const dLon = toRad(lon2 - lon1);
    const a = Math.sin(dLat/2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon/2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
}

function highlightStep(index) {
    activeStepIndex = Math.max(0, Math.min(index, (routeData.steps || []).length - 1));
    document.querySelectorAll(".step-item").forEach((el, i) => {
        el.classList.toggle("active", i === activeStepIndex);
        if (i === activeStepIndex) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });

    const step = routeData.steps[activeStepIndex];
    if (!step) return;
    document.getElementById("navInstruction").textContent = step.instruction || "Continue";
    document.getElementById("navSubtext").textContent = step.distance_m >= 1000
        ? `In ${(step.distance_m / 1000).toFixed(1)} km · ${step.road || "road"}`
        : `In ${Math.round(step.distance_m)} m · ${step.road || "road"}`;
    document.getElementById("navArrow").textContent = stepIcon(step);
}

async function rerouteFromCurrentLocation(lat, lon) {
    if (!routeData || rerouting) return;
    const now = Date.now();
    if (now - lastRerouteAt < 20000) return;

    rerouting = true;
    lastRerouteAt = now;
    document.getElementById("navSubtext").textContent = "Recalculating your route…";

    try {
        const response = await fetch("/route-weather", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                latitude: lat,
                longitude: lon,
                destination: routeData.destination.name
            })
        });
        const data = await response.json();
        if (data.ok) {
            showRouteWeather(data.route);
            activeStepIndex = 0;
            highlightStep(0);
            speak("Route recalculated. " + (data.route.steps?.[0]?.instruction || "Continue."));
        }
    } catch (error) {
        console.error("Reroute error", error);
    } finally {
        rerouting = false;
    }
}

function startNavigation() {
    if (!routeData || !routeData.steps || !routeData.steps.length) return;
    if (!navigator.geolocation) {
        document.getElementById("navSubtext").textContent = "Location is not supported by this browser.";
        return;
    }

    if (navigationWatchId !== null) navigator.geolocation.clearWatch(navigationWatchId);
    activeStepIndex = 0;
    highlightStep(0);
    document.getElementById("startNav").disabled = true;
    document.getElementById("stopNav").disabled = false;
    speak("Navigation started. " + routeData.steps[0].instruction);

    navigationWatchId = navigator.geolocation.watchPosition(
        position => {
            const lat = position.coords.latitude;
            const lon = position.coords.longitude;

            if (routeMap) {
                if (!userMarker) {
                    userMarker = L.circleMarker([lat, lon], {
                        radius: 10,
                        color: "#ffffff",
                        weight: 3,
                        fillColor: "#50d5c7",
                        fillOpacity: 1
                    }).addTo(routeMap).bindPopup("📍 <b>You are here</b><br><span style='opacity:.72'>Live GPS position</span>");
                } else {
                    userMarker.setLatLng([lat, lon]);
                }
            }

            let best = activeStepIndex;
            let bestDistance = Infinity;
            const from = activeStepIndex;
            const to = Math.min(routeData.steps.length - 1, activeStepIndex + 2);
            for (let i = from; i <= to; i++) {
                const step = routeData.steps[i];
                const d = distanceMeters(lat, lon, step.latitude, step.longitude);
                if (d < bestDistance) { bestDistance = d; best = i; }
            }

            // If the driver gets well away from the next maneuver, recalculate the road route.
            if (bestDistance > 300) {
                rerouteFromCurrentLocation(lat, lon);
                return;
            }

            if (bestDistance < 80 && best < routeData.steps.length - 1) {
                const next = best + 1;
                if (next !== activeStepIndex) {
                    activeStepIndex = next;
                    highlightStep(activeStepIndex);
                    speak(routeData.steps[activeStepIndex].instruction);
                }
            }

            if (best === routeData.steps.length - 1 && bestDistance < 70) {
                document.getElementById("navInstruction").textContent = "🏁 You have arrived";
                document.getElementById("navSubtext").textContent = routeData.destination.name || "Destination";
                speak("You have arrived at your destination.");
                stopNavigation();
            }
        },
        error => {
            document.getElementById("navSubtext").textContent = "Navigation location error: " + error.message;
        },
        { enableHighAccuracy: true, maximumAge: 3000, timeout: 10000 }
    );
}

function stopNavigation() {
    if (navigationWatchId !== null) {
        navigator.geolocation.clearWatch(navigationWatchId);
        navigationWatchId = null;
    }
    document.getElementById("startNav").disabled = false;
    document.getElementById("stopNav").disabled = true;
}

async function checkRouteWeather() {
    const destination = destinationInput.value.trim();

    if (!destination) {
        routeResult.innerHTML =
            '<div class="route-error">⚠️ Please enter a destination first.</div>';
        destinationInput.focus();
        return;
    }

    if (!currentLocation) {
        routeResult.innerHTML =
            '<div class="route-error">📍 Please allow location access first, then try again.</div>';
        loadCurrentLocationWeather(false);
        return;
    }

    routeButton.disabled = true;
    routeButton.textContent = "Checking route…";
    routeResult.innerHTML =
        '<div class="route-note">🛰️ Finding the road route and checking weather checkpoints…</div>';

    try {
        const response = await fetch("/route-weather", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                latitude: currentLocation.latitude,
                longitude: currentLocation.longitude,
                destination: destination
            })
        });

        const data = await response.json();

        if (!data.ok) {
            routeResult.innerHTML =
                `<div class="route-error">⚠️ ${escapeHTML(data.error || "Could not load route weather.")}</div>`;
            return;
        }

        showRouteWeather(data.route);

    } catch (error) {
        console.error(error);
        routeResult.innerHTML =
            '<div class="route-error">⚠️ Network error. Please check your internet connection and try again.</div>';
    } finally {
        routeButton.disabled = false;
        routeButton.textContent = "Check Route Weather";
    }
}

routeButton.addEventListener("click", checkRouteWeather);
document.getElementById("startNav").addEventListener("click", startNavigation);
document.getElementById("stopNav").addEventListener("click", stopNavigation);

destinationInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
        checkRouteWeather();
    }
});

document.getElementById("findSafePlaceBtn")?.addEventListener("click", findSafePlaces);

// Restore the optional local health profile and recent report history.
loadHealthProfile();
renderHealthHistory();
updateEmergencyCallLinks(getSavedHealthProfile());
initSafetyMap();

document.getElementById("saveHealthProfile")?.addEventListener("click", () => {
    saveHealthProfile();
    updateEmergencyCallLinks(getSavedHealthProfile());
    if (latestWeatherForHealth) {
        updateHealthReport(latestWeatherForHealth);
    }
});

// Create rain animation and then try to personalize the background.
createRain();
loadCurrentLocationWeather(false);

</script>


</body>

</html>
"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template_string(HTML)


# ============================================================
# HEALTH CHECK (used by Render to keep the service up)
# ============================================================

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ============================================================
# CHAT
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():

    try:
        data = request.get_json(silent=True) or {}
        message = str(data.get("message", "")).strip()

        if not message:
            return jsonify({
                "reply": "Please type a weather question."
            }), 200

        answer = generate_response(message)

        return jsonify({
            "reply": answer
        }), 200

    except Exception as error:
        print("Chat Error:", error)
        return jsonify({
            "reply": "⚠️ I couldn't process that message right now. Please try again."
        }), 200


# ============================================================
# CURRENT LOCATION WEATHER
# ============================================================

@app.route("/weather/location", methods=["POST"])
def location_weather():
    data = request.get_json(silent=True) or {}

    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid location."}), 400

    weather = get_weather_by_coordinates(latitude, longitude)
    if weather is None:
        return jsonify({"ok": False, "error": "Could not load current weather."}), 502

    return jsonify({"ok": True, "weather": weather})


# ============================================================
# SAFE PLACES / EMERGENCY WEATHER
# ============================================================

@app.route("/safe-places", methods=["POST"])
def safe_places():
    data = request.get_json(silent=True) or {}

    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid current location."}), 400

    # Public places that can be useful as emergency destinations.
    # This is NOT a claim that every returned place is flood-proof.
    query = f"""
    [out:json][timeout:15];
    (
      nwr(around:5000,{latitude},{longitude})["amenity"="hospital"];
      nwr(around:5000,{latitude},{longitude})["amenity"="police"];
      nwr(around:5000,{latitude},{longitude})["amenity"="fire_station"];
      nwr(around:5000,{latitude},{longitude})["amenity"="shelter"];
      nwr(around:5000,{latitude},{longitude})["amenity"="school"];
    );
    out center tags;
    """

    try:
        response = requests.post(
            "https://overpass-api.de/api/interpreter",
            data=query,
            timeout=25,
            headers={"User-Agent": "WeatherGPT/1.0 emergency-safe-place"}
        )
        response.raise_for_status()
        elements = response.json().get("elements", [])

        candidates = []
        type_map = {
            "hospital": ("🏥", "Hospital"),
            "police": ("🚓", "Police station"),
            "fire_station": ("🚒", "Fire station"),
            "shelter": ("🛡️", "Emergency shelter"),
            "school": ("🏫", "Public building / school")
        }

        import math

        for el in elements:
            tags = el.get("tags", {})
            amenity = tags.get("amenity")
            if amenity not in type_map:
                continue

            lat = el.get("lat") or el.get("center", {}).get("lat")
            lon = el.get("lon") or el.get("center", {}).get("lon")
            if lat is None or lon is None:
                continue

            dlat = math.radians(float(lat) - latitude)
            dlon = math.radians(float(lon) - longitude)
            a = (
                math.sin(dlat / 2) ** 2 +
                math.cos(math.radians(latitude)) *
                math.cos(math.radians(float(lat))) *
                math.sin(dlon / 2) ** 2
            )
            distance_km = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))

            name = (
                tags.get("name")
                or tags.get("official_name")
                or tags.get("short_name")
                or type_map[amenity][1]
            )

            candidates.append({
                "name": name,
                "type": type_map[amenity][1],
                "icon": type_map[amenity][0],
                "latitude": float(lat),
                "longitude": float(lon),
                "distance_km": round(distance_km, 2)
            })

        # Prefer hospitals / shelters / police, then distance.
        priority = {
            "Emergency shelter": 0,
            "Hospital": 1,
            "Police station": 2,
            "Fire station": 3,
            "Public building / school": 4
        }
        candidates.sort(key=lambda x: (priority.get(x["type"], 9), x["distance_km"]))

        # Keep the nearest few, but don't flood the UI with dozens of markers.
        places = candidates[:8]

        return jsonify({
            "ok": True,
            "places": places,
            "note": "Places come from OpenStreetMap. Verify local authority instructions during an actual emergency."
        })

    except Exception as error:
        print("Safe Places Error:", error)
        return jsonify({
            "ok": False,
            "error": "Could not load nearby public safety places."
        }), 502


# ============================================================
# ROUTE WEATHER
# ============================================================

@app.route("/route-weather", methods=["POST"])
def route_weather():
    data = request.get_json(silent=True) or {}

    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Please allow location access."}), 400

    destination = str(data.get("destination", "")).strip()
    if not destination:
        return jsonify({"ok": False, "error": "Please enter a destination."}), 400

    result, error = get_route_weather(latitude, longitude, destination)

    if result is None:
        return jsonify({"ok": False, "error": error}), 502

    return jsonify({"ok": True, "route": result})


# ============================================================
# OPEN BROWSER
# ============================================================

def open_browser():

    webbrowser.open(
        "http://127.0.0.1:5000"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    import os

    port = int(os.environ.get("PORT", 5000))
    is_render = bool(os.environ.get("RENDER"))

    print()
    print("=" * 55)
    print("       🌦️ WEATHERGPT")
    print("=" * 55)
    print()
    print("🚀 Server starting...")
    print("🌦️ Real weather API: ON")
    print("🤖 AI Assistant: ON")
    print("🎤 Voice Assistant: ON")
    print()
    print(f"🌐 Listening on port {port}")
    print()
    print("=" * 55)

    if not is_render:
        threading.Timer(
            1.5,
            open_browser
        ).start()

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )