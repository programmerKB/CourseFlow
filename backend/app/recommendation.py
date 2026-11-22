"""Recommend public sections and persist a student-owned preview."""
from datetime import time, timedelta
from decimal import Decimal
from unicodedata import normalize
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text

from . import registration as reg
from .db import SessionLocal
from .errors import DomainError
from .models import Department, RegistrationPeriod, SelectionPlan, SelectionPlanItem


# Changes to this mapping change how the same research question is ranked.
TOPIC_MAP_VERSION = '1'
TOPIC_MAP = {
    'llm': {
        'label': '大語言模型',
        'aliases': ('大語言模型', '大型語言模型', '大型語言模型研究', 'LLM', 'large language model', 'large language models'),
        'related': ('nlp', 'retrieval', 'deep_learning', 'ml_systems'),
        'keywords': ('大語言模型', '大型語言模型', 'LLM', '語言模型', '自然語言處理', 'NLP',
                     '資訊檢索', 'RAG', '向量資料庫', '語意搜尋', '模型部署', '深度學習', '模型量化'),
    },
}

PREVIEW_REASONS = {
    **reg.REASONS,
    'APPLICATION_EXISTS': '已有此課程的待分發登記。',
    'TIME_PREFERENCE_MISMATCH': '上課時間不在你指定的可上課時段內。',
    'TARGET_COUNT_REACHED': '已達目標門數。',
    'TERM_INELIGIBLE': '尚無此學期的修課資格。',
    'REGISTRATION_PHASE_CHANGED': '選課階段已變更，請更新試排。',
    'NO_FEASIBLE_SECTIONS': '目前沒有可加入試排的班別。',
}


class AvailableTime(BaseModel):
    weekday: int = Field(ge=1, le=7)
    start_time: time
    end_time: time

    @model_validator(mode='after')
    def validate_interval(self):
        if self.start_time.tzinfo or self.end_time.tzinfo or self.start_time >= self.end_time:
            raise ValueError('Available times must be increasing local times on the same weekday')
        return self


def normalize_keyword(value):
    return normalize('NFKC', value).strip().casefold()


def find_department(db, name, department_id):
    if department_id:
        try:
            key = str(UUID(department_id))
        except ValueError:
            key = None
        if key:
            department = db.get(Department, key)
            if department:
                return department
    requested = normalize_keyword(name or '')
    if not requested:
        return None
    departments = list(db.scalars(select(Department)))
    exact = [d for d in departments if requested in (normalize_keyword(d.name), normalize_keyword(d.code))]
    if exact:
        return exact[0] if len(exact) == 1 else None

    short_name = requested.replace('學系', '系')
    aliases = []
    for department in departments:
        title = normalize_keyword(department.name).replace('學系', '系')
        names = {title}
        if title.endswith('工程系'):
            names.add(title.removesuffix('工程系') + '系')
        if title == '資訊工程系':
            names.add('資工系')
        if short_name in names:
            aliases.append(department)
    return aliases[0] if len(aliases) == 1 else None


def topic_code(topic):
    requested = normalize_keyword(topic)
    for code, entry in TOPIC_MAP.items():
        if requested == code or requested in {normalize_keyword(alias) for alias in entry['aliases']}:
            return code
    return None


def resolve_subject(topic, keywords):
    code = topic_code(topic)
    if code:
        entry = TOPIC_MAP[code]
        # Synonyms describe the same request. Extra student-specified keywords
        # may affect ranking, an LLM-generated repetition of the topic may not.
        extras = [word for word in keywords if topic_code(word) != code]
        terms = (*entry['keywords'], *extras)
        label, related = entry['label'], entry['related']
    else:
        terms = (topic, *keywords)
        label, related = topic.strip() or '、'.join(dict.fromkeys(word.strip() for word in keywords if word.strip())), ()
    normalized = list(dict.fromkeys(filter(None, (normalize_keyword(term) for term in terms))))
    return code, label, related, normalized


