import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  DAYS,
  PHASES,
  STATUS,
  dateTime,
  newRequestKey,
  short,
  type Candidate,
  type Meeting,
  type Plan,
  type PlanConfirmation,
  type Recommendation,
  type Registration,
  type Roadmap,
  type Section,
} from "./api";

const REASONS: Record<string, string> = {
  ALREADY_ENROLLED: "已選上同課程",
  APPLICATION_EXISTS: "已有同課程的待分發登記",
  CREDIT_LIMIT_EXCEEDED: "超過學分上限",
  COURSE_COMPLETED: "已有通過紀錄",
  NO_FEASIBLE_SECTIONS: "目前沒有符合試排條件的班別",
  PREREQUISITE_MISSING: "尚未通過先修課程",
  REGISTRATION_CLOSED: "目前不在可送出的選課時段",
  REGISTRATION_PHASE_CHANGED: "選課階段已變更，請更新試排",
  SECTION_FULL: "班別已額滿",
  SECTION_UNAVAILABLE: "班別未開放選課",
  TARGET_COUNT_REACHED: "已排滿目標門數",
  TERM_INELIGIBLE: "本學期無修課資格",
  TIME_CONFLICT: "與課表或試排班別衝堂",
  TIME_PREFERENCE_MISMATCH: "上課時間不符指定時段",
};

function reason(code: string | null | undefined) {
  return (code && REASONS[code]) || "目前無法加入試排";
}

function MeetingList({ meetings }: { meetings: Meeting[] }) {
  return (
    <ul className="recommend-meetings">
      {meetings.map((m, index) => (
        <li key={m.id ?? index}>
          週{DAYS[m.weekday]} {short(m.start_time)}–{short(m.end_time)} ·{" "}
          {m.room || "教室待公告"}
        </li>
      ))}
    </ul>
  );
}

function BaselineList({
  title,
  sections,
  onOpenSection,
}: {
  title: string;
  sections: Section[];
  onOpenSection: (id: string) => void;
}) {
  return (
    <section>
      <h5>{title}</h5>
      {sections.length ? (
        <ul className="recommend-courses">
          {sections.map((section) => (
            <li key={section.id}>
              <button className="course-title" onClick={() => onOpenSection(section.id)}>
                {section.code} {section.title} · {section.section_no} 班
              </button>
              <MeetingList meetings={section.meetings} />
            </li>
          ))}
        </ul>
      ) : (
        <p>無</p>
      )}
    </section>
  );
}

type Attempt = { key: string; sectionIds: string[] };
type SubmissionError = { message: string; sectionId?: string; sectionIds: string[] };

