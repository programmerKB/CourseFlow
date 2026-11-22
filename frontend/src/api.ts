export type User = {
  id: string;
  login: string;
  role: "STUDENT" | "ADMIN";
  display_name: string;
  student_id: string | null;
};
export type Term = {
  id: string;
  code: string;
  start_date: string;
  end_date: string;
  catalog_published_at: string | null;
  allocation_seed: string;
};
export type Meeting = {
  id?: string;
  weekday: number;
  start_time: string;
  end_time: string;
  room: string;
};
export type Section = {
  id: string;
  term_id: string;
  course_id: string;
  code: string;
  title: string;
  description: string;
  section_no: string;
  department_id: string;
  department_name: string;
  instructor_id: string;
  instructor_name: string;
  credits: number | string;
  capacity: number;
  enrolled_count: number;
  remaining: number;
  is_selectable: boolean;
  meetings: Meeting[];
  prerequisites: { id: string; code: string; title: string }[];
  as_of: string;
};
export type Application = Section & {
  application_id: string;
  status: string;
  reason_code: string | null;
  reason: string | null;
  submitted_at: string;
  decided_at: string | null;
};
export type Timetable = {
  items: Section[];
  pending_items: Section[];
  total_credits: number | string;
  credit_limit: number | string;
  as_of: string;
};
export type Period = {
  kind: "INITIAL" | "ADD_DROP";
  starts_at: string;
  ends_at: string;
};
export type Registration = {
  term_id?: string;
  phase: string;
  periods: Period[];
  as_of: string;
  published_at: string | null;
  allocation?: {
    run_id: string;
    accepted_count: number;
    rejected_count: number;
  };
};
export type Page = {
  items: Section[];
  total: number;
  page: number;
  page_size: number;
  as_of: string;
};
export type Source = {
  title: string;
  url: string;
  term_id?: string;
  section_id?: string;
};
export type Eligibility = {
  eligible: boolean;
  reason_code: string | null;
  message: string;
  as_of: string;
};
export type Candidate = {
  section_id: string;
  course_code: string;
  course_title: string;
  section_no: string;
  credits: string;
  meetings: Meeting[];
  remaining: number;
  score: number;
  match_reason: string;
  selected: boolean;
  reason_code: string | null;
  prerequisites: Section["prerequisites"];
  is_selectable: boolean;
  eligibility: Pick<Eligibility, "eligible" | "reason_code"> | null;
};
export type Plan = {
  plan_id: string;
  term_id: string;
  phase: "INITIAL" | "ADD_DROP";
  as_of: string;
  expires_at: string;
  section_ids: string[];
  projected_credits: string;
};
export type Recommendation = {
  topic: string;
  candidates: Candidate[];
  learning_extensions: { title: string; next_step: string; section_ids: string[] }[];
  target_count: number;
  baseline: {
    enrolled: Section[];
    pending: Section[];
    considered_credits: string;
    credit_limit: string;
  } | null;
  registration: Registration;
  plan: Plan | null;
  plan_unavailable_reason: string | null;
};
export type ChatResponse = {
  answer: string;
  sources: Source[];
  as_of: string;
  mode: string;
  request_id: string;
  recommendation?: Recommendation;
};
export type PlanConfirmation = {
  plan_id: string;
  phase: Plan["phase"];
  items: { section_id: string; status: "PENDING" | "ENROLLED" }[];
  submitted_at: string;
};

export function newRequestKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: any,
  ) {
    super(message);
  }
}
export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch("/api/v1" + path, {
      credentials: "same-origin",
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "連線中斷。請重試同一操作，系統會保留原重送鍵。",
    );
  }
  if (response.status === 204) return undefined as T;
  let data;
  try {
    data = await response.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(
      response.ok ? 0 : response.status,
      "INVALID_RESPONSE",
      "伺服器回應格式錯誤，請稍後重試。",
    );
  }
  if (!response.ok)
    throw new ApiError(
      response.status,
      data?.code || "REQUEST_FAILED",
      data?.message || "請求未完成。",
      data?.details,
    );
  return data as T;
}
export const DAYS = ["", "一", "二", "三", "四", "五", "六", "日"];
export const PHASES: Record<string, string> = {
  UPCOMING: "初選未開放",
  INITIAL: "初選登記中",
  AWAITING_ALLOCATION: "等待分發公布",
  RESULTS: "分發結果已公布",
  ADD_DROP: "即時加退選",
  CLOSED: "選課已截止",
};
export const STATUS: Record<string, string> = {
  PENDING: "待分發",
  CANCELLED: "已取消登記",
  ACCEPTED: "初選錄取",
  REJECTED: "未錄取",
  ENROLLED: "正式選課",
  DROPPED: "已退選",
  SENT: "已寄送",
  PROCESSING: "寄送中",
  FAILED: "寄送失敗",
};
const taipeiDateTime = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
export function dateTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const parts = Object.fromEntries(taipeiDateTime.formatToParts(date).map(({ type, value }) => [type, value]));
  return `${parts.year}/${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`;
}
export const short = (s: string) => s.slice(0, 5);
