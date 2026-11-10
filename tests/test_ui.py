import re

from sqlalchemy import select, update

from petreport import db
from test_server import attach_media, publish, seed


def test_public_health(pgclient):
    response = pgclient.get("/api/health", headers={"authorization": ""})
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert pgclient.get("/favicon.ico").headers["content-type"] == "image/png"


def test_date_routes_timeline_and_weekly_links(pgclient):
    run, event, _, visit = seed(pgclient)
    attach_media(visit, event["id"])
    daily = publish(pgclient, run)
    weekly = publish(pgclient, run, "weekly", "2026-10-31")
    redirect = pgclient.get(f"/reports/{daily['report_id']}", follow_redirects=False)
    assert redirect.headers["location"] == "/reports/daily/2026-11-06"
    page = pgclient.get(redirect.headers["location"])
    assert 'class="timeline"' in page.text
    assert 'aria-haspopup="dialog"' in page.text
    assert "<dialog" in page.text and "8:00 AM" in page.text
    assert "/static/favicon.png" in page.text
    assert "The whole day" in page.text and "24:00" in page.text
    assert "Midnight · next day" in page.text
    assert "Take a closer look" not in page.text
    week = pgclient.get(f"/reports/{weekly['report_id']}")
    assert "/reports/daily/2026-11-06" in week.text
    assert "1 of 7 days fully collected" in week.text
    assert 'class="timeline"' not in week.text
    assert week.text.count('class="day-date"') == 7
    assert pgclient.get("/reports/daily/2026-11-05").status_code == 404
    assert "/reports/daily/2026-11-06" in pgclient.get("/journal").text


def confirmation(client, report_id):
    page = client.get(f"/reports/{report_id}/delete")
    assert page.status_code == 200
    return {key: re.search(f'name="{key}" value="([^"]+)"', page.text)[1] for key in ("token", "expires")}


def test_confirmed_delete_preserves_shared_evidence(pgclient):
    run, event, _, visit = seed(pgclient)
    mid, path = attach_media(visit, event["id"])
    daily = publish(pgclient, run)
    weekly = publish(pgclient, run, "weekly", "2026-10-31")
    target = f"/reports/{daily['report_id']}/delete"
    assert pgclient.post(target, data={}).status_code == 415
    form = confirmation(pgclient, daily["report_id"])
    assert pgclient.post(target, data=form, headers={"origin": "https://foreign.invalid"}).status_code == 403
    assert pgclient.post(target, data={**form, "token": "bad"}).status_code == 403
    assert pgclient.get(f"/reports/{daily['report_id']}").status_code == 200
    response = pgclient.post(target, data=form)
    assert response.status_code == 200 and "Report deleted" in response.text
    assert pgclient.get(f"/reports/{daily['report_id']}").status_code == 404
    assert pgclient.get("/reports/daily/2026-11-06").status_code == 404
    assert pgclient.get(f"/reports/{weekly['report_id']}").status_code == 200
    assert pgclient.get(f"/media/{mid}").status_code == 200 and path.exists()
    with db.sessions()() as session:
        assert session.execute(select(db.visits)).first()
        assert session.execute(select(db.events)).first()
    assert pgclient.post(target, data=form).status_code == 404


def test_stale_confirmation_and_deleted_media_access(pgclient):
    from sqlalchemy import update

    run, event, _, visit = seed(pgclient)
    mid, path = attach_media(visit, event["id"])
    daily = publish(pgclient, run)
    form = confirmation(pgclient, daily["report_id"])
    target = f"/reports/{daily['report_id']}/delete"
    with db.sessions().begin() as session:
        session.execute(update(db.reports).values(revision=db.reports.c.revision + 1))
    assert pgclient.post(target, data=form).status_code == 403
    form = confirmation(pgclient, daily["report_id"])
    assert (
        pgclient.post(target, data={**form, "expires": str(int(form["expires"]) - 2000)}).status_code == 403
    )
    assert pgclient.post(target, data=form).status_code == 200
    assert pgclient.get(f"/media/{mid}").status_code == 404

    assert not path.exists()
    with db.sessions()() as session:
        assert session.execute(select(db.media).where(db.media.c.id == mid)).first() is None


def test_failed_manual_media_removal_is_retried(pgclient, monkeypatch):
    from pathlib import Path
    from test_server import call

    run, event, _, visit = seed(pgclient)
    mid, path = attach_media(visit, event["id"])
    daily = publish(pgclient, run)
    original = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("simulated filesystem failure")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unavailable)
        response = pgclient.post(
            f"/reports/{daily['report_id']}/delete",
            data=confirmation(pgclient, daily["report_id"]),
        )
        assert response.status_code == 200
    assert path.exists()
    with db.sessions()() as session:
        assert (
            session.execute(select(db.media.c.status).where(db.media.c.id == mid)).scalar_one()
            == "pending_delete"
        )
    preview = call(pgclient, "/maintenance/cleanup", {"dry_run": True})
    assert preview["media_candidates"] == 1 and path.exists()
    assert call(pgclient, "/maintenance/cleanup", {"dry_run": False})["deleted_media"] == 1
    assert not path.exists()