function PlanReview({
  recommendation,
  plan,
  registration,
  onOpenSection,
  onConfirmed,
  onViewResult,
}: {
  recommendation: Recommendation;
  plan: Plan;
  registration: Registration;
  onOpenSection: (id: string) => void;
  onConfirmed: () => void;
  onViewResult: (phase: Plan["phase"]) => void;
}) {
  const [selected, setSelected] = useState(() => new Set(plan.section_ids));
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [error, setError] = useState<SubmissionError | null>(null);
  const [result, setResult] = useState<PlanConfirmation | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [expired, setExpired] = useState(() => Date.now() >= Date.parse(plan.expires_at));
  const inFlight = useRef<AbortController | null>(null);

  useLayoutEffect(() => () => inFlight.current?.abort(), []);
  useEffect(() => {
    const delay = Date.parse(plan.expires_at) - Date.now();
    setExpired(delay <= 0);
    if (delay > 0) {
      const timer = setTimeout(() => setExpired(true), delay);
      return () => clearTimeout(timer);
    }
  }, [plan.expires_at]);

  const byId = new Map(recommendation.candidates.map((entry) => [entry.section_id, entry]));
  const planned = plan.section_ids.flatMap((id) => {
    const entry = byId.get(id);
    return entry ? [entry] : [];
  });
  const enrollmentOpen = registration.phase === plan.phase;
  const initial = plan.phase === "INITIAL";
  const baseline = recommendation.baseline;
  const currentTenths = Math.round(Number(baseline?.considered_credits ?? 0) * 10);
  const additionalTenths = planned.reduce(
    (total, item) => total + (selected.has(item.section_id) ? Math.round(Number(item.credits) * 10) : 0),
    0,
  );
  const projected = ((currentTenths + additionalTenths) / 10).toFixed(1);

  async function submit() {
    if (inFlight.current || result) return;
    if (!attempt && (!selected.size || expired || !enrollmentOpen)) return;
    const control = new AbortController();
    inFlight.current = control;
    setSubmitting(true);
    setError(null);
    let request: Attempt | undefined;
    try {
      request = attempt ?? { key: newRequestKey(), sectionIds: [...selected].sort() };
      const response = await api<PlanConfirmation>(
        `/me/terms/${plan.term_id}/selection-plans/${plan.plan_id}/confirm`,
        {
          method: "POST",
          signal: control.signal,
          headers: { "Idempotency-Key": request.key },
          body: JSON.stringify({ section_ids: request.sectionIds }),
        },
      );
      if (control.signal.aborted) return;
      if (response?.plan_id !== plan.plan_id || !Array.isArray(response.items)) {
        throw new ApiError(0, "INVALID_RESPONSE", "無法確認提交結果，請重試同一提交。");
      }
      setAttempt(null);
      setResult(response);
      onConfirmed();
    } catch (error) {
      if (control.signal.aborted) return;
      const unknown = error instanceof ApiError && (error.status === 0 || error.status === 408 || error.status >= 500);
      if (request && unknown) setAttempt(request);
      else setAttempt(null);
      setError({
        message: unknown
          ? "目前無法確認送出結果。請重試同一提交，以取回原結果。"
          : error instanceof Error ? error.message : "操作未完成，請重試。",
        sectionId: error instanceof ApiError ? error.details?.section_id : undefined,
        sectionIds: request?.sectionIds ?? [],
      });
    } finally {
      inFlight.current = null;
      if (!control.signal.aborted) setSubmitting(false);
    }
  }

  function toggle(id: string) {
    if (attempt || result || submitting) return;
    setError(null);
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section className="recommend-plan">
      <h4>課表試排</h4>
      <p>
        草案到期 {dateTime(plan.expires_at)}（臺北） · {PHASES[plan.phase]}
      </p>
      <div className="recommend-plan-grid">
        {baseline && (
          <BaselineList title="正式課表" sections={baseline.enrolled} onOpenSection={onOpenSection} />
        )}
        {initial && baseline && (
          <BaselineList title="已有待分發登記" sections={baseline.pending} onOpenSection={onOpenSection} />
        )}
        <fieldset disabled={submitting || !!attempt || !!result}>
          <legend>建議新增課程</legend>
          <ul className="recommend-courses">
            {planned.map((course) => {
              const id = course.section_id;
              const outcome = result?.items.find((item) => item.section_id === id);
              const rejected = !attempt && error?.sectionIds.includes(id);
              return (
                <li key={id} className={error?.sectionId === id ? "plan-conflict" : undefined}>
                  <label>
                    <input type="checkbox" checked={selected.has(id)} onChange={() => toggle(id)} />
                    {course.course_code} {course.course_title} · {course.section_no} 班
                  </label>
                  <MeetingList meetings={course.meetings} />
                  {outcome && <small>{STATUS[outcome.status]}</small>}
                  {rejected && <small>{error?.sectionId === id ? "此班有變動 · 未提交" : "未提交"}</small>}
                </li>
              );
            })}
          </ul>
        </fieldset>
      </div>
      <div className="recommend-confirm">
        {!result && (
          <div aria-live="polite">
            <strong>預估 {projected} 學分</strong>
            <small>已勾選 {selected.size} 門{baseline && ` · 上限 ${baseline.credit_limit} 學分`}</small>
          </div>
        )}
        {result ? (
          <>
            <p role="status">{result.items.length} 門{initial ? "已登記，等待分發。" : "已加選，正式課表已更新。"}</p>
            <button onClick={() => onViewResult(result.phase)}>
              {initial ? "查看我的選課" : "查看我的課表"}
            </button>
          </>
        ) : (
          <>
            {error && <p className="plan-error" role="alert">{error.message}</p>}
            {!attempt && expired && <p role="status">草案已到期，請更新試排。</p>}
            {!attempt && !expired && !enrollmentOpen && (
              <p role="status">選課階段已變更，請更新試排。</p>
            )}
            {initial && <small>初選登記結果以分發為準。</small>}
            <button
              className="primary"
              disabled={submitting || (!attempt && (!selected.size || expired || !enrollmentOpen))}
              onClick={submit}
            >
              {submitting ? "提交中…" : attempt ? "重試同一提交" : initial ? "確認初選登記" : "確認加選"}
            </button>
          </>
        )}
      </div>
    </section>
  );
}

