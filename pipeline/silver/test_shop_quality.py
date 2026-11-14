import csv
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from pipeline.silver.shop_quality import BIGINT_MAX, TRANSFORM_VERSION, normalize_shop, parse_price
from pipeline.silver.shop_run import CSV_FIELDS, SHOP_FIELDS, normalize_rows, preview_csv, process_bronze

REPOSITORY = "test/coffee-analysis"


def bronze_row(**changes):
    return {
        "source_key": "place_id:ChIJsample_42",
        "id": "/g/11shop",
        "name": "  Ca\u0300  phe\u0302 Mây ",
        "main_category": " Coffee   Shop ",
        "address": "12 An Thượng 11 ,Đà Nẵng",
        "coordinate": {"latitude": "16.05", "longitude": 108.24},
        "rating": "4.7",
        "review": "1,200",
        "price_range": "D100-200K",
        "phone": "0905 123-456",
        "website": " HTTPS://example.com/menu?id=1#hours ",
        "link": "https://www.google.com/maps/place/M%C3%A2y",
    } | changes


def bronze_snapshot(run_id=42, **changes):
    return {
        "run_id": run_id,
        "mode": "weekly",
        "source": "omkar_cloud_google_maps",
        "schema_version": 1,
        "week_start": "2026-11-09",
        "collected_at": "2026-11-13T02:00:00Z",
        "row_count": 2,
        "rows": [
            bronze_row(),
            bronze_row(source_key="data_id:0xabc:0xdef", id=None, price_range=None),
        ],
    } | changes


