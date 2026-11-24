"""Teacher specification: instructor sign-in, grades, an 18-credit ceiling and Gmail API evidence.

Existing terms, sections, selections and notifications keep their IDs. Student-term credit
limits above 18 are capped and each change is recorded in audit_events. Enrollments that
already exceed the effective limit are listed by v_credit_limit_overloads for maintenance.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

OVERLOAD_VIEW = '''
    CREATE VIEW v_credit_limit_overloads AS
    SELECT st.student_id, st.term_id, t.code AS term_code, s.student_no,
           st.credit_limit, LEAST(st.credit_limit, 18.0) AS effective_credit_limit,
           SUM(sec.credits) AS enrolled_credits
    FROM student_terms st
    JOIN terms t ON t.id = st.term_id
    JOIN students s ON s.id = st.student_id
    JOIN enrollments e ON e.student_id = st.student_id AND e.term_id = st.term_id AND e.status = 'ENROLLED'
    JOIN sections sec ON sec.id = e.section_id
    GROUP BY st.student_id, st.term_id, t.code, s.student_no, st.credit_limit
    HAVING SUM(sec.credits) > LEAST(st.credit_limit, 18.0)
'''


def upgrade():
    op.drop_constraint('ck_user_role', 'users', type_='check')
    op.create_check_constraint('ck_user_role', 'users', "role IN ('STUDENT','INSTRUCTOR','ADMIN')")

    op.add_column('instructors', sa.Column('user_id', UUID(as_uuid=False), nullable=True))
    op.create_foreign_key('fk_instructor_user', 'instructors', 'users', ['user_id'], ['id'], ondelete='RESTRICT')
    op.create_unique_constraint('instructors_user_id_key', 'instructors', ['user_id'])
    op.create_index('ix_sections_instructor_term', 'sections', ['instructor_id', 'term_id'])

    # Record each capped limit before changing it, so the upgrade is traceable.
    op.execute('''
        INSERT INTO audit_events (id, actor_user_id, term_id, action, target_type, target_id, occurred_at, payload)
        SELECT gen_random_uuid(), NULL, st.term_id, 'CREDIT_LIMIT_CAPPED', 'student_term', st.student_id,
               CURRENT_TIMESTAMP,
               jsonb_build_object('student_id', st.student_id, 'previous_credit_limit', st.credit_limit,
                                  'credit_limit', 18.0, 'migration', '0006')
        FROM student_terms st WHERE st.credit_limit > 18
    ''')
    op.execute('UPDATE student_terms SET credit_limit = 18.0 WHERE credit_limit > 18')
    op.drop_constraint('ck_credit_limit', 'student_terms', type_='check')
    op.create_check_constraint('ck_credit_limit', 'student_terms', 'credit_limit BETWEEN 0 AND 18')

    op.create_table(
        'grades',
        sa.Column('enrollment_id', UUID(as_uuid=False), sa.ForeignKey('enrollments.id', ondelete='RESTRICT'),
                  primary_key=True),
        sa.Column('score', sa.Numeric(5, 2), nullable=False),
        sa.Column('graded_by_user_id', UUID(as_uuid=False), sa.ForeignKey('users.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('score BETWEEN 0 AND 100', name='ck_grade_score'),
        sa.CheckConstraint('created_at <= updated_at', name='ck_grade_times'),
    )
    op.add_column('notification_outbox', sa.Column('provider_message_id', sa.String(255)))
    op.execute(OVERLOAD_VIEW)

    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON grades TO course_app')
    op.execute('GRANT SELECT ON v_credit_limit_overloads TO course_app')
    op.execute('REVOKE ALL ON grades, v_credit_limit_overloads FROM course_public')


def downgrade():
    op.execute('''
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM grades) OR EXISTS (SELECT 1 FROM users WHERE role = 'INSTRUCTOR') THEN
            RAISE EXCEPTION 'Grades or instructor accounts exist. Restore a pre-upgrade backup to downgrade.';
          END IF;
        END $$
    ''')
    op.execute('DROP VIEW v_credit_limit_overloads')
    op.drop_column('notification_outbox', 'provider_message_id')
    op.drop_table('grades')
    # Capped limits stay at 18; the previous values remain in audit_events.
    op.drop_constraint('ck_credit_limit', 'student_terms', type_='check')
    op.create_check_constraint('ck_credit_limit', 'student_terms', 'credit_limit >= 0')
    op.drop_index('ix_sections_instructor_term', 'sections')
    op.drop_constraint('instructors_user_id_key', 'instructors', type_='unique')
    op.drop_constraint('fk_instructor_user', 'instructors', type_='foreignkey')
    op.drop_column('instructors', 'user_id')
    op.drop_constraint('ck_user_role', 'users', type_='check')
    op.create_check_constraint('ck_user_role', 'users', "role IN ('STUDENT','ADMIN')")
