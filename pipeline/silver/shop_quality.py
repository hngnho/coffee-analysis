"""Deterministic normalization of one café observation."""

from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal
from urllib.parse import urlsplit

TRANSFORM_VERSION = "shop-v1"
BIGINT_MAX = 2**63 - 1
PLACE_ID = re.compile(r"[A-Za-z0-9_-]+")
DATA_ID = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+", re.IGNORECASE)
KGMID = re.compile(r"/[gm]/[A-Za-z0-9_]+")
DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
WHOLE = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,3}(?:\.[0-9]{3})+)"
AMOUNT = rf"({WHOLE})\s*(k)?"
CURRENCY = r"(?:vnd|₫|đ|d)"
VND_INTERVAL = re.compile(
    rf"({CURRENCY})?\s*{AMOUNT}\s*({CURRENCY})?\s*[-–—]\s*"
    rf"({CURRENCY})?\s*{AMOUNT}\s*({CURRENCY})?",
    re.IGNORECASE,
)
GOOGLE_ROOTS = {"google.com", "google.com.vn", "google.vn"}
PRICE_TIER = re.compile(r"\$\$?\$?\$?")


def is_missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def clean_text(value):
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", " ".join(value.split()))
    if not value or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        return None
    return value


def clean_category(value):
    value = clean_text(value)
    return unicodedata.normalize("NFC", value.casefold()) if value else None


def clean_address(value):
    value = clean_text(value)
    return re.sub(r"\s*,\s*", ", ", value).strip() if value else None


def parse_decimal(value):
    if type(value) not in (int, float, str, Decimal):
        return None
    value = str(value).strip()
    if not DECIMAL.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except ArithmeticError:
        return None


def number_in_bounds(value, minimum, maximum):
    number = parse_decimal(value)
    return float(number) if number is not None and minimum <= number <= maximum else None


def parse_review_count(value):
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+", value):
            value = value.replace(",", "")
    number = parse_decimal(value)
    if number is not None and 0 <= number <= BIGINT_MAX and number == number.to_integral_value():
        return int(number)
    return None


def parse_coordinate(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    return value if isinstance(value, dict) else None


def clean_identifier(value, pattern):
    value = value.strip() if isinstance(value, str) else ""
    return value if pattern.fullmatch(value) else None


def clean_phone(value):
    value = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"\+?[0-9().\s-]+", value):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if value.startswith("+"):
        international = digits
    elif digits.startswith("00"):
        international = digits[2:]
    elif digits.startswith("0"):
        international = f"84{digits[1:]}"
    else:
        return None

    if international.startswith("84"):
        valid = re.fullmatch(r"84[1-9][0-9]{8,9}", international)
    else:
        valid = re.fullmatch(r"[1-9][0-9]{6,14}", international)
    return f"+{international}" if valid else None


def clean_url(value, *, maps=False):
    value = value.strip() if isinstance(value, str) else ""
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value):
        return None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # Reject malformed and out-of-range ports.
        if hostname is None or parts.username is not None or parts.password is not None:
            return None
        ascii_host = hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.netloc.startswith("["):
        if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc):
            return None
    else:
        domains = ascii_host.rstrip(".").split(".")
        if parts.netloc.endswith(":") or not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in domains
        ):
            return None

    if maps:
        host = ascii_host.rstrip(".").lower()
        path = parts.path.split("/")
        regular_host = host.removeprefix("www.") in GOOGLE_ROOTS and path[1:2] == ["maps"]
        maps_host = host.startswith("maps.") and host[5:] in GOOGLE_ROOTS
        short_link = host == "maps.app.goo.gl" or (host == "goo.gl" and path[1:2] == ["maps"])
        if not (regular_host or maps_host or short_link):
            return None
    return parts.geturl()