class ShopQualityTests(unittest.TestCase):
    def test_maps_bronze_to_expected_typed_fields_without_mutating_source(self):
        row = bronze_row(
            about=[{"name": "Wi-Fi"}],
            images=[{"link": "https://example.com/image"}],
            featured_reviews=[{"rating": 5}],
            review_keywords=["coffee"],
            reviews_per_rating={"5": 12},
        )
        original = deepcopy(row)
        self.assertEqual(normalize_shop(row), {
            "source_key": "place_id:ChIJsample_42",
            "place_id": "ChIJsample_42",
            "data_id": None,
            "kgmid": "/g/11shop",
            "name": "Cà phê Mây",
            "main_category": "coffee shop",
            "address": "12 An Thượng 11, Đà Nẵng",
            "lat": 16.05,
            "lng": 108.24,
            "rating": 4.7,
            "review_count": 1200,
            "price_range": "100,000–200,000 VND",
            "price_currency": "VND",
            "price_min": 100000,
            "price_max": 200000,
            "price_tier": None,
            "phone": "+84905123456",
            "website": "https://example.com/menu?id=1#hours",
            "maps_link": "https://www.google.com/maps/place/M%C3%A2y",
            "quality_flags": [],
        })
        self.assertEqual(row, original)

    def test_preserves_rows_and_flags_duplicate_ids_without_name_or_address_merging(self):
        rows = [
            bronze_row(place_id="ChIJABC", data_id="0x12:0x34"),
            bronze_row(place_id="ChIJABC", data_id="0x56:0x78"),
            bronze_row(place_id="", data_id="0x12:0x34"),
            bronze_row(place_id="bad id", data_id="wrong"),
        ]
        cleaned = normalize_rows(rows, csv_source=True)
        self.assertEqual([row["source_key"] for row in cleaned], [
            "place_id:ChIJABC", "place_id:ChIJABC", "data_id:0x12:0x34", None,
        ])
        self.assertIn("place_id_duplicate", cleaned[0]["quality_flags"])
        self.assertIn("source_key_duplicate", cleaned[1]["quality_flags"])
        self.assertIn("kgmid_duplicate", cleaned[1]["quality_flags"])
        self.assertIn("data_id_duplicate", cleaned[2]["quality_flags"])
        self.assertIn("identity_uses_data_id", cleaned[2]["quality_flags"])
        self.assertIn("place_id_missing", cleaned[2]["quality_flags"])
        self.assertIn("identity_missing", cleaned[3]["quality_flags"])
        self.assertIn("place_id_invalid", cleaned[3]["quality_flags"])
        self.assertIn("data_id_invalid", cleaned[3]["quality_flags"])

    def test_distinguishes_missing_invalid_and_review_consistency(self):
        row = normalize_shop(bronze_row(
            name="  ", coordinate='{"latitude":"90.0001","longitude":"-180"}',
            rating="NaN", review=2, price_range="500k+, contact", phone="0123",
            website="javascript:alert(1)", link="https://google.com.evil.test/maps",
        ))
        self.assertEqual(row["lng"], -180.0)
        for field in ("name", "lat", "rating", "price_range", "phone", "website", "maps_link"):
            self.assertIsNone(row[field])
        self.assertEqual(row["quality_flags"], [
            "lat_invalid", "maps_link_invalid", "name_missing", "phone_invalid",
            "positive_reviews_without_valid_rating", "price_range_invalid",
            "rating_invalid", "website_invalid",
        ])

        unrated = normalize_shop(bronze_row(rating="0", review=0, coordinate=None))
        self.assertEqual(unrated["review_count"], 0)
        self.assertIsNone(unrated["rating"])
        self.assertEqual(unrated["quality_flags"], [
            "coordinate_missing", "lat_missing", "lng_missing", "rating_missing", "rating_unrated",
        ])
        contradictory = normalize_shop(bronze_row(rating=5, review=0))
        self.assertEqual(contradictory["rating"], 5.0)
        self.assertEqual(contradictory["quality_flags"], ["rating_with_zero_reviews"])

    def test_bounds_and_unexpected_scalar_types(self):
        row = normalize_shop(bronze_row(
            coordinate={"latitude": -90, "longitude": 180},
            rating=Decimal("5.000"), review=str(BIGINT_MAX),
        ))
        self.assertEqual((row["lat"], row["lng"], row["rating"], row["review_count"]), (
            -90.0, 180.0, 5.0, BIGINT_MAX,
        ))
        for value in (True, -1, "2.5", "1e99", "1,20", BIGINT_MAX + 1):
            with self.subTest(review=value):
                row = normalize_shop(bronze_row(review=value))
                self.assertIsNone(row["review_count"])
                self.assertIn("review_count_invalid", row["quality_flags"])
        for value in ("not JSON", "[]", 7):
            with self.subTest(coordinate=value):
                row = normalize_shop(bronze_row(coordinate=value))
                self.assertEqual(row["quality_flags"], ["coordinate_invalid", "lat_invalid", "lng_invalid"])
        row = normalize_shop(bronze_row(coordinate={"latitude": 0, "longitude": 0}))
        self.assertEqual(row["quality_flags"], ["coordinate_zero_pair", "lat_invalid", "lng_invalid"])
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["lng"])

    def test_price_normalizes_observed_formats_and_preserves_currency_semantics(self):
        examples = {
            "D1-100,000": ("1–100,000 VND", "VND", 1, 100000, None),
            "D1-200,000": ("1–200,000 VND", "VND", 1, 200000, None),
            "D100-200K": ("100,000–200,000 VND", "VND", 100000, 200000, None),
            "D200-300K": ("200,000–300,000 VND", "VND", 200000, 300000, None),
            "100.000₫ – 200.000₫": ("100,000–200,000 VND", "VND", 100000, 200000, None),
            "$$": ("$$", None, None, None, 2),
        }
        for raw, clean in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_price(raw), clean)
        for invalid in ("", "D419884", "100-200K", "USD 10-20", "D300-200K", "D1-1,20",
                        "D1-9223372036854775808", "$$$$$"):
            with self.subTest(raw=invalid):
                self.assertIsNone(parse_price(invalid))
        row = normalize_shop(bronze_row(price_range=None))
        self.assertIn("price_range_missing", row["quality_flags"])

    def test_checks_links_and_normalizes_phones_locally(self):
        row = normalize_shop(bronze_row(
            phone="0084 (236) 3888 384", link="https://maps.app.goo.gl/example",
        ))
        self.assertEqual(row["phone"], "+842363888384")
        self.assertEqual(row["maps_link"], "https://maps.app.goo.gl/example")
        for raw in ("https://user:pass@example.com", "https://ex ample.com", "https://example.com:99999",
                    "https://[::1]ignored:443/", "https://example.com:/",
                    "https://example.com/%ZZ", "https://", "http://example.com\\fake"):
            with self.subTest(raw=raw):
                row = normalize_shop(bronze_row(website=raw))
                self.assertIsNone(row["website"])
                self.assertIn("website_invalid", row["quality_flags"])

    def test_csv_preview_reports_file_hash_and_logical_record_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "shops.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=[*CSV_FIELDS.values(), "about"])
                writer.writeheader()
                source = bronze_row(
                    place_id="ChIJsample", data_id="0x1:0x2", name="Mây\nCafé",
                    coordinate=json.dumps({"latitude": 16.05, "longitude": 108.24}),
                )
                writer.writerow({alias: source[field] for field, alias in CSV_FIELDS.items()}
                                | {"about": '{"untouched": true}'})

            before = path.read_bytes()
            result = preview_csv(path)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["source"], {
            "file_name": "shops.csv", "sha256": hashlib.sha256(before).hexdigest(),
        })
        self.assertEqual(result["rows"][0]["source_row"], 1)
        self.assertEqual(result["rows"][0]["name"], "Mây Café")
        self.assertEqual(result["summary"]["row_count"], 1)
        self.assertEqual(result["summary"]["distinct_source_keys"], 1)
        self.assertEqual(result["summary"]["quality_flags"], {})


