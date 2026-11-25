import {
  useState,
  useEffect,
  useLayoutEffect,
  useRef,
  type ReactNode,
  type FormEvent,
} from "react";
import {
  api,
  ApiError,
  DAYS,
  PHASES,
  STATUS,
  dateTime,
  newRequestKey,
  short,
  type User,
  type Term,
  type Section,
  type Application,
  type Timetable,
  type Registration,
  type Page,
  type Source,
  type Meeting,
  type ChatResponse,
  type Department,
  type School,
  type Topic,
  type Eligibility,
} from "./api";
import RecommendationCard from "./RecommendationCard";
import ChatVoice from "./ChatVoice";
import Teaching from "./Teaching";

type View =
  | "courses"
  | "applications"
  | "timetable"
  | "chat"
  | "teaching"
  | "admin"
  | "rules";
type Mutation = (path: string, method: string, body?: unknown) => Promise<any>;
type TextChat = { kind: "text"; text: string; sectionId?: string; trackCode?: string; key: string; signature: string };
type VoiceChat = { kind: "voice"; audio: Blob; trackCode?: string; key: string };
type ChatAttempt = TextChat | VoiceChat;
type ChatMessage = Partial<ChatResponse> & {
  role: "user" | "assistant";
  answer: string;
  prompt?: string;
  retry?: ChatAttempt;
};
const EMPTY_FILTERS = { q: "", school_code: "", department_id: "", topic_code: "", instructor: "", weekday: "" };
const NAV: { id: View; label: string; icon: string }[] = [
  { id: "courses", label: "課程查詢", icon: "▦" },
  { id: "applications", label: "我的選課", icon: "☷" },
  { id: "timetable", label: "我的課表", icon: "▤" },
  { id: "chat", label: "選課助理", icon: "✧" },
  { id: "rules", label: "選課規則", icon: "◈" },
];
const route = (): View => {
  const p = location.pathname.slice(1);
  return ["applications", "timetable", "chat", "teaching", "admin", "rules"].includes(p)
    ? (p as View)
    : "courses";
};
const message = (e: unknown) =>
  e instanceof Error ? e.message : "操作未完成，請重新嘗試。";

function removeUnavailableTermLink(list: Term[], url: URL) {
  const linkedTerm = url.searchParams.get("term");
  if (!linkedTerm || list.some((term) => term.id === linkedTerm)) return false;
  url.searchParams.delete("term");
  url.searchParams.delete("section");
  return true;
}

function Badge({
  status,
  children,
}: {
  status?: string;
  children?: ReactNode;
}) {
  return (
    <span className={"badge " + (status || "")}>
      {children || STATUS[status || ""] || status}
    </span>
  );
}
function State({
  loading,
  error,
  retry,
  empty,
  children,
}: {
  loading?: boolean;
  error?: string;
  retry?: () => void;
  empty?: string;
  children: ReactNode;
}) {
  return loading ? (
    <div className="state" role="status">
      <span className="spinner" />
      載入資料中…
    </div>
  ) : error ? (
    <div className="state error" role="alert">
      <strong>無法載入資料</strong>
      <p>{error}</p>
      <button onClick={retry}>重試</button>
    </div>
  ) : empty ? (
    <div className="state">
      <span className="empty-mark">▦</span>
      <h3>{empty}</h3>
      <p>可調整查詢條件或前往課程查詢。</p>
    </div>
  ) : (
    <>{children}</>
  );
}
function Modal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const callback = useRef(onClose);
  callback.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") callback.current();
      if (e.key === "Tab") {
        const nodes = Array.from(
          ref.current?.querySelectorAll<HTMLElement>(
            "button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href]",
          ) || [],
        );
        const first = nodes[0],
          last = nodes[nodes.length - 1];
        if (
          e.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === ref.current)
        ) {
          e.preventDefault();
          last?.focus();
        } else if (
          !e.shiftKey &&
          (document.activeElement === last ||
            document.activeElement === ref.current)
        ) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
      >
        <header>
          <h2>{title}</h2>
          <button className="icon" onClick={onClose} aria-label="關閉對話框">
            ×
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
function Meetings({ items }: { items: Meeting[] }) {
  return (
    <div className="meetings">
      {items.map((m, i) => (
        <div key={m.id || i}>
          <span>
            週{DAYS[m.weekday]} {short(m.start_time)}–{short(m.end_time)}
          </span>
          <small>{m.room || "教室待公告"}</small>
        </div>
      ))}
    </div>
  );
}

// Fixed hints for the email error codes the worker stores (no provider text is stored).
const GMAIL_HINTS: Record<string, string> = {
  SMTP_NOT_CONFIGURED: "尚未填寫 SMTP_USER 與 SMTP_PASSWORD（Gmail 應用程式密碼），填好後執行 manage retry-failed 重寄。",
  SMTP_AUTH_FAILED: "Gmail 拒絕登入：SMTP_PASSWORD 須是 16 碼應用程式密碼，修正後執行 manage retry-failed 重寄。",
  SMTP_UNREACHABLE: "連不到 SMTP 伺服器，稍後自動重試；請核對 SMTP_HOST 與 SMTP_PORT。",
  SMTP_STARTTLS_UNSUPPORTED: "伺服器不支援 STARTTLS，請核對 SMTP_HOST、SMTP_PORT 與 SMTP_STARTTLS。",
  SMTP_AUTH_UNSUPPORTED: "伺服器不支援帳密登入；寄真實郵件請改用 smtp.gmail.com。",
  GMAIL_NOT_CONFIGURED: "尚未設定 Gmail API。設定 GMAIL_* 後執行 manage retry-failed 重寄。",
  GMAIL_AUTH_FAILED: "OAuth 授權無效或已撤銷，請重新取得 refresh token。",
  GMAIL_FORBIDDEN: "寄件帳號未授權 gmail.send，或 GMAIL_SENDER 不是授權帳號。",
  GMAIL_REJECTED: "Gmail 拒絕此信，請檢查收件信箱。",
  GMAIL_RATE_LIMITED: "超過 Gmail 寄信上限，稍後自動重試。",
  GMAIL_TIMEOUT: "連線逾時，稍後自動重試。",
  GMAIL_UNAVAILABLE: "Gmail API 暫時無法使用，稍後自動重試。",
};

