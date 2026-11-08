from datetime import date, timedelta
import asyncio
import hashlib
import hmac
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import delete, select, update, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from . import db, schemas as s
from .config import settings
from .core import (
    calendar_bounds,
    date_list,
    fail,
    lease,
    lock,
    report_url,
    stale_for_visits,
    template_summary,
)


def authenticate(request: Request):
    expected = settings().internal_api_token.get_secret_value()
    supplied = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
        fail(401, "invalid_service_token")


class OperationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            authenticate(request)
            if request.method == "GET":
                return await handler(request)
            key = request.headers.get("idempotency-key", "")
            if not key or len(key) > 128:
                fail(400, "Idempotency-Key is required (1–128 characters)")
            digest = hashlib.sha256(
                request.method.encode() + request.url.path.encode() + await request.body()
            ).hexdigest()
            # Hash the key: clients may accidentally put private data in it.
            opkey = hashlib.sha256(key.encode()).hexdigest()
            with db.sessions()() as session:
                request.state.session = session
                lock(session, "operation:" + opkey)
                old = (
                    session.execute(select(db.operations).where(db.operations.c.key == opkey))
                    .mappings()
                    .first()
                )
                if old:
                    if old["fingerprint"] != digest:
                        fail(409, "idempotency_key_reused_with_different_request")
                    if old["status"] != "complete":
                        fail(
                            409, "operation_in_progress_or_interrupted; inspect state before using a new key"
                        )
                    result = old["response"]
                    if request.url.path == "/internal/v1/deliveries/claim":
                        result = {**result, "may_send": False}
                    return JSONResponse(result, headers={"Idempotency-Replayed": "true"})
                session.execute(
                    insert(db.operations).values(
                        key=opkey, fingerprint=digest, status="processing", created_at=db.now()
                    )
                )
                try:
                    response = await handler(request)
                    if response.status_code >= 400:
                        session.rollback()
                        return response
                    result = json.loads(response.body)
                    session.execute(
                        update(db.operations)
                        .where(db.operations.c.key == opkey)
                        .values(status="complete", response=result)
                    )
                    session.commit()
                    return response
                except Exception:
                    session.rollback()
                    raise

        return handle


router = APIRouter(prefix="/internal/v1", route_class=OperationRoute, dependencies=[Depends(authenticate)])


def session_for(request: Request):
    return request.state.session


