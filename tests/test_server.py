from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, select, update

from petreport import db
from petreport.config import Frigate, Settings, settings
from petreport.core import calendar_bounds
from petreport.evidence import EvidenceError, FrigateClient


def call(client, path, body, method="POST", key=None, expected=200):
    result = client.request(
        method, "/internal/v1" + path, json=body, headers={"Idempotency-Key": key or str(uuid4())}
    )
    assert result.status_code == expected, result.text
    return result.json()


def claim(client, day="2026-11-06", dry_run=True, notify=False):
    return call(
        client,
        "/runs/claim",
        {"local_date": day, "config_hash": "test-v1", "dry_run": dry_run, "notify": notify},
    )


def lease(run):
    return {"run_id": run["run_id"], "lease_token": run["lease_token"]}


def seed(client):
    run = claim(client)
    ev = {
        "id": "1793980800-cat-1",
        "start_time": "2026-11-06T13:00:00Z",
        "end_time": "2026-11-06T13:00:30Z",
        "camera": "feeder_camera",
        "zones": ["feeder"],
        "has_snapshot": True,
        "raw": {"label": "cat", "sub_label": None},
    }
    body = {
        **lease(run),
        "source_id": "frigate-home",
        "local_start": "2026-11-06",
        "local_end": "2026-11-07",
        "events": [ev],
        "collection_status": "complete",
    }
    call(client, "/events/upsert", body)
    batch = {
        **lease(run),
        "local_start": "2026-11-06",
        "local_end": "2026-11-07",
        "grouping_version": "v1",
        "candidates": [{"station_id": "feeder", "member_event_ids": [ev["id"]]}],
    }
    visit = call(client, "/visits/batch", batch, "PUT")["visit_ids"][0]
    return run, ev, batch, visit


def publish(client, run, kind="daily", start="2026-11-06"):
    report = call(
        client,
        "/reports/prepare",
        {**lease(run), "kind": kind, "local_start": start, "local_end": "2026-11-07"},
    )
    call(
        client,
        f"/reports/{report['report_id']}/finalize",
        {**lease(run), "expected_revision": report["revision"]},
        "PUT",
    )
    return report


def attach_media(visit, event_id, created_days=0):
    mid = db.uid()
    path = settings().media_root / f"{mid}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff" + b"test-image-data" * 5)
    with db.sessions().begin() as session:
        eid = session.execute(select(db.events.c.id).where(db.events.c.event_id == event_id)).scalar_one()
        session.execute(
            insert(db.media).values(
                id=mid,
                event_id=eid,
                kind="snapshot",
                variant="snapshot",
                relative_path=path.name,
                status="ready",
                mime="image/jpeg",
                created_at=db.now() - timedelta(days=created_days),
                fetched_at=db.now(),
            )
        )
        session.execute(insert(db.visit_media).values(visit_id=visit, media_id=mid))
    return mid, path


def test_health_and_auth(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/internal/v1/readiness", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/internal/v1/openapi.json").status_code == 200
    assert client.get("/internal/v1/openapi.json", headers={"authorization": ""}).status_code == 401
    assert client.post("/internal/v1/runs/claim", json={}).status_code == 400


def test_calendar_dst():
    a, b = calendar_bounds(date(2026, 11, 1), date(2026, 11, 2))
    assert (b - a).total_seconds() == 25 * 3600
    a, b = calendar_bounds(date(2026, 3, 8), date(2026, 3, 9))
    assert (b - a).total_seconds() == 23 * 3600


def test_config_rejects_bad_origin_and_db():
    for url in [
        "http://frigate.example.invalid",
        "https://evil.example",
        "https://frigate.example.invalid@evil.example",
        "https://frigate.example.invalid/api",
    ]:
        with pytest.raises(ValueError):
            Frigate(base_url=url)
    with pytest.raises(ValueError):
        Settings(database_url="sqlite:///test.db")


def test_replay_and_fencing(pgclient):
    c = pgclient
    body = {"local_date": "2026-11-06", "config_hash": "v1"}
    first = call(c, "/runs/claim", body, key="claim")
    assert first == call(c, "/runs/claim", body, key="claim")
    call(c, "/runs/claim", {**body, "config_hash": "v2"}, key="claim", expected=409)
    call(c, "/runs/claim", body, expected=409)
    with db.sessions().begin() as session:
        session.execute(update(db.runs).values(lease_until=db.now() - timedelta(seconds=1)))
    second = call(c, "/runs/claim", body)
    assert first["lease_token"] != second["lease_token"]
    call(c, f"/runs/{first['run_id']}/heartbeat", {"lease_token": first["lease_token"]}, expected=409)


