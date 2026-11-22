"""Idempotent synthetic fixtures. Existing terms, periods and registrations are never reset."""
import argparse
from datetime import datetime, date, time, timedelta, timezone
from uuid import uuid5, NAMESPACE_URL
from decimal import Decimal
from sqlalchemy import select
from .db import SessionLocal
from .models import (User, Department, Student, Instructor, Term, RegistrationPeriod, StudentTerm, Course,
                     Section, SectionMeeting, SectionPrerequisite, CourseCompletion, Application,
                     Enrollment, AllocationRun)
from .auth import hash_password


def uid(name):
    return str(uuid5(NAMESPACE_URL, 'ntust-course-demo/' + name))


def add(db, model, key, **values):
    row = db.get(model, key)
    if row is None:
        row = model(**values)
        db.add(row)
        db.flush()
    return row


def seed(mode='add-drop'):
    current = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        # Serialise concurrent invocations and retain all user-created records.
        db.execute(__import__('sqlalchemy').text("SELECT pg_advisory_xact_lock(73111021)"))
        cs, ee = uid('dep/CS'), uid('dep/EE')
        for code, name in [('CS', '資訊工程系'), ('EE', '電子工程系')]:
            add(db, Department, uid('dep/' + code), id=uid('dep/' + code), code=code, name=name)
        password = hash_password('Demo123!')
        add(db, User, uid('user/admin'), id=uid('user/admin'), login='admin', email='admin@example.test',
            password_hash=password, role='ADMIN', display_name='教務管理員')
        for n in range(1, 21):
            login = f'student{n:02}'
            add(db, User, uid('user/' + login), id=uid('user/' + login), login=login, email=login + '@example.test',
                password_hash=password, role='STUDENT', display_name=f'示範同學 {n:02}')
            add(db, Student, uid('student/' + login), id=uid('student/' + login), user_id=uid('user/' + login),
                name=f'示範同學 {n:02}', student_no=f'B115{n:05}', department_id=cs if n <= 10 else ee)
        for code, name, dep in [('chen', '陳明哲', cs), ('lin', '林雅涵', cs), ('wang', '王志遠', ee), ('huang', '黃思穎', ee)]:
            add(db, Instructor, uid('teacher/' + code), id=uid('teacher/' + code), name=name, department_id=dep)
        courses = [
            ('CS101', '程式設計', cs, '建立 Python 程式設計、問題分解與基礎演算法能力。'),
            ('CS201', '資料結構', cs, '探討串列、樹、雜湊與圖的資料表示及演算法效率。'),
            ('CS301', '資料庫設計', cs, '從 ER 模型、正規化到 SQL、交易與併發控制，實作可靠的資料庫系統。'),
            ('CS302', '作業系統', cs, '認識行程、記憶體管理、檔案系統與系統同步。'),
            ('CS303', '計算機網路', cs, '學習網路分層、路由、傳輸協定與應用服務。'),
            ('CS304', '軟體工程', cs, '以團隊專題實踐需求分析、設計、測試與版本管理。'),
            ('EE101', '電路學', ee, '介紹直流、交流電路分析與基本電路定理。'),
            ('EE201', '電子學', ee, '分析半導體、二極體與電晶體電路。'),
            ('EE301', '數位邏輯設計', ee, '從布林代數、組合邏輯到循序電路的設計。'),
            ('EE302', '訊號與系統', ee, '以時域與頻域觀點分析連續及離散系統。'),
            ('CS399', '人工智慧專題', cs, '專題研究班，本學期暫不開放選課。')]
        for code, title, dep, desc in courses:
            add(db, Course, uid('course/' + code), id=uid('course/' + code), code=code, title=title, department_id=dep, description=desc)
        history = add(db, Term, uid('term/history'), id=uid('term/history'), code='114-2 歷史學期',
                      start_date=date(2026, 2, 1), end_date=date(2026, 6, 30), allocation_seed='history')
        for n in range(1, 11):
            sid = uid(f'student/student{n:02}')
            for c in ('CS101', 'CS201'):
                add(db, CourseCompletion, (sid, uid('course/' + c), history.id), student_id=sid, course_id=uid('course/' + c), completed_term_id=history.id)
        specs = [('main', '115-1 示範學期', mode), ('initial', '115-2 初選登記示範', 'initial')]
        if mode in ('initial', 'short') and db.get(Term, uid('term/main')) is None:
            specs.append(('practice', '115-3 加退選練習', 'add-drop'))
        for slug, code, scenario in specs:
            tid = uid('term/' + slug)
            exists = db.get(Term, tid) is not None
            term = add(db, Term, tid, id=tid, code=code, start_date=date(2026, 9, 1) if slug == 'main' else date(2027, 2, 1),
                       end_date=date(2027, 1, 31) if slug == 'main' else date(2027, 6, 30),
                       catalog_published_at=current, allocation_seed='ntust-2026-fixed-seed')
            if not exists:
                if scenario == 'short':
                    a, b, c, d = current - timedelta(minutes=1), current + timedelta(minutes=3), current + timedelta(minutes=3), current + timedelta(minutes=30)
                elif scenario == 'initial':
                    a, b, c, d = current - timedelta(days=1), current + timedelta(days=7), current + timedelta(days=8), current + timedelta(days=30)
                elif scenario == 'awaiting':
                    a, b, c, d = current - timedelta(days=7), current - timedelta(hours=1), current - timedelta(minutes=30), current + timedelta(days=30)
                else:
                    a, b, c, d = current - timedelta(days=7), current - timedelta(days=2), current - timedelta(days=1), current + timedelta(days=30)
                db.add_all([RegistrationPeriod(term_id=tid, kind='INITIAL', starts_at=a, ends_at=b), RegistrationPeriod(term_id=tid, kind='ADD_DROP', starts_at=c, ends_at=d)])
                db.flush()
            for n in range(1, 21):
                sid = uid(f'student/student{n:02}')
                add(db, StudentTerm, (sid, tid), student_id=sid, term_id=tid, credit_limit=Decimal('3.0') if n == 20 else Decimal('25.0'))
            sections = [
                ('CS101', 'A', 'chen', 30, [(1, '09:00', '10:30', 'TR-211'), (3, '09:00', '10:30', 'TR-211')]),
                ('CS101', 'B', 'lin', 30, [(2, '09:00', '12:00', 'TR-212')]),
                ('CS201', 'A', 'lin', 30, [(1, '10:30', '12:00', 'TR-311'), (3, '10:30', '12:00', 'TR-311')]),
                ('CS301', 'A', 'chen', 30, [(2, '13:00', '15:00', 'TR-412'), (4, '13:00', '14:00', 'TR-412')]),
                ('CS302', 'A', 'lin', 25, [(1, '09:30', '12:30', 'TR-312')]),
                ('CS303', 'A', 'chen', 1, [(5, '09:00', '12:00', 'TR-413')]),
                ('CS304', 'A', 'lin', 1, [(4, '15:00', '18:00', 'TR-414')]),
                ('EE101', 'A', 'wang', 35, [(2, '09:00', '12:00', 'EE-201')]),
                ('EE201', 'A', 'huang', 30, [(3, '13:00', '16:00', 'EE-302')]),
                ('EE301', 'A', 'wang', 30, [(2, '15:00', '18:00', 'EE-303')]),
                ('EE302', 'A', 'huang', 30, [(5, '13:00', '16:00', 'EE-304')]),
                ('CS399', 'A', 'chen', 10, [(6, '09:00', '12:00', 'TR-510')])]
            for ccode, sno, teacher, cap, meetings in sections:
                secid = uid(f'section/{slug}/{ccode}/{sno}')
                add(db, Section, secid, id=secid, term_id=tid, course_id=uid('course/' + ccode), section_no=sno,
                    instructor_id=uid('teacher/' + teacher), credits=Decimal('3.0'), capacity=cap, is_selectable=ccode != 'CS399')
                for index, (day, start, end, room) in enumerate(meetings):
                    mid = uid(f'meeting/{secid}/{index}')
                    add(db, SectionMeeting, mid, id=mid, section_id=secid, weekday=day, start_time=time.fromisoformat(start), end_time=time.fromisoformat(end), room=room)
                if ccode in ('CS201', 'CS301', 'EE201'):
                    pre = {'CS201': 'CS101', 'CS301': 'CS201', 'EE201': 'EE101'}[ccode]
                    add(db, SectionPrerequisite, (secid, uid('course/' + pre)), section_id=secid, required_course_id=uid('course/' + pre))
            if not exists:
                # Both stages include one full class. CS303 always starts with exactly one seat.
                db.add(Enrollment(id=uid(f'enrollment/{slug}/full'), student_id=uid('student/student19'), term_id=tid,
                                  course_id=uid('course/CS304'), section_id=uid(f'section/{slug}/CS304/A'), status='ENROLLED', source='INITIAL'))
                if scenario == 'add-drop':
                    db.add(AllocationRun(id=uid('run/' + slug), term_id=tid, algorithm_version='v1', seed=term.allocation_seed,
                                         published_at=current - timedelta(days=1), accepted_count=0, rejected_count=0))
                if scenario in ('awaiting', 'short'):
                    for n in (1, 2):
                        db.add(Application(id=uid(f'application/{slug}/{n}'), student_id=uid(f'student/student{n:02}'),
                                           term_id=tid, course_id=uid('course/CS303'), section_id=uid(f'section/{slug}/CS303/A'), status='PENDING'))
    return {'mode': mode, 'main_term_id': uid('term/main'), 'initial_term_id': uid('term/initial')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', choices=['initial', 'awaiting', 'add-drop', 'short'], default='add-drop')
    print(seed(parser.parse_args().scenario))
