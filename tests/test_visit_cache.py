from test_server import seed


def test_visit_cache_metadata(pgclient):
    _, _, _, visit = seed(pgclient)
    response = pgclient.get(f"/internal/v1/visits/{visit}")
    assert response.status_code == 200
    assert response.json()["id"] == visit
    assert response.json()["classification"] is None
    assert response.json()["media"] == []


def test_finish_records_sanitized_diagnostic(pgclient):
    from test_server import call, claim, lease
    from petreport import db
    from sqlalchemy import select

    run = claim(pgclient)
    diagnostic = {"request_stage": "login", "http_status": 401, "code": "http_failure"}
    call(
        pgclient,
        f"/runs/{run['run_id']}/finish",
        {
            **lease(run),
            "status": "failed",
            "stage": "collect",
            "error_codes": ["dependency_unavailable"],
            "diagnostic": diagnostic,
        },
    )
    with db.sessions()() as session:
        assert session.execute(select(db.runs.c.errors)).scalar_one()["diagnostic"] == diagnostic
