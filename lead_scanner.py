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
_RESULT_COLUMNS = [
    "Account Name",
    "Account Profile",
    "Account Reported Listings",
    "Listing ID",
    "Address",
    "Location Precision",
    "Maps",
    "Link",
]
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


def _is_airbnb_host(host: str) -> bool:
    clean_host = (host or "").lower().split(":", 1)[0]
    return clean_host in {"airbnb.com", "www.airbnb.com"} or clean_host.endswith(
        ".airbnb.com"
    )


def extract_listing_id(url: str):
    """Return the numeric listing ID from an Airbnb /rooms/<id> URL."""
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, TypeError, ValueError):
        return None

    if not _is_airbnb_host(parsed.netloc):
        return None

    match = re.search(r"(?:^|/)rooms/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def extract_cohost_profile_id(url: str):
    """Return the numeric profile ID from an Airbnb co-host account URL."""
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, TypeError, ValueError):
        return None

    if not _is_airbnb_host(parsed.netloc):
        return None

    match = re.search(r"(?:^|/)co-hosts/profile/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def extract_profile_listing_count(body_text: str):
    """Return Airbnb's largest publicly reported listing count on a profile."""
    text = body_text or ""
    candidates = [
        int(value)
        for value in re.findall(r"\b(\d+)\s+listings\b", text, re.IGNORECASE)
    ]
    candidates.extend(
        int(value)
        for value in re.findall(
            r"\bShow all\s+(\d+)\s+listings\b", text, re.IGNORECASE
        )
    )
    return max(candidates) if candidates else None


def extract_room_ids(page_content: str):
    """Return de-duplicated public listing IDs in page order."""
    decoded = html.unescape(page_content or "")
    return list(dict.fromkeys(re.findall(r"/rooms/(\d+)", decoded)))


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


def _profile_name_from_title(title: str) -> str:
    return (title or "").split("·", 1)[0].strip()


def _result_row(
    *,
    account_name="",
    account_profile="",
    account_reported_listings=None,
    listing_id="",
    address="",
    precision="Unavailable",
    maps_url="",
    listing_url="",
):
    return {
        "Account Name": account_name,
        "Account Profile": account_profile,
        "Account Reported Listings": account_reported_listings,
        "Listing ID": listing_id,
        "Address": address,
        "Location Precision": precision,
        "Maps": maps_url,
        "Link": listing_url,
    }


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


async def _scan_listing(
    page,
    listing_id,
    stats,
    *,
    account_name="",
    account_profile="",
    account_reported_listings=None,
):
    listing_url = f"https://www.airbnb.com/rooms/{listing_id}"
    stats["listings_total"] += 1

    try:
        response = await page.goto(
            listing_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        if response and response.status in (401, 403):
            stats["listings_auth_blocked"] += 1
            return (
                _result_row(
                    account_name=account_name,
                    account_profile=account_profile,
                    account_reported_listings=account_reported_listings,
                    listing_id=listing_id,
                    address="Airbnb blocked location lookup",
                    listing_url=listing_url,
                ),
                True,
            )

        await page.wait_for_timeout(1_200)
        content = await page.content()
        latitude, longitude, is_exact, locality = extract_public_location(content)

        if latitude is None or longitude is None:
            stats["listings_without_location"] += 1
            return (
                _result_row(
                    account_name=account_name,
                    account_profile=account_profile,
                    account_reported_listings=account_reported_listings,
                    listing_id=listing_id,
                    address=locality or "Public map location not found",
                    listing_url=listing_url,
                ),
                False,
            )

        maps_url = build_maps_url(latitude, longitude)
        geocoded = await _reverse_geocode(latitude, longitude)
        precision = "Exact public pin" if is_exact else "Approximate public pin"
        address = geocoded.address if geocoded else locality
        if not address:
            address = f"{latitude:.6f}, {longitude:.6f}"

        stats["listings_mapped"] += 1
        return (
            _result_row(
                account_name=account_name,
                account_profile=account_profile,
                account_reported_listings=account_reported_listings,
                listing_id=listing_id,
                address=address,
                precision=precision,
                maps_url=maps_url,
                listing_url=listing_url,
            ),
            False,
        )
    except Exception as error:
        stats["listing_errors"] += 1
        return (
            _result_row(
                account_name=account_name,
                account_profile=account_profile,
                account_reported_listings=account_reported_listings,
                listing_id=listing_id,
                address=f"Location lookup failed: {error}",
                listing_url=listing_url,
            ),
            False,
        )


async def _discover_profile(page, profile_url, profile_id, stats):
    response = await page.goto(
        profile_url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    if response and response.status in (401, 403):
        stats["profiles_auth_blocked"] += 1
        return {
            "profile_id": profile_id,
            "profile_name": "",
            "profile_url": profile_url,
            "reported_listings": None,
            "room_ids": [],
            "blocked": True,
        }

    # Airbnb progressively hydrates the profile cards. Waiting for that first
    # batch prevents the final card from being omitted on larger profiles.
    await page.wait_for_timeout(5_000)
    content = await page.content()
    body_text = await page.locator("body").inner_text()
    reported_listings = extract_profile_listing_count(body_text)
    room_ids = extract_room_ids(content)
    profile_name = _profile_name_from_title(await page.title())

    show_all = page.get_by_role(
        "button", name=re.compile(r"Show all \d+ listings", re.IGNORECASE)
    )
    if await show_all.count():
        try:
            await show_all.first.click()
            await page.wait_for_timeout(3_000)
            room_ids = extract_room_ids(await page.content())
        except Exception:
            pass

    return {
        "profile_id": profile_id,
        "profile_name": profile_name,
        "profile_url": profile_url,
        "reported_listings": reported_listings,
        "room_ids": room_ids,
        "blocked": False,
    }


async def scan_leads(urls):
    """Scan Airbnb co-host profiles or listing URLs for public map locations."""
    results = []
    profile_summaries = []
    stats = {
        "inputs_total": len(urls),
        "profiles_total": 0,
        "profiles_auth_blocked": 0,
        "reported_listings_total": 0,
        "public_listing_links_found": 0,
        "listings_total": 0,
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
            profile_id = extract_cohost_profile_id(url)

            if listing_id:
                result, blocked = await _scan_listing(page, listing_id, stats)
                results.append(result)
                blocked_by_airbnb = blocked_by_airbnb or blocked
                continue

            if profile_id:
                stats["profiles_total"] += 1
                try:
                    profile = await _discover_profile(page, url, profile_id, stats)
                except Exception as error:
                    stats["listing_errors"] += 1
                    profile_summaries.append(
                        {
                            "Account Name": "",
                            "Account Profile": url,
                            "Reported Listings": None,
                            "Public Listing Links Found": 0,
                            "Mapped Listings": 0,
                            "Status": f"Profile lookup failed: {error}",
                        }
                    )
                    continue

                if profile["blocked"]:
                    blocked_by_airbnb = True
                    profile_summaries.append(
                        {
                            "Account Name": profile["profile_name"],
                            "Account Profile": url,
                            "Reported Listings": None,
                            "Public Listing Links Found": 0,
                            "Mapped Listings": 0,
                            "Status": "Airbnb blocked profile lookup",
                        }
                    )
                    continue

                reported = profile["reported_listings"]
                room_ids = profile["room_ids"]
                if reported is not None:
                    stats["reported_listings_total"] += reported
                stats["public_listing_links_found"] += len(room_ids)
                mapped_before = stats["listings_mapped"]

                for room_id in room_ids:
                    result, blocked = await _scan_listing(
                        page,
                        room_id,
                        stats,
                        account_name=profile["profile_name"],
                        account_profile=url,
                        account_reported_listings=reported,
                    )
                    results.append(result)
                    blocked_by_airbnb = blocked_by_airbnb or blocked

                mapped_for_profile = stats["listings_mapped"] - mapped_before
                status = "Complete"
                if reported is not None and len(room_ids) < reported:
                    status = "Some listings were not publicly linked"
                profile_summaries.append(
                    {
                        "Account Name": profile["profile_name"],
                        "Account Profile": url,
                        "Reported Listings": reported,
                        "Public Listing Links Found": len(room_ids),
                        "Mapped Listings": mapped_for_profile,
                        "Status": status,
                    }
                )

                if not room_ids:
                    results.append(
                        _result_row(
                            account_name=profile["profile_name"],
                            account_profile=url,
                            account_reported_listings=reported,
                            address="No public listing links found",
                        )
                    )
                continue

            stats["listings_invalid"] += 1
            results.append(
                _result_row(
                    address="Invalid Airbnb listing or co-host profile URL",
                    listing_url=url,
                )
            )

        await context.close()
        await browser.close()

    dataframe = pd.DataFrame(results, columns=_RESULT_COLUMNS)
    dataframe.attrs["scan_stats"] = stats
    dataframe.attrs["profile_summaries"] = profile_summaries
    dataframe.attrs["blocked_by_airbnb"] = blocked_by_airbnb
    dataframe.attrs["fatal_error"] = ""
    return dataframe