function CourseCard({ course, onOpenSection }: { course: Candidate; onOpenSection: (id: string) => void }) {
  const prerequisites = course.prerequisites;
  const placement = course.selected ? "已納入試排" : course.reason_code && reason(course.reason_code);
  return (
    <li className="recommend-course-card">
      <button className="course-title" onClick={() => onOpenSection(course.section_id)}>
        {course.course_code} {course.course_title} · {course.section_no} 班
      </button>
      <div className="recommend-card-status">
        <span>{course.school_name} {course.department_name}</span>
        <span>{course.credits} 學分</span>
        <span>{course.remaining ? `剩餘 ${course.remaining} 席` : "已額滿"}</span>
        {!course.is_selectable && <span>未開放</span>}
      </div>
      <p>{course.match_reason}</p>
      {course.source_url && <p><a href={course.source_url} target="_blank" rel="noreferrer">校方課程來源</a></p>}
      <MeetingList meetings={course.meetings} />
      <p>
        先修 {prerequisites.length ? prerequisites.map((p) => `${p.code} ${p.title}`).join("、") : "無指定科目"}
      </p>
      {course.eligibility && (
        <p>個別資格 {course.eligibility.eligible ? "符合" : reason(course.eligibility.reason_code)}</p>
      )}
      {placement && <strong>{placement}</strong>}
    </li>
  );
}

const STAGES = { FOUNDATION: "基礎", SPECIALIZATION: "進階", RESEARCH: "研究專題" };
const AVAILABILITY = {
  OFFERED: "本期有公開班別",
  NO_PUBLIC_SECTION: "本期無公開班別",
  CURRICULUM_REFERENCE: "校方課程規劃參考",
  TERM_NOT_SELECTED: "選擇學期後查看班別",
};

function ResearchRoadmap({ roadmap, onOpenSection }: { roadmap: Roadmap; onOpenSection: (id: string) => void }) {
  const nextSections = new Set(roadmap.next_section_ids);
  return (
    <section aria-label="研究修課路徑">
      <h4>{roadmap.title}修課路徑</h4>
      <div className="roadmap-stages">
        {roadmap.stages.map((stage) => (
          <section key={stage.stage} className="roadmap-stage">
            <h5>{STAGES[stage.stage]}</h5>
            <ul>{stage.goals.map((goal) => <li key={goal}>{goal}</li>)}</ul>
            {!!stage.courses.length && (
              <ul className="roadmap-courses">
                {stage.courses.map((course) => (
                  <li key={course.course_code ?? course.title}>
                    <strong>{course.title}</strong>
                    <p>{course.school_name} {course.department_name}</p>
                    <div className="recommend-card-status">
                      <span>{AVAILABILITY[course.availability]}</span>
                      {course.completed && <span>已通過</span>}
                    </div>
                    {course.source_url && <a href={course.source_url} target="_blank" rel="noreferrer">校方課程來源</a>}
                    {course.sections.map((section) => (
                      <div className="roadmap-section" key={section.section_id}>
                        <button className="course-title" onClick={() => onOpenSection(section.section_id)}>
                          {course.course_code} {section.section_no} 班
                        </button>
                        <span>{section.remaining > 0 ? `剩餘 ${section.remaining} 席` : "已額滿"} · {
                          nextSections.has(section.section_id) ? "建議優先修課" : section.eligibility.eligible
                            ? "資格符合" : reason(section.eligibility.reason_code)
                        }</span>
                        <MeetingList meetings={section.meetings} />
                      </div>
                    ))}
                  </li>
                ))}
              </ul>
            )}
            {stage.milestone && (
              <dl>
                <dt>專題</dt><dd>{stage.milestone.project}</dd>
                <dt>可重現成果</dt><dd>{stage.milestone.deliverable}</dd>
                <dt>評估指標</dt><dd>{stage.milestone.metrics.join("、")}</dd>
              </dl>
            )}
          </section>
        ))}
      </div>
    </section>
  );
}

