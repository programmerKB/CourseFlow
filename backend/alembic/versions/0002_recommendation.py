"""Add research tags and student-owned selection plans."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'topics',
        sa.Column('id', postgresql.UUID(), primary_key=True),
        sa.Column('code', sa.String(64), nullable=False, unique=True),
        sa.Column('label', sa.String(100), nullable=False),
    )
    op.create_table(
        'course_topics',
        sa.Column('course_id', postgresql.UUID(), sa.ForeignKey('courses.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('topic_id', postgresql.UUID(), sa.ForeignKey('topics.id', ondelete='RESTRICT'), primary_key=True),
    )
    op.create_index('ix_course_topics_topic_course', 'course_topics', ['topic_id', 'course_id'])
    op.create_table(
        'selection_plans',
        sa.Column('id', postgresql.UUID(), primary_key=True),
        sa.Column('student_id', postgresql.UUID(), nullable=False),
        sa.Column('term_id', postgresql.UUID(), nullable=False),
        sa.Column('phase', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='DRAFT'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(['student_id', 'term_id'], ['student_terms.student_id', 'student_terms.term_id'],
                                ondelete='RESTRICT', name='fk_plan_student_term'),
        sa.UniqueConstraint('id', 'term_id', name='uq_plan_term'),
        sa.CheckConstraint("phase IN ('INITIAL','ADD_DROP')", name='ck_plan_phase'),
        sa.CheckConstraint("status IN ('DRAFT','SUBMITTED')", name='ck_plan_status'),
        sa.CheckConstraint('created_at < expires_at', name='ck_plan_expiry'),
        sa.CheckConstraint("(status = 'SUBMITTED') = (confirmed_at IS NOT NULL)", name='ck_plan_confirmation'),
    )
    op.create_index('ix_plan_student_term', 'selection_plans', ['student_id', 'term_id'])
    op.create_table(
        'selection_plan_items',
        sa.Column('plan_id', postgresql.UUID(), primary_key=True),
        sa.Column('section_id', postgresql.UUID(), primary_key=True),
        sa.Column('term_id', postgresql.UUID(), nullable=False),
        sa.Column('course_id', postgresql.UUID(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id', 'term_id'], ['selection_plans.id', 'selection_plans.term_id'],
                                ondelete='RESTRICT', name='fk_plan_item_plan'),
        sa.ForeignKeyConstraint(['section_id', 'term_id', 'course_id'], ['sections.id', 'sections.term_id', 'sections.course_id'],
                                ondelete='RESTRICT', name='fk_plan_item_section'),
        sa.UniqueConstraint('plan_id', 'position', name='uq_plan_item_position'),
        sa.UniqueConstraint('plan_id', 'course_id', name='uq_plan_item_course'),
        sa.CheckConstraint('position BETWEEN 1 AND 5', name='ck_plan_item_position'),
    )
    op.drop_constraint('ck_idempotency_success', 'idempotency_keys', type_='check')
    op.create_check_constraint('ck_idempotency_success', 'idempotency_keys', 'response_status IN (200,201,204)')
    op.execute('''
        CREATE VIEW v_public_section_topics AS
        SELECT s.term_id, s.id AS section_id, topic.code AS topic_code, topic.label AS topic_label
        FROM sections s
        JOIN terms t ON t.id = s.term_id
        JOIN course_topics ct ON ct.course_id = s.course_id
        JOIN topics topic ON topic.id = ct.topic_id
        WHERE t.catalog_published_at IS NOT NULL
    ''')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON topics, course_topics, selection_plans, selection_plan_items TO course_app')
    op.execute('REVOKE ALL ON topics, course_topics, selection_plans, selection_plan_items FROM course_public')
    op.execute('GRANT SELECT ON v_public_section_topics TO course_public, course_app')


def downgrade():
    # A previous application cannot safely replay committed batch selections.
    op.execute('''
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM selection_plans WHERE status = 'SUBMITTED')
            OR EXISTS (SELECT 1 FROM idempotency_keys WHERE response_status = 200) THEN
            RAISE EXCEPTION 'Cannot downgrade after confirmed selection plans. Restore a pre-upgrade backup.';
          END IF;
        END $$
    ''')
    op.drop_constraint('ck_idempotency_success', 'idempotency_keys', type_='check')
    op.create_check_constraint('ck_idempotency_success', 'idempotency_keys', 'response_status IN (201,204)')
    op.execute('DROP VIEW v_public_section_topics')
    for table in ('selection_plan_items', 'selection_plans', 'course_topics', 'topics'):
        op.drop_table(table)
