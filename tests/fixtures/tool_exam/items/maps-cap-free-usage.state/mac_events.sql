-- 1,200 Enterprise Text Searches this month (October 8, Pacific): Google bills the 200 past the
-- free 1,000, $7.00 at $0.035 each, not $42.00.
INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, ingestion_node)
VALUES
  ('exam-spend-1200', 'maps.spend_recorded', 1, 1791486000000,
   '{"sku": "places.text_search.enterprise", "requests": 1200, "cost_usd": 42.0, "action_id": "exam-seed"}',
   'mac');
