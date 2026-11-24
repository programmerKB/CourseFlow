"""Give existing demo instructors the same school-based labels as new seeds."""
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

LEGACY_NAMES = {
    'chen': '陳明哲',
    'lin': '林雅涵',
    'wang': '王志遠',
    'huang': '黃思穎',
    **{code: f'{code} 示範教師' for code in (
        'NTU-EE', 'NTU-ET', 'NTU-CS', 'NTU-TE', 'NTNU-EE', 'NTNU-CS', 'NTUST-EE', 'NTUST-ET', 'NTUST-CS',
    )},
}
DEMO_NAME = "s.name || ' - 示範教師'"


def rename_demo_instructors(previous_name, new_name):
    # Match both the seed identity and its expected label to preserve edited records.
    statement = sa.text(f'''
        UPDATE instructors AS i
        SET name = {new_name}
        FROM departments AS d
        JOIN schools AS s ON s.id = d.school_id
        WHERE i.id = CAST(:instructor_id AS uuid)
          AND i.department_id = d.id
          AND i.name = {previous_name}
    ''')
    for code, name in LEGACY_NAMES.items():
        instructor_id = str(uuid5(NAMESPACE_URL, f'ntust-course-demo/teacher/{code}'))
        op.execute(statement.bindparams(instructor_id=instructor_id, legacy_name=name))


def upgrade():
    rename_demo_instructors(':legacy_name', DEMO_NAME)


def downgrade():
    rename_demo_instructors(DEMO_NAME, ':legacy_name')
