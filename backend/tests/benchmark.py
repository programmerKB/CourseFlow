"""Opt-in 1,000-section / 20-client benchmark against a disposable *_test DB."""
import concurrent.futures
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from datetime import time as localtime
from sqlalchemy import select, func, text
import httpx
from app.db import SessionLocal
from app.models import Section, SectionMeeting, Course
from app.seed import seed, uid

if not os.environ['DATABASE_URL'].rsplit('/',1)[-1].endswith('_test'):
    raise RuntimeError('Benchmark requires a disposable *_test database')
seed('add-drop')
tid=uid('term/main')
with SessionLocal.begin() as db:
    current=db.scalar(select(func.count()).select_from(Section).where(Section.term_id==tid))
    for i in range(1000-current):
        cid=uid(f'benchmark/course/{i}')
        db.add(Course(id=cid,code=f'BENCH{i:04}',department_id=uid('dep/CS'),title=f'效能驗證課程 {i:04}',description='Synthetic benchmark fixture'))
        db.flush()
        sid=uid(f'benchmark/section/{i}')
        db.add(Section(id=sid,term_id=tid,course_id=cid,section_no='A',instructor_id=uid('teacher/chen'),credits='3.0',capacity=30,is_selectable=True))
        db.flush()
        db.add(SectionMeeting(section_id=sid,weekday=i%7+1,start_time=localtime(9),end_time=localtime(12),room='BENCH'))
    db.execute(text('ANALYZE'))

process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18001'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    with httpx.Client(base_url='http://127.0.0.1:18001',timeout=10,limits=httpx.Limits(max_connections=25,max_keepalive_connections=25)) as client:
        for _ in range(50):
            try:
                if client.get('/health/ready').status_code==200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        path=f'/api/v1/terms/{tid}/sections?q=課程&page_size=20'
        for _ in range(25): client.get(path).raise_for_status()
        def request(_):
            started=time.perf_counter();response=client.get(path);response.raise_for_status()
            assert response.json()['total']>=988
            return (time.perf_counter()-started)*1000
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            durations=sorted(pool.map(request,range(200)))
finally:
    process.terminate();process.wait(timeout=10)
report={'date':'2026-11-21','postgresql':None,'platform':platform.platform(),'cpu_count':os.cpu_count(),
        'memory_kib':int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))),
        'sections_in_term':1000,'concurrency':20,'warmup_requests':25,'measured_requests':200,
        'p50_ms':round(statistics.median(durations),2),'p95_ms':round(durations[189],2),'max_ms':round(durations[-1],2),'target_p95_ms':500}
queries=[('Catalog query',"SELECT * FROM v_public_section_catalog WHERE term_id=CAST(:term_id AS uuid) AND (code ILIKE :q OR title ILIKE :q) ORDER BY code,section_no,section_id LIMIT 20",{'term_id':tid,'q':'%課程%'}),
         ('Availability query',"SELECT * FROM v_section_availability WHERE section_id=CAST(:section_id AS uuid)",{'section_id':uid('section/main/CS304/A')})]
explain=[]
with SessionLocal() as db:
    report['postgresql']=db.scalar(text('SELECT version()'))
    for label,sql,params in queries:
        rows=db.execute(text('EXPLAIN (ANALYZE, BUFFERS) '+sql),params)
        explain.extend([label,sql,json.dumps(params,ensure_ascii=False),*list(rows.scalars()),''])
out=Path(__file__).resolve().parents[2]/'docs'
out.mkdir(exist_ok=True)
(out/'performance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
(out/'explain.txt').write_text('\n'.join(explain))
print(json.dumps(report,ensure_ascii=False,indent=2))
