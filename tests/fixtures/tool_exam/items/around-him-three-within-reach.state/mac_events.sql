-- His phone at the same corner for the last twenty minutes: two fixes before the item's own report.
INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, ingestion_node)
VALUES
  ('exam-fix-20-min', 'phone.location_observed', 1, 1792627800000,
   '{"lat": 48.4284, "lng": -123.3656, "accuracy_m": 15.0}', 'iphone'),
  ('exam-fix-10-min', 'phone.location_observed', 1, 1792628400000,
   '{"lat": 48.4284, "lng": -123.3656, "accuracy_m": 15.0}', 'iphone');