@router.post("/runs/claim")
def claim(body: s.Claim, session=Depends(session_for)):
    cfg = settings()
    if body.household_id != cfg.household_id:
        fail(422, "unknown_household")
    lock(session, "household:" + cfg.household_id)
    row = (
        session.execute(
            select(db.runs)
            .where(db.runs.c.household_id == cfg.household_id, db.runs.c.local_date == body.local_date)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row and row["status"] == "running" and row["lease_until"] > db.now():
        fail(409, "run_already_leased")
    # Seed registry through the same transaction, with configuration as the source of truth.
    session.execute(
        insert(db.pets)
        .values(id=cfg.pet_id, display_name=cfg.pet_name, identity_policy={"method": "single_pet_assumption"})
        .on_conflict_do_update(index_elements=[db.pets.c.id], set_={"display_name": cfg.pet_name})
    )
    for station in cfg.stations:
        values = {**station.model_dump(), "source_id": cfg.source_id}
        session.execute(
            insert(db.stations)
            .values(**values)
            .on_conflict_do_update(index_elements=[db.stations.c.id], set_=values)
        )
    values = dict(
        status="running",
        stage="collect",
        config_hash=body.config_hash,
        lease_token=db.uid(),
        lease_until=db.now() + timedelta(seconds=cfg.lease_seconds),
        dry_run=body.dry_run,
        notify=body.notify and not body.dry_run and cfg.notifications_enabled,
        errors=[],
    )
    if row:
        run_id = row["id"]
        session.execute(update(db.runs).where(db.runs.c.id == run_id).values(**values))
    else:
        run_id = db.uid()
        session.execute(
            insert(db.runs).values(
                id=run_id, household_id=cfg.household_id, local_date=body.local_date, **values
            )
        )
    return {
        "run_id": run_id,
        "lease_token": values["lease_token"],
        "lease_until": values["lease_until"],
        "notification_permitted": values["notify"],
    }


@router.post("/runs/{run_id}/heartbeat")
def heartbeat(run_id: UUID, body: s.Heartbeat, session=Depends(session_for)):
    lease(session, s.Lease(run_id=run_id, lease_token=body.lease_token))
    until = db.now() + timedelta(seconds=settings().lease_seconds)
    session.execute(update(db.runs).where(db.runs.c.id == str(run_id)).values(lease_until=until))
    return {"lease_until": until}


@router.post("/events/upsert")
def events_upsert(body: s.Upsert, session=Depends(session_for)):
    lease(session, body)
    cfg = settings()
    if body.source_id != cfg.source_id:
        fail(422, "unknown_source")
    inserted = updated = 0
    for event in body.events:
        exists = session.execute(
            select(db.events.c.id).where(
                db.events.c.source_id == body.source_id, db.events.c.event_id == event.id
            )
        ).scalar_one_or_none()
        values = dict(
            source_id=body.source_id,
            event_id=event.id,
            start_time=event.start_time,
            end_time=event.end_time,
            camera=event.camera,
            zones=event.zones,
            raw=event.raw,
            has_snapshot=event.has_snapshot,
            has_clip=event.has_clip,
            fetched_at=db.now(),
        )
        if exists:
            session.execute(update(db.events).where(db.events.c.id == exists).values(**values))
            updated += 1
        else:
            session.execute(insert(db.events).values(**values))
            inserted += 1
    for day in date_list(body.local_start, body.local_end):
        values = dict(
            household_id=cfg.household_id,
            local_date=day,
            collection_status=body.collection_status,
            grouping_version=None,
            coverage=body.coverage,
            last_run_id=str(body.run_id),
        )
        session.execute(
            insert(db.days)
            .values(**values)
            .on_conflict_do_update(index_elements=[db.days.c.household_id, db.days.c.local_date], set_=values)
        )
    return {"inserted": inserted, "updated": updated}


@router.get("/processing-days")
def processing_days(local_start: date, local_end: date):
    interval = s.DateRange(local_start=local_start, local_end=local_end)
    with db.sessions()() as session:
        rows = (
            session.execute(
                select(db.days).where(
                    db.days.c.household_id == settings().household_id,
                    db.days.c.local_date >= local_start,
                    db.days.c.local_date < local_end,
                )
            )
            .mappings()
            .all()
        )
        bydate = {r["local_date"]: dict(r) for r in rows}
        open_ids = (
            session.execute(select(db.events.c.event_id).where(db.events.c.end_time.is_(None)))
            .scalars()
            .all()
        )
        return {
            "days": [
                bydate.get(
                    d, {"local_date": d, "collection_status": "missing", "coverage": {"status": "unknown"}}
                )
                for d in date_list(interval.local_start, interval.local_end)
            ],
            "open_event_ids": open_ids,
        }


@router.put("/visits/batch")
def visits_batch(body: s.Batch, session=Depends(session_for)):
    lease(session, body)
    cfg = settings()
    start, end = calendar_bounds(body.local_start, body.local_end)
    existing = (
        session.execute(
            select(db.visits).where(
                db.visits.c.active.is_(True),
                db.visits.c.start_time < end,
                (db.visits.c.end_time.is_(None) | (db.visits.c.end_time >= start)),
            )
        )
        .mappings()
        .all()
    )
    submitted = []
    touched = []
    used_members = set()
    for candidate in body.candidates:
        station = next((x for x in cfg.stations if x.id == candidate.station_id and x.enabled), None)
        if not station:
            fail(422, "unknown_or_disabled_station")
        if candidate.pet_id != cfg.pet_id and candidate.pet_id is not None:
            fail(422, "unknown_pet")
        if (candidate.pet_id is None) != (candidate.identity_method == "unknown"):
            fail(422, "identity_policy_mismatch")
        if len(set(candidate.member_event_ids)) != len(candidate.member_event_ids):
            fail(422, "duplicate_members")
        rows = (
            session.execute(
                select(db.events).where(
                    db.events.c.source_id == cfg.source_id,
                    db.events.c.event_id.in_(candidate.member_event_ids),
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != len(candidate.member_event_ids):
            fail(422, "unknown_member_events")
        rows = sorted(rows, key=lambda r: (r["start_time"], r["event_id"]))
        for event in rows:
            if (
                event["camera"] != station.camera
                or station.zone not in event["zones"]
                or event["raw"].get("label") != "cat"
            ):
                fail(422, "event_outside_station_or_not_cat")
            pair = (station.id, event["event_id"])
            if pair in used_members:
                fail(422, "event_reused_within_station")
            used_members.add(pair)
        vstart = min(r["start_time"] for r in rows)
        provisional = any(r["end_time"] is None for r in rows)
        vend = None if provisional else max(r["end_time"] for r in rows)
        # Require the complete ownership range, including cross-midnight fragments.
        latest = max(r["end_time"] or r["start_time"] for r in rows)
        if vstart < start or vstart >= end or latest > end:
            fail(422, "expand_affected_interval_to_include_boundary_visit")
        key = f"{cfg.source_id}:{station.id}:{rows[0]['event_id']}"
        old = session.execute(select(db.visits).where(db.visits.c.candidate_key == key)).mappings().first()
        values = dict(
            candidate_key=key,
            station_id=station.id,
            pet_id=candidate.pet_id,
            identity_method=candidate.identity_method,
            start_time=vstart,
            end_time=vend,
            provisional=provisional,
            active=True,
            grouping_version=body.grouping_version,
        )
        old_ids = (
            set(
                session.execute(
                    select(db.visit_events.c.event_id).where(db.visit_events.c.visit_id == old["id"])
                ).scalars()
            )
            if old
            else set()
        )
        changed = not old or old_ids != {r["id"] for r in rows} or any(old[k] != v for k, v in values.items())
        visit_id = old["id"] if old else db.uid()
        if changed:
            touched.append(visit_id)
            values.update(
                revision=old["revision"] + 1 if old else 1,
                classification=None,
                model=None,
                prompt_version=None,
                input_fingerprint=None,
            )
            if old:
                session.execute(update(db.visits).where(db.visits.c.id == visit_id).values(**values))
                session.execute(delete(db.visit_events).where(db.visit_events.c.visit_id == visit_id))
                session.execute(delete(db.visit_media).where(db.visit_media.c.visit_id == visit_id))
            else:
                session.execute(insert(db.visits).values(id=visit_id, **values))
            for event in rows:
                session.execute(insert(db.visit_events).values(visit_id=visit_id, event_id=event["id"]))
        # Associate ready evidence, including cached evidence from before regrouping.
        mids = session.execute(
            select(db.media.c.id).where(
                db.media.c.event_id.in_([r["id"] for r in rows]), db.media.c.status == "ready"
            )
        ).scalars()
        for mid in mids:
            session.execute(
                insert(db.visit_media).values(visit_id=visit_id, media_id=mid).on_conflict_do_nothing()
            )
        submitted.append(visit_id)
    removed = [r["id"] for r in existing if r["id"] not in submitted]
    # Do not silently delete a visit whose start belongs outside the submitted range.
    if any(r["id"] in removed and r["start_time"] < start for r in existing):
        fail(409, "expand_affected_interval_to_include_prior_boundary_day")
    if removed:
        session.execute(update(db.visits).where(db.visits.c.id.in_(removed)).values(active=False))
    stale_for_visits(session, touched + removed)
    # A newly added visit can affect reports even when there was no previous join row.
    if touched or removed:
        session.execute(
            update(db.reports)
            .where(
                db.reports.c.local_start < body.local_end,
                db.reports.c.local_end > body.local_start,
                db.reports.c.status.in_(["published", "prepared"]),
            )
            .values(status="stale")
        )
    session.execute(
        update(db.days)
        .where(db.days.c.local_date >= body.local_start, db.days.c.local_date < body.local_end)
        .values(grouping_version=body.grouping_version)
    )
    return {"visit_ids": submitted, "changed": len(touched), "retired": len(removed)}


@router.put("/visits/{visit_id}/classification")
def classify(visit_id: UUID, body: s.Classification, session=Depends(session_for)):
    lease(session, body)
    visit = (
        session.execute(
            select(db.visits).where(db.visits.c.id == str(visit_id), db.visits.c.active.is_(True))
        )
        .mappings()
        .first()
    )
    if not visit or str(body.result.visit_id) != str(visit_id):
        fail(422, "invalid_visit")
    if body.result.pet_id != visit["pet_id"] or body.result.identity_method != visit["identity_method"]:
        fail(422, "classification_identity_mismatch")
    allowed = set(
        session.execute(
            select(db.visit_media.c.media_id)
            .join(db.media, db.media.c.id == db.visit_media.c.media_id)
            .where(db.visit_media.c.visit_id == str(visit_id), db.media.c.status == "ready")
        ).scalars()
    )
    for observation in body.result.observations:
        if not {str(x) for x in observation.evidence_ids} <= allowed:
            fail(422, "evidence_not_in_visit")
        if observation.assessment == "supported" and (
            not observation.evidence_ids
            or body.result.evidence_quality == "missing"
            or observation.behavior == "unknown"
        ):
            fail(422, "insufficient_evidence_for_supported_assessment")
        if "http://" in observation.reason or "https://" in observation.reason:
            fail(422, "links_not_allowed_in_model_output")
    value = body.result.model_dump(mode="json")
    changed = (
        visit["classification"] != value
        or visit["model"] != body.model
        or visit["prompt_version"] != body.prompt_version
        or visit["input_fingerprint"] != body.input_fingerprint
    )
    if changed:
        session.execute(
            update(db.visits)
            .where(db.visits.c.id == str(visit_id))
            .values(
                classification=value,
                model=body.model,
                prompt_version=body.prompt_version,
                input_fingerprint=body.input_fingerprint,
                revision=visit["revision"] + 1,
            )
        )
        stale_for_visits(session, [str(visit_id)])
    return {"visit_id": str(visit_id), "revision": visit["revision"] + int(changed)}


@router.post("/reports/prepare")
def prepare(body: s.Prepare, session=Depends(session_for)):
    lease(session, body)
    cfg = settings()
    length = (body.local_end - body.local_start).days
    if body.timezone != cfg.timezone or length != (1 if body.kind == "daily" else 7):
        fail(422, "invalid_report_calendar_interval")
    start, end = calendar_bounds(body.local_start, body.local_end, body.timezone)
    key_where = (
        db.reports.c.household_id == cfg.household_id,
        db.reports.c.kind == body.kind,
        db.reports.c.local_start == body.local_start,
        db.reports.c.local_end == body.local_end,
        db.reports.c.timezone == body.timezone,
    )
    old = session.execute(select(db.reports).where(*key_where)).mappings().first()
    counts = {"feeder": 0, "litterbox": 0}
    daily = {str(d): {"feeder": 0, "litterbox": 0} for d in date_list(body.local_start, body.local_end)}
    behaviors = {}
    snapshots = []
    mids = set()
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(body.timezone)
    rows = (
        session.execute(
            select(db.visits, db.stations.c.kind)
            .join(db.stations, db.visits.c.station_id == db.stations.c.id)
            .where(
                db.visits.c.active.is_(True),
                db.visits.c.start_time < end,
                (db.visits.c.end_time.is_(None) | (db.visits.c.end_time >= start)),
            )
            .order_by(db.visits.c.start_time, db.visits.c.id)
        )
        .mappings()
        .all()
    )
    for visit in rows:
        continuation = visit["start_time"] < start
        if not continuation:
            counts[visit["kind"]] = counts.get(visit["kind"], 0) + 1
            local_day = str(visit["start_time"].astimezone(zone).date())
            daily[local_day][visit["kind"]] = daily[local_day].get(visit["kind"], 0) + 1
            supported = {
                o["behavior"]
                for o in (visit["classification"] or {}).get("observations", [])
                if o["assessment"] == "supported"
            }
            for behavior in supported:
                behaviors[behavior] = behaviors.get(behavior, 0) + 1
        assets = (
            session.execute(
                select(db.media.c.id, db.media.c.kind, db.media.c.variant)
                .join(db.visit_media, db.media.c.id == db.visit_media.c.media_id)
                .where(db.visit_media.c.visit_id == visit["id"], db.media.c.status == "ready")
            )
            .mappings()
            .all()
        )
        mids.update(a["id"] for a in assets)
        snapshots.append(
            jsonable_encoder(
                {
                    "id": visit["id"],
                    "revision": visit["revision"],
                    "station": visit["station_id"],
                    "kind": visit["kind"],
                    "pet_id": visit["pet_id"],
                    "identity_method": visit["identity_method"],
                    "start": visit["start_time"],
                    "local_time": visit["start_time"].astimezone(zone).strftime("%a %b %d · %I:%M %p"),
                    "end": visit["end_time"],
                    "provisional": visit["provisional"],
                    "continuation": continuation,
                    "classification": visit["classification"],
                    "media": [dict(a) for a in assets],
                }
            )
        )
    processing = (
        session.execute(
            select(db.days).where(
                db.days.c.household_id == cfg.household_id,
                db.days.c.local_date >= body.local_start,
                db.days.c.local_date < body.local_end,
            )
        )
        .mappings()
        .all()
    )
    complete = {
        r["local_date"] for r in processing if r["collection_status"] == "complete" and r["grouping_version"]
    }
    missing = [str(d) for d in date_list(body.local_start, body.local_end) if d not in complete]
    facts = {
        "counts": counts,
        "daily": daily,
        "supported_behavior_visits": behaviors,
        "collection_status": "partial" if missing else "complete",
        "missing_dates": missing,
        "coverage": "unknown",
        "highlight_date": str(body.local_end - timedelta(days=1)),
        "visits": snapshots,
    }
    if old and old["facts"] == facts and old["status"] != "stale":
        return {"report_id": old["id"], "revision": old["revision"], "facts": facts}
    report_id = old["id"] if old else db.uid()
    revision = old["revision"] + 1 if old else 1
    if old:
        session.execute(
            insert(db.report_revisions)
            .values(report_id=report_id, revision=old["revision"], snapshot=jsonable_encoder(dict(old)))
            .on_conflict_do_nothing()
        )
        session.execute(delete(db.report_visits).where(db.report_visits.c.report_id == report_id))
        session.execute(delete(db.report_media).where(db.report_media.c.report_id == report_id))
        # Protect historical media while the report's archived revisions remain retained.
        historical = session.execute(
            select(db.report_revisions.c.snapshot).where(db.report_revisions.c.report_id == report_id)
        ).scalars()
        for history in historical:
            for v in history.get("facts", {}).get("visits", []):
                mids.update(a["id"] for a in v["media"])
    values = dict(
        household_id=cfg.household_id,
        kind=body.kind,
        local_start=body.local_start,
        local_end=body.local_end,
        timezone=body.timezone,
        revision=revision,
        facts=facts,
        narrative=None,
        status="prepared",
        expires_at=db.now() + timedelta(days=cfg.report_retention_days),
    )
    if old:
        session.execute(update(db.reports).where(db.reports.c.id == report_id).values(**values))
    else:
        session.execute(insert(db.reports).values(id=report_id, **values))
    for snapshot in snapshots:
        session.execute(
            insert(db.report_visits).values(
                report_id=report_id, visit_id=snapshot["id"], revision=snapshot["revision"], snapshot=snapshot
            )
        )
    for mid in mids:
        session.execute(insert(db.report_media).values(report_id=report_id, media_id=mid))
    return {"report_id": report_id, "revision": revision, "facts": facts}


@router.put("/reports/{report_id}/finalize")
def finalize(report_id: UUID, body: s.Finalize, session=Depends(session_for)):
    lease(session, body)
    row = session.execute(select(db.reports).where(db.reports.c.id == str(report_id))).mappings().first()
    if not row:
        fail(404, "report_not_found")
    if row["revision"] != body.expected_revision or row["status"] == "stale":
        fail(409, "report_revision_changed")
    if not body.use_template or body.narrative:
        fail(422, "v1_requires_template_narration; unverified_model_claims_are_not_published")
    summary = template_summary(row["facts"])
    if row["expires_at"] <= db.now():
        fail(410, "report_expired")
    session.execute(
        update(db.reports)
        .where(db.reports.c.id == str(report_id))
        .values(status="published", narrative=summary)
    )
    return {
        "report_id": str(report_id),
        "revision": row["revision"],
        "url": report_url(report_id),
        "summary": summary,
        "usable": bool(row["facts"]["visits"]) or row["facts"]["collection_status"] == "complete",
    }


@router.post("/deliveries/claim")
def delivery_claim(body: s.DeliveryClaim, session=Depends(session_for)):
    run = lease(session, body)
    cfg = settings()
    if not cfg.notifications_enabled or run["dry_run"] or not run["notify"]:
        fail(403, "notification_disabled_or_dry_run")
    from zoneinfo import ZoneInfo

    if (
        body.local_send_date != run["local_date"]
        or body.local_send_date != db.now().astimezone(ZoneInfo(cfg.timezone)).date()
    ):
        fail(403, "historical_notification_forbidden")
    report = (
        session.execute(select(db.reports).where(db.reports.c.id == str(body.report_id))).mappings().first()
    )
    if (
        not report
        or report["status"] != "published"
        or report["expires_at"] <= db.now()
        or report["local_end"] != body.local_send_date
    ):
        fail(409, "report_not_ready_for_delivery")
    if not report["facts"]["visits"] and report["facts"]["collection_status"] != "complete":
        fail(409, "no_usable_report")
    where = (
        db.deliveries.c.household_id == cfg.household_id,
        db.deliveries.c.local_send_date == body.local_send_date,
        db.deliveries.c.channel == body.channel,
    )
    row = session.execute(select(db.deliveries).where(*where)).mappings().first()
    if row and row["state"] != "definitely_failed":
        return {"state": row["state"], "may_send": False}
    attempt = db.uid()
    values = dict(
        household_id=cfg.household_id,
        local_send_date=body.local_send_date,
        channel=body.channel,
        report_id=str(body.report_id),
        state="claimed",
        attempt_id=attempt,
        claimed_at=db.now(),
        last_error=None,
    )
    if row:
        session.execute(update(db.deliveries).where(db.deliveries.c.id == row["id"]).values(**values))
    else:
        session.execute(insert(db.deliveries).values(**values))
    return {"state": "claimed", "attempt_id": attempt, "may_send": True}


@router.post("/deliveries/{attempt}/result")
def delivery_result(attempt: UUID, body: s.DeliveryResult, session=Depends(session_for)):
    run = lease(session, body)
    row = (
        session.execute(select(db.deliveries).where(db.deliveries.c.attempt_id == str(attempt)))
        .mappings()
        .first()
    )
    if not row or row["local_send_date"] != run["local_date"]:
        fail(404, "delivery_not_found")
    if row["state"] != "claimed":
        if row["state"] != body.result:
            fail(409, "delivery_result_already_recorded")
        return {"state": row["state"]}
    session.execute(
        update(db.deliveries)
        .where(db.deliveries.c.id == row["id"])
        .values(
            state=body.result,
            last_error=body.error_code,
            accepted_at=db.now() if body.result == "accepted" else None,
        )
    )
    return {"state": body.result}


@router.post("/runs/{run_id}/finish")
def finish(run_id: UUID, body: s.Finish, session=Depends(session_for)):
    if str(run_id) != str(body.run_id):
        fail(422, "run_id_mismatch")
    lease(session, body)
    session.execute(
        update(db.runs)
        .where(db.runs.c.id == str(run_id))
        .values(
            status=body.status,
            stage=body.stage,
            errors=(
                {"codes": body.error_codes, "diagnostic": body.diagnostic.model_dump()}
                if body.diagnostic
                else body.error_codes
            ),
            lease_until=db.now(),
        )
    )
    return {"status": body.status}


@router.get("/readiness")
def readiness():
    checks = {
        "database": "unavailable",
        "media": "unavailable",
        "frigate": "configured" if settings().frigate.password.get_secret_value() else "not_configured",
    }
    try:
        with db.sessions()() as session:
            migrated = session.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL")).scalar_one()
            if migrated:
                version = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
                checks["database"] = "ready" if version == "0001" else "migration_required"
            else:
                checks["database"] = "migration_required"
    except (SQLAlchemyError, RuntimeError):
        pass
    try:
        root = settings().media_root
        root.mkdir(parents=True, exist_ok=True)
        probe = root / (".probe-" + db.uid())
        with probe.open("xb") as output:
            output.write(b"ok")
        probe.unlink()
        checks["media"] = "ready"
    except OSError:
        pass
    ready = checks["database"] == checks["media"] == "ready"
    return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)


@router.post("/media/cache")
async def cache_media(body: s.Cache, request: Request, session=Depends(session_for)):
    from .evidence import EvidenceError, media_path

    lease(session, body)
    cfg = settings()
    rows = (
        session.execute(
            select(db.events).where(
                db.events.c.source_id == cfg.source_id, db.events.c.event_id.in_(body.event_ids)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != len(set(body.event_ids)):
        fail(422, "unknown_source_event")
    work = []
    results = []
    for event in rows:
        for variant in sorted(set(body.variants)):
            kind = "clip" if variant == "clip" else "snapshot"
            row = (
                session.execute(
                    select(db.media).where(
                        db.media.c.event_id == event["id"],
                        db.media.c.kind == kind,
                        db.media.c.variant == variant,
                    )
                )
                .mappings()
                .first()
            )
            if row and row["status"] == "ready" and media_path(row["relative_path"]).is_file():
                results.append({"media_id": row["id"], "status": "ready", "variant": variant})
                continue
            if row and row["status"] == "pending_delete":
                fail(409, "media_cleanup_in_progress")
            if (
                row
                and row["status"] == "downloading"
                and row["fetched_at"]
                and row["fetched_at"] > db.now() - timedelta(minutes=5)
            ):
                fail(409, "media_download_in_progress")
            mid = row["id"] if row else db.uid()
            relative = f"{mid[:2]}/{mid}." + ("mp4" if kind == "clip" else "jpg")
            values = dict(
                event_id=event["id"],
                kind=kind,
                variant=variant,
                relative_path=relative,
                status="downloading",
                fetched_at=db.now(),
                last_error=None,
            )
            if row:
                session.execute(update(db.media).where(db.media.c.id == mid).values(**values))
            else:
                session.execute(insert(db.media).values(id=mid, created_at=db.now(), **values))
            work.append((event, variant, mid, relative))
    # Persist recoverable download intents and release all transaction locks before network I/O.
    session.execute(
        update(db.runs)
        .where(db.runs.c.id == str(body.run_id))
        .values(lease_until=db.now() + timedelta(seconds=max(cfg.lease_seconds, 120)))
    )
    session.commit()
    downloaded = []
    for event, variant, mid, relative in work:
        lease(session, body)
        session.execute(
            update(db.runs)
            .where(db.runs.c.id == str(body.run_id))
            .values(lease_until=db.now() + timedelta(seconds=max(cfg.lease_seconds, 120)))
        )
        session.commit()
        try:
            async with asyncio.timeout(90):
                values = await request.app.state.frigate.download(event["event_id"], variant, relative)
            downloaded.append(
                (
                    mid,
                    {**values, "status": "ready", "fetched_at": db.now(), "last_error": None},
                    variant,
                    event["id"],
                )
            )
        except (EvidenceError, TimeoutError) as exc:
            code = str(exc) if isinstance(exc, EvidenceError) else "media_timeout"
            downloaded.append((mid, {"status": "failed", "last_error": code}, variant, event["id"]))
    lease(session, body)
    for mid, values, variant, eid in downloaded:
        session.execute(update(db.media).where(db.media.c.id == mid).values(**values))
        results.append(
            {
                "media_id": mid,
                "status": values["status"],
                "variant": variant,
                "error_code": values.get("last_error"),
            }
        )
    # Include pre-existing ready files in membership reconciliation.
    for result in results:
        if result["status"] == "ready":
            eid = session.execute(
                select(db.media.c.event_id).where(db.media.c.id == result["media_id"])
            ).scalar_one()
            vids = (
                session.execute(select(db.visit_events.c.visit_id).where(db.visit_events.c.event_id == eid))
                .scalars()
                .all()
            )
            for vid in vids:
                added = session.execute(
                    insert(db.visit_media)
                    .values(visit_id=vid, media_id=result["media_id"])
                    .on_conflict_do_nothing()
                    .returning(db.visit_media.c.media_id)
                ).scalar_one_or_none()
                if added:
                    stale_for_visits(session, [vid])
    for result in results:
        result["event_id"] = session.execute(
            select(db.events.c.event_id)
            .join(db.media, db.media.c.event_id == db.events.c.id)
            .where(db.media.c.id == result["media_id"])
        ).scalar_one()
        result["inference_path"] = "/internal/v1/media/" + result["media_id"]
    return {"media": results}


@router.get("/media/{media_id}")
def internal_media(media_id: UUID):
    from fastapi.responses import FileResponse
    from .evidence import media_path

    with db.sessions()() as session:
        row = (
            session.execute(
                select(db.media).where(db.media.c.id == str(media_id), db.media.c.status == "ready")
            )
            .mappings()
            .first()
        )
        if not row or not media_path(row["relative_path"]).is_file():
            fail(404, "media_unavailable")
        return FileResponse(
            media_path(row["relative_path"]),
            media_type=row["mime"],
            headers={"Cache-Control": "private, no-store"},
        )


@router.post("/maintenance/cleanup")
def cleanup(body: s.Cleanup, session=Depends(session_for)):
    from .evidence import media_path

    cfg = settings()
    lock(session, "household:" + cfg.household_id)
    current = db.now()
    expired = (
        session.execute(
            select(db.reports.c.id).where(
                db.reports.c.expires_at <= current, db.reports.c.status != "expired"
            )
        )
        .scalars()
        .all()
    )
    retained = (
        select(db.report_media.c.media_id)
        .join(db.reports, db.report_media.c.report_id == db.reports.c.id)
        .where(db.reports.c.expires_at > current)
    )
    candidates = (
        session.execute(
            select(db.media).where(
                (db.media.c.status != "downloading")
                | (db.media.c.fetched_at < current - timedelta(minutes=5)),
                ~db.media.c.id.in_(retained),
                db.media.c.created_at < current - timedelta(days=cfg.draft_media_grace_days),
            )
        )
        .mappings()
        .all()
    )
    if body.dry_run:
        return {"dry_run": True, "expired_reports": len(expired), "media_candidates": len(candidates)}
    if expired:
        session.execute(delete(db.report_media).where(db.report_media.c.report_id.in_(expired)))
        session.execute(delete(db.report_visits).where(db.report_visits.c.report_id.in_(expired)))
        session.execute(delete(db.report_revisions).where(db.report_revisions.c.report_id.in_(expired)))
        session.execute(
            update(db.reports)
            .where(db.reports.c.id.in_(expired))
            .values(status="expired", facts={}, narrative=None)
        )
    for row in candidates:
        session.execute(update(db.media).where(db.media.c.id == row["id"]).values(status="pending_delete"))
    session.commit()
    # File removal can be retried after a crash: pending_delete remains eligible.
    deleted_ids = []
    failed = 0
    for row in candidates:
        try:
            if row["relative_path"]:
                media_path(row["relative_path"]).unlink(missing_ok=True)
            deleted_ids.append(row["id"])
        except OSError:
            failed += 1
    lock(session, "household:" + cfg.household_id)
    if deleted_ids:
        session.execute(delete(db.visit_media).where(db.visit_media.c.media_id.in_(deleted_ids)))
        session.execute(
            delete(db.media).where(
                db.media.c.id.in_(deleted_ids), ~db.media.c.id.in_(select(db.report_media.c.media_id))
            )
        )
    session.execute(
        delete(db.operations).where(
            db.operations.c.created_at < current - timedelta(days=min(7, cfg.report_retention_days))
        )
    )
    cutoff = current - timedelta(days=cfg.normalized_record_retention_days)
    old_visits = select(db.visits.c.id).where(
        db.visits.c.start_time < cutoff,
        db.visits.c.end_time.is_not(None),
        db.visits.c.end_time < cutoff,
        ~db.visits.c.id.in_(select(db.report_visits.c.visit_id)),
    )
    session.execute(delete(db.visits).where(db.visits.c.id.in_(old_visits)))
    session.execute(
        delete(db.events).where(
            db.events.c.start_time < cutoff,
            db.events.c.end_time.is_not(None),
            ~db.events.c.id.in_(select(db.visit_events.c.event_id)),
            ~db.events.c.id.in_(select(db.media.c.event_id)),
        )
    )
    return {
        "dry_run": False,
        "expired_reports": len(expired),
        "deleted_media": len(deleted_ids),
        "failed_media": failed,
    }


@router.get("/runs/{run_id}")
def get_run(run_id: UUID):
    with db.sessions()() as session:
        row = session.execute(select(db.runs).where(db.runs.c.id == str(run_id))).mappings().first()
        if not row:
            fail(404, "run_not_found")
        return {key: value for key, value in row.items() if key != "lease_token"}


@router.get("/events")
def list_events(start: s.AwareDatetime, end: s.AwareDatetime, cursor: UUID | None = None, limit: int = 200):
    if start >= end or (end - start).days > 31 or not 1 <= limit <= 500:
        fail(422, "invalid_interval_or_limit")
    with db.sessions()() as session:
        query = select(db.events).where(
            db.events.c.source_id == settings().source_id,
            db.events.c.start_time < end,
            db.events.c.end_time.is_(None) | (db.events.c.end_time >= start),
        )
        if cursor:
            query = query.where(db.events.c.id > str(cursor))
        rows = session.execute(query.order_by(db.events.c.id).limit(limit + 1)).mappings().all()
        return {
            "events": [dict(r) for r in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }


@router.get("/deliveries")
def get_delivery(local_send_date: date):
    with db.sessions()() as session:
        rows = (
            session.execute(
                select(db.deliveries).where(
                    db.deliveries.c.household_id == settings().household_id,
                    db.deliveries.c.local_send_date == local_send_date,
                )
            )
            .mappings()
            .all()
        )
        return {"deliveries": [dict(row) for row in rows]}


@router.get("/visits/{visit_id}")
def get_visit(visit_id: UUID):
    with db.sessions()() as session:
        row = (
            session.execute(
                select(db.visits).where(db.visits.c.id == str(visit_id), db.visits.c.active.is_(True))
            )
            .mappings()
            .first()
        )
        if not row:
            fail(404, "visit_not_found")
        assets = (
            session.execute(
                select(
                    db.media.c.id, db.media.c.kind, db.media.c.variant, db.media.c.sha256, db.media.c.status
                )
                .join(db.visit_media, db.visit_media.c.media_id == db.media.c.id)
                .where(db.visit_media.c.visit_id == str(visit_id))
            )
            .mappings()
            .all()
        )
        return {**dict(row), "media": [dict(asset) for asset in assets]}
