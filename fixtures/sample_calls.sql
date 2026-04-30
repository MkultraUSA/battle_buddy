-- Battle Buddy sample fixtures
-- Apply with: sqlite3 calls.db < fixtures/sample_calls.sql
--
-- Provides 5 fake radio calls and 2 fake incidents so a developer can run the
-- system end-to-end without live radio data. All identifiers are intentionally
-- non-realistic:
--   * tgid 9999 (not a real APD/AFD talkgroup)
--   * timestamps near 9999999999 (year 2286, far from any real capture)
--   * intersections like "Main and 5th" (not real Austin streets in this combo)

INSERT INTO calls (id, ts, tgid, tag, category, node, duration, transcript, lat, lon, location, coords_approx) VALUES
    (900001, 9999999000, 9999, 'FAKE_DISPATCH', 'fake', 'fixture', 4.2,
        'Unit 4 respond to the intersection of Main and 5th for a welfare check.',
        30.2672, -97.7431, 'Main and 5th', 1),
    (900002, 9999999060, 9999, 'FAKE_DISPATCH', 'fake', 'fixture', 5.0,
        'Unit 4 on scene at Main and 5th, subject is conscious and breathing.',
        30.2672, -97.7431, 'Main and 5th', 1),
    (900003, 9999999300, 9998, 'FAKE_FIRE', 'fake', 'fixture', 6.1,
        'Engine 7 dispatched to 1234 Fictional Lane for smoke showing from a single-story.',
        30.2700, -97.7400, '1234 Fictional Lane', 1),
    (900004, 9999999360, 9998, 'FAKE_FIRE', 'fake', 'fixture', 3.4,
        'Engine 7 working fire confirmed, requesting second alarm.',
        30.2700, -97.7400, '1234 Fictional Lane', 1),
    (900005, 9999999900, 9999, 'FAKE_DISPATCH', 'fake', 'fixture', 2.7,
        'Unit 4 clear from Main and 5th, code 4.',
        30.2672, -97.7431, 'Main and 5th', 1);

INSERT INTO incidents (id, ts_start, ts_updated, ts_cleared, itype, description, agencies, tgids, location, lat, lon, status, is_test, flagged, article_url) VALUES
    (900001, 9999999000, 9999999900, 9999999900, 'welfare_check',
        'Fake welfare check fixture at Main and 5th.',
        'FAKE_PD', '9999', 'Main and 5th', 30.2672, -97.7431, 'cleared', 1, 0, NULL),
    (900002, 9999999300, 9999999360, NULL, 'structure_fire',
        'Fake working structure fire fixture on Fictional Lane.',
        'FAKE_FD', '9998', '1234 Fictional Lane', 30.2700, -97.7400, 'active', 1, 0, NULL);

INSERT INTO incident_calls (incident_id, call_id) VALUES
    (900001, 900001),
    (900001, 900002),
    (900001, 900005),
    (900002, 900003),
    (900002, 900004);
