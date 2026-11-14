# Café and shop data quality report

The source is `cafe-in-an-thuong-da-nang.csv` (5,901,395 bytes, SHA-256 `fbfd920e81075d0df0768a8d91bc72f4a0d5357002fdb870916755093ab8b22a`). A UTF-8 CSV reader using the header and quoted records read **115 data rows and 72 columns**, with no short or overlong rows. Counts use data rows as the denominator. Every CSV cell starts as text. Empty cells count as missing, and the structured address check counts explicit JSON `null` values separately from empty CSV cells. Examples use public shop names from the source.

## Source profile

| Source fields | Present | Missing | Observations |
| --- | ---: | ---: | --- |
| `place_id`, `cid`, `data_id`, `kgmid` | 115 each | 0 each | Each identifier has 115 distinct values. There are zero identical full rows and zero duplicate IDs. Keep identifiers as text. All 115 CIDs exceed JavaScript’s safe integer limit, and 58 exceed the PostgreSQL signed `bigint` maximum. |
| `name` | 115 | 0 | 115 distinct names, including when compared case-insensitively. All are already NFC with clean whitespace. One name, `46 An Thuong 18, Ngu Hanh Son, Da Nang`, resembles an address. The source identifies it as a `Coffee shop` and supplies its own Place ID. |
| `main_category`, `categories` | 115 each | 0 each | 26 distinct main labels. Every category list parses as a nonempty JSON array, has no repeated labels under case-insensitive comparison, and starts with the corresponding `main_category`. There are 67 distinct labels across the arrays. |
| `address`, `detailed_address` | 115 each | 0 each | 113 distinct flat addresses and 113 distinct structured objects. All 115 objects parse and carry `city = Da Nang` and `country_code = VN`. |
| `detailed_address.postal_code` | 97 | 18 | All 97 populated codes agree with the flat address. Flat addresses include a code in all 115 cases, including the 18 whose structured code is null. Codes use five or six digits, including `02363` with a leading zero. `Roots Plant-Based Cafe` has null structured postal code and `550000` in its flat address. |
| `coordinates` | 115 | 0 | All 115 parse into JSON objects with numeric, finite latitude and longitude within WGS84 bounds. Latitude ranges from `16.0403175` to `16.0591178`. Longitude ranges from approximately `108.238273` to `108.2484905`. There are 115 distinct coordinate pairs. |
| `rating` | 115 raw | 0 raw | 113 ratings in the 1–5 scale, with observed values from `2` to `5`. Both `0` ratings occur alongside `reviews = 0`, for `Cafe Huy Hoang` and `HEM CA PHE`. |
| `reviews` | 115 | 0 | 115 nonnegative integer strings, from `0` to `9984` (`XLIII Specialty Coffee`). There are 2 zero counts, and all 113 positive counts have a rating in the 1–5 scale. |
| `price_range` | 94 | 21 | Four formats. `D1-100,000` appears 65 times, `D100-200K` 26 times, `D200-300K` twice, and `D1-200,000` once. The separate `price` field contains `D419884` for `Kiwi Hotel & Apartments`, whose `price_range` is blank. |
| `phone`, `phone_international` | 97 each | 18 each | Both columns agree on all 115 rows, including absence. All populated numbers use `+84` with spaces and either 9 national digits (95 rows) or 10 (2 rows). |
| `website` | 56 | 59 | 46 HTTPS and 10 HTTP URLs. Sixteen rows have neither a website nor a phone. Website destinations include restaurant domains, social profiles and ordering links. |
| `link`, `reviews_link` | 115 each | 0 each | All use HTTPS. Each Maps link contains its row’s `data_id`. Each reviews link carries its row’s `place_id` in the `placeid` query parameter. Links were validated locally without fetching their destinations. |
| `hours`, `time_zone` | 114, 115 | 1, 0 | Every populated `hours` cell parses as a JSON array. All 115 timezone values are `Asia/Saigon`. |