class ShopPublicationTests(unittest.TestCase):
    def test_pending_runs_use_exact_rpc_shapes_and_preserve_row_counts_on_retry(self):
        saved = {}
        run_ids = [42, 73]

        def transport(function, payload):
            self.assertEqual(payload["p_repository"], REPOSITORY)
            if function == "list_pending_shop_silver_runs":
                self.assertEqual(payload, {
                    "p_repository": REPOSITORY, "p_transform_version": TRANSFORM_VERSION,
                })
                return [run_id for run_id in run_ids if run_id not in saved]

            run_id = payload["p_run_id"]
            if function == "read_bronze_for_shop_silver":
                self.assertEqual(set(payload), {"p_repository", "p_run_id"})
                return bronze_snapshot(run_id)

            self.assertEqual(function, "publish_shop_silver")
            self.assertEqual(set(payload), {"p_repository", "p_run_id", "p_transform_version", "p_rows"})
            self.assertEqual(payload["p_transform_version"], TRANSFORM_VERSION)
            rows = payload["p_rows"]
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["source_key"] for row in rows], ["place_id:ChIJsample_42", "data_id:0xabc:0xdef"])
            self.assertTrue(all(set(row) == {*SHOP_FIELDS, "quality_flags"} for row in rows))
            self.assertEqual(rows[0]["review_count"], 1200)
            self.assertEqual(rows[1]["data_id"], "0xabc:0xdef")
            if run_id in saved:
                self.assertEqual(rows, saved[run_id])
                status = "already_published"
            else:
                saved[run_id] = deepcopy(rows)
                status = "published"
            return {"run_id": run_id, "status": status, "row_count": len(saved[run_id])}

        rpc = Mock(side_effect=transport)
        initial = list(process_bronze(rpc, REPOSITORY))
        self.assertEqual([result["run_id"] for result in initial], run_ids)
        self.assertEqual([result["status"] for result in initial], ["published", "published"])
        self.assertEqual(initial[0]["quality_flags"], {
            "identity_uses_data_id": 1, "kgmid_missing": 1, "price_range_missing": 1,
        })
        self.assertEqual(list(process_bronze(rpc, REPOSITORY)), [])
        repeated = list(process_bronze(rpc, REPOSITORY, 42))
        self.assertEqual([(result["status"], result["row_count"]) for result in repeated],
                         [("already_published", 2)])
        self.assertEqual(sum(len(rows) for rows in saved.values()), 4)

    def test_refuses_duplicates_partial_runs_and_wrong_schema_before_publish(self):
        cases = (
            (bronze_snapshot(rows=[bronze_row(), bronze_row()]), "duplicate Bronze snapshot keys"),
            (bronze_snapshot(row_count=3), "incomplete Bronze shop snapshot"),
            (bronze_snapshot(rows=[bronze_row(), {"source_key": "place_id:broken"}]),
             "incomplete Bronze shop snapshot"),
            (bronze_snapshot(mode="smoke"), "unsupported Bronze run or shop schema"),
            (bronze_snapshot(schema_version=2), "unsupported Bronze run or shop schema"),
            (bronze_snapshot(run_id=43), "unsupported Bronze run or shop schema"),
        )
        for value, error in cases:
            with self.subTest(error=error, snapshot=value):
                rpc = Mock(return_value=value)
                with self.assertRaisesRegex(ValueError, error):
                    list(process_bronze(rpc, REPOSITORY, 42))
                rpc.assert_called_once_with("read_bronze_for_shop_silver", {
                    "p_repository": REPOSITORY, "p_run_id": 42,
                })

    def test_refuses_invalid_pending_ids_or_publication_receipt(self):
        for response in ([42, 42], [73, 42], [True], ["42"], {"run_id": 42}):
            with self.subTest(response=response):
                rpc = Mock(return_value=response)
                with self.assertRaisesRegex(ValueError, "invalid pending run IDs"):
                    list(process_bronze(rpc, REPOSITORY))

        rpc = Mock(side_effect=[
            bronze_snapshot(), {"run_id": 42, "status": "published", "row_count": 1},
        ])
        with self.assertRaisesRegex(ValueError, "Silver publication could not be confirmed"):
            list(process_bronze(rpc, REPOSITORY, 42))
        self.assertEqual(rpc.call_count, 2)

    def test_an_invalid_run_does_not_prevent_later_eligible_runs(self):
        rpc = Mock(side_effect=[
            [42, 73], bronze_snapshot(row_count=99), bronze_snapshot(73),
            {"run_id": 73, "status": "published", "row_count": 2},
        ])
        results = process_bronze(rpc, REPOSITORY)
        self.assertEqual(next(results)["run_id"], 73)
        with self.assertRaisesRegex(ValueError, "Unpublished Bronze runs 42"):
            next(results)
        self.assertEqual(rpc.call_count, 4)


if __name__ == "__main__":
    unittest.main()
