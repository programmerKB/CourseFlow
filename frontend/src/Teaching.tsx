import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  ApiError,
  DAYS,
  dateTime,
  short,
  type GradeResult,
  type Roster,
  type TeachingSection,
} from "./api";

const SCORE = /^(?:100(?:\.0{1,2})?|\d{1,2}(?:\.\d{1,2})?)$/;

function scoreError(value: string) {
  const text = value.trim();
  if (!text) return "請輸入成績。";
  if (!SCORE.test(text)) return "成績須為 0–100，最多兩位小數。";
  return "";
}

function meetingText(section: TeachingSection) {
  return section.meetings.length
    ? section.meetings.map((m) => `週${DAYS[m.weekday]} ${short(m.start_time)}–${short(m.end_time)}`).join("、")
    : "上課時間待公告";
}

export default function Teaching({
  termId,
  termCode,
  onToast,
}: {
  termId: string;
  termCode?: string;
  onToast: (message: string) => void;
}) {
  const [sections, setSections] = useState<TeachingSection[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [roster, setRoster] = useState<Roster | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!termId) return;
    let active = true;
    setSections(null);
    setLoadError("");
    api<{ items: TeachingSection[] }>(`/instructor/terms/${termId}/sections`)
      .then((data) => {
        if (!active) return;
        setSections(data.items);
        setSelected((current) =>
          data.items.some((s) => s.section_id === current) ? current : data.items[0]?.section_id ?? null,
        );
      })
      .catch((e) => active && setLoadError(e instanceof Error ? e.message : "無法載入授課班別。"));
    return () => {
      active = false;
    };
  }, [termId, revision]);

  useEffect(() => {
    if (!termId || !selected) {
      setRoster(null);
      return;
    }
    let active = true;
    setRoster(null);
    setErrors({});
    api<Roster>(`/instructor/terms/${termId}/sections/${selected}/roster`)
      .then((data) => {
        if (!active) return;
        setRoster(data);
        setDrafts(Object.fromEntries(data.items.map((row) => [row.enrollment_id, row.score ?? ""])));
      })
      .catch((e) => active && setLoadError(e instanceof Error ? e.message : "無法載入修課名單。"));
    return () => {
      active = false;
    };
  }, [termId, selected, revision]);

  async function save(event: FormEvent, enrollmentId: string) {
    event.preventDefault();
    if (!selected || saving) return;
    const value = drafts[enrollmentId] ?? "";
    const problem = scoreError(value);
    setErrors((current) => ({ ...current, [enrollmentId]: problem }));
    if (problem) return;
    setSaving(enrollmentId);
    try {
      const result = await api<GradeResult>(
        `/instructor/terms/${termId}/sections/${selected}/roster/${enrollmentId}/grade`,
        { method: "PUT", body: JSON.stringify({ score: value.trim() }) },
      );
      // The server's stored value is authoritative.
      setRoster((current) =>
        current && {
          ...current,
          items: current.items.map((row) =>
            row.enrollment_id === enrollmentId ? { ...row, score: result.score, graded_at: result.graded_at } : row,
          ),
        },
      );
      setDrafts((current) => ({ ...current, [enrollmentId]: result.score }));
      onToast(result.changed ? `成績已儲存：${result.score} 分。` : "成績與目前紀錄相同，未變更。");
    } catch (e) {
      const message = e instanceof ApiError && e.status === 422 ? "成績須為 0–100，最多兩位小數。"
        : e instanceof ApiError && e.status === 404 ? "此學生已不在本班正式名單，請重新整理。"
          : e instanceof Error ? e.message : "成績未儲存，請重試。";
      setErrors((current) => ({ ...current, [enrollmentId]: message }));
      if (e instanceof ApiError && e.status === 404) setRevision((x) => x + 1);
    } finally {
      setSaving(null);
    }
  }

  if (loadError)
    return (
      <div className="state error" role="alert">
        <strong>無法載入授課資料</strong>
        <p>{loadError}</p>
        <button onClick={() => setRevision((x) => x + 1)}>重試</button>
      </div>
    );
  if (!sections)
    return (
      <div className="state" role="status">
        <span className="spinner" />
        載入授課班別中…
      </div>
    );
  if (!sections.length)
    return (
      <div className="state">
        <h3>{termCode || "本學期"} 沒有你授課的班別</h3>
        <p>可在上方切換學期。</p>
      </div>
    );
  return (
    <div className="teaching">
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>我的授課班別</h2>
            <p>{termCode} · 只列出你授課的班別</p>
          </div>
          <button className="small" onClick={() => setRevision((x) => x + 1)}>
            重新整理
          </button>
        </div>
        <div className="teaching-sections" role="list">
          {sections.map((s) => (
            <button
              key={s.section_id}
              role="listitem"
              className={"teaching-section" + (s.section_id === selected ? " active" : "")}
              aria-current={s.section_id === selected ? "true" : undefined}
              onClick={() => setSelected(s.section_id)}
            >
              <strong>
                {s.code} {s.title}
              </strong>
              <span>
                {s.section_no} 班 · {s.credits} 學分 · 正式選課 {s.enrolled_count} / {s.capacity}
              </span>
              <small>{meetingText(s)}</small>
            </button>
          ))}
        </div>
      </section>
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>
              {roster ? `${roster.section.code} ${roster.section.title} · ${roster.section.section_no} 班` : "修課名單"}
            </h2>
            <p>正式選課（ENROLLED）學生依學號排列 · 成績 0–100，最多兩位小數</p>
          </div>
          {roster && <small>查詢時間 {dateTime(roster.as_of)}</small>}
        </div>
        {!roster ? (
          <div className="state" role="status">
            <span className="spinner" />
            載入名單中…
          </div>
        ) : !roster.items.length ? (
          <div className="state">
            <h3>目前沒有正式選課學生</h3>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="roster">
              <thead>
                <tr>
                  <th>學號</th>
                  <th>姓名</th>
                  <th>目前成績</th>
                  <th>登錄或修改成績</th>
                </tr>
              </thead>
              <tbody>
                {roster.items.map((row) => (
                  <tr key={row.enrollment_id}>
                    <td>{row.student_no}</td>
                    <td>{row.student_name}</td>
                    <td>
                      {row.score ?? "未評分"}
                      {row.graded_at && <small>更新 {dateTime(row.graded_at)}</small>}
                    </td>
                    <td>
                      <form className="grade-form" onSubmit={(e) => save(e, row.enrollment_id)} noValidate>
                        <label className="sr-only" htmlFor={`grade-${row.enrollment_id}`}>
                          {row.student_no} 成績
                        </label>
                        <input
                          id={`grade-${row.enrollment_id}`}
                          inputMode="decimal"
                          value={drafts[row.enrollment_id] ?? ""}
                          aria-invalid={!!errors[row.enrollment_id]}
                          aria-describedby={errors[row.enrollment_id] ? `grade-error-${row.enrollment_id}` : undefined}
                          onChange={(e) => {
                            const value = e.target.value;
                            setDrafts((current) => ({ ...current, [row.enrollment_id]: value }));
                            setErrors((current) => ({ ...current, [row.enrollment_id]: "" }));
                          }}
                        />
                        <button className="primary small" disabled={saving !== null}>
                          {saving === row.enrollment_id ? "儲存中…" : "儲存"}
                        </button>
                        {errors[row.enrollment_id] && (
                          <p className="error" role="alert" id={`grade-error-${row.enrollment_id}`}>
                            {errors[row.enrollment_id]}
                          </p>
                        )}
                      </form>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
