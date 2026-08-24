import unittest

from lead_scanner import (
    build_maps_url,
    extract_cohost_profile_id,
    extract_listing_id,
    extract_profile_listing_count,
    extract_public_location,
    extract_room_ids,
)


class LeadScannerParsingTests(unittest.TestCase):
    def test_extracts_listing_id_from_full_airbnb_url(self):
        url = (
            "https://www.airbnb.com/rooms/1699533136478015821"
            "?check_in=2026-10-02&check_out=2026-10-04"
        )
        self.assertEqual(extract_listing_id(url), "1699533136478015821")

    def test_rejects_non_listing_url(self):
        self.assertIsNone(extract_listing_id("https://example.com/rooms/123"))
        self.assertIsNone(extract_listing_id("https://www.airbnb.com/users/show/123"))

    def test_extracts_cohost_profile_id(self):
        url = (
            "https://www.airbnb.com/co-hosts/profile/1243754975929178209"
            "?no_redirect=1&location_id=example"
        )
        self.assertEqual(
            extract_cohost_profile_id(url),
            "1243754975929178209",
        )

    def test_rejects_non_cohost_profile_url(self):
        self.assertIsNone(
            extract_cohost_profile_id("https://example.com/co-hosts/profile/123")
        )
        self.assertIsNone(
            extract_cohost_profile_id("https://www.airbnb.com/rooms/123")
        )

    def test_extracts_reported_listing_count(self):
        text = (
            "11 listings\nMy service area\n"
            "I support 11 listings and can help hosts in Huntsville.\n"
            "Show all 11 listings"
        )
        self.assertEqual(extract_profile_listing_count(text), 11)

    def test_extracts_unique_room_ids_in_page_order(self):
        content = (
            '<a href="/rooms/111">First</a>'
            '<a href="/rooms/222?source=profile">Second</a>'
            '<a href="/rooms/111">First repeated</a>'
        )
        self.assertEqual(extract_room_ids(content), ["111", "222"])

    def test_extracts_current_airbnb_coordinate_fields(self):
        content = (
            '{"latitude":30.38275,"longitude":-86.41586,'
            '"isExactLocation":false,"addressLocality":"Destin"}'
        )
        self.assertEqual(
            extract_public_location(content),
            (30.38275, -86.41586, False, "Destin"),
        )

    def test_supports_legacy_coordinate_fields(self):
        content = '{"lat":30.1,"lng":-86.2,"isExactLocation":true}'
        self.assertEqual(
            extract_public_location(content),
            (30.1, -86.2, True, ""),
        )

    def test_builds_clickable_google_maps_url(self):
        self.assertEqual(
            build_maps_url(30.38275, -86.41586),
            "https://www.google.com/maps/search/?api=1&query=30.382750%2C-86.415860",
        )


if __name__ == "__main__":
    unittest.main()