def test_reconcile_report_snapshot_and_html(pgclient):
    c = pgclient
    run, ev, batch, visit = seed(c)
    result = call(c, "/visits/batch", batch, "PUT")
    assert result == {"visit_ids": [visit], "changed": 0, "retired": 0}
    mid, path = attach_media(visit, ev["id"])
    assert c.get(f"/media/{mid}").status_code == 404
    report = publish(c, run)
    assert report["facts"]["counts"] == {"feeder": 1, "litterbox": 0}
    assert c.get("/").status_code == 200
    html = c.get(f"/reports/{report['report_id']}")
    assert html.status_code == 200 and "Coverage unknown" in html.text
    assert c.get(f"/media/{mid}", headers={"Range": "bytes=0-9"}).status_code == 206
    oldfacts = report["facts"]
    call(c, "/visits/batch", {**batch, "candidates": []}, "PUT")
    with db.sessions()() as session:
        saved = session.execute(select(db.reports)).mappings().one()
        assert saved["status"] == "stale" and saved["facts"] == oldfacts
    changed = publish(c, run)
    assert changed["revision"] == 2 and changed["facts"]["counts"]["feeder"] == 0
    with db.sessions()() as session:
        assert session.execute(select(db.report_revisions.c.revision)).scalars().all() == [1]


def test_classification_rejects_invented_evidence(pgclient):
    c = pgclient
    run, ev, batch, vid = seed(c)
    result = {
        "schema_version": 1,
        "visit_id": vid,
        "pet_id": "coconut",
        "identity_method": "single_pet_assumption",
        "observations": [
            {
                "behavior": "eating",
                "assessment": "supported",
                "evidence_ids": [str(uuid4())],
                "reason": "Seen",
            }
        ],
        "evidence_quality": "good",
    }
    body = {
        **lease(run),
        "result": result,
        "model": {"name": "fixture"},
        "prompt_version": "v1",
        "input_fingerprint": "abc",
    }
    call(c, f"/visits/{vid}/classification", body, "PUT", expected=422)
    mid, _ = attach_media(vid, ev["id"])
    result["observations"][0]["evidence_ids"] = [mid]
    call(c, f"/visits/{vid}/classification", body, "PUT")
    report = publish(c, run)
    assert report["facts"]["supported_behavior_visits"] == {"eating": 1}
    result["observations"][0]["reason"] = "<script>alert(1)</script>"
    call(c, f"/visits/{vid}/classification", body, "PUT")
    new = publish(c, run)
    html = c.get(f"/reports/{new['report_id']}").text
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_partial_week_no_false_zeros(pgclient):
    run, _, _, _ = seed(pgclient)
    report = publish(pgclient, run, "weekly", "2026-10-31")
    assert len(report["facts"]["missing_dates"]) == 6
    page = pgclient.get(f"/reports/{report['report_id']}")
    assert page.status_code == 200 and "YESTERDAY" in page.text and "Incomplete collection" in page.text


def test_cleanup_preserves_shared_and_recovers(pgclient):
    c = pgclient
    run, ev, _, vid = seed(c)
    mid, path = attach_media(vid, ev["id"])
    daily = publish(c, run)
    weekly = publish(c, run, "weekly", "2026-10-31")
    with db.sessions().begin() as session:
        session.execute(
            update(db.reports)
            .where(db.reports.c.id == daily["report_id"])
            .values(expires_at=db.now() - timedelta(seconds=1))
        )
    call(c, "/maintenance/cleanup", {"dry_run": False})
    assert path.exists() and c.get(f"/media/{mid}").status_code == 200
    with db.sessions().begin() as session:
        session.execute(update(db.reports).values(expires_at=db.now() - timedelta(seconds=1)))
        session.execute(update(db.media).values(status="pending_delete"))
    call(c, "/maintenance/cleanup", {"dry_run": False})
    assert not path.exists()
    assert c.get(f"/reports/{weekly['report_id']}").status_code == 410
    assert c.get(f"/media/{mid}").status_code == 404
    assert call(c, "/maintenance/cleanup", {"dry_run": False})["deleted_media"] == 0


