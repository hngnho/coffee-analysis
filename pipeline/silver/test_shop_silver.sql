-- Run as the migration owner in a disposable database after the Bronze schema
-- and 006_shop_silver.sql. The fixtures roll back.
begin;

do $$
declare
    repository constant text := 'test/shop-silver';
    stamp constant timestamptz := '2026-11-13 02:00:00+00';
    bronze_rows constant jsonb := '[
        {"source_key":"place_id:ChIJsample","id":"/g/123","name":"Mây Café",
         "main_category":"Coffee shop","address":"An Thượng, Đà Nẵng",
         "coordinate":{"latitude":16.05,"longitude":108.24},
         "rating":4.7,"review":20,"price_range":"D100-200K","website":"https://example.com",
         "phone":"+84 905 123 456","link":"https://www.google.com/maps/place/example",
         "about":[{"name":"Outside this phase"}],"images":[],"featured_reviews":[],
         "review_keywords":[],"review_per_rating":{"5":20}},
        {"source_key":"data_id:0x123:0x456","name":"Another café"}
    ]';
    silver_rows constant jsonb := '[
        {"source_key":"place_id:ChIJsample","place_id":"ChIJsample",
         "kgmid":"/g/123","name":"Mây Café","lat":16.05,"lng":108.24,
         "rating":4.7,"review_count":20,"price_currency":"VND","price_min":100000,
         "price_max":200000,"quality_flags":[]},
        {"source_key":"data_id:0x123:0x456","data_id":"0x123:0x456",
         "name":"Another café","quality_flags":["rating_missing"]}
    ]';
    weekly bigint;
    later_week bigint;
    smoke bigint;
    exported jsonb;
    result jsonb;