### Category and identity consistency

| Main category count | Labels |
| ---: | --- |
| 53 | Coffee shop |
| 22 | Cafe |
| 7 | Restaurant |
| 4 | Bar |
| 3 | Espresso bar |
| 2 each | Bistro, Breakfast restaurant, Indian restaurant, Sports bar, Vietnamese restaurant |
| 1 each | Association / Organization, Bakery, Belgian restaurant, Coffee stand, Coworking space, Health food restaurant, Internet cafe, Irish pub, Juice shop, Mediterranean restaurant, Mexican restaurant, Poke bar, Pub, Ramen restaurant, Vegan restaurant, Vegetarian restaurant |

The query `cafe in an thuong da nang` returned establishments with a range of main categories. `Coffee shop`, `Cafe`, `Espresso bar` and `Coffee stand` account for 79 main categories and appear at least once in 90 category lists. Both measures depend on these four labels. Applying either as a filter would drop source observations.

Four rows share two street addresses. `Soul Specialty Coffee` and `Ca Phe Noi Co` share `12 An Thuong 11`. `Faifo Coffee` and `KHU PHO DU LICH AN THUONG` share `298 Vo Nguyen Giap`. Each pair has different Place IDs and different coordinate pairs. The hexadecimal tail of `data_id` converts to the same number as `cid` in all 115 records.

`about`, `images`, `featured_reviews`, `review_keywords` and `reviews_per_rating` were kept out of the quality calculations above. Their detailed contents were excluded from feature extraction and the examples.

## Verified Silver results

The local CSV preview ran with `shop-v1`. It produced 115 rows and 115 distinct source keys. Reconciliation compared all 115 generated rows with their source record.

| Check | Before | After |
| --- | --- | --- |
| Observations / distinct identities | 115 / 115 | 115 / 115, including both pairs at shared addresses |
| Names and addresses | 115 each | 115 each, text identical to the source |
| Main categories | 26 distinct labels across 115 rows | 26 distinct case-folded labels across 115 rows, including `coffee shop` (53) and `cafe` (22) |
| Valid coordinates | 115 pairs | 115 pairs with the same numeric values |
| Ratings | 115 source numbers, including two zeros | 113 numeric ratings from 2 to 5 and two nulls flagged `rating_unrated` and `rating_missing` |
| Reviews | 115 counts totaling 57,298 | 115 integer counts totaling 57,298, including both zeros |
| Shop price ranges | 94 populated, 21 blank | 94 parsed VND ranges, 21 null with `price_range_missing` |
| Phone | 97 populated, 18 blank | 97 compact international numbers, 18 null with `phone_missing` |
| Website | 56 populated, 59 blank | 56 retained links, 59 null with `website_missing` |
| Google Maps link | 115 populated | 115 retained links with source query strings |

Sixty-six observations have at least one quality flag. The preview has zero `*_invalid` flags and zero duplicate identity flags. The two unrated shops retain their original review count of zero.

| CSV price range | Parsed minimum (VND) | Parsed maximum (VND) | Rows |
| --- | ---: | ---: | ---: |
| `D1-100,000` | 1 | 100,000 | 65 |
| `D100-200K` | 100,000 | 200,000 | 26 |
| `D200-300K` | 200,000 | 300,000 | 2 |
| `D1-200,000` | 1 | 200,000 | 1 |

Silver preserves the literal lower bound of 1 VND in the 66 source ranges containing `D1`. The separate `price` column has no effect on these bounds.

A disposable PostgreSQL database loaded the 115 rows projected to the existing Bronze schema. The shop runner published all 115 rows in one run. A repeat returned `already_published`, with one publication record and 115 Silver snapshots. All Bronze rows were unchanged. Stored values matched the CSV preview, with `data_id` null because the weekly Bronze projection supplies only the Place ID key for these observations. The local check exercised the SQL RPCs. Hosted Supabase and GitHub Actions were outside this verification.
