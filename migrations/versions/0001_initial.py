"""Initial report server schema. Frozen DDL; changes require a new revision."""

from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute(
        "\nCREATE TABLE operations (\n\tkey VARCHAR NOT NULL, \n\tfingerprint VARCHAR NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tresponse JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (key)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE pets (\n\tid VARCHAR NOT NULL, \n\tdisplay_name VARCHAR NOT NULL, \n\tidentity_policy JSONB NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE reports (\n\tid UUID NOT NULL, \n\thousehold_id VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tlocal_start DATE NOT NULL, \n\tlocal_end DATE NOT NULL, \n\ttimezone VARCHAR NOT NULL, \n\trevision INTEGER NOT NULL, \n\tfacts JSONB NOT NULL, \n\tnarrative TEXT, \n\tstatus VARCHAR NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (household_id, kind, local_start, local_end, timezone)\n)\n\n"
    )

    op.execute("CREATE INDEX ix_report_dates ON reports (local_start, local_end)")

    op.execute(
        "\nCREATE TABLE runs (\n\tid UUID NOT NULL, \n\thousehold_id VARCHAR NOT NULL, \n\tlocal_date DATE NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tstage VARCHAR NOT NULL, \n\tconfig_hash VARCHAR NOT NULL, \n\tlease_token UUID NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdry_run BOOLEAN NOT NULL, \n\tnotify BOOLEAN NOT NULL, \n\terrors JSONB NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (household_id, local_date)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE source_events (\n\tid UUID NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tevent_id VARCHAR NOT NULL, \n\tstart_time TIMESTAMP WITH TIME ZONE NOT NULL, \n\tend_time TIMESTAMP WITH TIME ZONE, \n\tcamera VARCHAR NOT NULL, \n\tzones JSONB NOT NULL, \n\traw JSONB NOT NULL, \n\thas_snapshot BOOLEAN NOT NULL, \n\thas_clip BOOLEAN NOT NULL, \n\tfetched_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, event_id)\n)\n\n"
    )

    op.execute("CREATE INDEX ix_events_start ON source_events (start_time)")

    op.execute(
        "\nCREATE TABLE stations (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tcamera VARCHAR NOT NULL, \n\tzone VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE day_processing (\n\thousehold_id VARCHAR NOT NULL, \n\tlocal_date DATE NOT NULL, \n\tcollection_status VARCHAR NOT NULL, \n\tgrouping_version VARCHAR, \n\tclassified_at TIMESTAMP WITH TIME ZONE, \n\tcoverage JSONB NOT NULL, \n\tlast_run_id UUID NOT NULL, \n\tPRIMARY KEY (household_id, local_date), \n\tFOREIGN KEY(last_run_id) REFERENCES runs (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE deliveries (\n\tid UUID NOT NULL, \n\thousehold_id VARCHAR NOT NULL, \n\tlocal_send_date DATE NOT NULL, \n\tchannel VARCHAR NOT NULL, \n\treport_id UUID NOT NULL, \n\tstate VARCHAR NOT NULL, \n\tattempt_id UUID NOT NULL, \n\tclaimed_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlast_error VARCHAR, \n\taccepted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (household_id, local_send_date, channel), \n\tFOREIGN KEY(report_id) REFERENCES reports (id), \n\tUNIQUE (attempt_id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE media (\n\tid UUID NOT NULL, \n\tevent_id UUID NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tvariant VARCHAR NOT NULL, \n\trelative_path VARCHAR, \n\tsha256 VARCHAR, \n\tbytes INTEGER, \n\tmime VARCHAR, \n\tstatus VARCHAR NOT NULL, \n\tfetched_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlast_error VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (event_id, kind, variant), \n\tFOREIGN KEY(event_id) REFERENCES source_events (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE report_revisions (\n\treport_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tsnapshot JSONB NOT NULL, \n\tPRIMARY KEY (report_id, revision), \n\tFOREIGN KEY(report_id) REFERENCES reports (id) ON DELETE CASCADE\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE visits (\n\tid UUID NOT NULL, \n\tcandidate_key VARCHAR NOT NULL, \n\tstation_id VARCHAR NOT NULL, \n\tpet_id VARCHAR, \n\tidentity_method VARCHAR NOT NULL, \n\tstart_time TIMESTAMP WITH TIME ZONE NOT NULL, \n\tend_time TIMESTAMP WITH TIME ZONE, \n\tprovisional BOOLEAN NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tgrouping_version VARCHAR NOT NULL, \n\trevision INTEGER NOT NULL, \n\tclassification JSONB, \n\tmodel JSONB, \n\tprompt_version VARCHAR, \n\tinput_fingerprint VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (candidate_key), \n\tFOREIGN KEY(station_id) REFERENCES stations (id), \n\tFOREIGN KEY(pet_id) REFERENCES pets (id)\n)\n\n"
    )

    op.execute("CREATE INDEX ix_visits_pet_start ON visits (pet_id, start_time)")

    op.execute("CREATE INDEX ix_visits_station_start ON visits (station_id, start_time)")

    op.execute(
        "\nCREATE TABLE report_media (\n\treport_id UUID NOT NULL, \n\tmedia_id UUID NOT NULL, \n\tUNIQUE (report_id, media_id), \n\tFOREIGN KEY(report_id) REFERENCES reports (id) ON DELETE CASCADE, \n\tFOREIGN KEY(media_id) REFERENCES media (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE report_visits (\n\treport_id UUID NOT NULL, \n\tvisit_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tsnapshot JSONB NOT NULL, \n\tUNIQUE (report_id, visit_id), \n\tFOREIGN KEY(report_id) REFERENCES reports (id) ON DELETE CASCADE, \n\tFOREIGN KEY(visit_id) REFERENCES visits (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE visit_events (\n\tvisit_id UUID NOT NULL, \n\tevent_id UUID NOT NULL, \n\tUNIQUE (visit_id, event_id), \n\tFOREIGN KEY(visit_id) REFERENCES visits (id) ON DELETE CASCADE, \n\tFOREIGN KEY(event_id) REFERENCES source_events (id)\n)\n\n"
    )

    op.execute(
        "\nCREATE TABLE visit_media (\n\tvisit_id UUID NOT NULL, \n\tmedia_id UUID NOT NULL, \n\tUNIQUE (visit_id, media_id), \n\tFOREIGN KEY(visit_id) REFERENCES visits (id) ON DELETE CASCADE, \n\tFOREIGN KEY(media_id) REFERENCES media (id) ON DELETE CASCADE\n)\n\n"
    )


def downgrade():
    op.execute("DROP TABLE visit_media")
    op.execute("DROP TABLE visit_events")
    op.execute("DROP TABLE report_visits")
    op.execute("DROP TABLE report_media")
    op.execute("DROP TABLE visits")
    op.execute("DROP TABLE report_revisions")
    op.execute("DROP TABLE media")
    op.execute("DROP TABLE deliveries")
    op.execute("DROP TABLE day_processing")
    op.execute("DROP TABLE stations")
    op.execute("DROP TABLE source_events")
    op.execute("DROP TABLE runs")
    op.execute("DROP TABLE reports")
    op.execute("DROP TABLE pets")
    op.execute("DROP TABLE operations")