begin
    weekly := (public.begin_bronze_collection(
        '2026-11-09', 'weekly', 'test', 2, repository, 61001, 1, 'test', 'test', stamp
    )->>'run_id')::bigint;
    smoke := (public.begin_bronze_collection(
        '2026-11-09', 'smoke', 'test', 2, repository, 61002, 1, 'test', 'test', stamp
    )->>'run_id')::bigint;
    later_week := (public.begin_bronze_collection(
        '2026-11-16', 'weekly', 'test', 2, repository, 61003, 1, 'test', 'test', stamp + interval '7 days'
    )->>'run_id')::bigint;

    if public.list_pending_shop_silver_runs(repository, 'v1') is distinct from '[]'::jsonb then
        raise exception 'an unpublished Bronze run was eligible';
    end if;

    perform public.finish_bronze_collection(weekly, bronze_rows, 2, stamp, null);
    perform public.finish_bronze_collection(smoke, bronze_rows, 2, stamp, null);
    if public.list_pending_shop_silver_runs(repository, 'v1') is distinct from jsonb_build_array(weekly) then
        raise exception 'pending discovery did not select only the published weekly run';
    end if;

    exported := public.read_bronze_for_shop_silver(repository, weekly);
    if exported->'run_id' is distinct from to_jsonb(weekly)
       or exported->>'mode' is distinct from 'weekly'
       or exported->'schema_version' is distinct from '1'::jsonb
       or exported->>'week_start' is distinct from '2026-11-09'
       or (exported->>'collected_at')::timestamptz is distinct from stamp
       or exported->'row_count' is distinct from '2'::jsonb
       or exported->'rows'->1 is distinct from '{
           "source_key":"place_id:ChIJsample","id":"/g/123","name":"Mây Café",
           "main_category":"Coffee shop","address":"An Thượng, Đà Nẵng",
           "coordinate":{"latitude":16.05,"longitude":108.24},
           "rating":4.7,"review":20,"price_range":"D100-200K","website":"https://example.com",
           "phone":"+84 905 123 456","link":"https://www.google.com/maps/place/example"
       }'::jsonb then
        raise exception 'read did not return the expected shop-only projection';
    end if;

    begin
        perform public.read_bronze_for_shop_silver('wrong/repo', weekly);
        raise exception 'a run from a different repository was exported';
    exception when sqlstate 'PT404' then
        null;
    end;
    begin
        perform public.publish_shop_silver(repository, smoke, 'v1', silver_rows);
        raise exception 'a smoke run was accepted';
    exception when sqlstate 'PT404' then
        null;
    end;
    begin
        perform public.publish_shop_silver(repository, weekly, 'v1', jsonb_build_array(silver_rows->0));
        raise exception 'an incomplete run was accepted';
    exception when invalid_parameter_value then
        null;
    end;
    begin
        perform public.publish_shop_silver(repository, weekly, 'v1',
            jsonb_build_array(silver_rows->0, silver_rows->0));
        raise exception 'duplicate source keys were accepted';
    exception when invalid_parameter_value then
        null;
    end;
    begin
        perform public.publish_shop_silver(repository, weekly, 'v1',
            jsonb_set(silver_rows, '{0,source_key}', '"place_id:unknown"'));
        raise exception 'a foreign source key was accepted';
    exception when invalid_parameter_value then
        null;
    end;
    begin
        perform public.publish_shop_silver(repository, weekly, 'v1',
            jsonb_set(silver_rows, '{0,quality_flags}', '[null]'));
        raise exception 'a non-string quality flag was accepted';
    exception when invalid_parameter_value then
        null;
    end;
    begin
        perform public.publish_shop_silver(repository, weekly, 'v1',
            jsonb_set(silver_rows, '{0,rating}', '6'));
        raise exception 'an out-of-range rating was accepted';
    exception when check_violation then
        null;
    end;
    if exists (select 1 from public.shop_silver_runs where bronze_run_id = weekly)
       or exists (select 1 from public.shop_silver_snapshots where bronze_run_id = weekly) then
        raise exception 'a failing publication left partially written data';
    end if;

    result := public.publish_shop_silver(repository, weekly, 'v1', silver_rows);
    if result is distinct from jsonb_build_object('run_id', weekly, 'status', 'published', 'row_count', 2)
       or public.list_pending_shop_silver_runs(repository, 'v1') is distinct from '[]'::jsonb then
        raise exception 'publication did not resolve the pending run';
    end if;
    result := public.publish_shop_silver(repository, weekly, 'v1',
        jsonb_set(silver_rows, '{0,name}', '"Changed on retry"'));
    if result->>'status' is distinct from 'already_published'
        or not exists (
            select 1 from public.shop_silver_snapshots
            where bronze_run_id = weekly and transform_version = 'v1'
              and source_key = 'place_id:ChIJsample' and name = 'Mây Café'
        ) then
        raise exception 'retry did not preserve the original snapshot';
    end if;

    perform public.finish_bronze_collection(later_week,
        jsonb_set(bronze_rows, '{0,review}', '21'), 2, stamp + interval '7 days', null);
    if public.list_pending_shop_silver_runs(repository, 'v2')
       is distinct from jsonb_build_array(weekly, later_week) then
        raise exception 'versioned pending discovery did not return both runs in order';
    end if;
    perform public.publish_shop_silver(repository, later_week, 'v1',
        jsonb_set(silver_rows, '{0,review_count}', '21'));
    perform public.publish_shop_silver(repository, weekly, 'v2', silver_rows);

    if (select count(*) from public.shop_silver_snapshots
        where bronze_run_id in (weekly, later_week)) <> 6
        or (select review from public.cafe_snapshots
            where run_id = weekly and source_key = 'place_id:ChIJsample') is distinct from 20::bigint then
        raise exception 'weekly/version history or immutable Bronze data changed unexpectedly';
    end if;

    if not has_function_privilege('service_role',
        'public.publish_shop_silver(text,bigint,text,jsonb)', 'execute')
        or has_function_privilege('anon', 'public.read_bronze_for_shop_silver(text,bigint)', 'execute')
        or has_function_privilege('authenticated', 'public.list_pending_shop_silver_runs(text,text)', 'execute')
        or has_table_privilege('service_role', 'public.shop_silver_snapshots', 'select')
        or has_table_privilege('service_role', 'public.cafe_snapshots', 'insert') then
        raise exception 'shop Silver grants exceed the three scoped RPCs';
    end if;
end;
$$;

rollback;