def parse_price(value):
    value = clean_text(value)
    if not value:
        return None
    if PRICE_TIER.fullmatch(value):
        return value, None, None, None, len(value)

    match = VND_INTERVAL.fullmatch(value)
    if not match:
        return None
    prefix, lower, lower_k, first_suffix, second_prefix, upper, upper_k, suffix = match.groups()
    if not any((prefix, first_suffix, second_prefix, suffix)):
        return None
    # In shorthand like D100-200K, K applies to both plain numbers.
    shared_k = upper_k and lower.isdigit() and upper.isdigit()
    low = bounded_price(lower, lower_k or shared_k)
    high = bounded_price(upper, upper_k)
    if low is None or high is None or low > high:
        return None
    return f"{low:,}–{high:,} VND", "VND", low, high, None


def bounded_price(amount, thousands):
    digits = amount.replace(",", "").replace(".", "").lstrip("0") or "0"
    if len(digits) > 19:
        return None
    amount = int(digits) * (1000 if thousands else 1)
    return amount if amount <= BIGINT_MAX else None


def normalize_shop(row, *, csv_source=False):
    """Return typed shop values and stable reason codes for missing or invalid values."""
    flags = set()

    def field(name, value, parser):
        if is_missing(value):
            flags.add(f"{name}_missing")
            return None
        result = parser(value)
        if result is None:
            flags.add(f"{name}_invalid")
        return result

    if csv_source:
        place_id = field("place_id", row.get("place_id"), lambda v: clean_identifier(v, PLACE_ID))
        data_id = field("data_id", row.get("data_id"), lambda v: clean_identifier(v, DATA_ID))
        source_key = f"place_id:{place_id}" if place_id else f"data_id:{data_id}" if data_id else None
        if source_key is None:
            flags.add("identity_missing")
    else:
        source_key = row["source_key"]
        prefix, _, identifier = source_key.partition(":")
        place_id = identifier if prefix == "place_id" and PLACE_ID.fullmatch(identifier) else None
        data_id = identifier if prefix == "data_id" and DATA_ID.fullmatch(identifier) else None
        if not (place_id or data_id):
            flags.add("source_key_invalid")

    if not place_id and data_id:
        flags.add("identity_uses_data_id")
    kgmid = field("kgmid", row.get("id"), lambda v: clean_identifier(v, KGMID))

    raw_coordinates = row.get("coordinate")
    coordinate = field("coordinate", raw_coordinates, parse_coordinate)
    lat = field("lat", (coordinate or {}).get("latitude"), lambda v: number_in_bounds(v, -90, 90))
    lng = field("lng", (coordinate or {}).get("longitude"), lambda v: number_in_bounds(v, -180, 180))
    if coordinate is None and not is_missing(raw_coordinates):
        flags.difference_update({"lat_missing", "lng_missing"})
        flags.update({"lat_invalid", "lng_invalid"})
    if lat == 0 and lng == 0:
        lat = lng = None
        flags.update({"coordinate_zero_pair", "lat_invalid", "lng_invalid"})

    review_count = field("review_count", row.get("review"), parse_review_count)
    raw_rating = row.get("rating")
    if parse_decimal(raw_rating) == 0 and review_count == 0:
        rating = None
        flags.update({"rating_missing", "rating_unrated"})
    else:
        rating = field("rating", raw_rating, lambda v: number_in_bounds(v, 1, 5))
    if rating is not None and review_count == 0:
        flags.add("rating_with_zero_reviews")
    if rating is None and review_count is not None and review_count > 0:
        flags.add("positive_reviews_without_valid_rating")

    price = field("price_range", row.get("price_range"), parse_price)
    display, currency, low, high, tier = price or (None, None, None, None, None)
    result = {
        "source_key": source_key,
        "place_id": place_id,
        "data_id": data_id,
        "kgmid": kgmid,
        "name": field("name", row.get("name"), clean_text),
        "main_category": field("main_category", row.get("main_category"), clean_category),
        "address": field("address", row.get("address"), clean_address),
        "lat": lat,
        "lng": lng,
        "rating": rating,
        "review_count": review_count,
        "price_range": display,
        "price_currency": currency,
        "price_min": low,
        "price_max": high,
        "price_tier": tier,
        "phone": field("phone", row.get("phone"), clean_phone),
        "website": field("website", row.get("website"), clean_url),
        "maps_link": field("maps_link", row.get("link"), lambda v: clean_url(v, maps=True)),
    }
    return result | {"quality_flags": sorted(flags)}
