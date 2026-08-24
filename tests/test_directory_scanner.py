import unittest
from urllib.error import HTTPError

from directory_scanner import (
    DirectoryBlockedError,
    DirectoryThrottle,
    count_directory_profiles,
    scan_directory_cities,
    select_location_suggestion,
)


class DirectoryScannerTests(unittest.TestCase):
    def test_selects_exact_city_and_state_instead_of_street(self):
        suggestions = [
            {
                "location": {"id": "city-id"},
                "suggestion": {
                    "mainText": "Lihue",
                    "secondaryText": "HI, USA",
                },
            },
            {
                "location": {"id": "street-id"},
                "suggestion": {
                    "mainText": "Hipa Road",
                    "secondaryText": "Lihue, HI, USA",
                },
            },
        ]
        selected = select_location_suggestion("Lihue", "HI", suggestions)
        self.assertEqual(selected["location"]["id"], "city-id")

    def test_rejects_same_named_city_in_wrong_state(self):
        suggestions = [
            {
                "location": {"id": "wrong-state"},
                "suggestion": {
                    "mainText": "Portland",
                    "secondaryText": "ME, USA",
                },
            }
        ]
        self.assertIsNone(
            select_location_suggestion("Portland", "OR", suggestions)
        )

    def test_supports_common_city_and_territory_aliases(self):
        suggestions = [
            {
                "location": {"id": "new-york"},
                "suggestion": {
                    "mainText": "New York",
                    "secondaryText": "NY, USA",
                },
            },
            {
                "location": {"id": "canberra"},
                "suggestion": {
                    "mainText": "Canberra",
                    "secondaryText": "Australian Capital Territory, Australia",
                },
            },
        ]
        self.assertEqual(
            select_location_suggestion("New York City", "NY", suggestions)[
                "location"
            ]["id"],
            "new-york",
        )
        self.assertEqual(
            select_location_suggestion("Canberra", "ACT", suggestions)[
                "location"
            ]["id"],
            "canberra",
        )

    def test_counts_profiles_from_public_directory_total(self):
        def request_json(url):
            if "QueryAutocompleteLocation" in url:
                return {
                    "data": {
                        "cohostMarketplace": {
                            "autocompleteLocationV2": [
                                {
                                    "location": {"id": "location-id"},
                                    "suggestion": {
                                        "mainText": "Lihue",
                                        "secondaryText": "HI, USA",
                                    },
                                }
                            ]
                        }
                    }
                }
            return {
                "data": {
                    "cohostMarketplace": {
                        "matches": {
                            "edges": [{"node": {"id": "profile-id"}}],
                            "pageInfo": {
                                "totalCount": 1,
                                "displayLocation": "Lihue, HI",
                            },
                        }
                    }
                }
            }

        result = count_directory_profiles(
            "Lihue",
            "HI",
            throttle=DirectoryThrottle(
                min_delay_seconds=0,
                max_delay_seconds=0,
                batch_pause_min_seconds=0,
                batch_pause_max_seconds=0,
            ),
            request_json=request_json,
            sleep_fn=lambda _: None,
            random_fn=lambda low, high: low,
        )
        self.assertEqual(result["Profiles Found"], 1)
        self.assertEqual(result["Directory Location"], "Lihue, HI")
        self.assertIn("location_id=location-id", result["Directory URL"])
        self.assertEqual(result["Status"], "Complete")

    def test_returns_no_match_without_running_results_query(self):
        calls = []

        def request_json(url):
            calls.append(url)
            return {
                "data": {
                    "cohostMarketplace": {
                        "autocompleteLocationV2": []
                    }
                }
            }

        result = count_directory_profiles(
            "Not A City",
            "ZZ",
            request_json=request_json,
            sleep_fn=lambda _: None,
        )
        self.assertIsNone(result["Profiles Found"])
        self.assertEqual(result["Status"], "No exact city match")
        self.assertEqual(len(calls), 1)

    def test_batch_scanner_uses_regular_and_cooldown_delays(self):
        delays = []

        def request_json(url):
            if "QueryAutocompleteLocation" in url:
                city = "One" if "One" in url else "Two"
                return {
                    "data": {
                        "cohostMarketplace": {
                            "autocompleteLocationV2": [
                                {
                                    "location": {"id": f"{city}-id"},
                                    "suggestion": {
                                        "mainText": city,
                                        "secondaryText": "TX, USA",
                                    },
                                }
                            ]
                        }
                    }
                }
            return {
                "data": {
                    "cohostMarketplace": {
                        "matches": {
                            "edges": [],
                            "pageInfo": {
                                "totalCount": 0,
                                "displayLocation": "Test, TX",
                            },
                        }
                    }
                }
            }

        results = scan_directory_cities(
            [("One", "TX"), ("Two", "TX"), ("Three", "TX")],
            throttle=DirectoryThrottle(
                min_delay_seconds=2,
                max_delay_seconds=2,
                batch_size=2,
                batch_pause_min_seconds=10,
                batch_pause_max_seconds=10,
            ),
            request_json=request_json,
            sleep_fn=delays.append,
            random_fn=lambda low, high: low,
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(delays, [2, 10])

    def test_stops_after_repeated_rate_limit_responses(self):
        attempts = []

        def request_json(url):
            attempts.append(url)
            raise HTTPError(url, 429, "Too Many Requests", {}, None)

        with self.assertRaises(DirectoryBlockedError):
            count_directory_profiles(
                "Lihue",
                "HI",
                throttle=DirectoryThrottle(
                    max_retries=1,
                    retry_base_seconds=0,
                ),
                request_json=request_json,
                sleep_fn=lambda _: None,
                random_fn=lambda low, high: low,
            )
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
