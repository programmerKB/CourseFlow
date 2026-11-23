"""Add source-aware university catalogs and exact course aliases."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

LEGACY_SCHOOL = 'faa1fd88-b42b-57f1-9a47-6c076953de0f'

CATALOG_VIEW = '''
    SELECT s.id AS section_id, s.term_id, t.code AS term_code, s.course_id,
           c.code, c.title, c.description, c.department_id, d.name AS department_name,
           s.section_no, s.instructor_id, i.name AS instructor_name, s.credits,
           s.capacity, s.is_selectable, a.enrolled_count, a.remaining {extra_columns}
    FROM sections s
    JOIN terms t ON t.id = s.term_id
    JOIN courses c ON c.id = s.course_id
    JOIN departments d ON d.id = c.department_id
    JOIN instructors i ON i.id = s.instructor_id
    JOIN v_section_availability a ON a.section_id = s.id
    {extra_joins}
    WHERE t.catalog_published_at IS NOT NULL
'''


def upgrade():
    schools = op.create_table(
        'schools',
        sa.Column('id', UUID(as_uuid=False), primary_key=True),
        sa.Column('code', sa.String(16), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False),
    )
    op.bulk_insert(schools, [{'id': LEGACY_SCHOOL, 'code': 'NTUST', 'name': '國立臺灣科技大學'}])
    op.add_column('departments', sa.Column('school_id', UUID(as_uuid=False), nullable=False,
                                          server_default=sa.text(f"'{LEGACY_SCHOOL}'::uuid")))
    op.create_foreign_key('departments_school_id_fkey', 'departments', 'schools',
                          ['school_id'], ['id'], ondelete='RESTRICT')
    op.drop_constraint('departments_code_key', 'departments', type_='unique')
    op.create_unique_constraint('uq_department_school_code', 'departments', ['school_id', 'code'])

    op.add_column('courses', sa.Column('catalog_origin', sa.String(24), nullable=False,
                                      server_default='PROJECT_DEMO'))
    op.add_column('courses', sa.Column('source_url', sa.Text()))
    op.create_check_constraint('ck_course_catalog_source', 'courses', """
        (catalog_origin = 'PROJECT_DEMO' AND source_url IS NULL)
        OR (catalog_origin = 'UNIVERSITY_REFERENCE' AND source_url IS NOT NULL
            AND source_url LIKE 'https://%' AND length(source_url) > 8)
    """)
    op.create_table(
        'course_aliases',
        sa.Column('course_id', UUID(as_uuid=False), sa.ForeignKey('courses.id', ondelete='RESTRICT'),
                  primary_key=True),
        sa.Column('normalized_alias', sa.String(255), primary_key=True),
        sa.CheckConstraint("normalized_alias <> ''", name='ck_course_alias_not_empty'),
    )
    op.create_index('ix_course_alias_lookup', 'course_aliases', ['normalized_alias', 'course_id'])
    op.execute('CREATE OR REPLACE VIEW v_public_section_catalog AS ' + CATALOG_VIEW.format(
        extra_columns=', school.code AS school_code, school.name AS school_name, c.catalog_origin, c.source_url',
        extra_joins='JOIN schools school ON school.id = d.school_id',
    ))
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON schools, course_aliases TO course_app')
    op.execute('REVOKE ALL ON schools, course_aliases FROM course_public')


def downgrade():
    op.execute('''
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM courses WHERE catalog_origin = 'UNIVERSITY_REFERENCE')
            OR EXISTS (SELECT 1 FROM departments WHERE school_id <> 'faa1fd88-b42b-57f1-9a47-6c076953de0f')
            OR EXISTS (SELECT 1 FROM audit_events WHERE payload->>'channel' IN ('CHAT_TEXT', 'CHAT_VOICE')) THEN
            RAISE EXCEPTION 'Restore a pre-upgrade backup to downgrade a used university catalog or chat enrollment.';
          END IF;
        END $$
    ''')
    op.execute('DROP VIEW v_public_section_catalog')
    op.execute('CREATE VIEW v_public_section_catalog AS ' + CATALOG_VIEW.format(extra_columns='', extra_joins=''))
    op.execute('GRANT SELECT ON v_public_section_catalog TO course_app, course_public')
    op.drop_table('course_aliases')
    op.drop_column('courses', 'source_url')
    op.drop_column('courses', 'catalog_origin')
    op.drop_constraint('uq_department_school_code', 'departments', type_='unique')
    op.create_unique_constraint('departments_code_key', 'departments', ['code'])
    op.drop_column('departments', 'school_id')
    op.drop_table('schools')