def test_dry_run_cannot_claim_delivery(pgclient):
    run, _, _, _ = seed(pgclient)
    report = publish(pgclient, run)
    call(
        pgclient,
        "/deliveries/claim",
        {**lease(run), "local_send_date": "2026-11-07", "report_id": report["report_id"]},
        expected=403,
    )


def test_concurrent_claims(pgclient):
    body = {"local_date": "2026-11-06", "config_hash": "v1"}

    def attempt(_):
        return pgclient.post(
            "/internal/v1/runs/claim", json=body, headers={"Idempotency-Key": str(uuid4())}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 409]


def test_out_of_station_rejected(pgclient):
    run, ev, batch, _ = seed(pgclient)
    batch["candidates"][0]["station_id"] = "litterbox"
    call(pgclient, "/visits/batch", batch, "PUT", expected=422)


@pytest.mark.anyio
async def test_media_validation_and_auth_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("PETREPORT_CONFIG", "/nonexistent")
    monkeypatch.setenv("PETREPORT_SECRETS", "/nonexistent")
    settings.cache_clear()
    cfg = settings()
    cfg.media_root = tmp_path
    from pydantic import SecretStr

    cfg.frigate.password = SecretStr("fake-fixture-only")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/login":
            return httpx.Response(200, headers={"set-cookie": "session=fake"})
        if "session=fake" not in request.headers.get("cookie", ""):
            return httpx.Response(401)
        return httpx.Response(
            200, content=b"\xff\xd8\xff" + b"a" * 20, headers={"content-type": "image/jpeg"}
        )

    adapter = FrigateClient()
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(base_url=cfg.frigate.base_url, transport=httpx.MockTransport(handler))
    downloaded = await adapter.download("fixture-event", "snapshot", "valid.jpg")
    assert downloaded["bytes"] == 23 and (tmp_path / "valid.jpg").exists()
    assert calls.count("/api/login") == 1
    with pytest.raises(EvidenceError, match="clip_not_verified"):
        await adapter.download("fixture-event", "clip", "bad.mp4")
    cfg.frigate.max_snapshot_bytes = 1024

    def too_large(request):
        return httpx.Response(
            200, content=b"\xff\xd8\xff" + b"x" * 2000, headers={"content-type": "image/jpeg"}
        )

    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url=cfg.frigate.base_url, transport=httpx.MockTransport(too_large)
    )
    with pytest.raises(EvidenceError, match="media_too_large"):
        await adapter.download("fixture-event", "snapshot", "oversized.jpg")
    assert not (tmp_path / "oversized.jpg").exists() and not list(tmp_path.glob(".tmp-*"))
    await adapter.close()


def test_delivery_slot_replay_and_unknown_never_resend(pgclient):
    from zoneinfo import ZoneInfo

    c = pgclient
    today = db.now().astimezone(ZoneInfo("America/Toronto")).date()
    yesterday = today - timedelta(days=1)
    run = claim(c, str(today), dry_run=False, notify=True)
    call(
        c,
        "/events/upsert",
        {
            **lease(run),
            "source_id": "frigate-home",
            "local_start": str(yesterday),
            "local_end": str(today),
            "events": [],
            "collection_status": "complete",
        },
    )
    call(
        c,
        "/visits/batch",
        {
            **lease(run),
            "local_start": str(yesterday),
            "local_end": str(today),
            "grouping_version": "v1",
            "candidates": [],
        },
        "PUT",
    )
    prepared = call(
        c,
        "/reports/prepare",
        {**lease(run), "kind": "daily", "local_start": str(yesterday), "local_end": str(today)},
    )
    call(c, f"/reports/{prepared['report_id']}/finalize", {**lease(run), "expected_revision": 1}, "PUT")
    body = {**lease(run), "local_send_date": str(today), "report_id": prepared["report_id"]}
    first = call(c, "/deliveries/claim", body, key="daily-slot")
    assert first["may_send"] is True
    assert call(c, "/deliveries/claim", body, key="daily-slot")["may_send"] is False
    assert call(c, "/deliveries/claim", body)["may_send"] is False
    call(
        c,
        f"/deliveries/{first['attempt_id']}/result",
        {**lease(run), "result": "unknown", "error_code": "timeout"},
    )
    assert call(c, "/deliveries/claim", body) == {"state": "unknown", "may_send": False}
    # Regenerating a report cannot reset the household/date/channel slot.
    assert (
        c.get("/internal/v1/deliveries", params={"local_send_date": str(today)}).json()["deliveries"][0][
            "state"
        ]
        == "unknown"
    )


