begin;

create table if not exists public.shop_silver_runs (
    bronze_run_id bigint not null references public.collection_runs (run_id),
    transform_version text not null check (transform_version ~ '^[A-Za-z0-9._-]{1,64}$'),
    row_count integer not null check (row_count > 0),
    published_at timestamptz not null default clock_timestamp(),
    primary key (bronze_run_id, transform_version)
);

create table if not exists public.shop_silver_snapshots (
    bronze_run_id bigint not null,
    transform_version text not null,
    source_key text not null,
    place_id text,
    data_id text,
    kgmid text,
    name text,
    main_category text,
    address text,
    lat numeric check (lat between -90 and 90),
    lng numeric check (lng between -180 and 180),
    rating numeric check (rating between 1 and 5),
    review_count bigint check (review_count >= 0),
    price_range text,
    price_currency text check (price_currency ~ '^[A-Z]{3}$'),
    price_min bigint check (price_min >= 0),
    price_max bigint check (price_max >= 0),
    price_tier smallint check (price_tier between 1 and 4),
    phone text,
    website text,
    maps_link text,
    quality_flags jsonb not null check (jsonb_typeof(quality_flags) = 'array'),
    primary key (bronze_run_id, transform_version, source_key),
    foreign key (bronze_run_id, transform_version)
        references public.shop_silver_runs (bronze_run_id, transform_version),
    foreign key (bronze_run_id, source_key)
        references public.cafe_snapshots (run_id, source_key),
    check (price_min <= price_max)
);

create index if not exists shop_silver_snapshots_source_history
    on public.shop_silver_snapshots (source_key, bronze_run_id, transform_version);

alter table public.shop_silver_runs enable row level security;
alter table public.shop_silver_snapshots enable row level security;
revoke all on public.shop_silver_runs, public.shop_silver_snapshots
    from public, anon, authenticated, service_role;

create or replace function public.list_pending_shop_silver_runs(
    p_repository text,
    p_transform_version text
) returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
    if p_repository is null or btrim(p_repository) = ''
        or p_transform_version is null or p_transform_version !~ '^[A-Za-z0-9._-]{1,64}$' then
        raise exception 'repository and valid transform version are required' using errcode = '22023';
    end if;

    return (
        select coalesce(jsonb_agg(b.run_id order by b.run_id), '[]'::jsonb)
        from public.collection_runs b
        where b.github_repository = p_repository
          and b.mode = 'weekly'
          and b.status = 'succeeded'
          and not exists (
              select 1
              from public.shop_silver_runs sr
              where sr.bronze_run_id = b.run_id
                and sr.transform_version = p_transform_version
          )
    );
end;
$$;

create or replace function public.read_bronze_for_shop_silver(
    p_repository text,
    p_run_id bigint
) returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
    source_run public.collection_runs;
    snapshots jsonb;
    snapshot_count integer;
begin
    select * into source_run
    from public.collection_runs b
    where b.run_id = p_run_id
      and b.github_repository = p_repository
      and b.mode = 'weekly'
      and b.status = 'succeeded';

    if not found then
        raise exception 'published weekly Bronze run not found' using errcode = 'PT404';
    end if;

    select count(*), jsonb_agg(to_jsonb(s) order by s.source_key)
    into snapshot_count, snapshots
    from (
        select source_key, id, name, main_category, address, coordinate,
               rating, review, price_range, website, phone, link
        from public.cafe_snapshots
        where run_id = source_run.run_id
    ) s;

    if snapshot_count <> source_run.row_count then
        raise exception 'Bronze row count differs from published count';
    end if;

    return jsonb_build_object(
        'run_id', source_run.run_id,
        'mode', source_run.mode,
        'source', source_run.source,
        'schema_version', source_run.schema_version,
        'week_start', source_run.week_start,
        'collected_at', source_run.collected_at,
        'row_count', snapshot_count,
        'rows', snapshots
    );
end;
$$;

create or replace function public.publish_shop_silver(
    p_repository text,
    p_run_id bigint,
    p_transform_version text,
    p_rows jsonb
) returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
    expected_count integer;
    distinct_count integer;
    inserted_count integer;