def test_today_uses_morning_report_and_local_midnight(pgclient, monkeypatch):
    from datetime import datetime, timezone

    run, _, _, _ = seed(pgclient)
    publish(pgclient, run)
    # UTC is already Sunday, while Toronto is still Saturday: show Friday's report.
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 11, 8, 4, 30, tzinfo=timezone.utc))
    response = pgclient.get("/today", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/reports/daily/2026-11-06"
    assert response.headers["cache-control"] == "private, no-store"
    assert pgclient.get("/today").status_code == 200
    # After local midnight, the same bookmark requires Saturday's report.
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 11, 8, 5, 30, tzinfo=timezone.utc))
    response = pgclient.get("/today")
    assert response.status_code == 200
    assert "Today’s journal isn’t available yet" in response.text
    assert "November 07, 2026" in response.text
    assert "8:00 AM" in response.text and "America/Toronto" in response.text
    assert response.headers["cache-control"] == "private, no-store"


def test_today_only_displays_available_household_daily_report(pgclient, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import update

    run, _, _, _ = seed(pgclient)
    daily = publish(pgclient, run)
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 11, 7, 15, tzinfo=timezone.utc))
    changes = [
        {"status": "prepared"},
        {"status": "deleted"},
        {"status": "expired"},
        {"expires_at": db.now() - timedelta(seconds=1)},
        {"household_id": "other-household"},
        {"timezone": "UTC"},
    ]
    for change in changes:
        with db.sessions().begin() as session:
            session.execute(
                update(db.reports)
                .where(db.reports.c.id == daily["report_id"])
                .values(
                    status="published",
                    expires_at=db.now() + timedelta(days=30),
                    household_id="home",
                    timezone="America/Toronto",
                )
            )
            session.execute(update(db.reports).where(db.reports.c.id == daily["report_id"]).values(**change))
        response = pgclient.get("/today")
        assert response.status_code == 200
        assert "Today’s journal isn’t available yet" in response.text


def test_grouped_visit_shows_ordered_photos_and_a_span(pgclient):
    from test_server import call, lease

    run, first, batch, _ = seed(pgclient)
    second = {
        **first,
        "id": "later-cat",
        "start_time": "2026-11-06T13:01:00Z",
        "end_time": "2026-11-06T13:02:00Z",
    }
    call(
        pgclient,
        "/events/upsert",
        {
            **lease(run),
            "source_id": "frigate-home",
            "local_start": "2026-11-06",
            "local_end": "2026-11-07",
            "events": [second],
            "collection_status": "complete",
        },
    )
    batch["candidates"][0]["member_event_ids"] = [first["id"], second["id"]]
    visit = call(pgclient, "/visits/batch", batch, "PUT")["visit_ids"][0]
    later, _ = attach_media(visit, second["id"])
    earlier, _ = attach_media(visit, first["id"])
    result = {
        "schema_version": 1,
        "visit_id": visit,
        "pet_id": "coconut",
        "identity_method": "single_pet_assumption",
        "evidence_quality": "good",
        "observations": [
            {
                "behavior": "eating",
                "assessment": "supported",
                "evidence_ids": [mid],
                "reason": "Visible eating",
            }
            for mid in (earlier, later)
        ],
    }
    call(
        pgclient,
        f"/visits/{visit}/classification",
        {
            **lease(run),
            "result": result,
            "model": {"name": "fixture"},
            "prompt_version": "v1",
            "input_fingerprint": "grouped-fixture",
        },
        "PUT",
    )
    report = publish(pgclient, run)
    assert report["facts"]["counts"]["feeder"] == 1
    saved = report["facts"]["visits"][0]
    assert saved["detection_count"] == 2
    assert len(saved["classification"]["observations"]) == 1
    assert saved["classification"]["observations"][0]["evidence_ids"] == [earlier, later]
    # Older report snapshots may still contain separate per-image findings.
    saved["classification"] = result
    with db.sessions().begin() as session:
        session.execute(
            update(db.reports).where(db.reports.c.id == report["report_id"]).values(facts=report["facts"])
        )
    assert [item["id"] for item in saved["media"]] == [earlier, later]
    page = pgclient.get("/reports/daily/2026-11-06")
    assert "2 detections" in page.text and "2 snapshots" in page.text
    assert page.text.count("<strong>Eating</strong>") == 1
    assert "photos-swipe" in page.text
    start, end = re.search(
        r'<line x1="([^"]+)" x2="([^"]+)" [^>]+class="map-visit feeder"', page.text
    ).groups()
    assert float(end) > float(start)
