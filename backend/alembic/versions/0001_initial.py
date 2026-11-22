"""Immutable initial PostgreSQL schema, indexes, catalog views and access grants."""
from pathlib import Path
from alembic import context, op
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    schema = Path(__file__).with_name('0001_schema.sql').read_text()
    if context.is_offline_mode():
        op.execute(schema.rstrip().removesuffix(';'))
    else:
        op.get_bind().exec_driver_sql(schema)


def downgrade():
    op.execute('DROP VIEW IF EXISTS v_public_section_meetings, v_public_section_catalog, v_section_availability')
    # Reverse dependency order; this database belongs exclusively to this application.
    for name in ['idempotency_keys', 'audit_events', 'notification_outbox', 'applications', 'enrollments',
                 'allocation_runs', 'section_prerequisites', 'course_completions', 'section_meetings',
                 'student_terms', 'sections', 'courses', 'students', 'instructors', 'registration_periods',
                 'terms', 'departments', 'auth_sessions', 'users']:
        op.execute('DROP TABLE ' + name)