begin
    if p_transform_version is null or p_transform_version !~ '^[A-Za-z0-9._-]{1,64}$' then
        raise exception 'valid transform version is required' using errcode = '22023';
    end if;

    select b.row_count into expected_count
    from public.collection_runs b
    where b.run_id = p_run_id
      and b.github_repository = p_repository
      and b.mode = 'weekly'
      and b.status = 'succeeded';

    if not found then
        raise exception 'published weekly Bronze run not found' using errcode = 'PT404';
    end if;

    if jsonb_typeof(p_rows) is distinct from 'array' then
        raise exception 'Silver rows must be a JSON array' using errcode = '22023';
    end if;

    if jsonb_array_length(p_rows) <> expected_count or expected_count <> (
        select count(*) from public.cafe_snapshots s where s.run_id = p_run_id
    ) then
        raise exception 'Silver rows must cover the complete Bronze run' using errcode = '22023';
    end if;

    if exists (
        select 1
        from jsonb_array_elements(p_rows) as r(snapshot)
        where jsonb_typeof(r.snapshot) is distinct from 'object'
           or jsonb_typeof(r.snapshot -> 'source_key') is distinct from 'string'
           or jsonb_typeof(r.snapshot -> 'quality_flags') is distinct from 'array'
    ) then
        raise exception 'each row requires a source key string and a quality flags array'
            using errcode = '22023';
    end if;

    if exists (
        select 1
        from jsonb_array_elements(p_rows) as r(snapshot)
        cross join lateral jsonb_array_elements(r.snapshot -> 'quality_flags') as f(flag)
        where jsonb_typeof(f.flag) <> 'string'
    ) then
        raise exception 'quality flags must contain strings' using errcode = '22023';
    end if;

    select count(distinct r.snapshot ->> 'source_key') into distinct_count
    from jsonb_array_elements(p_rows) as r(snapshot)
    join public.cafe_snapshots b
      on b.run_id = p_run_id and b.source_key = r.snapshot ->> 'source_key';

    if distinct_count <> expected_count then
        raise exception 'Silver source keys must match the Bronze run without duplicates'
            using errcode = '22023';
    end if;

    -- The primary key waits for any concurrent writer of this run/version.
    -- If that transaction rolls back, this transaction can publish the snapshot.
    insert into public.shop_silver_runs (bronze_run_id, transform_version, row_count)
    values (p_run_id, p_transform_version, expected_count)
    on conflict (bronze_run_id, transform_version) do nothing;

    if not found then
        return jsonb_build_object(
            'run_id', p_run_id, 'status', 'already_published', 'row_count', expected_count
        );
    end if;

    insert into public.shop_silver_snapshots (
        bronze_run_id, transform_version, source_key, place_id, data_id, kgmid,
        name, main_category, address, lat, lng, rating, review_count, price_range,
        price_currency, price_min, price_max, price_tier, phone, website, maps_link,
        quality_flags
    )
    select p_run_id, p_transform_version, r.source_key, r.place_id, r.data_id, r.kgmid,
           r.name, r.main_category, r.address, r.lat, r.lng, r.rating, r.review_count,
           r.price_range, r.price_currency, r.price_min, r.price_max, r.price_tier,
           r.phone, r.website, r.maps_link, r.quality_flags
    from jsonb_to_recordset(p_rows) as r(
        source_key text, place_id text, data_id text, kgmid text, name text,
        main_category text, address text, lat numeric, lng numeric, rating numeric,
        review_count bigint, price_range text, price_currency text, price_min bigint,
        price_max bigint, price_tier smallint, phone text, website text, maps_link text,
        quality_flags jsonb
    );
    get diagnostics inserted_count = row_count;

    return jsonb_build_object(
        'run_id', p_run_id, 'status', 'published', 'row_count', inserted_count
    );
end;
$$;

revoke all on function
    public.list_pending_shop_silver_runs(text, text),
    public.read_bronze_for_shop_silver(text, bigint),
    public.publish_shop_silver(text, bigint, text, jsonb)
    from public, anon, authenticated, service_role;

grant execute on function
    public.list_pending_shop_silver_runs(text, text),
    public.read_bronze_for_shop_silver(text, bigint),
    public.publish_shop_silver(text, bigint, text, jsonb)
    to service_role;

notify pgrst, 'reload schema';

commit;