def test_media_cache_no_network_transaction_and_membership(pgclient):
    c = pgclient
    run, ev, batch, vid = seed(c)

    class FixtureFrigate:
        async def download(self, event_id, variant, relative):
            with db.sessions()() as session:
                # The cache intent is committed before requesting the binary.
                row = (
                    session.execute(select(db.media).where(db.media.c.relative_path == relative))
                    .mappings()
                    .one()
                )
                assert row["status"] == "downloading"
            path = settings().media_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8\xff" + b"a" * 20)
            return {"sha256": "fixture", "bytes": 23, "mime": "image/jpeg"}

    original = c.app.state.frigate
    c.app.state.frigate = FixtureFrigate()
    try:
        body = {**lease(run), "event_ids": [ev["id"]], "variants": ["snapshot"]}
        first = call(c, "/media/cache", body)
        assert first["media"][0]["status"] == "ready"
        second = call(c, "/media/cache", body)
        assert first["media"][0]["media_id"] == second["media"][0]["media_id"]
        with db.sessions()() as session:
            assert len(session.execute(select(db.visit_media)).all()) == 1
    finally:
        c.app.state.frigate = original


def test_boundary_expansion_and_ongoing(pgclient):
    c = pgclient
    run = claim(c)
    ev = {
        "id": "boundary-cat",
        "start_time": "2026-11-06T04:59:00Z",
        "end_time": "2026-11-06T05:01:00Z",
        "camera": "litterbox_camera",
        "zones": ["litterbox"],
        "raw": {"label": "cat"},
    }
    call(
        c,
        "/events/upsert",
        {
            **lease(run),
            "source_id": "frigate-home",
            "local_start": "2026-11-05",
            "local_end": "2026-11-07",
            "events": [ev],
            "collection_status": "complete",
        },
    )
    body = {
        **lease(run),
        "local_start": "2026-11-06",
        "local_end": "2026-11-07",
        "grouping_version": "v1",
        "candidates": [{"station_id": "litterbox", "member_event_ids": [ev["id"]]}],
    }
    call(c, "/visits/batch", body, "PUT", expected=422)
    body["local_start"] = "2026-11-05"
    call(c, "/visits/batch", body, "PUT")
    day = publish(c, run)
    assert day["facts"]["counts"]["litterbox"] == 0
    assert day["facts"]["visits"][0]["continuation"] is True
    listed = c.get(
        "/internal/v1/events",
        params={"start": "2026-11-06T05:00:00Z", "end": "2026-11-07T05:00:00Z", "limit": 1},
    )
    assert listed.status_code == 200 and listed.json()["events"][0]["event_id"] == ev["id"]


def test_readiness_migrated_and_missing_schema(pgclient):
    from sqlalchemy import text

    response = pgclient.get("/internal/v1/readiness")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["checks"]["database"] == "ready"
    with db.engine().begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    response = pgclient.get("/internal/v1/readiness")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "migration_required"


def test_saved_notification_requires_finished_live_generation(pgclient):
    c = pgclient
    run, _, _, _ = seed(c)
    daily = publish(c, run)
    path = "/internal/v1/reports/for-delivery?local_send_date=2026-11-07&kind=daily"
    assert c.get(path).json()["available"] is False
    with db.sessions().begin() as session:
        session.execute(
            update(db.runs)
            .where(db.runs.c.id == run["run_id"])
            .values(
                local_date=date(2026, 11, 7), status="complete", stage="classify", dry_run=False, errors=[]
            )
        )
    assert c.get(path).json()["available"] is False
    with db.sessions().begin() as session:
        session.execute(update(db.runs).values(stage="finished"))
    saved = c.get(path).json()
    assert saved["available"] is True and saved["usable"] is True
    assert saved["report_id"] == daily["report_id"]
    assert "summary" in saved and "facts" not in saved
    assert saved["notification"]["pet_name"] == "Coconut"
    assert saved["notification"]["local_start"] == "2026-11-06"
    assert saved["notification"]["counts"]["feeder"] == 1
    assert saved["notification"]["icon_url"].endswith("/static/icon.png")
    assert c.get(path.replace("kind=daily", "kind=weekly")).json()["available"] is False
    with db.sessions().begin() as session:
        session.execute(update(db.reports).values(status="deleted"))
    assert c.get(path).json()["available"] is False
