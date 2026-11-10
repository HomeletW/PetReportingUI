from datetime import date

from petreport.timeline import day_timeline


def event(id, start):
    return {"id": id, "start": start, "kind": "feeder"}


def test_day_positions_periods_and_gaps():
    data = day_timeline(
        [event("late", "2026-11-07T01:00:00+00:00"), event("early", "2026-11-06T05:30:00+00:00")],
        date(2026, 11, 6),
        "America/Toronto",
    )
    assert [v["id"] for v in data["visits"]] == ["early", "late"]
    assert data["hours"] == 24
    assert data["visits"][0]["x"] == 51.5
    assert data["visits"][1]["gap_label"] == "19h 30m"
    assert data["periods"][0]["visits"][0]["id"] == "early"
    assert data["periods"][3]["visits"][0]["id"] == "late"
    assert data["next_date"] == date(2026, 11, 7)
    assert data["ticks"][0]["label"] == "00:00" and data["ticks"][-1]["label"] == "24:00"


def test_clock_changes_use_elapsed_time_and_disambiguate_repeated_hours():
    data = day_timeline(
        [event("first", "2026-11-01T05:30:00+00:00"), event("second", "2026-11-01T06:30:00+00:00")],
        date(2026, 11, 1),
        "America/Toronto",
    )
    assert data["hours"] == 25
    assert data["visits"][0]["clock"] == "1:30 AM EDT"
    assert data["visits"][1]["clock"] == "1:30 AM EST"
    assert data["visits"][0]["x"] < data["visits"][1]["x"]
    spring = day_timeline([], date(2026, 3, 8), "America/Toronto")
    assert spring["hours"] == 23


def test_midnight_continuation_is_anchored_to_start_and_empty_periods_remain_visible():
    data = day_timeline(
        [event("continued", "2026-11-06T04:59:00+00:00")], date(2026, 11, 6), "America/Toronto"
    )
    assert data["visits"][0]["x"] == 32
    assert len(data["periods"]) == 4
    assert len(data["periods"][0]["visits"]) == 1
    assert all(not period["visits"] for period in data["periods"][1:])


def test_conflicting_findings_become_uncertain_without_merging_distinct_behaviors():
    from petreport.classification import consolidate_classification

    result = {
        "observations": [
            {"behavior": "eating", "assessment": "supported", "reason": "One cue", "evidence_ids": ["one"]},
            {
                "behavior": "drinking",
                "assessment": "supported",
                "reason": "Water contact",
                "evidence_ids": ["two"],
            },
            {"behavior": "eating", "assessment": "uncertain", "reason": "Obscured", "evidence_ids": ["two"]},
        ]
    }
    findings = consolidate_classification(result)["observations"]
    assert [(f["behavior"], f["assessment"]) for f in findings] == [
        ("eating", "uncertain"),
        ("drinking", "supported"),
    ]
    assert findings[0]["evidence_ids"] == ["one", "two"]
    assert result["observations"][0]["assessment"] == "supported"


def test_visit_map_bars_scale_to_grouped_span_and_clip_at_midnight():
    short = {**event("short", "2026-11-06T10:00:00+00:00"), "end": "2026-11-06T11:00:00+00:00"}
    long = {**event("long", "2026-11-06T12:00:00+00:00"), "end": "2026-11-06T14:00:00+00:00"}
    open_visit = event("open", "2026-11-06T18:00:00+00:00")
    crosses_midnight = {**event("cross", "2026-11-07T04:00:00+00:00"), "end": "2026-11-07T06:00:00+00:00"}
    visits = day_timeline([short, long, open_visit, crosses_midnight], date(2026, 11, 6), "America/Toronto")[
        "visits"
    ]
    assert visits[1]["map_width"] == 2 * visits[0]["map_width"]
    assert visits[2]["end_x"] == visits[2]["x"] and visits[2]["map_width"] == 0.1
    assert visits[3]["end_x"] == 968 and visits[3]["map_width"] == visits[0]["map_width"]