export default function App() {
  const [view, setView] = useState<View>(route),
    [user, setUser] = useState<User | null>(null),
    [terms, setTerms] = useState<Term[]>([]),
    [termId, setTermId] = useState(""),
    [boot, setBoot] = useState(true),
    [baseError, setBaseError] = useState("");
  const [reg, setReg] = useState<Registration | null>(null),
    [revision, setRevision] = useState(0),
    [regError, setRegError] = useState(""),
    [toast, setToast] = useState(""),
    [busy, setBusy] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false),
    [login, setLogin] = useState("student01"),
    [password, setPassword] = useState(""),
    [loginError, setLoginError] = useState("");
  const [filters, setFilters] = useState(EMPTY_FILTERS),
    [debounced, setDebounced] = useState(filters),
    [page, setPage] = useState(1),
    [departments, setDepartments] = useState<Department[]>([]),
    [schools, setSchools] = useState<School[]>([]),
    [topics, setTopics] = useState<Topic[]>([]);
  const [catalog, setCatalog] = useState<Page | null>(null),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const [apps, setApps] = useState<Application[]>([]),
    [table, setTable] = useState<Timetable | null>(null),
    [personalLoading, setPersonalLoading] = useState(false),
    [personalError, setPersonalError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(
      new URLSearchParams(location.search).get("section"),
    ),
    [detail, setDetail] = useState<Section | null>(null),
    [detailError, setDetailError] = useState(""),
    [check, setCheck] = useState<Eligibility | null>(null),
    [checking, setChecking] = useState(false);
  const [confirmation, setConfirmation] = useState<{
    section: Pick<Section, "id" | "code" | "title" | "credits" | "instructor_name" | "meetings">;
    action: "apply" | "cancel" | "add" | "drop";
  } | null>(null);
  const [chatInput, setChatInput] = useState(""),
    [chatBusy, setChatBusy] = useState(false),
    [messages, setMessages] = useState<ChatMessage[]>([]);
  const lastTrackCode = messages.slice().reverse().find((m) => m.recommendation?.roadmap)
    ?.recommendation?.roadmap?.track_code;
  const chatRequest = useRef<AbortController | null>(null);
  const chatKeys = useRef(new Map<string, string>());
  const eligibilityRequest = useRef<AbortController | null>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const pendingKeys = useRef(new Map<string, string>()),
    operations = useRef(new Set<string>());
  const term = terms.find((t) => t.id === termId),
    student = user?.role === "STUDENT",
    instructor = user?.role === "INSTRUCTOR",
    admin = user?.role === "ADMIN";
  const navItems: { id: View; label: string; icon: string }[] = instructor
    ? [NAV[0], { id: "teaching", label: "我的授課", icon: "✎" }, NAV[NAV.length - 1]]
    : [...NAV, ...(admin ? [{ id: "admin" as View, label: "教務管理", icon: "⚙" }] : [])];
  const viewLabel =
    view === "admin" ? "教務管理" : view === "teaching" ? "我的授課" : NAV.find((n) => n.id === view)?.label;
  const refresh = () => setRevision((x) => x + 1);
  function navigate(next: View) {
    setView(next);
    history.pushState({}, "", `/${next}`);
  }
  function openSource(s: Source) {
    const target = new URL(s.url, location.origin);
    if (target.origin !== location.origin) return;
    if (s.term_id && !terms.some((t) => t.id === s.term_id)) {
      setToast("這個學期目前無法瀏覽。");
      return;
    }
    const hidden = removeUnavailableTermLink(terms, target);
    const linkedTerm = s.term_id || target.searchParams.get("term");
    if (linkedTerm) setTermId(linkedTerm);
    setSelectedId(hidden ? null : s.section_id || target.searchParams.get("section"));
    history.pushState({}, "", target.pathname + target.search);
    setView(route());
  }
  useEffect(() => {
    const listener = () => {
      setView(route());
      if (boot) return;
      const url = new URL(location.href);
      if (removeUnavailableTermLink(terms, url)) history.replaceState(history.state, "", url);
      const linkedTerm = url.searchParams.get("term");
      if (linkedTerm) setTermId(linkedTerm);
      setSelectedId(url.searchParams.get("section"));
    };
    addEventListener("popstate", listener);
    return () => removeEventListener("popstate", listener);
  }, [boot, terms]);
  async function loadTerms(knownUser: User | null = user, selected?: string) {
    const list = await api<Term[]>(
      knownUser?.role === "ADMIN" ? "/admin/terms" : "/terms",
    );
    const url = new URL(location.href);
    if (removeUnavailableTermLink(list, url)) {
      history.replaceState(history.state, "", url);
      setSelectedId(null);
    }
    setTerms(list);
    setTermId((previous) => {
      const wanted =
        selected ||
        previous ||
        url.searchParams.get("term") ||
        localStorage.getItem("course-term");
      return (
        list.find((t) => t.id === wanted)?.id ||
        list.find((t) => t.code.startsWith("115-1"))?.id ||
        list[0]?.id ||
        ""
      );
    });
  }
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const who = await api<User>("/auth/me").catch((e) => {
          if (e instanceof ApiError && e.status === 401) return null;
          throw e;
        });
        if (!active) return;
        setUser(who);
        const [, departments, schools, topics] = await Promise.all([
          loadTerms(who), api<Department[]>("/departments"), api<School[]>("/schools"), api<Topic[]>("/topics"),
        ]);
        if (active) {
          setDepartments(departments);
          setSchools(schools);
          setTopics(topics);
        }
      } catch (e) {
        if (active) setBaseError(message(e));
      } finally {
        if (active) setBoot(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(filters);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [filters]);
  useEffect(() => {
    if (termId) localStorage.setItem("course-term", termId);
  }, [termId]);
  useLayoutEffect(() => {
    setCatalog(null);
    setPage(1);
  }, [termId]);
  useLayoutEffect(() => {
    setMessages([]);
    setChatInput("");
    setChatBusy(false);
    setApps([]);
    setTable(null);
    setConfirmation(null);
    return () => {
      chatRequest.current?.abort();
      chatRequest.current = null;
      chatKeys.current.clear();
    };
  }, [termId, user?.id]);
  useEffect(() => () => window.speechSynthesis?.cancel(), [view, termId, user?.id]);
  useEffect(() => {
    const list = transcript.current;
    if (!list || !list.lastElementChild) return;
    const last = list.lastElementChild.getBoundingClientRect();
    list.scrollTop += last.top - list.getBoundingClientRect().top - 20;
  }, [messages.length, chatBusy, view]);
  useEffect(() => {
    if (!termId) return;
    let active = true;
    setReg(null);
    setRegError("");
    api<Registration>(`${admin ? "/admin" : ""}/terms/${termId}/registration`)
      .then((v) => {
        if (active) setReg(v);
      })
      .catch((e) => {
        if (active) setRegError(message(e));
      });
    return () => {
      active = false;
    };
  }, [termId, revision, admin]);
  useEffect(() => {
    if (view === "admin") return;
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [view]);
  useEffect(() => {
    if (!termId || view !== "courses") return;
    const control = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      page: String(page),
      page_size: "20",
      ...Object.fromEntries(Object.entries(debounced).filter(([, v]) => v)),
    });
    const path = `${admin ? "/admin" : ""}/terms/${termId}/sections?${params}`;
    api<Page>(path, { signal: control.signal })
      .then((value) => {
        if (!control.signal.aborted) setCatalog(value);
      })
      .catch((e) => {
        if (!control.signal.aborted) setError(message(e));
      })
      .finally(() => {
        if (!control.signal.aborted) setLoading(false);
      });
    return () => control.abort();
  }, [
    termId,
    debounced,
    page,
    revision,
    view,
    admin,
  ]);
  useEffect(() => {
    if (!termId || !student) {
      setApps([]);
      setTable(null);
      return;
    }
    let active = true;
    setPersonalLoading(true);
    setPersonalError("");
    Promise.all([
      api<{ items: Application[] }>(`/me/terms/${termId}/applications`),
      api<Timetable>(`/me/terms/${termId}/timetable`),
    ])
      .then(([a, t]) => {
        if (active) {
          setApps(a.items);
          setTable(t);
        }
      })
      .catch((e) => {
        if (active) setPersonalError(message(e));
      })
      .finally(() => {
        if (active) setPersonalLoading(false);
      });
    return () => {
      active = false;
    };
  }, [termId, user?.id, revision]);
  useLayoutEffect(() => {
    setCheck(null);
    setChecking(false);
    return () => {
      eligibilityRequest.current?.abort();
      eligibilityRequest.current = null;
    };
  }, [selectedId, termId, user?.id, revision]);
  useLayoutEffect(() => {
    const control = new AbortController();
    setDetail(null);
    setDetailError("");
    if (!selectedId || !termId) return;
    api<Section>(`${admin ? "/admin" : ""}/terms/${termId}/sections/${selectedId}`, {
      signal: control.signal,
    })
      .then((value) => {
        if (!control.signal.aborted) setDetail(value);
      })
      .catch((e) => {
        if (!control.signal.aborted) setDetailError(message(e));
      });
    return () => control.abort();
  }, [selectedId, termId, user?.id, revision, admin]);
  async function checkEligibility(sectionId: string) {
    if (eligibilityRequest.current) return;
    const control = new AbortController();
    eligibilityRequest.current = control;
    setChecking(true);
    setCheck(null);
    try {
      const result = await api<Eligibility>(
        `/me/terms/${termId}/sections/${sectionId}/eligibility`,
        { signal: control.signal },
      );
      if (!control.signal.aborted) setCheck(result);
    } catch (e) {
      if (!control.signal.aborted) setToast(message(e));
    } finally {
      if (eligibilityRequest.current === control) {
        eligibilityRequest.current = null;
        setChecking(false);
      }
    }
  }
  const mutate: Mutation = async (path, method, body) => {
    const idempotent = /^\/me\/terms\/[^/]+\/(?:enrollments(?:\/[^/]+)?|timetable\/email)$/.test(path);
    const signature = JSON.stringify([user?.id, method, path, idempotent ? body : null]);
    if (operations.current.has(signature))
      throw new Error("此操作正在處理中。");
    operations.current.add(signature);
    setBusy(true);
    try {
      let headers: Record<string, string> | undefined;
      if (idempotent) {
        const key = pendingKeys.current.get(signature) ?? newRequestKey();
        pendingKeys.current.set(signature, key);
        headers = { "Idempotency-Key": key };
      }
      const result = await api(path, {
        method,
        headers,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      pendingKeys.current.delete(signature);
      return result;
    } catch (e) {
      if (e instanceof ApiError && e.status > 0 && e.status !== 408 && e.status < 500)
        pendingKeys.current.delete(signature);
      throw e;
    } finally {
      operations.current.delete(signature);
      setBusy(operations.current.size > 0);
    }
  };
  async function auth(e: FormEvent) {
    e.preventDefault();
    setLoginError("");
    try {
      const who = await mutate("/auth/login", "POST", { login, password });
      setUser(who);
      setLoginOpen(false);
      setPassword("");
      await loadTerms(who);
      refresh();
      setToast("登入成功，歡迎回來。");
    } catch (e) {
      setLoginError(message(e));
    }
  }
  async function logout() {
    try {
      await mutate("/auth/logout", "POST");
      setUser(null);
      setTable(null);
      setApps([]);
      navigate("courses");
      await loadTerms(null);
      setToast("已登出。");
    } catch (e) {
      setToast(message(e));
    }
  }
  async function emailTimetable() {
    try {
      await mutate(`/me/terms/${termId}/timetable/email`, "POST");
      setToast("課表已排入寄送，將寄到你的帳號信箱。");
    } catch (e) {
      setToast(message(e));
    }
  }
  function sectionAction(s: Section, status?: "ENROLLED" | "PENDING") {
    if (!user)
      return (
        <button className="primary small" onClick={() => setLoginOpen(true)}>
          登入選課
        </button>
      );
    if (admin) return <Badge>管理員檢視</Badge>;
    if (instructor) return <Badge>教師檢視</Badge>;
    const isEnrolled = status ? status === "ENROLLED" : table?.items.some((t) => t.id === s.id),
      isPending = status ? status === "PENDING" : apps.some((a) => a.id === s.id && a.status === "PENDING");
    if (isEnrolled)
      return (
        <div className="action">
          <Badge status="ENROLLED" />
          <button
            className="danger small"
            disabled={busy || reg?.phase !== "ADD_DROP"}
            onClick={() => setConfirmation({ section: s, action: "drop" })}
          >
            退選
          </button>
        </div>
      );
    if (isPending)
      return (
        <div className="action">
          <Badge status="PENDING" />
          <button
            className="small"
            disabled={busy || reg?.phase !== "INITIAL"}
            onClick={() => setConfirmation({ section: s, action: "cancel" })}
          >
            取消登記
          </button>
        </div>
      );
    return (
      <button
        className="primary small"
        disabled={
          busy ||
          !s.is_selectable ||
          personalLoading ||
          !!personalError ||
          !["INITIAL", "ADD_DROP"].includes(reg?.phase || "")
        }
        onClick={() =>
          setConfirmation({
            section: s,
            action: reg?.phase === "INITIAL" ? "apply" : "add",
          })
        }
      >
        {!s.is_selectable
          ? "未開放"
          : reg?.phase === "INITIAL"
            ? "登記初選"
            : reg?.phase === "ADD_DROP"
              ? "加選課程"
              : "目前未開放"}
      </button>
    );
  }
  async function commit() {
    if (!confirmation) return;
    const { section: s, action } = confirmation;
    const base = `/me/terms/${termId}`;
    try {
      if (action === "apply")
        await mutate(`${base}/applications/${s.id}`, "PUT");
      if (action === "cancel")
        await mutate(`${base}/applications/${s.id}`, "DELETE");
      if (action === "add")
        await mutate(`${base}/enrollments`, "POST", { section_id: s.id });
      if (action === "drop")
        await mutate(`${base}/enrollments/${s.id}`, "DELETE");
      setConfirmation(null);
      refresh();
      setToast(
        action === "apply"
          ? "初選登記成功，目前待分發。可在我的課表查看。"
          : action === "cancel"
            ? "已取消初選登記。"
            : action === "add"
              ? "選課成功，確認信已排入寄送。正式課表已更新。"
              : "退選成功，名額已釋出。",
      );
    } catch (e) {
      setToast(message(e));
    }
  }
  function ask(text = chatInput, sectionId?: string, trackCode?: string) {
    text = text.trim();
    if (!text || chatRequest.current) return;
    const track = trackCode ?? lastTrackCode;
    const signature = JSON.stringify([text, sectionId, track]);
    const key = chatKeys.current.get(signature) ?? newRequestKey();
    void sendChat({ kind: "text", text, sectionId, trackCode: track, key, signature });
  }

  async function sendChat(attempt: ChatAttempt) {
    if (chatRequest.current) return;
    if (!student) {
      setLoginOpen(true);
      return;
    }
    const control = new AbortController();
    chatRequest.current = control;
    if (attempt.kind === "text") {
      chatKeys.current.set(attempt.signature, attempt.key);
      setChatInput("");
    }
    setMessages((m) => [
      ...m.map((item) => item.retry?.key === attempt.key ? { ...item, retry: undefined } : item),
      { role: "user", answer: attempt.kind === "text" ? attempt.text : "語音提問" },
    ]);
    setChatBusy(true);
    try {
      const form = new FormData();
      if (attempt.kind === "voice") {
        form.append("audio", attempt.audio, "recording");
        if (termId) form.append("term_id", termId);
        if (attempt.trackCode) form.append("track_code", attempt.trackCode);
      }
      const result = await api<ChatResponse>(attempt.kind === "voice" ? "/chat/voice" : "/chat", {
        method: "POST",
        signal: control.signal,
        headers: { "Idempotency-Key": attempt.key },
        body: attempt.kind === "voice" ? form : JSON.stringify({
          message: attempt.text, term_id: termId || undefined, section_id: attempt.sectionId, track_code: attempt.trackCode,
        }),
      });
      if (!control.signal.aborted) {
        if (attempt.kind === "text") chatKeys.current.delete(attempt.signature);
        setMessages((m) => [...m, { role: "assistant", ...result,
          prompt: attempt.kind === "text" ? attempt.text : result.transcript }]);
        if (result.action) refresh();
      }
    } catch (e) {
      if (control.signal.aborted) return;
      const details = e instanceof ApiError ? e.details : undefined;
      const retry = e instanceof ApiError && (e.status === 0 || e.status === 408 || e.status >= 500);
      if (attempt.kind === "text" && !retry) chatKeys.current.delete(attempt.signature);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          answer: e instanceof ApiError && e.status === 413 && attempt.kind === "voice"
            ? (e.code === "VOICE_TOO_LONG" ? e.message : "每段音訊最多 5 MiB，請縮短後重試。") : message(e),
          mode: retry ? "unavailable" : "error",
          sources: details?.sources,
          registration: details?.registration,
          transcript: details?.transcript,
          agent: details?.agent,
          retry: retry ? attempt : undefined,
        },
      ]);
    } finally {
      if (chatRequest.current === control) {
        chatRequest.current = null;
        setChatBusy(false);
      }
    }
  }
  function speak(reply: ChatMessage) {
    const text = [reply.answer, ...(reply.choices ?? []).map(
      (item) => `${item.school_name} ${item.department_name} ${item.title} ${item.section_no} 班，剩餘 ${item.remaining} 席`,
    )].join(" ");
    const speech = new SpeechSynthesisUtterance(text);
    speech.lang = "zh-TW";
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(speech);
  }
  const schedule = [
    ...(table?.items ?? []).map((section) => ({ section, status: "ENROLLED" as const })),
    ...(table?.pending_items ?? []).map((section) => ({ section, status: "PENDING" as const })),
  ];
  const privateGate = !student ? (
    <div className="state">
      <h3>
        {admin || instructor ? "請以學生帳號查看個人選課資料" : "登入以查看你的選課資料"}
      </h3>
      <p>每位學生的登記、分發結果與正式課表分開保存。</p>
      <button className="primary" onClick={() => setLoginOpen(true)}>
        登入學生帳號
      </button>
    </div>
  ) : null;
  return (
    <div className="shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="/courses"
          onClick={(e) => {
            e.preventDefault();
            navigate("courses");
          }}
        >
          <span className="brand-logo">
            C<span>f</span>
          </span>
          <div>
            <strong>CourseFlow</strong>
            <small>大學選課系統</small>
          </div>
        </a>
        <p className="nav-caption">學期規劃 · 自主學習</p>
        <nav aria-label="主要導覽">
          {navItems.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "active" : ""}
              aria-current={view === n.id ? "page" : undefined}
              onClick={() => navigate(n.id)}
            >
              <span aria-hidden="true">{n.icon}</span>
              {n.label}
              {n.id === "applications" &&
                apps.filter((a) => a.status === "PENDING").length > 0 && (
                  <b aria-hidden="true">
                    {apps.filter((a) => a.status === "PENDING").length}
                  </b>
                )}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <span>每一堂課，都是新的可能</span>
          <p>
            確認時段與先修條件，
            <br />
            安排屬於你的學期。
          </p>
          <small>
            國立臺灣科技大學
            <br />
            資料庫設計期末專題 · 虛構資料
          </small>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div className="breadcrumb">
            學習工作台 <span>/</span>{" "}
            <strong>{viewLabel}</strong>
          </div>
          <div className="top-actions">
            {terms.length > 1 ? (
              <label>
                <select
                  aria-label="選擇學期"
                  value={termId}
                  onChange={(e) => {
                    setTermId(e.target.value);
                    setSelectedId(null);
                  }}
                >
                  {terms.map((t) => (
                    <option value={t.id} key={t.id}>
                      {t.code}
                      {!t.catalog_published_at ? "（草稿）" : ""}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <Badge>
                {term?.code || (boot ? "載入學期中…" : "尚無學期")}
                {term && !term.catalog_published_at && "（草稿）"}
              </Badge>
            )}
            {user ? (
              <>
                <span className="avatar">{user.display_name.slice(0, 1)}</span>
                <span className="user-name">
                  {user.display_name}
                  <small>{admin ? "教務管理員" : instructor ? "授課教師" : user.login}</small>
                </span>
                <button className="small" onClick={logout} disabled={busy}>
                  登出
                </button>
              </>
            ) : (
              <button
                className="primary small"
                onClick={() => setLoginOpen(true)}
              >
                登入帳號
              </button>
            )}
          </div>
        </header>
        <div className="workspace">
          <section className="page-heading">
            <div>
              <p className="eyebrow">YOUR SEMESTER, YOUR WAY</p>
              <h1>
                {viewLabel}
                <span>.</span>
              </h1>
              <p>
                {view === "courses"
                  ? "探索課程，規劃每一個值得投入的時刻。"
                  : view === "applications"
                    ? "追蹤初選登記，查看每一筆分發結果。"
                    : view === "timetable"
                      ? "查看正式選課與待分發的上課時段。"
                      : view === "chat"
                        ? "用自然語言查詢課程與課表，也可用英文直接加選或退選。"
                        : view === "teaching"
                          ? "查看授課班別的正式選課名單，登錄與更新成績。"
                        : view === "rules"
                          ? "從初選到加退選，清楚了解每一步。"
                          : "管理學期、開課班別、分發與通知。"}
              </p>
            </div>
            <Badge status={reg?.phase}>
              {PHASES[reg?.phase || ""] || "學期資訊"}
            </Badge>
          </section>
          <State
            loading={boot}
            error={baseError}
            retry={() => location.reload()}
            empty={
              !terms.length && view !== "admin" ? "目前尚無公開學期" : undefined
            }
          >
            {regError && (
              <div role="alert" className="notice error">
                {regError}
                <button onClick={refresh}>重試</button>
              </div>
            )}
            {view === "courses" && (
              <>
                <div className="stats">
                  {[
                    ["本次查詢課程", catalog?.total ?? "—", "班"],
                    [
                      "初選待分發",
                      student
                        ? apps.filter((a) => a.status === "PENDING").length
                        : "—",
                      "班",
                    ],
                    [
                      "正式選課",
                      student ? (table?.items.length ?? 0) : "—",
                      "班",
                    ],
                    [
                      "本學期總學分",
                      student ? (table?.total_credits ?? 0) : "—",
                      "學分",
                    ],
                  ].map(([label, value, unit], i) => (
                    <div className="stat" key={String(label)}>
                      <div>
                        <span>{label}</span>
                        <i className={"stat-icon c" + i}>
                          {["▦", "◷", "✓", "◇"][i]}
                        </i>
                      </div>
                      <strong>
                        {value}
                        <small>{unit}</small>
                      </strong>
                    </div>
                  ))}
                </div>
                <div className="phase-banner">
                  <div className="banner-icon">▤</div>
                  <div>
                    <strong>{PHASES[reg?.phase || ""] || "選課時程"}</strong>
                    <p>
                      {reg?.phase === "INITIAL"
                        ? "初選登記為待分發，登記時間不影響分發順序。"
                        : reg?.phase === "ADD_DROP"
                          ? "加選成功即納入正式課表；退選後立即釋出名額。"
                          : "請依公告時程辦理選課，正式課表以有效選課紀錄為準。"}
                    </p>
                  </div>
                  <div className="periods">
                    {reg?.periods.map((p) => (
                      <p key={p.kind}>
                        <b>{p.kind === "INITIAL" ? "初選" : "加退選"}</b>
                        {dateTime(p.starts_at)} ～ {dateTime(p.ends_at)}
                      </p>
                    ))}
                  </div>
                </div>
                <section className="panel">
                  <div className="panel-title">
                    <div>
                      <h2>找到適合你的課程</h2>
                      <p>模擬選課 · 依學校、系所、主題和上課時段查詢</p>
                    </div>
                    <span className="muted">
                      共 {catalog?.total ?? 0} 個班別
                    </span>
                  </div>
                  <div className="filters">
                    <label className="search">
                      <span className="sr-only">課程名稱或代碼</span>
                      <input
                        placeholder="搜尋課程名稱或代碼…"
                        value={filters.q}
                        onChange={(e) =>
                          setFilters({ ...filters, q: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      <span className="sr-only">開課學校</span>
                      <select value={filters.school_code} onChange={(e) =>
                        setFilters({ ...filters, school_code: e.target.value, department_id: "" })
                      }>
                        <option value="">所有學校</option>
                        {schools.map((school) => <option key={school.code} value={school.code}>{school.name}</option>)}
                      </select>
                    </label>
                    <label>
                      <span className="sr-only">開課系所</span>
                      <select
                        value={filters.department_id}
                        onChange={(e) =>
                          setFilters({
                            ...filters,
                            department_id: e.target.value,
                          })
                        }
                      >
                        <option value="">所有系所</option>
                        {departments.filter((d) => !filters.school_code || d.school_code === filters.school_code).map((d) => (
                          <option value={d.id} key={d.id}>
                            {d.name}{!filters.school_code && d.school_name ? ` · ${d.school_name}` : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span className="sr-only">課程主題</span>
                      <select value={filters.topic_code} onChange={(e) => setFilters({ ...filters, topic_code: e.target.value })}>
                        <option value="">所有主題</option>
                        {topics.map((topic) => <option key={topic.code} value={topic.code}>{topic.label}</option>)}
                      </select>
                    </label>
                    <label>
                      <span className="sr-only">教師名稱</span>
                      <input
                        placeholder="教師名稱"
                        value={filters.instructor}
                        onChange={(e) =>
                          setFilters({ ...filters, instructor: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      <span className="sr-only">上課星期</span>
                      <select
                        value={filters.weekday}
                        onChange={(e) =>
                          setFilters({ ...filters, weekday: e.target.value })
                        }
                      >
                        <option value="">所有星期</option>
                        {DAYS.slice(1).map((d, i) => (
                          <option value={i + 1} key={d}>
                            星期{d}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="filter-note">
                    <span>選課名額以成功提交的交易為準</span>
                    <button
                      className="text"
                      onClick={() => setFilters(EMPTY_FILTERS)}
                    >
                      清除篩選
                    </button>
                  </div>
                  <State
                    loading={loading}
                    error={error}
                    retry={refresh}
                    empty={
                      !catalog?.items.length ? "查無符合條件的課程" : undefined
                    }
                  >
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>課程資訊</th>
                            <th>教師 / 系所</th>
                            <th>上課時段</th>
                            <th>學分</th>
                            <th>剩餘名額</th>
                            <th>選課操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {catalog?.items.map((s) => (
                            <tr key={s.id}>
                              <td>
                                <button
                                  className="course-title"
                                  onClick={() => setSelectedId(s.id)}
                                >
                                  {s.title}
                                </button>
                                <small>
                                  {s.code} · 班別 {s.section_no}
                                </small>
                                {!s.is_selectable && <Badge>未開放選課</Badge>}
                              </td>
                              <td>
                                {s.instructor_name}
                                <small>{s.school_name} {s.department_name}</small>
                              </td>
                              <td>
                                <Meetings items={s.meetings} />
                              </td>
                              <td>{s.credits}</td>
                              <td>
                                <div
                                  className={
                                    "seats " + (s.remaining === 0 ? "full" : "")
                                  }
                                >
                                  <strong>{s.remaining}</strong> / {s.capacity}
                                  <progress
                                    value={s.enrolled_count}
                                    max={Math.max(1, s.capacity)}
                                    aria-label="已選人數比例"
                                  />
                                </div>
                              </td>
                              <td>{sectionAction(s)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </State>
                  <div className="pagination">
                    <span>
                      名額查詢時間：{dateTime(catalog?.as_of)}（臺北）
                    </span>
                    <div>
                      <button
                        className="small"
                        disabled={page === 1 || loading}
                        onClick={() => setPage((p) => p - 1)}
                      >
                        上一頁
                      </button>
                      <span>
                        {page} /{" "}
                        {Math.max(1, Math.ceil((catalog?.total || 0) / 20))}
                      </span>
                      <button
                        className="small"
                        disabled={page * 20 >= (catalog?.total || 0) || loading}
                        onClick={() => setPage((p) => p + 1)}
                      >
                        下一頁
                      </button>
                    </div>
                  </div>
                </section>
              </>
            )}
            {view === "applications" && (
              <section className="panel">
                <div className="panel-title">
                  <div>
                    <h2>初選登記與歷史結果</h2>
                  </div>
                  <button className="small" onClick={refresh}>
                    重新整理
                  </button>
                </div>
                {privateGate || (
                  <State
                    loading={personalLoading}
                    error={personalError}
                    retry={refresh}
                    empty={!apps.length ? "尚無初選登記或分發結果" : undefined}
                  >
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>課程</th>
                            <th>初選狀態</th>
                            <th>原因 / 決定時間</th>
                            <th>目前正式選課</th>
                            <th>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {apps.map((a) => (
                            <tr key={a.application_id}>
                              <td>
                                <button
                                  className="course-title"
                                  onClick={() => setSelectedId(a.id)}
                                >
                                  {a.title}
                                </button>
                                <small>
                                  {a.code} · {a.section_no}
                                </small>
                              </td>
                              <td>
                                <Badge status={a.status} />
                                <small>登記 {dateTime(a.submitted_at)}</small>
                              </td>
                              <td>
                                {a.reason || "—"}
                                <small>{dateTime(a.decided_at)}</small>
                              </td>
                              <td>
                                {table?.items.some((s) => s.id === a.id) ? (
                                  <Badge status="ENROLLED" />
                                ) : (
                                  <span className="muted">尚未正式選課</span>
                                )}
                              </td>
                              <td>
                                {a.status === "PENDING" ? (
                                  sectionAction(a, "PENDING")
                                ) : (
                                  <button
                                    className="small"
                                    onClick={() => setSelectedId(a.id)}
                                  >
                                    查看班別
                                  </button>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </State>
                )}
              </section>
            )}
            {view === "timetable" &&
              (privateGate || (
                <State
                  loading={personalLoading}
                  error={personalError}
                  retry={refresh}
                >
                  <div className="credit-summary">
                    <span>本學期正式選課</span>
                    <strong>
                      {table?.total_credits ?? 0}
                      <small> / {table?.credit_limit ?? 0} 學分上限</small>
                    </strong>
                    <div className="action timetable-actions">
                      <button className="small" onClick={refresh}>
                        更新課表
                      </button>
                      <button
                        className="primary small"
                        disabled={busy || !table}
                        onClick={emailTimetable}
                      >
                        寄送課表
                      </button>
                    </div>
                  </div>
                  <section className="panel">
                    <div className="panel-title">
                      <div>
                        <h2>每週課表</h2>
                        <p>上課時間以臺北時間顯示</p>
                      </div>
                    </div>
                    <div className="week-wrap">
                      <div className="week">
                        {DAYS.slice(1).map((day, i) => {
                          const meetings = schedule
                            .flatMap(({ section, status }) =>
                              section.meetings
                                .filter((meeting) => meeting.weekday === i + 1)
                                .map((meeting, index) => ({ section, status, meeting, index })),
                            )
                            .sort((a, b) => a.meeting.start_time.localeCompare(b.meeting.start_time));
                          return (
                            <section key={day}>
                              <h3>星期{day}</h3>
                              {meetings.map(({ section, status, meeting, index }) => (
                                <button
                                  className={status === "PENDING" ? "class-block pending" : "class-block"}
                                  key={meeting.id ?? section.id + index}
                                  onClick={() => setSelectedId(section.id)}
                                >
                                  <span>
                                    {short(meeting.start_time)}–{short(meeting.end_time)}
                                  </span>
                                  <strong>{section.title}</strong>
                                  <small>{meeting.room}{status === "PENDING" && " · 待分發"}</small>
                                </button>
                              ))}
                              {!meetings.length && <p className="no-class">尚無課程</p>}
                            </section>
                          );
                        })}
                      </div>
                    </div>
                    <div className="enrolled-list">
                      {schedule.map(({ section, status }) => (
                        <div key={section.id}>
                          <button
                            className="course-title"
                            onClick={() => setSelectedId(section.id)}
                          >
                            {section.code} {section.title}
                          </button>
                          <span>{section.credits} 學分</span>
                          {sectionAction(section, status)}
                        </div>
                      ))}
                    </div>
                  </section>
                </State>
              ))}
            <section className="panel chat-panel" hidden={view !== "chat"}>
              <div className="panel-title">
                <div>
                  <h2>✧ 選課助理</h2>
                  <p>查課、規劃研究路徑或試排。加退選期間可用中文明確指令加選，或用英文加選、退選單門課（Llama 3.2 3B）。</p>
                </div>
              </div>
              <div className="messages" ref={transcript} aria-label="助理對話">
                {!messages.length && (
                  <div className="chat-welcome">
                    <span>✧</span>
                    <h2>一起找到下一堂好課</h2>
                    <p>輸入你的研究方向，或詢問課程名額與個人課表。</p>
                    <div>
                      {[
                        "我的研究方向是大語言模型，請推薦並幫我選三門。",
                        "我想做 RF，這學期先修什麼？",
                        "找星期二還有空位的電子系課",
                        "我星期三有課嗎？",
                        "Please enroll me in CS101, section A.",
                        "Please drop CS101, section A.",
                      ].map((t) => (
                        <button
                          key={t}
                          disabled={chatBusy}
                          onClick={() => ask(t)}
                        >
                          {t}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {messages.map((m, i) => (
                  <article key={i} className={`message ${m.role}${m.recommendation ? " has-recommendation" : ""}`}>
                    <small>
                      {m.role === "user"
                        ? "你"
                        : m.mode === "unavailable"
                          ? "助理暫時無法使用"
                          : m.mode === "error" ? "操作未完成" : "選課助理"}
                    </small>
                    {m.transcript && <p className="chat-transcript">語音辨識 {m.transcript}</p>}
                    <p aria-live={m.role === "assistant" ? "polite" : undefined}>{m.answer}</p>
                    {m.action && (
                      <p>
                        {m.action.status === "DROPPED" ? "已退選" : "已加選"} {m.action.course_code ?? ""}{" "}
                        {m.action.course_title} {m.action.section_no} 班 · 本學期正式選課 {m.action.total_credits} 學分
                        · 此班剩餘 {m.action.remaining} 席
                        {m.action.status === "ENROLLED" && m.action.notification_id && " · 選課確認信已排入寄送（Gmail）"}
                      </p>
                    )}
                    {m.agent && (
                      <small className="agent-trace">
                        LangGraph · {m.agent.llm?.stub
                          ? "固定測試規則（CHAT_STUB），未呼叫 LLM"
                          : m.agent.llm?.local_rules
                            ? "完整句型由本機規則判讀，未呼叫 LLM"
                            : `${m.agent.llm?.server_model ?? m.agent.model} 伺服器回報 ${m.agent.llm?.prompt_tokens ?? "?"}+${m.agent.llm?.output_tokens ?? "?"} tokens · ${m.agent.llm?.total_ms ?? "?"} ms`}
                        {" · "}{m.agent.tools.join(" → ")}
                      </small>
                    )}
                    {m.as_of && <small>查詢時間 {dateTime(m.as_of)}</small>}
                    {m.registration && (
                      <div className="chat-registration">
                        <strong>{PHASES[m.registration.phase]}</strong>
                        {m.registration.periods.map((p) => (
                          <small key={p.kind}>{p.kind === "INITIAL" ? "初選" : "加退選"} {dateTime(p.starts_at)} ～ {dateTime(p.ends_at)}</small>
                        ))}
                      </div>
                    )}
                    {m.track_choices && (
                      <div className="sources">
                        {m.track_choices.map((choice) => (
                          <button key={choice.track_code} type="button" disabled={chatBusy}
                            onClick={() => ask(choice.title, undefined, choice.track_code)}>{choice.title}</button>
                        ))}
                      </div>
                    )}
                    {!!m.choices?.length && (
                      <ul className="chat-choices" aria-label="待選班別">
                        {m.choices.map((choice) => (
                          <li key={choice.section_id}>
                            <button className="course-title" onClick={() => setSelectedId(choice.section_id)}>
                              {choice.code} {choice.title} · {choice.section_no} 班
                            </button>
                            <p>{choice.school_name} {choice.department_name} · {choice.is_selectable ? `剩餘 ${choice.remaining} 席` : "未開放"}</p>
                            <Meetings items={choice.meetings} />
                            {m.choice_action !== "drop" && m.choice_action !== null &&
                              m.registration?.phase === "INITIAL" && reg?.phase === "INITIAL" && (
                              <button type="button" className="small"
                                disabled={busy || chatBusy || !choice.is_selectable || personalLoading || !!personalError}
                                onClick={() => {
                                  const { section_id, code, title, credits, instructor_name, meetings } = choice;
                                  setConfirmation({
                                    section: { id: section_id, code, title, credits, instructor_name, meetings },
                                    action: "apply",
                                  });
                                }}>登記此班</button>
                            )}
                            {m.prompt && m.choice_action === "drop" && reg?.phase === "ADD_DROP" && (
                              <button type="button" className="small danger" disabled={chatBusy}
                                onClick={() => ask(m.prompt, choice.section_id)}>退選此班</button>
                            )}
                            {m.prompt && m.choice_action !== "drop" && m.choice_action !== null &&
                              m.registration?.phase === "ADD_DROP" && reg?.phase === "ADD_DROP" && (
                              <button type="button" className="small" disabled={chatBusy || !choice.is_selectable || choice.remaining <= 0}
                                onClick={() => ask(m.prompt, choice.section_id)}>加選此班</button>
                            )}
                          </li>
                        ))}
                        {m.choice_total && m.choice_total > m.choices.length
                          ? <li>找到 {m.choice_total} 班。可用課程查詢查看完整清單。</li> : null}
                      </ul>
                    )}
                    {m.recommendation ? (
                      <RecommendationCard
                        recommendation={m.recommendation}
                        currentRegistration={reg}
                        onOpenSection={setSelectedId}
                        onConfirmed={refresh}
                        onViewResult={(phase) => navigate(phase === "INITIAL" ? "applications" : "timetable")}
                        onUpdate={() => m.prompt && ask(m.prompt, undefined, m.recommendation?.roadmap?.track_code)}
                        onPlanRoadmap={(code) => ask("請試排這個方向的本期課程。", undefined, code)}
                        updating={chatBusy}
                      />
                    ) : (
                      <div className="sources">
                        {m.sources?.map((s, j) => (
                          <button
                            className="small"
                            key={j}
                            onClick={() => openSource(s)}
                          >
                            {s.title}
                          </button>
                        ))}
                      </div>
                    )}
                    {m.role === "assistant" && (
                      <div className="chat-actions">
                        {window.speechSynthesis && typeof SpeechSynthesisUtterance !== "undefined" &&
                          <button type="button" className="small" onClick={() => speak(m)}>朗讀回覆</button>}
                        {m.transcript && (
                          <button type="button" className="small" disabled={chatBusy} onClick={() => {
                            setChatInput(m.transcript ?? "");
                            document.getElementById("question")?.focus();
                          }}>編輯辨識文字</button>
                        )}
                        {m.retry && <button type="button" className="small" disabled={chatBusy}
                          onClick={() => m.retry && void sendChat(m.retry)}>重送同一請求</button>}
                      </div>
                    )}
                  </article>
                ))}
                {chatBusy && (
                  <div className="message" role="status">
                    <span className="spinner" /> 正在查詢資料…
                  </div>
                )}
              </div>
              <form
                className="chat-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  ask();
                }}
              >
                <label className="sr-only" htmlFor="question">
                  輸入選課問題
                </label>
                <textarea
                  id="question"
                  value={chatInput}
                  maxLength={1000}
                  rows={2}
                  placeholder="輸入你的選課問題…（英文可直接加退選，例如 Please drop CS101 A.）"
                  onChange={(e) => setChatInput(e.target.value)}
                />
                <button
                  className="primary"
                  disabled={chatBusy || !chatInput.trim()}
                >
                  {chatBusy ? "查詢中" : "送出"}
                </button>
              </form>
              {view === "chat" && student && (
                <ChatVoice key={`${user?.id}-${termId}`} disabled={chatBusy}
                  onSend={(audio) => void sendChat({ kind: "voice", audio, trackCode: lastTrackCode, key: newRequestKey() })} />
              )}
              <p className="chat-note">
                {chatInput.length}/1000 字 ·
                初選登記須待分發。即時加選以提交結果為準。
              </p>
            </section>
            {view === "rules" && (
              <section className="panel rules">
                <p className="eyebrow">SRS 4.0 · 選課規則</p>
                <h2>從登記，到正式選課</h2>
                <div className="rule-grid">
                  {[
                    [
                      "初選登記",
                      "開放期間可登記或取消。每門課只能保留一個待分發班別；登記不代表選上。",
                    ],
                    [
                      "固定種子分發",
                      "管理員於初選截止後分發。以 SHA-256(v1|seed|student_id|section_id) 升冪決定順序，登記時間不影響排序。",
                    ],
                    [
                      "即時加退選",
                      "分發公布且加退選期間開放時，可在網頁或英文選課助理加選、退選，兩者執行同一套檢查。加選成功後以 Gmail 寄出選課確認信。",
                    ],
                  ].map(([h, p]) => (
                    <div key={h}>
                      <h3>{h}</h3>
                      <p>{p}</p>
                    </div>
                  ))}
                </div>
                <h3>錄取依序檢查</h3>
                <ol>
                  <li>班別屬於該學期，且開放選課。</li>
                  <li>同學期尚未正式選上相同課程（不同班別也算重複）。</li>
                  <li>所有先修科目已於先前結束的學期通過。</li>
                  <li>全部上課時段均未衝堂；首尾相接的時段可以同時選。</li>
                  <li>加選後總學分不超過 18 學分，也不超過個人本學期上限；剛好 18 學分可受理。</li>
                  <li>班別有效選課人數小於容量。</li>
                </ol>
                <p>
                  所有期限採 [開始時間,
                  截止時間)，到達截止時刻即不再受理。畫面名額為查詢當下快照，以資料庫交易提交結果為準。
                </p>
                <p>
                  初選結果與目前正式選課分開保存。退選後仍可查閱當初分發結果。候補、收藏、授權碼與多志願順位不在本版功能範圍。
                </p>
              </section>
            )}
            {view === "teaching" &&
              (instructor ? (
                <Teaching termId={termId} termCode={term?.code} onToast={setToast} />
              ) : (
                <div className="state">
                  <h3>此頁限授課教師使用</h3>
                  <button onClick={() => setLoginOpen(true)}>登入教師帳號</button>
                </div>
              ))}
            {view === "admin" &&
              (admin ? (
                <Admin
                  term={term}
                  mutate={mutate}
                  busy={busy}
                  revision={revision}
                  onRefresh={refresh}
                  onSelect={async (id) => {
                    await loadTerms(user, id);
                    refresh();
                  }}
                  onToast={setToast}
                />
              ) : (
                <div className="state">
                  <h3>此頁限教務管理員使用</h3>
                  <button onClick={() => setLoginOpen(true)}>登入管理員</button>
                </div>
              ))}
          </State>
          <footer className="footer">
            <span>CourseFlow · 大學選課系統</span>
            <span>Asia/Taipei · 資料以系統交易結果為準</span>
          </footer>
        </div>
      </main>
      {toast && (
        <div className="toast" role="alert">
          <span>{toast}</span>
          <button
            className="icon"
            aria-label="關閉訊息"
            onClick={() => setToast("")}
          >
            ×
          </button>
        </div>
      )}
      {loginOpen && (
        <Modal title="登入選課系統" onClose={() => setLoginOpen(false)}>
          <form className="form-body" onSubmit={auth}>
            <p>
              示範帳號：student01～student20 / teacher01～teacher04 / admin
              <br />
              共用密碼：Demo123!
            </p>
            <label>
              帳號
              <input
                required
                autoComplete="username"
                value={login}
                onChange={(e) => setLogin(e.target.value)}
              />
            </label>
            <label>
              密碼
              <input
                type="password"
                required
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {loginError && (
              <p className="error" role="alert">
                {loginError}
              </p>
            )}
            <button className="primary" disabled={busy}>
              {busy ? "登入中…" : "登入帳號"}
            </button>
          </form>
        </Modal>
      )}
      {selectedId && termId && (
        <Modal
          title={detail ? `${detail.title} · ${detail.section_no}` : "班別詳情"}
          onClose={() => setSelectedId(null)}
        >
          <State
            loading={!detail && !detailError}
            error={detailError}
            retry={refresh}
          >
            {detail && (
              <>
                <div className="detail">
                  <p className="eyebrow">
                    {detail.code} / {detail.school_name} {detail.department_name}
                  </p>
                  <div className="detail-chips">
                    <Badge>模擬選課</Badge>
                    <Badge>{detail.instructor_name}</Badge>
                    <Badge>{detail.credits} 學分</Badge>
                    <Badge>
                      {detail.is_selectable ? "開放選課" : "未開放選課"}
                    </Badge>
                  </div>
                  <div className="detail-grid">
                    <section>
                      <h3>上課時間與教室</h3>
                      <Meetings items={detail.meetings} />
                    </section>
                    <section>
                      <h3>班別名額</h3>
                      <strong className="seat-big">
                        {detail.remaining}
                        <small> / {detail.capacity} 名</small>
                      </strong>
                      <p>已選 {detail.enrolled_count} 人</p>
                    </section>
                  </div>
                  <h3>課程大綱</h3>
                  <p>{detail.description}</p>
                  {detail.source_url && <p><a href={detail.source_url} target="_blank" rel="noreferrer">校方課程來源</a></p>}
                  {detail.topics?.length > 0 && <p>主題 {detail.topics.map((topic) => topic.label).join("、")}</p>}
                  <h3>先修條件</h3>
                  <p>
                    {detail.prerequisites.length
                      ? detail.prerequisites
                          .map((p) => `${p.code} ${p.title}`)
                          .join("、")
                      : "無指定先修科目"}
                  </p>
                  {student && (
                    <button
                      disabled={busy || checking}
                      onClick={() => checkEligibility(detail.id)}
                    >
                      {checking ? "檢查中…" : "檢查目前選課條件"}
                    </button>
                  )}
                  {check && (
                    <p role="status" className="notice">
                      {check.message}
                      <small>查詢時間 {dateTime(check.as_of)}</small>
                    </p>
                  )}
                </div>
                <div className="modal-actions">
                  <small>查詢時間：{dateTime(detail.as_of)}</small>
                  {sectionAction(detail)}
                </div>
              </>
            )}
          </State>
        </Modal>
      )}
      {confirmation && (
        <Modal
          title={
            {
              apply: "確認登記初選",
              cancel: "確認取消初選登記",
              add: "確認加選課程",
              drop: "確認退選課程",
            }[confirmation.action]
          }
          onClose={() => {
            if (!busy) setConfirmation(null);
          }}
        >
          <div className="detail">
            <h3>
              {confirmation.section.code} {confirmation.section.title}
            </h3>
            <p>
              {confirmation.section.credits} 學分 ·{" "}
              {confirmation.section.instructor_name}
            </p>
            <Meetings items={confirmation.section.meetings} />
            <p>
              {confirmation.action === "apply"
                ? "登記後會在課表顯示「待分發」。"
                : confirmation.action === "drop"
                  ? "成功退選後即釋出名額。再次加選須依當時資格與剩餘名額重新審核。"
                  : confirmation.action === "add"
                    ? "提交時重新檢查先修條件、衝堂、學分上限與名額。"
                    : "取消後可在初選期限內重新登記其他班別。"}
            </p>
          </div>
          <div className="modal-actions">
            <button onClick={() => setConfirmation(null)} disabled={busy}>
              返回
            </button>
            <button
              className={confirmation.action === "drop" ? "danger" : "primary"}
              disabled={busy}
              onClick={commit}
            >
              {busy ? "處理中…" : "確認提交"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

type CatalogOptions = {
  courses: { id: string; code: string; title: string }[];
  instructors: { id: string; name: string; department_id: string }[];
  departments: { id: string; name: string }[];
};
const localInput = (s: string) =>
  new Date(new Date(s).getTime() + 8 * 3600000).toISOString().slice(0, 16);
function Admin({
  term,
  mutate,
  busy,
  revision,
  onRefresh,
  onSelect,
  onToast,
}: {
  term?: Term;
  mutate: Mutation;
  busy: boolean;
  revision: number;
  onRefresh: () => void;
  onSelect: (id?: string) => Promise<void>;
  onToast: (s: string) => void;
}) {
  const [options, setOptions] = useState<CatalogOptions | null>(null),
    [registration, setRegistration] = useState<Registration | null>(null),
    [sections, setSections] = useState<Page | null>(null),
    [page, setPage] = useState(1),
    [notes, setNotes] = useState<any[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const [termForm, setTermForm] = useState({
      code: "",
      start_date: "",
      end_date: "",
      allocation_seed: "ntust-demo-seed",
    }),
    [times, setTimes] = useState(["", "", "", ""]),
    [seed, setSeed] = useState(""),
    [editing, setEditing] = useState<Section | null | undefined>(undefined),
    [operation, setOperation] = useState<"publish" | "allocation" | null>(null);
  useEffect(() => {
    api<CatalogOptions>("/admin/catalog")
      .then(setOptions)
      .catch((e) => setError(message(e)));
  }, []);
  useEffect(() => setPage(1), [term?.id]);
  useEffect(() => {
    if (!term) return;
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([
      api<Registration>(`/admin/terms/${term.id}/registration`),
      api<Page>(`/admin/terms/${term.id}/sections?page=${page}&page_size=50`),
      api<{ items: any[] }>(
        `/admin/terms/${term.id}/notifications?page_size=100`,
      ),
    ])
      .then(([r, s, n]) => {
        if (active) {
          setRegistration(r);
          setSections(s);
          setNotes(n.items);
          const init = r.periods.find((p) => p.kind === "INITIAL"),
            add = r.periods.find((p) => p.kind === "ADD_DROP");
          setTimes([
            init ? localInput(init.starts_at) : "",
            init ? localInput(init.ends_at) : "",
            add ? localInput(add.starts_at) : "",
            add ? localInput(add.ends_at) : "",
          ]);
          setSeed(term.allocation_seed);
        }
      })
      .catch((e) => {
        if (active) setError(message(e));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [term?.id, revision, page]);
  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      const t = await mutate("/admin/terms", "POST", termForm);
      await onSelect(t.id);
      setTermForm({
        code: "",
        start_date: "",
        end_date: "",
        allocation_seed: "ntust-demo-seed",
      });
      onToast("學期草稿已建立。");
    } catch (e) {
      onToast(message(e));
    }
  }
  async function periods(e: FormEvent) {
    e.preventDefault();
    if (!term) return;
    try {
      await mutate(`/admin/terms/${term.id}/registration-periods`, "PUT", {
        periods: [
          {
            kind: "INITIAL",
            starts_at: times[0] + ":00+08:00",
            ends_at: times[1] + ":00+08:00",
          },
          {
            kind: "ADD_DROP",
            starts_at: times[2] + ":00+08:00",
            ends_at: times[3] + ":00+08:00",
          },
        ],
        allocation_seed: seed,
      });
      onRefresh();
      onToast("選課時段與分發種子已儲存。");
    } catch (e) {
      onToast(message(e));
    }
  }
  async function run() {
    if (!operation || !term) return;
    try {
      const data = await mutate(`/admin/terms/${term.id}/${operation}`, "POST");
      setOperation(null);
      onRefresh();
      await onSelect(term.id);
      onToast(
        operation === "allocation"
          ? `分發公布完成：錄取 ${data.accepted_count} 筆、未錄取 ${data.rejected_count} 筆。`
          : "課程目錄已公開。",
      );
    } catch (e) {
      onToast(message(e));
    }
  }
  return (
    <div className="admin-stack">
      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>建立學期草稿</h2>
            <p>課程主檔、教師與學生修課資格由種子資料建立。</p>
          </div>
        </div>
        <form className="admin-form" onSubmit={create}>
          <div className="form-grid">
            <label>
              學期代碼
              <input
                required
                value={termForm.code}
                onChange={(e) =>
                  setTermForm({ ...termForm, code: e.target.value })
                }
                placeholder="例如 116-1"
              />
            </label>
            <label>
              分發種子
              <input
                required
                value={termForm.allocation_seed}
                onChange={(e) =>
                  setTermForm({ ...termForm, allocation_seed: e.target.value })
                }
              />
            </label>
            <label>
              學期開始日
              <input
                type="date"
                required
                value={termForm.start_date}
                onChange={(e) =>
                  setTermForm({ ...termForm, start_date: e.target.value })
                }
              />
            </label>
            <label>
              學期結束日
              <input
                type="date"
                required
                min={termForm.start_date}
                value={termForm.end_date}
                onChange={(e) =>
                  setTermForm({ ...termForm, end_date: e.target.value })
                }
              />
            </label>
          </div>
          <button className="primary" disabled={busy}>
            建立草稿
          </button>
        </form>
      </section>
      {term && (
        <State loading={loading} error={error} retry={onRefresh}>
          <section className="panel">
            <div className="panel-title">
              <div>
                <h2>{term.code} · 選課時程</h2>
                <p>臺北時間（UTC+8）；公開後固定時段與分發種子。</p>
              </div>
              <Badge>{term.catalog_published_at ? "已公開" : "草稿"}</Badge>
            </div>
            <form className="admin-form" onSubmit={periods}>
              <fieldset disabled={!!term.catalog_published_at || busy}>
                <div className="form-grid">
                  {["初選開始", "初選截止", "加退選開始", "加退選截止"].map(
                    (label, i) => (
                      <label key={label}>
                        {label}
                        <input
                          type="datetime-local"
                          required
                          value={times[i]}
                          onChange={(e) =>
                            setTimes((t) =>
                              t.map((v, j) => (i === j ? e.target.value : v)),
                            )
                          }
                        />
                      </label>
                    ),
                  )}
                  <label>
                    分發種子
                    <input
                      required
                      value={seed}
                      onChange={(e) => setSeed(e.target.value)}
                    />
                  </label>
                </div>
                <button className="primary">儲存時程</button>
              </fieldset>
            </form>
          </section>
          <section className="panel">
            <div className="panel-title">
              <div>
                <h2>開課班別</h2>
                <p>
                  {term.catalog_published_at
                    ? "公開後僅能調整容量，且不可低於目前有效選課人數。"
                    : "設定課程、教師、時段與先修條件。"}
                </p>
              </div>
              <button
                className="primary small"
                disabled={!!term.catalog_published_at || !options}
                onClick={() => setEditing(null)}
              >
                新增開課
              </button>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>課程 / 班別</th>
                    <th>教師 / 時段</th>
                    <th>已選 / 容量</th>
                    <th>狀態</th>
                    <th>管理</th>
                  </tr>
                </thead>
                <tbody>
                  {sections?.items.map((s) => (
                    <tr key={s.id}>
                      <td>
                        {s.code} {s.title}
                        <small>
                          班別 {s.section_no} · {s.credits} 學分
                        </small>
                      </td>
                      <td>
                        {s.instructor_name}
                        <Meetings items={s.meetings} />
                      </td>
                      <td>
                        {s.enrolled_count} / {s.capacity}
                      </td>
                      <td>
                        <Badge>{s.is_selectable ? "開放" : "未開放"}</Badge>
                      </td>
                      <td>
                        <button className="small" onClick={() => setEditing(s)}>
                          編輯{term.catalog_published_at ? "容量" : ""}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!sections?.items.length && (
              <div className="state">尚無開課班別</div>
            )}
            <div className="pagination">
              <span>共 {sections?.total || 0} 個班別</span>
              <div>
                <button
                  className="small"
                  disabled={page === 1}
                  onClick={() => setPage((p) => p - 1)}
                >
                  上一頁
                </button>
                {page}
                <button
                  className="small"
                  disabled={page * 50 >= (sections?.total || 0)}
                  onClick={() => setPage((p) => p + 1)}
                >
                  下一頁
                </button>
              </div>
            </div>
          </section>
          <section className="panel">
            <div className="panel-title">
              <div>
                <h2>公布與初選分發</h2>
                <p>分發、結果、正式選課及通知事件在同一交易公布。</p>
              </div>
            </div>
            <div className="admin-operations">
              <div>
                <h3>公開課程目錄</h3>
                <p>公開前確認所有班別都有合法且互不重疊的上課時段。</p>
                <button
                  disabled={busy || !!term.catalog_published_at}
                  onClick={() => setOperation("publish")}
                >
                  {term.catalog_published_at ? "目錄已公開" : "公開目錄"}
                </button>
              </div>
              <div>
                <h3>分發並公布</h3>
                <p>固定種子順序分發。重送會返回相同批次。</p>
                <button
                  className="primary"
                  disabled={busy || !term.catalog_published_at}
                  onClick={() => setOperation("allocation")}
                >
                  {registration?.allocation
                    ? "查詢既有分發批次"
                    : "執行分發並公布"}
                </button>
                {registration?.allocation && (
                  <p className="notice">
                    錄取 {registration.allocation.accepted_count} 筆 / 未錄取{" "}
                    {registration.allocation.rejected_count} 筆
                    <small>公布 {dateTime(registration.published_at)}</small>
                  </p>
                )}
              </div>
            </div>
          </section>
          <section className="panel">
            <div className="panel-title">
              <div>
                <h2>Email 通知狀態</h2>
                <p>最近 100 筆寄送紀錄 · 通知信經 Gmail 寄出（訊息 ID 以 smtp: 開頭為 Gmail SMTP，其餘為 Gmail API）</p>
              </div>
              <button className="small" onClick={onRefresh}>
                更新狀態
              </button>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>事件</th>
                    <th>狀態</th>
                    <th>嘗試次數</th>
                    <th>寄送時間 / 下次嘗試</th>
                    <th>錯誤摘要</th>
                    <th>訊息 ID</th>
                  </tr>
                </thead>
                <tbody>
                  {notes.map((n) => (
                    <tr key={n.id}>
                      <td>
                        {{
                          ALLOCATION: "分發摘要",
                          TIMETABLE: "課表寄送",
                          INITIAL_RECEIPT: "初選登記確認",
                          REGISTRATION_CONFIRMATION: "選課確認信",
                          ENROLLED: "加選成功",
                          DROPPED: "退選成功",
                        }[n.type as string] || n.type}
                        <small>{n.id.slice(0, 8)}</small>
                      </td>
                      <td>
                        <Badge status={n.status}>{n.status === "PENDING" ? "等待寄送" : undefined}</Badge>
                      </td>
                      <td>{n.attempt_count} / 3</td>
                      <td>{dateTime(n.sent_at || n.next_attempt_at)}</td>
                      <td>
                        {n.last_error || "—"}
                        {n.last_error && GMAIL_HINTS[n.last_error] && <small>{GMAIL_HINTS[n.last_error]}</small>}
                      </td>
                      <td>{n.provider_message_id || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!notes.length && <div className="state">尚無通知事件</div>}
          </section>
        </State>
      )}
      {editing !== undefined && term && options && (
        <SectionEditor
          section={editing}
          term={term}
          options={options}
          busy={busy}
          mutate={mutate}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined);
            onRefresh();
            onToast("班別已儲存。");
          }}
          onError={onToast}
        />
      )}
      {operation && (
        <Modal
          title={
            operation === "allocation" ? "確認分發並公布" : "確認公開課程目錄"
          }
          onClose={() => {
            if (!busy) setOperation(null);
          }}
        >
          <div className="detail">
            <h3>{term?.code}</h3>
            <p>
              {operation === "allocation"
                ? "初選截止後可執行分發，公布後每位有結果的學生產生一封摘要通知。既有分發批次將直接返回。"
                : "公開後將固定選課時段、分發種子與班別條件，之後只可調整班別容量。"}
            </p>
          </div>
          <div className="modal-actions">
            <button disabled={busy} onClick={() => setOperation(null)}>
              返回
            </button>
            <button className="primary" disabled={busy} onClick={run}>
              {busy ? "處理中…" : "確認執行"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
function SectionEditor({
  section,
  term,
  options,
  busy,
  mutate,
  onClose,
  onSaved,
  onError,
}: {
  section: Section | null;
  term: Term;
  options: CatalogOptions;
  busy: boolean;
  mutate: Mutation;
  onClose: () => void;
  onSaved: () => void;
  onError: (s: string) => void;
}) {
  const [form, setForm] = useState({
    course_id: section?.course_id || options.courses[0]?.id || "",
    section_no: section?.section_no || "A",
    instructor_id: section?.instructor_id || options.instructors[0]?.id || "",
    credits: String(section?.credits || 3),
    capacity: section?.capacity ?? 30,
    is_selectable: section?.is_selectable ?? true,
    prerequisite_course_ids: section?.prerequisites.map((p) => p.id) || [],
  });
  const [meetings, setMeetings] = useState<Meeting[]>(
    section?.meetings.map(({ weekday, start_time, end_time, room }) => ({
      weekday,
      start_time: short(start_time),
      end_time: short(end_time),
      room,
    })) || [{ weekday: 1, start_time: "09:00", end_time: "12:00", room: "" }],
  );
  const frozen = !!term.catalog_published_at;
  async function save(e: FormEvent) {
    e.preventDefault();
    try {
      const payload = frozen
        ? { capacity: form.capacity }
        : { ...form, meetings };
      await mutate(
        `/admin/terms/${term.id}/sections${section ? "/" + section.id : ""}`,
        section ? "PATCH" : "POST",
        payload,
      );
      onSaved();
    } catch (e) {
      onError(message(e));
    }
  }
  function change(i: number, key: keyof Meeting, value: string | number) {
    setMeetings((m) => m.map((v, j) => (i === j ? { ...v, [key]: value } : v)));
  }
  return (
    <Modal
      title={
        frozen ? "調整班別容量" : section ? "編輯開課班別" : "新增開課班別"
      }
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form className="form-body" onSubmit={save}>
        {frozen && (
          <p>
            {section?.code} {section?.title} · 已選 {section?.enrolled_count} 人
          </p>
        )}
        <div className="form-grid">
          <label>
            班別容量
            <input
              type="number"
              min={section?.enrolled_count || 0}
              max={10000}
              required
              value={form.capacity}
              onChange={(e) =>
                setForm({ ...form, capacity: Number(e.target.value) })
              }
            />
          </label>
        </div>
        {!frozen && (
          <>
            <div className="form-grid">
              <label>
                課程
                <select
                  required
                  value={form.course_id}
                  onChange={(e) =>
                    setForm({ ...form, course_id: e.target.value })
                  }
                >
                  {options.courses.map((c) => (
                    <option value={c.id} key={c.id}>
                      {c.code} {c.title}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                班別序號
                <input
                  required
                  value={form.section_no}
                  onChange={(e) =>
                    setForm({ ...form, section_no: e.target.value })
                  }
                />
              </label>
              <label>
                授課教師
                <select
                  required
                  value={form.instructor_id}
                  onChange={(e) =>
                    setForm({ ...form, instructor_id: e.target.value })
                  }
                >
                  {options.instructors.map((i, index) => (
                    <option value={i.id} key={i.id}>
                      {i.name} · {options.departments.find((d) => d.id === i.department_id)?.name} · {index + 1}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                學分
                <input
                  type="number"
                  min="0.1"
                  max="99.9"
                  step="0.1"
                  required
                  value={form.credits}
                  onChange={(e) =>
                    setForm({ ...form, credits: e.target.value })
                  }
                />
              </label>
            </div>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={form.is_selectable}
                onChange={(e) =>
                  setForm({ ...form, is_selectable: e.target.checked })
                }
              />
              開放選課
            </label>
            <fieldset>
              <legend>先修科目（可複選）</legend>
              <div className="prereq-options">
                {options.courses
                  .filter((c) => c.id !== form.course_id)
                  .map((c) => (
                    <label className="checkbox" key={c.id}>
                      <input
                        type="checkbox"
                        checked={form.prerequisite_course_ids.includes(c.id)}
                        onChange={(e) =>
                          setForm({
                            ...form,
                            prerequisite_course_ids: e.target.checked
                              ? [...form.prerequisite_course_ids, c.id]
                              : form.prerequisite_course_ids.filter(
                                  (id) => id !== c.id,
                                ),
                          })
                        }
                      />
                      {c.code} {c.title}
                    </label>
                  ))}
              </div>
            </fieldset>
            <h3>每週上課時段</h3>
            {meetings.map((m, i) => (
              <div className="meeting-form" key={i}>
                <label>
                  星期
                  <select
                    value={m.weekday}
                    onChange={(e) =>
                      change(i, "weekday", Number(e.target.value))
                    }
                  >
                    {DAYS.slice(1).map((d, j) => (
                      <option value={j + 1} key={d}>
                        週{d}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  開始
                  <input
                    type="time"
                    required
                    value={m.start_time}
                    onChange={(e) => change(i, "start_time", e.target.value)}
                  />
                </label>
                <label>
                  結束
                  <input
                    type="time"
                    required
                    value={m.end_time}
                    onChange={(e) => change(i, "end_time", e.target.value)}
                  />
                </label>
                <label>
                  教室
                  <input
                    value={m.room}
                    onChange={(e) => change(i, "room", e.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="danger small"
                  disabled={meetings.length === 1}
                  onClick={() =>
                    setMeetings((ms) => ms.filter((_, j) => j !== i))
                  }
                >
                  移除
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() =>
                setMeetings((m) => [
                  ...m,
                  {
                    weekday: 1,
                    start_time: "13:00",
                    end_time: "14:00",
                    room: "",
                  },
                ])
              }
            >
              新增時段
            </button>
          </>
        )}
        <div className="modal-actions">
          <button type="button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "儲存中…" : "儲存班別"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
