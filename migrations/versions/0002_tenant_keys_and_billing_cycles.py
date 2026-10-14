"""Scope idempotency keys and add auditable processing cycles.

Existing ledger entries are retained verbatim; their cycle stays NULL because
the broken historical code did not record reliable cycle boundaries.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade():
    uniques = sa.inspect(op.get_bind()).get_unique_constraints("jobs")
    global_key = next(c for c in uniques if c["column_names"] == ["idempotency_key"])
    with op.batch_alter_table("jobs", naming_convention=NAMING) as batch:
        batch.drop_constraint(global_key["name"] or "uq_jobs_idempotency_key", type_="unique")
        batch.add_column(sa.Column("billing_cycle", sa.Integer, nullable=False, server_default="1"))
        batch.create_unique_constraint("uq_jobs_tenant_key", ["tenant_id", "idempotency_key"])
    with op.batch_alter_table("ledger") as batch:
        batch.add_column(sa.Column("billing_cycle", sa.Integer, nullable=True))
        batch.create_unique_constraint("uq_ledger_job_cycle_kind", ["job_id", "billing_cycle", "kind"])


def downgrade():
    # Reinstating global uniqueness is only lossless if tenants have not reused
    # a key. Refuse instead of deleting/renaming existing jobs.
    jobs = sa.table("jobs", sa.column("idempotency_key"))
    duplicate = op.get_bind().scalar(
        sa.select(jobs.c.idempotency_key).group_by(jobs.c.idempotency_key)
        .having(sa.func.count() > 1).limit(1)
    )
    if duplicate is not None:
        raise RuntimeError("Cannot downgrade: idempotency keys overlap between tenants")
    with op.batch_alter_table("ledger") as batch:
        batch.drop_constraint("uq_ledger_job_cycle_kind", type_="unique")
        batch.drop_column("billing_cycle")
    with op.batch_alter_table("jobs", naming_convention=NAMING) as batch:
        batch.drop_constraint("uq_jobs_tenant_key", type_="unique")
        batch.drop_column("billing_cycle")
        batch.create_unique_constraint("uq_jobs_idempotency_key", ["idempotency_key"])
