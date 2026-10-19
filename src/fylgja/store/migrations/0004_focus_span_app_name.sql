-- 0004_focus_span_app_name: a focus span keeps the app's name (docs/decisions/0059).
--
-- focus_spans kept only the bundle id, so a closed window's place and an app item with nothing
-- open were named by it. The new column is filled for stored spans from windows_snapshot: the
-- newest name recorded for the span's bundle id, else, for a span without one, for its window.
-- AeroSpace's NULL-APP-BUNDLE-ID placeholder is cleared first, in both tables, as the parser now
-- reads it as no bundle id. The migrations runner wraps this file in a transaction and sets
-- PRAGMA user_version, so a failure leaves the database at version 3.

ALTER TABLE focus_spans ADD COLUMN app_name TEXT;

UPDATE focus_spans SET app_bundle_id = NULL WHERE app_bundle_id = 'NULL-APP-BUNDLE-ID';
UPDATE windows_snapshot SET app_bundle_id = NULL WHERE app_bundle_id = 'NULL-APP-BUNDLE-ID';

UPDATE focus_spans
SET app_name = named.app_name
FROM (
    SELECT app_bundle_id, app_name
    FROM (
        SELECT app_bundle_id, app_name,
               ROW_NUMBER() OVER (
                   PARTITION BY app_bundle_id ORDER BY valid_from DESC, id DESC
               ) AS newest
        FROM windows_snapshot
        WHERE app_bundle_id IS NOT NULL AND app_name IS NOT NULL AND app_name != ''
    )
    WHERE newest = 1
) AS named
WHERE focus_spans.app_bundle_id = named.app_bundle_id;

UPDATE focus_spans
SET app_name = named.app_name
FROM (
    SELECT window_id, app_name
    FROM (
        SELECT window_id, app_name,
               ROW_NUMBER() OVER (
                   PARTITION BY window_id ORDER BY valid_from DESC, id DESC
               ) AS newest
        FROM windows_snapshot
        WHERE app_name IS NOT NULL AND app_name != ''
    )
    WHERE newest = 1
) AS named
WHERE focus_spans.app_bundle_id IS NULL AND focus_spans.window_id = named.window_id;