export default function RecommendationCard({
  recommendation,
  currentRegistration,
  onOpenSection,
  onConfirmed,
  onViewResult,
  onUpdate,
  onPlanRoadmap,
  updating,
}: {
  recommendation: Recommendation;
  currentRegistration: Registration | null;
  onOpenSection: (id: string) => void;
  onConfirmed: () => void;
  onViewResult: (phase: Plan["phase"]) => void;
  onUpdate: () => void;
  onPlanRoadmap: (code: string) => void;
  updating: boolean;
}) {
  const { candidates, learning_extensions: extensions, plan, roadmap } = recommendation;
  const registration = currentRegistration ?? recommendation.registration;
  const byId = new Map(candidates.map((entry) => [entry.section_id, entry]));
  return (
    <div className="recommendation">
      {roadmap && <ResearchRoadmap roadmap={roadmap} onOpenSection={onOpenSection} />}
      {plan && registration ? (
        <PlanReview
          key={plan.plan_id}
          recommendation={recommendation}
          plan={plan}
          registration={registration}
          onOpenSection={onOpenSection}
          onConfirmed={onConfirmed}
          onViewResult={onViewResult}
        />
      ) : recommendation.plan_unavailable_reason && registration ? (
        <section className="recommend-plan">
          <h4>目前無可送出的試排</h4>
          <p>{reason(recommendation.plan_unavailable_reason)}</p>
          <p>{PHASES[registration.phase]}</p>
          <ul>
            {registration.periods.map((period) => (
              <li key={period.kind}>
                {period.kind === "INITIAL" ? "初選" : "加退選"} {dateTime(period.starts_at)} ～ {dateTime(period.ends_at)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {candidates.length > 0 && (
        <section>
          <h4>{recommendation.topic}相關課程</h4>
          <ol className="recommend-course-list">
            {candidates.map((course) => (
              <CourseCard key={course.section_id} course={course} onOpenSection={onOpenSection} />
            ))}
          </ol>
        </section>
      )}
      {extensions.length > 0 && (
        <section>
          <h4>研究延伸</h4>
          <ul className="recommend-extensions">
            {extensions.map((extension) => (
              <li key={extension.title}>
                <strong>{extension.title}</strong>
                <p>{extension.next_step}</p>
                {extension.section_ids.map((id) => {
                  const match = byId.get(id);
                  return match && (
                    <button key={id} className="course-title" onClick={() => onOpenSection(id)}>
                      {match.course_code} {match.course_title}
                    </button>
                  );
                })}
              </li>
            ))}
          </ul>
        </section>
      )}
      {plan || recommendation.plan_unavailable_reason ? (
        <button className="small" onClick={onUpdate} disabled={updating}>更新試排</button>
      ) : roadmap && registration && ["INITIAL", "ADD_DROP"].includes(registration.phase) ? (
        <button className="small" onClick={() => onPlanRoadmap(roadmap.track_code)} disabled={updating}>試排本期課程</button>
      ) : null}
    </div>
  );
}
