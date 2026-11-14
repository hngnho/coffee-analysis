# Shop information Silver

This job standardizes shop observations from completed weekly Bronze snapshots. The [sample data quality report](DATA_QUALITY.md) records the source checks and the verified results.

## Source and storage

- The Bronze collector publishes `public.collection_runs` and `public.cafe_snapshots` in the Bronze Supabase project. Each observation has a `(run_id, source_key)` key. Bronze `id` is `kgmid`.
- `public.shop_silver_runs` records successful publications by Bronze run and transform version. `public.shop_silver_snapshots` stores one observation per source key for that run and version. Foreign keys point directly to the Bronze observation and run. Join the Bronze run for the source, query, week, collection time, and GitHub run metadata. Original field values stay available in Bronze.
- The database verifies that each publication covers the complete set of Bronze source keys. The publication and its completion record share one transaction. Concurrent or repeated valid publications of the same run and version return `already_published`. Every later Bronze run can add new observations, including places whose values are unchanged or which were absent from a previous snapshot. Use a new transform version to reprocess history with revised rules.
- This workflow and migration access only the Bronze project and the two new shop Silver tables. The existing menu database, its `shop` table and manual review records, and any existing production workflows have independent lifecycles. `about`, `images`, `featured_reviews`, `review_keywords` and the Bronze `review_per_rating` remain in Bronze. The shop RPC omits them from its response. Desktop-only `categories`, `detailed_address`, `cid`, and `reviews_link` informed the sample checks and are outside the weekly Bronze projection.

## Cleaning rules

| Field | Rule |
| --- | --- |
| Identity | Keep the exact Bronze `source_key`. Split recognized `place_id:` and hex `data_id:` keys into typed text identifiers. Preserve a valid `kgmid` separately, including case. A Bronze Place ID key does not carry a second Data ID column. CSV previews use supplied Place ID first, then Data ID, and can carry both as text. Flag missing or malformed identifiers and repeat identifiers within a run. Retain rows for inspection. Shared names or addresses never merge observations. |
| Name, main category, address | Apply Unicode NFC, trim and collapse whitespace. Preserve diacritics and the case of names and addresses. Case-fold category labels while preserving the source categories' distinctions. Standardize flat address comma spacing. |
| Coordinates | Read the latitude and longitude separately from the source coordinate object. Require finite decimal values within WGS84 bounds. Flag the zero pair as invalid. A valid coordinate member is kept when the other is missing or invalid. |
| Rating and review count | Accept ratings from 1 through 5 and review counts that are nonnegative integers within PostgreSQL `bigint` range. A zero rating paired with zero reviews produces a null rating with `rating_unrated`. Flag positive reviews without a valid rating and valid ratings paired with zero reviews. |
| Shop price range | Parse explicit VND intervals with common VND markers, grouping and `K` shorthand. A trailing `K` applies to both plain bounds in `D100-200K`. `D1-100,000` retains a literal lower bound of 1 VND. Bounds must be ordered and fit in `bigint`. Dollar sign strings from `$` through `$$$$` store an ordinal tier. The separate source `price` field is unused. |
| Phone | Remove presentation punctuation. Store compact international form. Convert Vietnam domestic leading `0` to `+84` and international dialing prefix `00` to `+`. Validate plausible lengths, including nine or ten Vietnam national digits. |
| Website and Maps link | Accept absolute HTTP or HTTPS URLs without credentials. Maps links must use supported Google Maps hosts or short links. Preserve path, query, and fragment. Destinations are never fetched. |

Missing source fields produce nulls and `*_missing` flags. Present but unparsable values produce nulls and `*_invalid` flags. Reason codes are stored in a sorted JSON array. The pipeline does not assign café types based on the search query. It preserves shops across all supplied categories.

## Install on Supabase and GitHub

1. Start from a branch with the weekly Bronze collector. [PR #1](https://github.com/hngnho/coffee-analysis/pull/1) contains the Bronze baseline. The shop patch was prepared against its head `c9f48dc` and applies on the Bronze-only commit `8557f48` as well. PR #1 also contains a separate OCR/About workflow. For a shop-only release, start from the Bronze-only baseline or bring in only the Bronze collection changes.
2. In the **Bronze** Supabase project's SQL Editor, apply [001_bronze.sql](../db/migrations/001_bronze.sql) if the Bronze tables are absent, then apply [006_shop_silver.sql](../db/migrations/006_shop_silver.sql). The equivalent existing `pipeline/db/bronze.sql` also satisfies the dependency. The shop migration grants only its three RPCs to `service_role`. Run it as the Supabase project owner. Its numbering follows the branch that already has migrations 001 through 005.
3. In the repository's [GitHub Actions secrets](https://github.com/hngnho/coffee-analysis/settings/secrets/actions), set `SUPABASE_URL` to the Bronze project base URL (`https://<project-ref>.supabase.co`) and `SUPABASE_SECRET_KEY` to that project's `sb_secret_…` key. If Bronze already uses these secrets, the shop workflow reuses them.
4. Once the workflow is on the default branch with Actions enabled, **Weekly shop Silver processing** runs Friday at 11:17 Vietnam time (04:17 UTC), after the published Bronze schedule of 09:00 Vietnam time. Each scheduled invocation checks for all successful weekly Bronze runs that lack a publication for the current transform version. If a late Bronze run misses the check, dispatch Silver manually or let the next weekly invocation pick it up. Failed, skipped, smoke, and other repositories' Bronze runs are ineligible.

To apply the review patch, run these from the selected Bronze checkout.

```sh
git apply --check /path/to/shop-silver.patch
git apply /path/to/shop-silver.patch
```

## Preview and run manually

From the repository root, use Python 3.10 or newer. Use an absolute path to the source file if it is outside the checkout. CSV preview prints the checksum, field counts and flags. `--output` optionally writes the full clean preview to a new JSON file, including the source filename, checksum, transform version and one-based CSV record numbers.

```sh
python -m unittest pipeline.silver.test_shop_quality
python -m pipeline.silver.shop_run --csv /path/to/cafe-in-an-thuong-da-nang.csv
python -m pipeline.silver.shop_run --csv /path/to/cafe-in-an-thuong-da-nang.csv --output /tmp/shop-preview.json
```

Select **Actions → Weekly shop Silver processing → Run workflow** with the default branch. Leave the Bronze run ID blank to process pending weekly collections or enter the `run_id` printed by a successful weekly Bronze job. The same operations run locally with the two Bronze Supabase secrets in the environment.

```sh
python -m pipeline.silver.shop_run --repository hngnho/coffee-analysis
python -m pipeline.silver.shop_run --repository hngnho/coffee-analysis --bronze-run-id 123
```

The logs report counts and flags. A failed publication can be retried with the same run ID. Each successful publication remains available even if another run fails. For a known source key, query historical versions and weeks in the Bronze project.

```sql
select b.week_start, b.collected_at, s.transform_version, s.name, s.rating,
       s.review_count, s.price_min, s.price_max, s.quality_flags
from public.shop_silver_snapshots s
join public.collection_runs b on b.run_id = s.bronze_run_id
where s.source_key = 'place_id:<Google Maps ID>'
order by b.collected_at, s.transform_version;
```

The [SQL integration fixture](test_shop_silver.sql) runs in a disposable PostgreSQL database after the Bronze and shop migrations. On plain PostgreSQL, create the `anon`, `authenticated` and `service_role` role stubs before applying the migrations.
