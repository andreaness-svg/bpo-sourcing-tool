import asyncio
import nest_asyncio
import re
import pandas as pd
from playwright.async_api import async_playwright
from geopy.geocoders import Nominatim
from functools import partial
import time

nest_asyncio.apply()
geolocator = Nominatim(user_agent="bpo_fresh_recon_2026")

# Simple in-memory cache for reverse geocoding
_geocode_cache = {}

async def _reverse_geocode(lat: str, lng: str, timeout: float = 10.0):
    key = f"{lat},{lng}"
    if key in _geocode_cache:
        return _geocode_cache[key]

    try:
        # Run the blocking geopy call in a thread to avoid blocking the event loop
        func = partial(geolocator.reverse, f"{lat}, {lng}", exactly_one=True, addressdetails=True)
        loc = await asyncio.wait_for(asyncio.to_thread(func), timeout=timeout)
    except Exception:
        loc = None

    _geocode_cache[key] = loc
    # Be polite to Nominatim: small delay to avoid rate-limit bursts
    await asyncio.sleep(1)
    return loc


async def _scan_host(url: str, browser, semaphore: asyncio.Semaphore, results: list, stats: dict, blocked_flag: dict):
    url = url.strip()
    if not url:
        stats["hosts_skipped"] += 1
        return

    async with semaphore:
        try:
            ctx = await browser.new_context(user_agent="Mozilla/5.0")
            page = await ctx.new_page()

            # Navigate to host profile
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if resp and resp.status in (401, 403):
                blocked_flag["blocked"] = True
                stats["hosts_auth_blocked"] += 1
                await ctx.close()
                return

            await asyncio.sleep(2)
            content = await page.content()

            keywords = ["my home", "our home", "owner", "we own", "my villa", "my business", "local", "own and operate"]
            if not any(w in content.lower() for w in keywords):
                stats["hosts_skipped"] += 1
                await ctx.close()
                return

            stats["hosts_scanned"] += 1

            # Extract room ids (de-duplicated)
            room_ids = list(dict.fromkeys(re.findall(r"/rooms/(\d+)", content)))

            for rid in room_ids[:3]:
                try:
                    room_url = f"https://www.airbnb.com/rooms/{rid}"
                    rresp = await page.goto(room_url, wait_until="domcontentloaded", timeout=60000)
                    if rresp and rresp.status in (401, 403):
                        blocked_flag["blocked"] = True
                        stats["rooms_auth_blocked"] += 1
                        break

                    await asyncio.sleep(2)
                    r_content = await page.content()

                    # Try several patterns for lat/lng
                    lat = None
                    lng = None
                    for pat in [r'"lat":\s*([-+]?\d*\.\d+|\d+)', r'"latitude":\s*([-+]?\d*\.\d+|\d+)', r'"lat\":\s*([-+]?\d*\.\d+|\d+)']:
                        mlat = re.search(pat, r_content)
                        if mlat:
                            lat = mlat.group(1)
                            break
                    for pat in [r'"lng":\s*([-+]?\d*\.\d+|\d+)', r'"longitude":\s*([-+]?\d*\.\d+|\d+)', r'"lon\":\s*([-+]?\d*\.\d+|\d+)']:
                        mlng = re.search(pat, r_content)
                        if mlng:
                            lng = mlng.group(1)
                            break

                    address = "Vicinity Only"
                    maps_url = "N/A"

                    if lat and lng:
                        stats["rooms_scanned"] += 1
                        maps_url = f"https://www.google.com/maps?q={lat},{lng}"

                        loc = await _reverse_geocode(lat, lng)
                        if loc:
                            try:
                                raw = loc.raw.get('address', {}) if hasattr(loc, 'raw') else {}
                                h_num, road = raw.get('house_number', ''), raw.get('road', '')
                                address = f"{h_num} {road}".strip() if h_num and road else loc.address
                            except Exception:
                                address = loc.address if hasattr(loc, 'address') else "Vicinity Only"
                    else:
                        stats["rooms_skipped"] += 1

                    # Extract host id more robustly
                    host_id = None
                    m = re.search(r"/users/show/(\d+)", url)
                    if m:
                        host_id = m.group(1)
                    else:
                        # fallback to last path segment
                        host_id = url.rstrip('/').split('/')[-1]

                    results.append({"Host": host_id, "Address": address, "Maps": maps_url, "Link": room_url})

                except Exception as e:
                    stats.setdefault("room_errors", 0)
                    stats["room_errors"] += 1
                    # continue to next room
                    continue

            await ctx.close()
        except Exception:
            stats.setdefault("host_errors", 0)
            stats["host_errors"] += 1
            try:
                await ctx.close()
            except Exception:
                pass


async def scan_leads(urls, concurrency: int = 5):
    """Scan multiple Airbnb host profile URLs concurrently and return a pandas DataFrame.

    Returns a DataFrame with attrs:
      - scan_stats: dict
      - blocked_by_airbnb: bool
      - fatal_error: str (empty on success)
    """
    results = []
    stats = {
        "hosts_total": len(urls),
        "hosts_scanned": 0,
        "hosts_skipped": 0,
        "hosts_auth_blocked": 0,
        "rooms_scanned": 0,
        "rooms_skipped": 0,
        "rooms_auth_blocked": 0,
    }
    blocked_flag = {"blocked": False}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"]) 
            sem = asyncio.Semaphore(concurrency)
            tasks = [asyncio.create_task(_scan_host(u, browser, sem, results, stats, blocked_flag)) for u in urls]
            await asyncio.gather(*tasks)
            await browser.close()

        df = pd.DataFrame(results)
        df.attrs["scan_stats"] = stats
        df.attrs["blocked_by_airbnb"] = blocked_flag["blocked"]
        df.attrs["fatal_error"] = ""
        return df

    except Exception as e:
        df = pd.DataFrame(results)
        stats.setdefault("fatal_error", 0)
        df.attrs["scan_stats"] = stats
        df.attrs["blocked_by_airbnb"] = blocked_flag.get("blocked", False)
        df.attrs["fatal_error"] = str(e)
        return df
