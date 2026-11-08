"""Preserve daily delivery fencing and audit explicit manual resend attempts."""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("ALTER TABLE deliveries ADD COLUMN resend_key VARCHAR NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE deliveries DROP CONSTRAINT deliveries_household_id_local_send_date_channel_key")
    op.execute(
        "ALTER TABLE deliveries ADD CONSTRAINT uq_deliveries_scope UNIQUE (household_id, local_send_date, channel, resend_key)"
    )


def downgrade():
    # A downgrade must never silently discard the resend audit trail.
    if (
        op.get_bind()
        .exec_driver_sql("SELECT EXISTS (SELECT 1 FROM deliveries WHERE resend_key <> '')")
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade while manual resend history exists")
    op.execute("ALTER TABLE deliveries DROP CONSTRAINT uq_deliveries_scope")
    op.execute(
        "ALTER TABLE deliveries ADD CONSTRAINT deliveries_household_id_local_send_date_channel_key UNIQUE (household_id, local_send_date, channel)"
    )
    op.execute("ALTER TABLE deliveries DROP COLUMN resend_key")
