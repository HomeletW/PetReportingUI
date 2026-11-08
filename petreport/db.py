from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import sessionmaker
from .config import settings

metadata = MetaData()


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


def pk():
    return Column("id", UUID(as_uuid=False), primary_key=True, default=uid)


def fk(name, target, **kw):
    return Column(name, UUID(as_uuid=False), ForeignKey(target, **kw), nullable=False)


pets = Table(
    "pets",
    metadata,
    Column("id", String, primary_key=True),
    Column("display_name", String, nullable=False),
    Column("identity_policy", JSONB, nullable=False),
)
stations = Table(
    "stations",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, nullable=False),
    Column("camera", String, nullable=False),
    Column("zone", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("enabled", Boolean, nullable=False),
)
events = Table(
    "source_events",
    metadata,
    pk(),
    Column("source_id", String, nullable=False),
    Column("event_id", String, nullable=False),
    Column("start_time", DateTime(timezone=True), nullable=False),
    Column("end_time", DateTime(timezone=True)),
    Column("camera", String, nullable=False),
    Column("zones", JSONB, nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("has_snapshot", Boolean, nullable=False),
    Column("has_clip", Boolean, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source_id", "event_id"),
)
runs = Table(
    "runs",
    metadata,
    pk(),
    Column("household_id", String, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("status", String, nullable=False),
    Column("stage", String, nullable=False),
    Column("config_hash", String, nullable=False),
    Column("lease_token", UUID(as_uuid=False), nullable=False),
    Column("lease_until", DateTime(timezone=True), nullable=False),
    Column("dry_run", Boolean, nullable=False),
    Column("notify", Boolean, nullable=False),
    Column("errors", JSONB, nullable=False),
    UniqueConstraint("household_id", "local_date"),
)
days = Table(
    "day_processing",
    metadata,
    Column("household_id", String, primary_key=True),
    Column("local_date", Date, primary_key=True),
    Column("collection_status", String, nullable=False),
    Column("grouping_version", String),
    Column("classified_at", DateTime(timezone=True)),
    Column("coverage", JSONB, nullable=False),
    fk("last_run_id", "runs.id"),
)
visits = Table(
    "visits",
    metadata,
    pk(),
    Column("candidate_key", String, nullable=False, unique=True),
    Column("station_id", String, ForeignKey("stations.id"), nullable=False),
    Column("pet_id", String, ForeignKey("pets.id")),
    Column("identity_method", String, nullable=False),
    Column("start_time", DateTime(timezone=True), nullable=False),
    Column("end_time", DateTime(timezone=True)),
    Column("provisional", Boolean, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("grouping_version", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("classification", JSONB),
    Column("model", JSONB),
    Column("prompt_version", String),
    Column("input_fingerprint", String),
)
visit_events = Table(
    "visit_events",
    metadata,
    fk("visit_id", "visits.id", ondelete="CASCADE"),
    fk("event_id", "source_events.id"),
    UniqueConstraint("visit_id", "event_id"),
)
media = Table(
    "media",
    metadata,
    pk(),
    fk("event_id", "source_events.id"),
    Column("kind", String, nullable=False),
    Column("variant", String, nullable=False),
    Column("relative_path", String),
    Column("sha256", String),
    Column("bytes", Integer),
    Column("mime", String),
    Column("status", String, nullable=False),
    Column("fetched_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_error", String),
    UniqueConstraint("event_id", "kind", "variant"),
)
visit_media = Table(
    "visit_media",
    metadata,
    fk("visit_id", "visits.id", ondelete="CASCADE"),
    fk("media_id", "media.id", ondelete="CASCADE"),
    UniqueConstraint("visit_id", "media_id"),
)
reports = Table(
    "reports",
    metadata,
    pk(),
    Column("household_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("local_start", Date, nullable=False),
    Column("local_end", Date, nullable=False),
    Column("timezone", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("facts", JSONB, nullable=False),
    Column("narrative", Text),
    Column("status", String, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("household_id", "kind", "local_start", "local_end", "timezone"),
)
report_visits = Table(
    "report_visits",
    metadata,
    fk("report_id", "reports.id", ondelete="CASCADE"),
    fk("visit_id", "visits.id"),
    Column("revision", Integer, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    UniqueConstraint("report_id", "visit_id"),
)
report_media = Table(
    "report_media",
    metadata,
    fk("report_id", "reports.id", ondelete="CASCADE"),
    fk("media_id", "media.id"),
    UniqueConstraint("report_id", "media_id"),
)
deliveries = Table(
    "deliveries",
    metadata,
    pk(),
    Column("household_id", String, nullable=False),
    Column("local_send_date", Date, nullable=False),
    Column("channel", String, nullable=False),
    fk("report_id", "reports.id"),
    Column("state", String, nullable=False),
    Column("attempt_id", UUID(as_uuid=False), nullable=False, unique=True),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("last_error", String),
    Column("accepted_at", DateTime(timezone=True)),
    UniqueConstraint("household_id", "local_send_date", "channel"),
)
operations = Table(
    "operations",
    metadata,
    Column("key", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
    Column("status", String, nullable=False),
    Column("response", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
report_revisions = Table(
    "report_revisions",
    metadata,
    Column("report_id", UUID(as_uuid=False), ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("snapshot", JSONB, nullable=False),
)
Index("ix_events_start", events.c.start_time)
Index("ix_visits_station_start", visits.c.station_id, visits.c.start_time)
Index("ix_visits_pet_start", visits.c.pet_id, visits.c.start_time)
Index("ix_report_dates", reports.c.local_start, reports.c.local_end)


@lru_cache
def engine():
    url = settings().database_url.get_secret_value()
    if not url:
        raise RuntimeError("database_not_configured")
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=15000 -c lock_timeout=1000"},
        hide_parameters=True,
    )


@lru_cache
def sessions():
    return sessionmaker(engine(), expire_on_commit=False)
