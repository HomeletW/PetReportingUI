"""Local cache reclamation; callers hold the household transaction lock."""

from sqlalchemy import delete, select, update

from . import db
from .evidence import media_path


def purge_media(session, media_ids):
    candidates = (
        session.execute(
            select(db.media).where(
                db.media.c.id.in_(media_ids),
                ~db.media.c.id.in_(select(db.report_media.c.media_id)),
                db.media.c.status != "downloading",
            )
        )
        .mappings()
        .all()
    )
    deleted = 0
    failed = 0
    for row in candidates:
        session.execute(update(db.media).where(db.media.c.id == row["id"]).values(status="pending_delete"))
        try:
            if row["relative_path"]:
                media_path(row["relative_path"]).unlink(missing_ok=True)
        except OSError:
            failed += 1
            continue
        session.execute(delete(db.visit_media).where(db.visit_media.c.media_id == row["id"]))
        session.execute(delete(db.media).where(db.media.c.id == row["id"]))
        deleted += 1
    return deleted, failed
