from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated
from uuid import UUID
from pydantic import BaseModel, Field, AfterValidator, model_validator, ConfigDict
from sqlalchemy import select, delete, func
from .models import (Term, RegistrationPeriod, Section, SectionMeeting, SectionPrerequisite, Course,
                     Instructor, Department, Enrollment)
from .errors import DomainError
from .registration import get_term, get_section, now, audit, section_dict

UUIDStr = Annotated[str, AfterValidator(lambda v: str(UUID(v)))]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TermInput(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    start_date: date
    end_date: date
    allocation_seed: str = Field(min_length=1, max_length=200)

    @model_validator(mode='after')
    def dates(self):
        if self.start_date > self.end_date:
            raise ValueError('學期開始日須早於或等於結束日')
        return self


class Period(StrictModel):
    kind: str = Field(pattern='^(INITIAL|ADD_DROP)$')
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode='after')
    def times(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError('時間必須包含時區')
        if self.starts_at >= self.ends_at:
            raise ValueError('開放時間必須早於截止時間')
        return self


class PeriodsInput(StrictModel):
    periods: list[Period] = Field(min_length=2, max_length=2)
    allocation_seed: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def ordered(self):
        p = {x.kind: x for x in self.periods}
        if len(p) != 2 or p['INITIAL'].ends_at > p['ADD_DROP'].starts_at:
            raise ValueError('初選必須先於加退選，兩個時段不能重疊')
        return self


class Meeting(StrictModel):
    weekday: int = Field(ge=1, le=7)
    start_time: time
    end_time: time
    room: str = Field(default='', max_length=255)

    @model_validator(mode='after')
    def ordered(self):
        if self.start_time.tzinfo or self.end_time.tzinfo or self.start_time >= self.end_time:
            raise ValueError('請填寫有效的本地開始及結束時間')
        return self


class SectionInput(StrictModel):
    course_id: UUIDStr
    section_no: str = Field(min_length=1, max_length=32)
    instructor_id: UUIDStr
    credits: Decimal = Field(gt=0, le=99.9, decimal_places=1)
    capacity: int = Field(ge=0, le=10000)
    is_selectable: bool = True
    meetings: list[Meeting] = Field(min_length=1, max_length=20)
    prerequisite_course_ids: list[UUIDStr] = Field(default_factory=list, max_length=30)


class SectionPatch(StrictModel):
    course_id: UUIDStr | None = None
    section_no: str | None = Field(default=None, min_length=1, max_length=32)
    instructor_id: UUIDStr | None = None
    credits: Decimal | None = Field(default=None, gt=0, le=99.9, decimal_places=1)
    capacity: int | None = Field(default=None, ge=0, le=10000)
    is_selectable: bool | None = None
    meetings: list[Meeting] | None = Field(default=None, min_length=1, max_length=20)
    prerequisite_course_ids: list[UUIDStr] | None = Field(default=None, max_length=30)


def validate_section(db, data):
    if not db.get(Course, data['course_id']) or not db.get(Instructor, data['instructor_id']):
        raise DomainError(422, 'INVALID_CATALOG_REFERENCE', '課程或教師不存在。')
    if data['course_id'] in data['prerequisite_course_ids']:
        raise DomainError(422, 'INVALID_PREREQUISITE', '課程不能以自己作為先修科目。')
    for cid in data['prerequisite_course_ids']:
        if not db.get(Course, cid):
            raise DomainError(422, 'INVALID_PREREQUISITE', '先修科目不存在。')
    meetings = data['meetings']
    for index, a in enumerate(meetings):
        for b in meetings[index + 1:]:
            if a['weekday'] == b['weekday'] and a['start_time'] < b['end_time'] and b['start_time'] < a['end_time']:
                raise DomainError(422, 'MEETING_OVERLAP', '同班上課時間不能重疊。')
    duplicate = db.scalar(select(Section.id).where(Section.term_id == data['term_id'], Section.course_id == data['course_id'], Section.section_no == data['section_no'], Section.id != data.get('id')))
    if duplicate:
        raise DomainError(409, 'SECTION_EXISTS', '此課程班別序號已存在。')


def term_dict(t):
    return {'id': t.id, 'code': t.code, 'start_date': t.start_date, 'end_date': t.end_date,
            'catalog_published_at': t.catalog_published_at, 'allocation_seed': t.allocation_seed}


def create_term(db, data, actor):
    if db.scalar(select(Term.id).where(Term.code == data.code)):
        raise DomainError(409, 'TERM_EXISTS', '學期代碼已存在。')
    t = Term(**data.model_dump())
    db.add(t)
    db.flush()
    audit(db, actor, t.id, 'TERM_CREATED', 'term', t.id, data.model_dump(mode='json'))
    return term_dict(t)


def set_periods(db, term_id, data, actor):
    t = get_term(db, term_id, 'update', public=False)
    if t.catalog_published_at:
        raise DomainError(409, 'CATALOG_FROZEN', '學期公開後不可更改時段或分發種子。')
    for value in data.periods:
        p = db.get(RegistrationPeriod, (term_id, value.kind))
        if p:
            p.starts_at, p.ends_at = value.starts_at, value.ends_at
        else:
            db.add(RegistrationPeriod(term_id=term_id, **value.model_dump()))
    if data.allocation_seed is not None:
        t.allocation_seed = data.allocation_seed
    audit(db, actor, term_id, 'PERIODS_UPDATED', 'term', term_id, data.model_dump(mode='json'))
    return {'periods': [x.model_dump() for x in data.periods]}


def save_section(db, term_id, data, actor, section_id=None):
    term = get_term(db, term_id, 'update', public=False)
    section = get_section(db, term_id, section_id, True) if section_id else None
    changes = data.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise DomainError(422, 'NULL_FIELD', '班別欄位不能是 null。')
    if term.catalog_published_at:
        if not section or any(k != 'capacity' for k in changes):
            raise DomainError(409, 'CATALOG_FROZEN', '學期公開後只可調整容量。')
        n = db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.section_id == section_id, Enrollment.status == 'ENROLLED'))
        if changes.get('capacity', section.capacity) < n:
            raise DomainError(409, 'CAPACITY_BELOW_ENROLLED', '新容量不可少於目前有效選課人數。', {'enrolled_count': n})
        old = section.capacity
        section.capacity = changes.get('capacity', old)
        audit(db, actor, term_id, 'CAPACITY_UPDATED', 'section', section_id, {'before': old, 'after': section.capacity})
    else:
        if section:
            existing = section_dict(db, section)
            full = {k: getattr(section, k) for k in ('course_id', 'section_no', 'instructor_id', 'credits', 'capacity', 'is_selectable')}
            full['meetings'] = [{k: m[k] for k in ('weekday', 'start_time', 'end_time', 'room')} for m in existing['meetings']]
            full['prerequisite_course_ids'] = [p['id'] for p in existing['prerequisites']]
            full.update(changes)
        else:
            full = data.model_dump()
        validate_section(db, {**full, 'term_id': term_id, 'id': section_id})
        attrs = {k: v for k, v in full.items() if k not in ('meetings', 'prerequisite_course_ids')}
        if not section:
            section = Section(term_id=term_id, **attrs)
            db.add(section)
            db.flush()
        else:
            for k, v in attrs.items():
                setattr(section, k, v)
            db.execute(delete(SectionMeeting).where(SectionMeeting.section_id == section_id))
            db.execute(delete(SectionPrerequisite).where(SectionPrerequisite.section_id == section_id))
        db.add_all([SectionMeeting(section_id=section.id, **m) for m in full['meetings']])
        db.add_all([SectionPrerequisite(section_id=section.id, required_course_id=cid) for cid in set(full['prerequisite_course_ids'])])
        audit(db, actor, term_id, 'SECTION_UPDATED' if section_id else 'SECTION_CREATED', 'section', section.id, {'fields': list(changes)})
    db.flush()
    return section_dict(db, section)


def publish(db, term_id, actor):
    term = get_term(db, term_id, 'update', public=False)
    if term.catalog_published_at:
        return term_dict(term)
    periods = list(db.scalars(select(RegistrationPeriod).where(RegistrationPeriod.term_id == term_id)))
    try:
        PeriodsInput(periods=[Period(kind=p.kind, starts_at=p.starts_at, ends_at=p.ends_at) for p in periods])
    except ValueError:
        raise DomainError(409, 'INVALID_PERIODS', '請先設定不重疊的初選與加退選時間。')
    for s in db.scalars(select(Section).where(Section.term_id == term_id)):
        d = section_dict(db, s)
        if not d['meetings']:
            raise DomainError(409, 'MEETINGS_REQUIRED', '每班至少需要一個上課時段。')
        validate_section(db, {**d, 'prerequisite_course_ids': [p['id'] for p in d['prerequisites']]})
    term.catalog_published_at = now(db)
    audit(db, actor, term_id, 'CATALOG_PUBLISHED', 'term', term_id)
    return term_dict(term)
