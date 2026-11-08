import re

from sqlalchemy import select

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
    mid, _ = attach_media(visit, event["id"])
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
