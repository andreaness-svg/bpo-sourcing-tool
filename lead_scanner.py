import asyncio
import html
import re
from urllib.parse import quote, urlparse

import nest_asyncio
import pandas as pd
from geopy.geocoders import Nominatim
from playwright.async_api import async_playwright


nest_asyncio.apply()

_GEOCODE_CACHE = {}
_GEOCODER = Nominatim(user_agent="bpo_sourcing_tool_2026", timeout=10)
_COORDINATE_PATTERNS = (
    re.compile(
        r'"latitude"\s*:\s*"?([-+]?\d+(?:\.\d+)?)"?\s*,\s*'
        r'"longitude"\s*:\s*"?([-+]?\d+(?:\.\d+)?)"?',
        re.IGNORECASE,
    ),
    re.compile(
        r'"lat"\s*:\s*"?([-+]?\d+(?:\.\d+)?)"?\s*,\s*'
        r'"lng"\s*:\s*"?([-+]?\d+(?:\.\d+)?)"?',
        re.IGNORECASE,
    ),
    re.compile(
        r'"lat"\s*:\s*"?([-+]?\d+(?:\.\d+)?)"?\s*,\s*'
        r'"lon"\s*:\s*"?([-+]?\d+(?:\.\d+)?)"?',
        re.IGNORECASE,
    ),
)


def extract_listing_id(url: str):
    """Return the numeric listing ID from an Airbnb /rooms/<id> URL."""
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, TypeError, ValueError):
        return None

    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"airbnb.com", "www.airbnb.com"} and not host.endswith(".airbnb.com"):
        return None

    match = re.search(r"(?:^|/)rooms/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def extract_public_location(page_content: str):
    """Extract Airbnb's public listing coordinates and precision metadata."""
    decoded = html.unescape(page_content or "")
    latitude = longitude = None

    for pattern in _COORDINATE_PATTERNS:
        match = pattern.search(decoded)
        if not match:
            continue
        candidate_latitude = float(match.group(1))
        candidate_longitude = float(match.group(2))
        if -90 <= candidate_latitude <= 90 and -180 <= candidate_longitude <= 180:
            latitude, longitude = candidate_latitude, candidate_longitude
            break

    exact_match = re.search(
        r'"isExactLocation"\s*:\s*(true|false)', decoded, re.IGNORECASE
    )
    locality_match = re.search(
        r'"addressLocality"\s*:\s*"([^"\\]+)', decoded, re.IGNORECASE
    )

    is_exact = exact_match.group(1).lower() == "true" if exact_match else None
    locality = locality_match.group(1).strip() if locality_match else ""
    return latitude, longitude, is_exact, locality


def build_maps_url(latitude: float, longitude: float) -> str:
    coordinates = quote(f"{latitude:.6f},{longitude:.6f}", safe="")
    return f"https://www.google.com/maps/search/?api=1&query={coordinates}"


async def _reverse_geocode(latitude: float, longitude: float):
    cache_key = (round(latitude, 6), round(longitude, 6))
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]

    try:
        location = await asyncio.wait_for(
            asyncio.to_thread(
                _GEOCODER.reverse,
                f"{latitude:.6f}, {longitude:.6f}",
                exactly_one=True,
                addressdetails=True,
            ),
            timeout=12,
        )
    except Exception:
        location = None

    _GEOCODE_CACHE[cache_key] = location
    return location


async def scan_leads(urls):
    """Scan public Airbnb listing URLs and return their public map locations."""
    results = []
    stats = {
        "listings_total": len(urls),
        "listings_mapped": 0,
        "listings_without_location": 0,
        "listings_invalid": 0,
        "listings_auth_blocked": 0,
        "listing_errors": 0,
    }
    blocked_by_airbnb = False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        for raw_url in urls:
            url = str(raw_url).strip()
            listing_id = extract_listing_id(url)
            if not listing_id:
                stats["listings_invalid"] += 1
                results.append(
                    {
                        "Listing ID": "",
                        "Address": "Invalid Airbnb listing URL",
                        "Location Precision": "Unavailable",
                        "Maps": "",
                        "Link": url,
                    }
                )
                continue

            listing_url = f"https://www.airbnb.com/rooms/{listing_id}"
            try:
                response = await page.goto(
                    listing_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                if response and response.status in (401, 403):
                    blocked_by_airbnb = True
                    stats["listings_auth_blocked"] += 1
                    results.append(
                        {
                            "Listing ID": listing_id,
                            "Address": "Airbnb blocked location lookup",
                            "Location Precision": "Unavailable",
                            "Maps": "",
                            "Link": listing_url,
                        }
                    )
                    continue

                await page.wait_for_timeout(2_500)
                content = await page.content()
                latitude, longitude, is_exact, locality = extract_public_location(content)

                if latitude is None or longitude is None:
                    stats["listings_without_location"] += 1
                    results.append(
                        {
                            "Listing ID": listing_id,
                            "Address": locality or "Public map location not found",
                            "Location Precision": "Unavailable",
                            "Maps": "",
                            "Link": listing_url,
                        }
                    )
                    continue

                maps_url = build_maps_url(latitude, longitude)
                geocoded = await _reverse_geocode(latitude, longitude)
                precision = "Exact public pin" if is_exact else "Approximate public pin"
                address = geocoded.address if geocoded else locality
                if not address:
                    address = f"{latitude:.6f}, {longitude:.6f}"

                stats["listings_mapped"] += 1
                results.append(
                    {
                        "Listing ID": listing_id,
                        "Address": address,
                        "Location Precision": precision,
                        "Maps": maps_url,
                        "Link": listing_url,
                    }
                )
            except Exception as error:
                stats["listing_errors"] += 1
                results.append(
                    {
                        "Listing ID": listing_id,
                        "Address": f"Location lookup failed: {error}",
                        "Location Precision": "Unavailable",
                        "Maps": "",
                        "Link": listing_url,
                    }
                )

        await context.close()
        await browser.close()

    dataframe = pd.DataFrame(
        results,
        columns=["Listing ID", "Address", "Location Precision", "Maps", "Link"],
    )
    dataframe.attrs["scan_stats"] = stats
    dataframe.attrs["blocked_by_airbnb"] = blocked_by_airbnb
    dataframe.attrs["fatal_error"] = ""
    return dataframe