# Every branch is fixed SQL. strpos treats '%' and '_' as literal characters.
# Correlated subqueries aggregate topic hits before ranking one row per section.
RELATED_SECTIONS = text('''
    WITH matches AS (
        SELECT c.section_id, c.code, c.title, c.description, c.section_no,
            ARRAY(SELECT DISTINCT t.topic_label FROM v_public_section_topics t
                  WHERE t.term_id = c.term_id AND t.section_id = c.section_id
                    AND t.topic_code = :topic_code ORDER BY t.topic_label) AS direct_labels,
            ARRAY(SELECT DISTINCT t.topic_label FROM v_public_section_topics t
                  WHERE t.term_id = c.term_id AND t.section_id = c.section_id
                    AND t.topic_code = ANY(CAST(:related_topics AS text[])) ORDER BY t.topic_label) AS related_labels,
            ARRAY(SELECT DISTINCT t.topic_code FROM v_public_section_topics t
                  WHERE t.term_id = c.term_id AND t.section_id = c.section_id ORDER BY t.topic_code) AS topic_codes,
            (SELECT key.word FROM unnest(CAST(:keywords AS text[])) WITH ORDINALITY AS key(word, position)
             WHERE strpos(lower(c.title), key.word) > 0 OR strpos(lower(c.description), key.word) > 0
             ORDER BY key.position LIMIT 1) AS keyword_hit
        FROM v_public_section_catalog c WHERE c.term_id = CAST(:term_id AS uuid)
    ), ranked AS (
        SELECT *, (CASE WHEN cardinality(direct_labels) > 0 THEN 6 ELSE 0 END
                 + CASE WHEN cardinality(related_labels) > 0 THEN 3 ELSE 0 END
                 + CASE WHEN keyword_hit IS NOT NULL THEN 1 ELSE 0 END) AS score
        FROM matches
    )
    SELECT * FROM ranked WHERE score > 0 ORDER BY score DESC, code, section_no, section_id LIMIT 20
''')


def search_related_sections(engine, term_id, topic, keywords):
    code, label, related, words = resolve_subject(topic, keywords)
    with engine.connect() as connection:
        rows = connection.execute(RELATED_SECTIONS, {
            'term_id': term_id, 'topic_code': code, 'related_topics': list(related), 'keywords': words,
        }).mappings().all()
    return code, label, rows


DEPARTMENT_SECTIONS = text('''
    SELECT section_id, department_name, 1 AS score
    FROM v_public_section_catalog
    WHERE term_id = CAST(:term_id AS uuid) AND department_id = CAST(:department_id AS uuid)
      AND (NOT :has_seats OR (is_selectable AND remaining > 0))
    ORDER BY is_selectable DESC, code, section_no, section_id LIMIT 20
''')


def match_reason(row):
    reasons = []
    if row['direct_labels']:
        reasons.append('主題標籤：' + '、'.join(row['direct_labels']))
    if row['related_labels']:
        reasons.append('相關標籤：' + '、'.join(row['related_labels']))
    if row['keyword_hit']:
        word = row['keyword_hit']
        if word in row['title'].lower():
            reasons.append(f'課名「{row["title"]}」符合關鍵字')
        else:
            description = row['description']
            offset = max(description.lower().find(word), 0)
            snippet = description[max(0, offset - 28):offset + len(word) + 38].strip()
            reasons.append(f'大綱提到「{snippet}」')
    return '；'.join(reasons) + '。'


def learning_extensions(code, matches):
    if code != 'llm':
        return []
    extensions = []
    for title, next_step, tag_order in (
        ('語言模型與檢索增強生成', '比較不同文件檢索策略對回答正確率的影響。', ('nlp', 'retrieval', 'llm')),
        ('模型效率與服務部署', '比較量化前後的推論延遲與回答品質。', ('deep_learning', 'ml_systems', 'llm')),
    ):
        relevant = [str(row['section_id']) for tag in tag_order for row in matches if tag in row['topic_codes']]
        if any(any(tag in row['topic_codes'] for tag in tag_order[:-1]) for row in matches):
            extensions.append({'title': title, 'next_step': next_step, 'section_ids': list(dict.fromkeys(relevant))})
    return extensions


def within_availability(meetings, availability):
    return not availability or all(any(
        meeting['weekday'] == slot.weekday and slot.start_time <= meeting['start_time'] and meeting['end_time'] <= slot.end_time
        for slot in availability
    ) for meeting in meetings)


