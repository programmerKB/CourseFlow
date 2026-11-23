"""Consolidate published demo offerings into 115-1 and archive old demo terms."""
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def term_id(slug):
    return uuid5(NAMESPACE_URL, f'ntust-course-demo/term/{slug}')


MAIN = term_id('main')
# Prefer later demo offerings if multiple terms used the same course and class number.
DEMO_TERMS = tuple(map(term_id, ('university-catalog', 'recommendation-initial',
                                'recommendation-add-drop', 'practice', 'initial')))


def update_public_views(archive_filter):
    # The public catalog joins availability, so it inherits the same filter.
    op.execute(f'''
        CREATE OR REPLACE VIEW v_section_availability AS
        SELECT s.id AS section_id, s.term_id, s.course_id, s.capacity,
               COALESCE(e.n, 0)::integer AS enrolled_count,
               (s.capacity - COALESCE(e.n, 0))::integer AS remaining
        FROM sections s JOIN terms t ON t.id = s.term_id
        LEFT JOIN (
            SELECT section_id, COUNT(*) AS n FROM enrollments
            WHERE status = 'ENROLLED' GROUP BY section_id
        ) e ON e.section_id = s.id
        WHERE t.catalog_published_at IS NOT NULL{archive_filter}
    ''')
    op.execute(f'''
        CREATE OR REPLACE VIEW v_public_section_meetings AS
        SELECT m.id, m.section_id, s.term_id, m.weekday, m.start_time, m.end_time, m.room
        FROM section_meetings m JOIN sections s ON s.id = m.section_id
        JOIN terms t ON t.id = s.term_id
        WHERE t.catalog_published_at IS NOT NULL{archive_filter}
    ''')
    op.execute(f'''
        CREATE OR REPLACE VIEW v_public_section_topics AS
        SELECT s.term_id, s.id AS section_id, topic.code AS topic_code, topic.label AS topic_label
        FROM sections s JOIN terms t ON t.id = s.term_id
        JOIN course_topics ct ON ct.course_id = s.course_id
        JOIN topics topic ON topic.id = ct.topic_id
        WHERE t.catalog_published_at IS NOT NULL{archive_filter}
    ''')


def upgrade():
    op.add_column('terms', sa.Column('archived_at', sa.DateTime(timezone=True)))
    demo_ids = ', '.join(f"('{key}'::uuid, {priority})" for priority, key in enumerate(DEMO_TERMS))
    # Keep the old sections as targets for historical selection and audit records.
    op.execute(f'''
        WITH demos(id, priority) AS (VALUES {demo_ids}),
        incoming AS MATERIALIZED (
            SELECT DISTINCT ON (s.course_id, s.section_no)
                   s.*, gen_random_uuid() AS new_id
            FROM sections s
            JOIN demos ON demos.id = s.term_id
            JOIN terms t ON t.id = s.term_id
            WHERE t.catalog_published_at IS NOT NULL AND EXISTS (SELECT 1 FROM terms WHERE id = '{MAIN}')
            ORDER BY s.course_id, s.section_no, demos.priority, s.id
        ),
        added AS (
            INSERT INTO sections (id, term_id, course_id, section_no, instructor_id, credits, capacity, is_selectable)
            SELECT new_id, '{MAIN}', course_id, section_no, instructor_id, credits, capacity, is_selectable
            FROM incoming
            ON CONFLICT ON CONSTRAINT uq_section_number DO NOTHING
            RETURNING id
        ),
        copied_meetings AS (
            INSERT INTO section_meetings (id, section_id, weekday, start_time, end_time, room)
            SELECT gen_random_uuid(), added.id, m.weekday, m.start_time, m.end_time, m.room
            FROM added JOIN incoming ON incoming.new_id = added.id
            JOIN section_meetings m ON m.section_id = incoming.id
        )
        INSERT INTO section_prerequisites (section_id, required_course_id)
        SELECT added.id, p.required_course_id
        FROM added JOIN incoming ON incoming.new_id = added.id
        JOIN section_prerequisites p ON p.section_id = incoming.id
    ''')
    archived_ids = ', '.join(f"'{key}'" for key in (*DEMO_TERMS, term_id('history')))
    op.execute(f'UPDATE terms SET archived_at = CURRENT_TIMESTAMP WHERE id IN ({archived_ids})')
    update_public_views(' AND t.archived_at IS NULL')


def downgrade():
    update_public_views('')
    # The added 115-1 sections may now have selections, so keep their rows.
    op.drop_column('terms', 'archived_at')
