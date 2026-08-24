"""Rate-limited Airbnb co-host directory city counting.

This module uses the same public JSON endpoints loaded by Airbnb's directory
page. It deliberately stays sequential, honors Retry-After, pauses between
batches, and stops after repeated block responses. It does not rotate
identities, use proxies, or attempt to bypass Airbnb protections.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_API_ROOT = "https://www.airbnb.com/api/v3"
_AUTOCOMPLETE_OPERATION = "QueryAutocompleteLocation"
_AUTOCOMPLETE_HASH = (
    "d705264a1585447c54d4adcca1086e3ca22ee0821021cb9e730ff4f9d4fea7b8"
)
_MATCHES_OPERATION = "QueryCohostMarketplaceMatches"
_MATCHES_HASH = (
    "66f0614fee8dd47a258c12679d861d3cfddca4b5566ab4f37a5a363bc0a7fb98"
)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_PUBLIC_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"


class DirectoryBlockedError(RuntimeError):
    """Raised when Airbnb repeatedly returns a block or rate-limit response."""


@dataclass(frozen=True)
class DirectoryThrottle:
    min_delay_seconds: float = 8.0
    max_delay_seconds: float = 14.0
    batch_size: int = 20
    batch_pause_min_seconds: float = 45.0
    batch_pause_max_seconds: float = 75.0
    max_retries: int = 3
    retry_base_seconds: float = 30.0

    def __post_init__(self):
        if self.min_delay_seconds < 0:
            raise ValueError("min_delay_seconds must be at least 0")
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("max_delay_seconds must be >= min_delay_seconds")
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if self.batch_pause_min_seconds < 0:
            raise ValueError("batch_pause_min_seconds must be at least 0")
        if self.batch_pause_max_seconds < self.batch_pause_min_seconds:
            raise ValueError(
                "batch_pause_max_seconds must be >= batch_pause_min_seconds"
            )
        if self.max_retries < 0:
            raise ValueError("max_retries must be at least 0")


def _api_url(operation: str, query_hash: str, variables: dict) -> str:
    params = {
        "operationName": operation,
        "locale": "en",
        "currency": "USD",
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": query_hash,
                }
            },
            separators=(",", ":"),
        ),
    }
    return f"{_API_ROOT}/{operation}/{query_hash}?{urlencode(params)}"


def _http_json(url: str, *, timeout: float = 30.0) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Referer": "https://www.airbnb.com/co-hosts/results",
            "User-Agent": _USER_AGENT,
            "X-Airbnb-API-Key": _PUBLIC_API_KEY,
            "X-Airbnb-GraphQL-Platform": "web",
            "X-Airbnb-GraphQL-Platform-Client": "minimalist-niobe",
            "X-Airbnb-Supports-Airlock-v2": "true",
            "X-CSRF-Without-Token": "1",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _normalize(value: str) -> str:
    return " ".join((value or "").lower().replace(".", "").split())


def select_location_suggestion(city: str, state: str, suggestions: Iterable[dict]):
    """Choose the exact city/state suggestion, never a street-level fallback."""
    wanted_city = _normalize(city)
    wanted_state = _normalize(state)
    fallback = None

    for item in suggestions:
        suggestion = item.get("suggestion") or {}
        main_text = _normalize(suggestion.get("mainText", ""))
        secondary_text = _normalize(suggestion.get("secondaryText", ""))
        if main_text != wanted_city:
            continue
        if fallback is None:
            fallback = item
        secondary_tokens = {
            token.strip()
            for token in secondary_text.replace(",", " ").split()
            if token.strip()
        }
        if wanted_state in secondary_tokens:
            return item

    return fallback


def _extract_suggestions(payload: dict) -> list:
    return (
        payload.get("data", {})
        .get("cohostMarketplace", {})
        .get("autocompleteLocationV2", [])
        or []
    )


def _extract_match_summary(payload: dict) -> tuple[int, str]:
    matches = (
        payload.get("data", {})
        .get("cohostMarketplace", {})
        .get("matches", {})
    )
    page_info = matches.get("pageInfo") or {}
    total_count = page_info.get("totalCount")
    if total_count is None:
        total_count = len(matches.get("edges") or [])
    return int(total_count), str(page_info.get("displayLocation") or "")


def _directory_url(location_id: str) -> str:
    return (
        "https://www.airbnb.com/co-hosts/results"
        f"?no_redirect=1&location_id={location_id}"
    )


def _request_with_backoff(
    request_fn: Callable[[], dict],
    *,
    throttle: DirectoryThrottle,
    sleep_fn: Callable[[float], None],
    random_fn: Callable[[float, float], float],
) -> dict:
    for attempt in range(throttle.max_retries + 1):
        try:
            return request_fn()
        except HTTPError as error:
            retryable = error.code in {403, 429, 500, 502, 503, 504}
            if not retryable:
                raise
            if attempt >= throttle.max_retries:
                if error.code in {403, 429}:
                    raise DirectoryBlockedError(
                        f"Airbnb returned HTTP {error.code} after retries"
                    ) from error
                raise
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = throttle.retry_base_seconds * (2**attempt)
            wait_seconds += random_fn(0, min(10.0, wait_seconds * 0.25))
            sleep_fn(wait_seconds)
        except URLError:
            if attempt >= throttle.max_retries:
                raise
            wait_seconds = throttle.retry_base_seconds * (2**attempt)
            wait_seconds += random_fn(0, min(10.0, wait_seconds * 0.25))
            sleep_fn(wait_seconds)
    raise RuntimeError("unreachable")


def count_directory_profiles(
    city: str,
    state: str,
    *,
    throttle: DirectoryThrottle | None = None,
    request_json: Callable[[str], dict] = _http_json,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> dict:
    """Return the public Airbnb directory count for one exact city/state."""
    throttle = throttle or DirectoryThrottle()
    query = f"{city}, {state}"
    autocomplete_url = _api_url(
        _AUTOCOMPLETE_OPERATION,
        _AUTOCOMPLETE_HASH,
        {"search": query},
    )
    autocomplete = _request_with_backoff(
        lambda: request_json(autocomplete_url),
        throttle=throttle,
        sleep_fn=sleep_fn,
        random_fn=random_fn,
    )
    suggestion = select_location_suggestion(
        city,
        state,
        _extract_suggestions(autocomplete),
    )
    if not suggestion:
        return {
            "City": city,
            "State": state,
            "Profiles Found": None,
            "Directory Location": "",
            "Directory URL": "",
            "Status": "No exact city match",
            "Scan Date": date.today().isoformat(),
        }

    location_id = str((suggestion.get("location") or {}).get("id") or "")
    if not location_id:
        return {
            "City": city,
            "State": state,
            "Profiles Found": None,
            "Directory Location": "",
            "Directory URL": "",
            "Status": "City match did not include a directory location ID",
            "Scan Date": date.today().isoformat(),
        }

    matches_url = _api_url(
        _MATCHES_OPERATION,
        _MATCHES_HASH,
        {
            "origin": "UNKNOWN",
            "search": {"locationId": location_id},
            "pageSize": 48,
            "skipMessages": True,
        },
    )
    matches = _request_with_backoff(
        lambda: request_json(matches_url),
        throttle=throttle,
        sleep_fn=sleep_fn,
        random_fn=random_fn,
    )
    total_count, display_location = _extract_match_summary(matches)
    return {
        "City": city,
        "State": state,
        "Profiles Found": total_count,
        "Directory Location": display_location,
        "Directory URL": _directory_url(location_id),
        "Status": "Complete",
        "Scan Date": date.today().isoformat(),
    }


def scan_directory_cities(
    cities: Iterable[tuple[str, str]],
    *,
    throttle: DirectoryThrottle | None = None,
    progress_callback: Callable[[int, int, dict], None] | None = None,
    request_json: Callable[[str], dict] = _http_json,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> list[dict]:
    """Scan cities sequentially with per-city delays and periodic cooldowns."""
    throttle = throttle or DirectoryThrottle()
    city_list = [(str(city).strip(), str(state).strip()) for city, state in cities]
    results = []
    total = len(city_list)

    for index, (city, state) in enumerate(city_list, start=1):
        try:
            result = count_directory_profiles(
                city,
                state,
                throttle=throttle,
                request_json=request_json,
                sleep_fn=sleep_fn,
                random_fn=random_fn,
            )
        except DirectoryBlockedError:
            raise
        except Exception as error:
            result = {
                "City": city,
                "State": state,
                "Profiles Found": None,
                "Directory Location": "",
                "Directory URL": "",
                "Status": f"Lookup failed: {error}",
                "Scan Date": date.today().isoformat(),
            }
        results.append(result)
        if progress_callback:
            progress_callback(index, total, result)

        if index >= total:
            continue
        if index % throttle.batch_size == 0:
            delay = random_fn(
                throttle.batch_pause_min_seconds,
                throttle.batch_pause_max_seconds,
            )
        else:
            delay = random_fn(
                throttle.min_delay_seconds,
                throttle.max_delay_seconds,
            )
        sleep_fn(delay)

    return results