def build_selection_plan(engine, student_id, term_id, topic, keywords, target_count, availability,
                         *, department=None, has_seats=False):
    if department is None:
        code, label, matches = search_related_sections(engine, term_id, topic, keywords)
    else:
        code, label = None, department.name
        with engine.connect() as connection:
            matches = connection.execute(DEPARTMENT_SECTIONS, {
                'term_id': term_id, 'department_id': department.id, 'has_seats': has_seats,
            }).mappings().all()
    with SessionLocal.begin() as db:
        term = reg.get_term(db, term_id, 'share')
        try:
            student_term = reg.student_term(db, student_id, term_id, lock=True)
        except DomainError as error:
            if error.code != 'TERM_INELIGIBLE':
                raise
            student_term = None
        registration = reg.registration_status(db, term)
        phase = registration['phase']
        open_for_confirmation = phase in ('INITIAL', 'ADD_DROP')
        policy_phase = 'INITIAL' if phase == 'INITIAL' else 'ADD_DROP'
        baseline = reg.load_admission_state(db, student_term, term) if student_term else None
        baseline_credits = reg.baseline_credits(baseline, policy_phase) if baseline is not None else Decimal('0.0')
        ids = [str(row['section_id']) for row in matches]
        sections = {row['id']: row for row in reg.section_rows(db, ids)}
        selected, candidates = [], []
        for row in matches:
            section = sections[str(row['section_id'])]
            if baseline is None:
                eligibility, reason = None, 'TERM_INELIGIBLE'
            else:
                eligibility_reason, _ = reg.evaluate_admission(term_id, section, baseline, policy_phase)
                reason = eligibility_reason
                if not reason and not within_availability(section['meetings'], availability):
                    reason = 'TIME_PREFERENCE_MISMATCH'
                if not open_for_confirmation:
                    eligibility_reason = eligibility_reason or 'REGISTRATION_CLOSED'
                    reason = reason or 'REGISTRATION_CLOSED'
                eligibility = {'eligible': eligibility_reason is None, 'reason_code': eligibility_reason}
                if not reason:
                    if len(selected) >= target_count:
                        reason = 'TARGET_COUNT_REACHED'
                    else:
                        reason, _ = reg.evaluate_admission(term_id, section, baseline, policy_phase, planned=selected)
            if reason is None:
                selected.append(section)
            candidates.append({
                'section_id': section['id'], 'course_code': section['code'], 'course_title': section['title'],
                'section_no': section['section_no'], 'credits': str(section['credits']), 'meetings': section['meetings'],
                'remaining': section['remaining'], 'is_selectable': section['is_selectable'],
                'prerequisites': section['prerequisites'], 'eligibility': eligibility,
                'score': row['score'], 'selected': reason is None, 'reason_code': reason,
                'match_reason': (f'開課系所：{row["department_name"]}。' if department is not None else match_reason(row)),
            })

        plan = None
        if student_term is None:
            unavailable_reason = 'TERM_INELIGIBLE'
        elif not open_for_confirmation:
            unavailable_reason = 'REGISTRATION_CLOSED'
        elif not selected:
            unavailable_reason = 'NO_FEASIBLE_SECTIONS'
        else:
            created_at = reg.now(db)
            window = db.get(RegistrationPeriod, (term_id, phase))
            if reg.phase(db, term, created_at) != phase:
                unavailable_reason = 'REGISTRATION_PHASE_CHANGED'
                for candidate in candidates:
                    if candidate['selected']:
                        candidate.update(selected=False, reason_code=unavailable_reason)
            else:
                expires_at = min(created_at + timedelta(minutes=15), window.ends_at)
                draft = SelectionPlan(student_id=student_id, term_id=term_id, phase=phase, status='DRAFT',
                                      created_at=created_at, expires_at=expires_at)
                db.add(draft)
                db.flush()
                db.add_all(SelectionPlanItem(plan_id=draft.id, term_id=term_id, section_id=section['id'],
                                             course_id=section['course_id'], position=position)
                           for position, section in enumerate(selected, 1))
                db.flush()
                plan = {'plan_id': draft.id, 'term_id': term_id, 'phase': phase, 'as_of': registration['as_of'],
                        'expires_at': expires_at, 'section_ids': [section['id'] for section in selected],
                        'projected_credits': str(baseline_credits + sum(section['credits'] for section in selected))}
                unavailable_reason = None

        return {
            'topic': label, 'target_count': target_count, 'candidates': candidates,
            'learning_extensions': learning_extensions(code, matches), 'plan': plan,
            'baseline': None if baseline is None else {
                'enrolled': baseline.enrolled, 'pending': baseline.pending,
                'considered_credits': str(baseline_credits), 'credit_limit': str(baseline.credit_limit),
            },
            'registration': registration, 'plan_unavailable_reason': unavailable_reason,
        }


def describe_plan(recommendation):
    candidates, plan, target = (recommendation[key] for key in ('candidates', 'plan', 'target_count'))
    count = len(plan['section_ids']) if plan else 0
    if not candidates:
        return '這個學期找不到符合條件的班別。請調整系所或關鍵字。'
    message = f'找到 {len(candidates)} 個相關班別，目標 {target} 門，目前可試排 {count} 門。'
    if not plan:
        message += PREVIEW_REASONS[recommendation['plan_unavailable_reason']]
    if count < target:
        reasons = dict.fromkeys(row['reason_code'] for row in candidates if row['reason_code'])
        descriptions = [PREVIEW_REASONS[reason].rstrip('。') for reason in reasons if reason in PREVIEW_REASONS][:3]
        if descriptions:
            message += '主要限制：' + '、'.join(descriptions) + '。'
    return message
