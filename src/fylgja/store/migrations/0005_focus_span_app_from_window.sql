-- 0005_focus_span_app_from_window: a span focused before the window list showed its window gets
-- the window's app (docs/decisions/0061).
--
-- The focus event of a window the AeroSpace collector had not listed yet carried no app, so its
-- span was stored without a bundle id and without a name; 0004 filled only the name. Such a span
-- takes the bundle id and the name of the newest windows_snapshot row of its window whose
-- validity overlaps the span. A value the span has is kept. A span still without a bundle id but
-- with a name (its window closed before any list showed it) then takes the newest bundle id
-- recorded for that app name. The migrations runner wraps this file in a transaction and sets
-- PRAGMA user_version, so a failure leaves the database at version 4.

UPDATE focus_spans
SET app_bundle_id = named.app_bundle_id,
    app_name = COALESCE(focus_spans.app_name, named.app_name)
FROM (
    SELECT span_id, app_bundle_id, app_name
    FROM (
        SELECT s.id AS span_id, w.app_bundle_id, w.app_name,
               ROW_NUMBER() OVER (
                   PARTITION BY s.id ORDER BY w.valid_from DESC, w.id DESC
               ) AS newest
        FROM focus_spans AS s
        JOIN windows_snapshot AS w ON w.window_id = s.window_id
        WHERE s.app_bundle_id IS NULL
          AND (w.app_bundle_id IS NOT NULL OR (w.app_name IS NOT NULL AND w.app_name != ''))
          AND w.valid_from <= COALESCE(s.ended_at, '9999')
          AND (w.valid_to IS NULL OR w.valid_to >= s.started_at)
    )
    WHERE newest = 1
) AS named
WHERE focus_spans.id = named.span_id;

UPDATE focus_spans
SET app_bundle_id = named.app_bundle_id
FROM (
    SELECT app_name, app_bundle_id
    FROM (
        SELECT app_name, app_bundle_id,
               ROW_NUMBER() OVER (
                   PARTITION BY app_name ORDER BY valid_from DESC, id DESC
               ) AS newest
        FROM windows_snapshot
        WHERE app_bundle_id IS NOT NULL AND app_name IS NOT NULL AND app_name != ''
    )
    WHERE newest = 1
) AS named
WHERE focus_spans.app_bundle_id IS NULL AND focus_spans.app_name = named.app_name;
