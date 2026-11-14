"""Preview the shop transform from CSV or publish weekly Bronze runs to Supabase."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from pathlib import Path

from pipeline.silver.shop_quality import BIGINT_MAX, TRANSFORM_VERSION, normalize_shop

CSV_FIELDS = {
    "place_id": "place_id",
    "data_id": "data_id",
    "id": "kgmid",
    "name": "name",
    "main_category": "main_category",
    "address": "address",
    "coordinate": "coordinates",
    "rating": "rating",
    "review": "reviews",
    "price_range": "price_range",
    "phone": "phone",
    "website": "website",
    "link": "link",
}
SHOP_FIELDS = (
    "source_key", "place_id", "data_id", "kgmid", "name", "main_category", "address",
    "lat", "lng", "rating", "review_count", "price_range", "price_currency",
    "price_min", "price_max", "price_tier", "phone", "website", "maps_link",
)
SOURCE_FIELDS = (set(CSV_FIELDS) - {"place_id", "data_id"}) | {"source_key"}


def normalize_rows(rows, *, csv_source=False):
    cleaned = [normalize_shop(row, csv_source=csv_source) for row in rows]
    identifiers = ("source_key", "place_id", "data_id", "kgmid")
    frequencies = {key: Counter(row[key] for row in cleaned if row[key]) for key in identifiers}
    for row in cleaned:
        duplicates = {f"{key}_duplicate" for key, counts in frequencies.items() if counts[row[key]] > 1}
        row["quality_flags"] = sorted(duplicates.union(row["quality_flags"]))
    return cleaned


def summarize(rows):
    return {
        "row_count": len(rows),
        "distinct_source_keys": len({row["source_key"] for row in rows if row["source_key"]}),
        "non_null": {key: sum(row[key] is not None for row in rows) for key in SHOP_FIELDS},
        "quality_flags": dict(sorted(Counter(
            flag for row in rows for flag in row["quality_flags"]
        ).items())),
    }


def preview_csv(path):
    raw = path.read_bytes()
    with io.StringIO(raw.decode("utf-8-sig"), newline="") as stream:
        csv.field_size_limit(max(csv.field_size_limit(), 10_000_000))
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames
        if (not headers or len(headers) != len(set(headers))
                or not set(CSV_FIELDS.values()).issubset(headers)):
            raise ValueError("CSV is missing required shop headers or contains duplicate headers")

        rows = []
        for record_number, row in enumerate(reader, 1):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV record {record_number} does not match the header width")
            rows.append({target: row[column] for target, column in CSV_FIELDS.items()})

    if not rows:
        raise ValueError("CSV contains no shop records")
    cleaned = normalize_rows(rows, csv_source=True)
    return {
        "source": {"file_name": path.name, "sha256": hashlib.sha256(raw).hexdigest()},
        "transform_version": TRANSFORM_VERSION,
        "summary": summarize(cleaned),
        "rows": [{"source_row": number, **row} for number, row in enumerate(cleaned, 1)],
    }


def valid_run_id(value):
    return type(value) is int and 0 < value <= BIGINT_MAX


def process_bronze(rpc, repository, run_id=None):
    """Process the selected run or each currently pending run, one transaction per run."""
    scope = {"p_repository": repository, "p_transform_version": TRANSFORM_VERSION}
    if run_id is None:
        run_ids = rpc("list_pending_shop_silver_runs", scope)
        if (not isinstance(run_ids, list) or not all(map(valid_run_id, run_ids))
                or run_ids != sorted(set(run_ids))):
            raise ValueError("Supabase returned invalid pending run IDs")
    else:
        run_ids = [run_id]

    failures = []
    for current_id in run_ids:
        try:
            yield process_bronze_run(rpc, scope, current_id)
        except ValueError as error:
            if run_id is not None:
                raise
            failures.append(f"{current_id} ({error})")
    if failures:
        raise ValueError("Unpublished Bronze runs " + ", ".join(failures))


def process_bronze_run(rpc, scope, run_id):
    selector = {"p_repository": scope["p_repository"], "p_run_id": run_id}
    snapshot = rpc("read_bronze_for_shop_silver", selector)
    if (not isinstance(snapshot, dict) or snapshot.get("run_id") != run_id
            or snapshot.get("mode") != "weekly" or snapshot.get("schema_version") != 1):
        raise ValueError("unsupported Bronze run or shop schema")
    rows = snapshot.get("rows")
    if (not isinstance(rows, list) or not rows or len(rows) != snapshot.get("row_count")
            or any(not isinstance(row, dict) or not SOURCE_FIELDS.issubset(row)
                   or not isinstance(row["source_key"], str) or not row["source_key"].strip()
                   for row in rows)):
        raise ValueError("incomplete Bronze shop snapshot")
    if len({row["source_key"] for row in rows}) != len(rows):
        raise ValueError("duplicate Bronze snapshot keys")

    cleaned = normalize_rows(rows)
    result = rpc("publish_shop_silver", scope | selector | {"p_rows": cleaned})
    if (not isinstance(result, dict) or result.get("run_id") != run_id
            or result.get("status") not in ("published", "already_published")
            or result.get("row_count") != len(cleaned)):
        raise ValueError("Silver publication could not be confirmed")
    return result | {"quality_flags": summarize(cleaned)["quality_flags"]}


def parse_run_id(value):
    try:
        run_id = int(value)
    except ValueError:
        run_id = None
    if not value.isascii() or not value.isdigit() or not valid_run_id(run_id):
        raise argparse.ArgumentTypeError("Bronze run ID must be a positive PostgreSQL bigint")
    return run_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--csv", type=Path, help="Preview a desktop scraper CSV locally")
    modes.add_argument("--repository", help="Bronze GitHub repository name, OWNER/REPO")
    parser.add_argument("--output", type=Path, help="Write a new JSON preview file with --csv")
    parser.add_argument("--bronze-run-id", type=parse_run_id,
                        help="Process this weekly Bronze run instead of all pending runs")
    args = parser.parse_args()
    if args.csv and args.bronze_run_id is not None:
        parser.error("--bronze-run-id requires --repository")
    if args.output and not args.csv:
        parser.error("--output requires --csv")

    if args.csv:
        try:
            preview = preview_csv(args.csv)
            if args.output:
                payload = json.dumps(preview, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
                with args.output.open("x", encoding="utf-8") as output:
                    output.write(payload)
            print(json.dumps({key: value for key, value in preview.items() if key != "rows"}))
        except (ValueError, OSError, csv.Error) as error:
            print(f"CSV preview failed: {error}", file=sys.stderr)
            return 1
        return 0

    # Load the Bronze transport only for Supabase mode. Reuse its hosted URL check,
    # fixed error codes, no-redirect policy, and transient HTTP retry handling.
    from pipeline.bronze.collect import CollectionError, hosted_supabase_url, request_json, required

    try:
        url = hosted_supabase_url(required("SUPABASE_URL"))
        key = required("SUPABASE_SECRET_KEY")
        if not key.startswith("sb_secret_"):
            raise CollectionError("invalid_supabase_secret_key")

        def rpc(function, payload):
            try:
                return request_json("supabase", f"{url}/rest/v1/rpc/{function}", {"apikey": key}, payload)
            except CollectionError as error:
                if str(error) in {"supabase_http_400", "supabase_http_404", "supabase_http_409",
                                  "supabase_http_422"}:
                    raise ValueError(f"{function} failed ({error})") from None
                raise

        count = 0
        for result in process_bronze(rpc, args.repository, args.bronze_run_id):
            print(json.dumps(result), flush=True)
            count += 1
        if not count:
            print("No unpublished weekly Bronze runs for this shop transform version.")
    except (CollectionError, ValueError) as error:
        print(f"Shop Silver processing failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
