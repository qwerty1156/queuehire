"""Indexes for tenant-scoped list ordering and filtered list/count queries."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # Equality on the leading columns permits a backwards scan of created/id.
    op.create_index("ix_jobs_tenant_created_id", "jobs", ["tenant_id", "created_at", "id"])
    op.create_index("ix_jobs_tenant_status_created_id", "jobs", ["tenant_id", "status", "created_at", "id"])


def downgrade():
    op.drop_index("ix_jobs_tenant_status_created_id", table_name="jobs")
    op.drop_index("ix_jobs_tenant_created_id", table_name="jobs")
