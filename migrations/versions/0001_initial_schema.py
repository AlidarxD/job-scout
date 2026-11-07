"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-07 14:16:00.818052
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched", sa.Integer(), nullable=False),
        sa.Column("new", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_crawl_runs_source_key"), "crawl_runs", ["source_key"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("company_norm", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location_raw", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("countries", postgresql.ARRAY(sa.String(length=2)), nullable=False),
        sa.Column("remote", sa.String(length=16), nullable=False),
        sa.Column("remote_regions", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("seniority", sa.Integer(), nullable=False),
        sa.Column("salary_min_usd_month", sa.Float(), nullable=True),
        sa.Column("salary_max_usd_month", sa.Float(), nullable=True),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("desc_hash", sa.String(length=40), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(company,''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_company_norm"), "jobs", ["company_norm"], unique=False)
    op.create_index(
        "ix_jobs_countries", "jobs", ["countries"], unique=False, postgresql_using="gin"
    )
    op.create_index(op.f("ix_jobs_posted_at"), "jobs", ["posted_at"], unique=False)
    op.create_index(op.f("ix_jobs_remote"), "jobs", ["remote"], unique=False)
    op.create_index(op.f("ix_jobs_scope"), "jobs", ["scope"], unique=False)
    op.create_index("ix_jobs_search", "jobs", ["search"], unique=False, postgresql_using="gin")
    op.create_index(op.f("ix_jobs_seniority"), "jobs", ["seniority"], unique=False)
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_table(
        "profiles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "applications",
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("profile_id", "job_id"),
    )
    op.create_table(
        "job_sources",
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("directness", sa.Integer(), nullable=False),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source", "external_id"),
    )
    op.create_index(op.f("ix_job_sources_job_id"), "job_sources", ["job_id"], unique=False)
    op.create_table(
        "job_status_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_job_status_history_job_id"), "job_status_history", ["job_id"], unique=False
    )
    op.create_table(
        "scores",
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("gated", sa.Boolean(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("profile_id", "job_id"),
    )
    op.create_index(op.f("ix_scores_score"), "scores", ["score"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_scores_score"), table_name="scores")
    op.drop_table("scores")
    op.drop_index(op.f("ix_job_status_history_job_id"), table_name="job_status_history")
    op.drop_table("job_status_history")
    op.drop_index(op.f("ix_job_sources_job_id"), table_name="job_sources")
    op.drop_table("job_sources")
    op.drop_table("applications")
    op.drop_table("profiles")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_seniority"), table_name="jobs")
    op.drop_index("ix_jobs_search", table_name="jobs", postgresql_using="gin")
    op.drop_index(op.f("ix_jobs_scope"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_remote"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_posted_at"), table_name="jobs")
    op.drop_index("ix_jobs_countries", table_name="jobs", postgresql_using="gin")
    op.drop_index(op.f("ix_jobs_company_norm"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_crawl_runs_source_key"), table_name="crawl_runs")
    op.drop_table("crawl_runs")
