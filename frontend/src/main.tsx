import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  Anchor,
  ArrowDownRight,
  ArrowRight,
  BarChart3,
  Check,
  ChevronRight,
  CircleHelp,
  ClipboardCheck,
  Clock3,
  CloudSun,
  Container,
  FileText,
  Globe2,
  Layers3,
  Leaf,
  Map,
  Menu,
  RefreshCw,
  Route as RouteIcon,
  Settings2,
  ShieldCheck,
  Ship,
  SlidersHorizontal,
  TriangleAlert,
  X,
} from "lucide-react";
import "./style.css";

type Port = {
  id: string;
  name: string;
  country: string;
  lat: number;
  lon: number;
  kind: string;
};
type Leg = {
  service_id: string;
  origin_id: string;
  destination_id: string;
  mode: string;
  departure_at: string;
  arrival_at: string;
  cutoff_at: string;
  distance_km: number;
  cost_usd: number;
  co2_kg: number;
};
type Route = {
  id: string;
  label: string;
  feasible: boolean;
  reason: string | null;
  missed_connection: boolean;
  eta: string | null;
  cost_usd: number;
  co2_kg: number;
  legs: Leg[];
  emissions: {
    total_kg: number;
    by_leg_kg: number[];
    factor_version: string;
    factors_kg_per_tonne_km: Record<string, number>;
    disclaimer: string;
  };
};
type Event = {
  id: string;
  event_type: string;
  location_id: string;
  planned_at: string | null;
  estimated_at: string | null;
  actual_at: string | null;
  source: string;
  quality_flag: string;
};
type Doc = {
  kind: string;
  number: string;
  container_id: string;
  mass_kg: number;
};
type Payload = {
  shipper: string;
  consignee: string;
  description: string;
  mass_kg: number;
  value_usd: number;
  origin_country: string;
  hs_code: string;
  container_id: string;
  documents: Doc[];
};
type Declaration = {
  id: string;
  version: number;
  status: string;
  payload: Payload;
  validation: {
    valid: boolean;
    issues: { field: string; code: string; message: string }[];
  };
  content_hash: string;
  updated_at: string;
};
type Detail = {
  simulation_now: string;
  synthetic: boolean;
  shipment: {
    id: string;
    reference: string;
    container_id: string;
    cargo: string;
    mass_kg: number;
    origin_id: string;
    destination_id: string;
    current_location_id: string;
    planned_port_arrival: string;
    planned_delivery_at: string;
    documents_ready_at: string;
    queue_hours: number;
    events: Event[];
  };
  weather: {
    wind_mps: number;
    precipitation_mm: number;
    source: string;
    observed_at: string;
    stale: boolean;
  };
  scenario: { kind: string; wind_mps?: number | null };
  prediction: {
    delay_hours: number;
    ready_at: string;
    miss_probability: number;
    risk: string;
    factors: string[];
    model_version: string;
    data_source: string;
  };
  current_route: Route;
  alternatives: Route[];
  selected_route_id: string | null;
  preclearance: Declaration | null;
};
type Dashboard = {
  simulation_now: string;
  synthetic: boolean;
  shipments: Detail[];
  summary: Record<string, number>;
};
type Metrics = {
  model_version: string;
  train_rows: number;
  test_rows: number;
  split_at: string;
  baseline_mae_hours: number;
  model_mae_hours: number;
  roc_auc: number | null;
  disclaimer: string;
};
type Audit = {
  sequence: number;
  action: string;
  actor: string;
  at: string;
  hash: string;
  previous_hash: string;
  payload: Record<string, unknown>;
};
type Verification = {
  valid: boolean;
  entries_checked: number;
  first_invalid_sequence: number | null;
  disclaimer: string;
};

const fmt = (n: number | null | undefined, digits = 0) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(
        n,
      );
const date = (s: string | null | undefined) => {
  if (!s) return "—";
  const t = new Date(s);
  return Number.isNaN(t.getTime())
    ? "—"
    : new Intl.DateTimeFormat("ru-RU", {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "UTC",
      }).format(t) + " UTC";
};
// Own-property lookup: server strings such as "constructor" or "__proto__"
// must not resolve to inherited Object.prototype members.
const lookup = (
  table: Record<string, string>,
  key: unknown,
): string | undefined =>
  typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key)
    ? table[key]
    : undefined;
const riskLabels: Record<string, string> = {
  low: "Низкий",
  medium: "Средний",
  high: "Высокий",
};
const statuses: Record<string, string> = {
  draft: "Черновик",
  validation_failed: "Есть замечания",
  ready: "Готова к отправке",
  submitted: "На рассмотрении",
  demo_accepted: "Принята в демо",
  needs_revision: "На доработке",
};
const scenarios: Record<string, string> = {
  none: "Базовый",
  storm: "Шторм",
  congestion: "Очередь",
  closure: "Закрытие",
};
const modes: Record<string, string> = {
  sea: "Море",
  road: "Авто",
  rail: "Ж/д",
};
const docs: Record<string, string> = {
  invoice: "Инвойс",
  packing_list: "Упаковочный лист",
  transport: "Транспортный документ",
};
const portNames: Record<string, string> = {
  rail_hub: "Астраханский ж/д терминал",
  makhachkala: "Порт Махачкала",
  olya: "Порт Оля",
  anzali: "Порт Энзели",
  tehran: "Сухой порт Тегеран",
};
const translations: Record<string, string> = {
  "High wind": "Сильный ветер",
  "Moderate wind": "Умеренный ветер",
  "Low wind": "Слабый ветер",
  "Long terminal queue": "Большая очередь в терминале",
  "Short terminal queue": "Небольшая очередь в терминале",
  "Precipitation present": "Есть осадки",
  "Known arrival delay included once": "Учтена известная задержка прибытия",
  "Scenario queue extension included once":
    "Учтено увеличение очереди по сценарию",
  "Documents determine readiness": "Готовность зависит от документов",
  "Documents ready after cutoff": "Документы будут готовы после дедлайна",
  "Miss probability from synthetic forecast-error spread; not guaranteed calibrated":
    "Вероятность оценена по разбросу ошибок прогноза на синтетике; калибровка не гарантирована",
  "Readiness after demo cutoff": "Готовность после демо-дедлайна",
  "Readiness within demo cutoff": "Готовность до демо-дедлайна",
  "Planned route": "Плановый маршрут",
  "Planned Caspian crossing": "Плановый переход через Каспий",
  "Next-departure recovery": "Восстановление на следующем рейсе",
  "Alternative route": "Альтернативный маршрут",
  "Planned route is unavailable under current conditions.":
    "Плановый маршрут недоступен в текущих условиях.",
  "Planned connection missed; showing earliest recovery.":
    "Плановая стыковка пропущена; показан ближайший вариант восстановления.",
  rail_departure: "Отправление по железной дороге",
  port_arrival: "Прибытие в порт",
  sea_departure: "Морское отправление",
  road_departure: "Отправление автотранспортом",
  delivery: "Доставка",
};
const translate = (value: string) => lookup(translations, value) ?? value;
const portName = (port: Port) => lookup(portNames, port.id) ?? port.name;
const riskLabel = (risk: string) => lookup(riskLabels, risk) ?? risk;
const riskClass = (risk: string) =>
  lookup(riskLabels, risk) !== undefined ? risk : "";
function scheduleStatus(route: Route): string | null {
  if (route.missed_connection) return "Стыковка пропущена";
  if (!route.feasible) return "Маршрут недоступен";
  return null;
}
const clip = (s: string, max = 300) =>
  s.length > max ? s.slice(0, max) + "…" : s;
function validationSummary(detail: unknown): string {
  if (!Array.isArray(detail)) return "";
  return detail
    .slice(0, 3)
    .map((item: unknown) => {
      if (!item || typeof item !== "object") return "";
      const { loc, msg } = item as { loc?: unknown; msg?: unknown };
      const field = Array.isArray(loc)
        ? loc
            .filter(
              (x: unknown) =>
                (typeof x === "string" && x !== "body") ||
                typeof x === "number",
            )
            .join(".")
        : "";
      const text = typeof msg === "string" ? msg : "некорректное значение";
      return field ? `${field}: ${text}` : text;
    })
    .filter((s) => s !== "")
    .join("; ");
}
function errorMessage(
  status: number,
  retryAfter: string | null,
  body: unknown,
): string {
  const detail =
    body && typeof body === "object" && !Array.isArray(body)
      ? (body as { detail?: unknown }).detail
      : undefined;
  const serverText = typeof detail === "string" ? detail : "";
  if (status === 401)
    return "Неверный или отсутствующий токен доступа. Проверьте настройки доступа.";
  if (status === 403) return "Недостаточно прав для этого действия.";
  if (status === 413) return "Слишком большой объём данных в запросе.";
  if (status === 422) {
    const summary = validationSummary(detail) || serverText;
    return clip(
      "Данные не прошли проверку" + (summary ? ": " + summary : "."),
    );
  }
  if (status === 429) {
    const wait = (retryAfter || "").trim();
    return /^\d{1,5}$/.test(wait)
      ? `Слишком много запросов, повторите через ${Number(wait)} с.`
      : "Слишком много запросов, повторите позже.";
  }
  if (status >= 500) return "Ошибка сервера. Повторите попытку позже.";
  if ((status === 404 || status === 409 || status === 400) && serverText)
    return clip(serverText);
  return `Ошибка запроса (${status}).`;
}
async function api<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  if (token && !/^[\x21-\x7e]+$/.test(token))
    throw new Error("Токен содержит недопустимые символы.");
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...((options.headers as Record<string, string>) || {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  let response: Response;
  try {
    response = await fetch("/api" + path, {
      ...options,
      headers,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  } catch {
    throw new Error("Сервер недоступен или соединение прервано.");
  }
  let text = "";
  try {
    text = await response.text();
  } catch {
    text = "";
  }
  let body: unknown = undefined;
  let parsed = false;
  if (text) {
    try {
      body = JSON.parse(text);
      parsed = true;
    } catch {
      parsed = false;
    }
  }
  if (!response.ok)
    throw new Error(
      errorMessage(
        response.status,
        response.headers.get("Retry-After"),
        parsed ? body : undefined,
      ),
    );
  if (!parsed || body === null || typeof body !== "object")
    throw new Error("Некорректный ответ сервера.");
  return body as T;
}
function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
const TOKEN_KEYS = { broker: "sct-broker", reviewer: "sct-reviewer" } as const;
const DEFAULT_TOKENS = {
  broker: "demo-broker-token",
  reviewer: "demo-reviewer-token",
} as const;
// Tokens live only in sessionStorage (cleared with the tab); storage access
// can throw (private mode, blocked storage), so every call is guarded.
function loadToken(key: string, fallback: string): string {
  try {
    const value = sessionStorage.getItem(key);
    return value === null ? fallback : value.trim();
  } catch {
    return fallback;
  }
}
function storeToken(key: string, value: string) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    /* storage unavailable: token stays in memory for this page only */
  }
}
try {
  localStorage.removeItem(TOKEN_KEYS.broker);
  localStorage.removeItem(TOKEN_KEYS.reviewer);
} catch {
  /* storage unavailable */
}
const AMOUNT_MAX = { mass_kg: 1_000_000, value_usd: 1_000_000_000_000 } as const;
// Number inputs: empty/NaN/Infinity/negative become 0 (rejected by the
// server as "must be > 0"), values above the server cap are clamped.
const toAmount = (raw: string, max: number) => {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.min(n, max) : 0;
};
const post = (body?: unknown): RequestInit => ({
  method: "POST",
  ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
});
function initialPayload(d: Detail): Payload {
  return {
    shipper: "",
    consignee: "",
    description: "",
    mass_kg: d.shipment.mass_kg,
    value_usd: 0,
    origin_country: "",
    hs_code: "",
    container_id: d.shipment.container_id,
    documents: Object.keys(docs).map((kind) => ({
      kind,
      number: "",
      container_id: d.shipment.container_id,
      mass_kg: d.shipment.mass_kg,
    })),
  };
}

function App() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null),
    [ports, setPorts] = useState<Port[]>([]),
    [selected, setSelected] = useState(""),
    [detail, setDetail] = useState<Detail | null>(null),
    [metrics, setMetrics] = useState<Metrics | null>(null);
  const [page, setPage] = useState<"overview" | "documents" | "model">(
      "overview",
    ),
    [broker, setBroker] = useState(() =>
      loadToken(TOKEN_KEYS.broker, DEFAULT_TOKENS.broker),
    ),
    [reviewer, setReviewer] = useState(() =>
      loadToken(TOKEN_KEYS.reviewer, DEFAULT_TOKENS.reviewer),
    ),
    [brokerDraft, setBrokerDraft] = useState(""),
    [reviewerDraft, setReviewerDraft] = useState(""),
    [settings, setSettings] = useState(false),
    [mobile, setMobile] = useState(false);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true),
    [form, setForm] = useState<Payload | null>(null),
    [audit, setAudit] = useState<Audit[]>([]),
    [verification, setVerification] = useState<Verification | null>(null),
    [comment, setComment] = useState("");
  const selectedRef = useRef("");
  const refreshVersion = useRef(0);
  const submitKey = useRef<{ scope: string; key: string } | null>(null);
  // Reads accept either role: broker token, else reviewer token.
  const authToken = broker || reviewer;
  const openSettings = () => {
    setBrokerDraft(broker);
    setReviewerDraft(reviewer);
    setSettings(true);
  };
  const closeSettings = () => {
    setBrokerDraft("");
    setReviewerDraft("");
    setSettings(false);
  };
  const name = (id: string) => {
    const port = ports.find((p) => p.id === id);
    return port ? portName(port) : id;
  };
  const switchShipment = (id: string) => {
    if (busy || id === selectedRef.current) return;
    refreshVersion.current++;
    selectedRef.current = id;
    setDetail(null);
    setForm(null);
    setAudit([]);
    setVerification(null);
    setSelected(id);
  };
  const refresh = async (id?: string) => {
    const version = ++refreshVersion.current;
    const [dash, cat] = await Promise.all([
      api<Dashboard>("/dashboard", {}, authToken),
      api<{ ports: Port[] }>("/catalog", {}, authToken),
    ]);
    if (version !== refreshVersion.current) return;
    setDashboard(dash);
    setPorts(cat.ports);
    const target = id || selectedRef.current || dash.shipments[0]?.shipment.id;
    if (target) {
      if (!selectedRef.current) {
        selectedRef.current = target;
        setSelected(target);
      } else {
        const updated = await api<Detail>(
          "/shipments/" + encodeURIComponent(target),
          {},
          authToken,
        );
        if (
          version === refreshVersion.current &&
          selectedRef.current === target
        )
          setDetail(updated);
      }
    }
  };
  // Re-runs when the saved token changes so a corrected token reloads data.
  useEffect(() => {
    setError("");
    refresh()
      .catch((e) => setError("Не удалось загрузить данные: " + e.message))
      .finally(() => setLoading(false));
  }, [authToken]);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    Promise.all([
      api<Detail>("/shipments/" + encodeURIComponent(selected), {}, authToken),
      api<Audit[]>(
        "/shipments/" + encodeURIComponent(selected) + "/audit",
        {},
        authToken,
      ),
    ])
      .then(([d, a]) => {
        if (active && selectedRef.current === selected) {
          setDetail(d);
          // Keep unsaved edits when only the token changed (switchShipment
          // resets form to null, so a new shipment still loads its payload).
          setForm((prev) => prev ?? (d.preclearance?.payload || initialPayload(d)));
          setAudit(a);
          setVerification(null);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [selected, authToken]);
  useEffect(() => {
    if (page === "model" && !metrics)
      api<Metrics>("/model/metrics", {}, authToken)
        .then(setMetrics)
        .catch((e) => setError(e.message));
  }, [page, authToken]);
  const mutate = async (
    path: string,
    options: RequestInit,
    role: "broker" | "reviewer" = "broker",
    declaration = false,
  ) => {
    if (!selected || busy || detail?.shipment.id !== selected) return;
    refreshVersion.current++;
    setBusy(true);
    setError("");
    try {
      const result = await api<Detail | Declaration>(
        `/shipments/${encodeURIComponent(selected)}${path}`,
        options,
        role === "broker" ? broker : reviewer,
      );
      if (declaration) {
        const dec = result as Declaration;
        setForm(dec.payload);
        setAudit(
          await api<Audit[]>(
            `/shipments/${encodeURIComponent(selected)}/audit`,
            {},
            authToken,
          ),
        );
        setVerification(null);
      }
      await refresh(selected);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const verify = async () => {
    const target = selected;
    try {
      const result = await api<Verification>(
        `/shipments/${encodeURIComponent(target)}/audit/verify`,
        {},
        authToken,
      );
      if (selectedRef.current === target) setVerification(result);
    } catch (e) {
      if (selectedRef.current === target) setError((e as Error).message);
    }
  };
  const selectPage = (p: "overview" | "documents" | "model") => {
    setPage(p);
    setMobile(false);
  };
  const d = detail?.shipment.id === selected ? detail : null,
    r = d?.current_route,
    submitted = d?.preclearance?.status === "submitted",
    dirty = !form || !d?.preclearance ||
      JSON.stringify(form) !== JSON.stringify(d.preclearance.payload),
    displayedForm = submitted ? d.preclearance!.payload : form;
  const review = (decision: "demo_accepted" | "needs_revision") => {
    if (busy || !submitted || dirty) return;
    mutate(
      "/preclearance/review",
      post({ decision, comment }),
      "reviewer",
      true,
    );
  };
  return (
    <div className="app">
      <aside className={"sidebar " + (mobile ? "open" : "")}>
        <div className="brand">
          <div className="brand-mark">
            <Globe2 size={22} />
          </div>
          <div>
            <strong>SMART CASPIAN</strong>
            <span>TRANSIT INTELLIGENCE</span>
          </div>
          <button
            className="icon mobile-close"
            onClick={() => setMobile(false)}
          >
            <X size={18} />
          </button>
        </div>
        <div className="workspace">
          <span className="workspace-icon">
            <Layers3 size={17} />
          </span>
          <div>
            <strong>Север — Юг</strong>
            <small>Демо-пространство</small>
          </div>
          <ChevronRight size={15} />
        </div>
        <div className="nav-caption">РАБОЧЕЕ ПРОСТРАНСТВО</div>
        <nav>
          <button
            className={page === "overview" ? "active" : ""}
            onClick={() => selectPage("overview")}
          >
            <Map size={18} /> Обзор перевозок{" "}
            <ChevronRight className="nav-arrow" size={15} />
          </button>
          <button
            className={page === "documents" ? "active" : ""}
            onClick={() => selectPage("documents")}
          >
            <FileText size={18} /> Предварительная декларация
          </button>
          <button
            className={page === "model" ? "active" : ""}
            onClick={() => selectPage("model")}
          >
            <BarChart3 size={18} /> Качество модели
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="demo-box">
            <span className="pulse" />
            <div>
              <strong>Демонстрационный режим</strong>
              <small>
                Синтетические данные.
                <br />
                Не для операционных решений.
              </small>
            </div>
          </div>
          <button className="settings-link" onClick={openSettings}>
            <Settings2 size={17} /> Настройки доступа
          </button>
          <div className="version">
            SMART CASPIAN TRANSIT <span>v1.0</span>
          </div>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <button className="icon mobile-menu" onClick={() => setMobile(true)}>
            <Menu size={20} />
          </button>
          <div className="breadcrumbs">
            Рабочее пространство <ChevronRight size={14} />{" "}
            <strong>
              {page === "overview"
                ? "Обзор перевозок"
                : page === "documents"
                  ? "Декларация"
                  : "Качество модели"}
            </strong>
          </div>
          <div className="top-right">
            <span className="live">
              <span className="pulse" /> ДЕМО ДАННЫЕ
            </span>
            <span className="clock">
              <Clock3 size={15} /> {date(dashboard?.simulation_now)}
            </span>
            <button
              className="avatar"
              onClick={openSettings}
              title="Настройки доступа"
            >
              SC
            </button>
          </div>
        </header>
        <div className="content">
          {error && (
            <div className="error">
              <TriangleAlert size={18} />
              <span>{error}</span>
              <button onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {loading ? (
            <div className="empty">Загрузка данных…</div>
          ) : !dashboard ? (
            <div className="empty">
              <TriangleAlert />
              <h2>Нет соединения с сервером</h2>
              <p>Проверьте, что API запущен на localhost:8000.</p>
              <button
                className="primary"
                onClick={() => {
                  setLoading(true);
                  refresh()
                    .catch((e) => setError(e.message))
                    .finally(() => setLoading(false));
                }}
              >
                Повторить
              </button>
            </div>
          ) : (
            <>
              {page === "overview" && (
                <>
                  <div className="page-heading">
                    <div>
                      <div className="eyebrow">
                        КОРИДОР СЕВЕР — ЮГ <span>/</span> ОБЗОР
                      </div>
                      <h1>
                        Контроль перевозок<span className="heading-dot">.</span>
                      </h1>
                      <p>
                        Вся логистика Каспийского коридора в одном пространстве
                      </p>
                    </div>
                    <div className="heading-actions">
                      <span className="outline-tag">
                        <span className="pulse" /> Симуляция активна
                      </span>
                      <button
                        className="secondary"
                        onClick={() =>
                          refresh().catch((e) => setError(e.message))
                        }
                      >
                        <RefreshCw size={15} /> Обновить
                      </button>
                    </div>
                  </div>
                  <div className="kpi-grid">
                    <Kpi
                      icon={<Container />}
                      label="ВСЕГО ПЕРЕВОЗОК"
                      value={fmt(dashboard.summary.total)}
                      sub="В демонстрационном контуре"
                    />
                    <Kpi
                      icon={<TriangleAlert />}
                      label="ВЫСОКИЙ РИСК"
                      value={fmt(dashboard.summary.high_risk)}
                      sub="Высокий риск модели или пропущенная стыковка"
                      accent="orange"
                    />
                    <Kpi
                      icon={<Leaf />}
                      label="ВЫБРОСЫ CO₂e"
                      value={fmt(dashboard.summary.co2_kg)}
                      unit="кг"
                      sub="Оставшаяся часть пути"
                    />
                    <Kpi
                      icon={<Activity />}
                      label="ИСТОЧНИК ДАННЫХ"
                      value={dashboard.synthetic ? "Демо" : "API"}
                      sub="Фиксированное время симуляции"
                      small
                    />
                  </div>
                  <div className="section-heading">
                    <div>
                      <div className="eyebrow">МОНИТОРИНГ</div>
                      <h2>
                        Активные перевозки{" "}
                        <span className="count">
                          {dashboard.shipments.length}
                        </span>
                      </h2>
                    </div>
                    <span className="muted small">
                      Выберите перевозку для деталей
                    </span>
                  </div>
                  <div className="shipment-list">
                    {dashboard.shipments.map((item) => (
                      <button
                        key={item.shipment.id}
                        className={
                          "shipment " +
                          (selected === item.shipment.id ? "selected" : "")
                        }
                        disabled={busy}
                        onClick={() => switchShipment(item.shipment.id)}
                      >
                        <span className="ship-icon">
                          <Container size={20} />
                        </span>
                        <span className="shipment-id">
                          <strong>{item.shipment.id}</strong>
                          <small>{item.shipment.container_id}</small>
                        </span>
                        <span className="shipment-route">
                          <strong>
                            {name(item.shipment.origin_id)}{" "}
                            <ArrowRight size={14} />{" "}
                            {name(item.shipment.destination_id)}
                          </strong>
                          <small>{item.shipment.reference}</small>
                        </span>
                        <span className="shipment-eta">
                          <small>ПРОГНОЗ ПРИБЫТИЯ</small>
                          <strong>{date(item.current_route.eta)}</strong>
                        </span>
                        <span
                          className={
                            "risk " +
                            (scheduleStatus(item.current_route)
                              ? "high"
                              : riskClass(item.prediction.risk))
                          }
                        >
                          <span />{" "}
                          {scheduleStatus(item.current_route) ||
                            `Риск модели: ${riskLabel(item.prediction.risk)}`}
                        </span>
                        <ChevronRight className="shipment-chevron" size={17} />
                      </button>
                    ))}
                  </div>
                  {d && r && (
                    <>
                      <div className="section-heading detail-heading">
                        <div>
                          <div className="eyebrow">
                            ДЕТАЛИ ПЕРЕВОЗКИ <span className="slash">/</span>{" "}
                            {d.shipment.id}
                          </div>
                          <h2>Маршрут и прогноз</h2>
                        </div>
                        <div className="detail-tags">
                          <span className="outline-tag">
                            {d.shipment.container_id}
                          </span>
                          <span className="outline-tag">
                            {fmt(d.shipment.mass_kg / 1000, 1)} т
                          </span>
                        </div>
                      </div>
                      <div className="main-grid">
                        <div className="left-column">
                          <section className="card map-card">
                            <div className="card-heading">
                              <div>
                                <h3>Обзор маршрута</h3>
                                <p>
                                  Оставшийся путь ·{" "}
                                  {d.selected_route_id
                                    ? "выбранная альтернатива"
                                    : "текущий маршрут"}
                                </p>
                              </div>
                              <span className="map-label">
                                <span className="pulse" /> СХЕМА МАРШРУТА
                              </span>
                            </div>
                            <RouteMap
                              ports={ports}
                              route={r}
                              current={d.shipment.current_location_id}
                            />
                            <div className="map-footer">
                              <div>
                                <span className="legend-line" /> Маршрут
                                перевозки
                              </div>
                              <div>
                                <span className="legend-dot" /> Следующий пункт
                                обработки
                              </div>
                              <span>
                                Схема по координатам · не навигационная карта
                              </span>
                            </div>
                          </section>
                          <section className="card timeline-card">
                            <div className="card-heading">
                              <div>
                                <h3>Хронология перевозки</h3>
                                <p>План, прогноз и фактические события · UTC</p>
                              </div>
                              <Clock3 size={18} className="muted" />
                            </div>
                            <div className="timeline-head">
                              <span>СОБЫТИЕ</span>
                              <span>ПЛАН</span>
                              <span>ПРОГНОЗ</span>
                              <span>ФАКТ</span>
                            </div>
                            {d.shipment.events.length ? (
                              d.shipment.events.map((e) => (
                                <div className="timeline-row" key={e.id}>
                                  <div className="event-name">
                                    <span className="event-dot" />
                                    <div>
                                      <strong>{translate(e.event_type)}</strong>
                                      <small>
                                        {name(e.location_id)} · {e.source} /{" "}
                                        {e.quality_flag}
                                      </small>
                                    </div>
                                  </div>
                                  <span>{date(e.planned_at)}</span>
                                  <span>{date(e.estimated_at)}</span>
                                  <span>{date(e.actual_at)}</span>
                                </div>
                              ))
                            ) : (
                              <div className="empty-small">
                                Событий пока нет
                              </div>
                            )}
                          </section>
                        </div>
                        <div className="right-column">
                          <section className="card insight-card">
                            <div className="card-heading">
                              <div>
                                <h3>Прогноз доставки</h3>
                                <p>На основе текущего сценария</p>
                              </div>
                              <span className={"risk " + riskClass(d.prediction.risk)}>
                                <span /> Риск модели:{" "}
                                {riskLabel(d.prediction.risk)}
                              </span>
                            </div>
                            <div className="eta-label">РАСЧЁТНОЕ ПРИБЫТИЕ</div>
                            <div className="eta-value">{date(r.eta)}</div>
                            <div className="eta-plan">
                              План: {date(d.shipment.planned_delivery_at)}
                            </div>
                            {!r.feasible && (
                              <div className="warning">
                                <TriangleAlert size={16} />
                                <span>
                                  {r.reason
                                    ? translate(r.reason)
                                    : "Исходное соединение недоступно"}
                                </span>
                              </div>
                            )}
                            <div className="separator" />
                            <div className="metric-row">
                              <span>Вероятность пропуска по модели</span>
                              <strong>
                                {fmt(d.prediction.miss_probability * 100, 1)}%
                              </strong>
                            </div>
                            <div className="progress">
                              <i
                                style={{
                                  width: `${Math.max(0, Math.min(100, d.prediction.miss_probability * 100 || 0))}%`,
                                }}
                              />
                            </div>
                            <p className="micro">
                              Синтетическая оценка модели, не гарантирует
                              калибровку; фактическая доступность определяется
                              расписанием.
                            </p>
                            <div className="metric-row">
                              <span>Готовность к отправке</span>
                              <strong>{date(d.prediction.ready_at)}</strong>
                            </div>
                            <div className="metric-row">
                              <span>
                                Дедлайн текущего рейса
                                {!r.feasible && r.eta
                                  ? " (восстановление)"
                                  : ""}
                              </span>
                              <strong>{date(r.legs[0]?.cutoff_at)}</strong>
                            </div>
                            <div className="metric-row">
                              <span>Доп. прогноз задержки</span>
                              <strong>
                                {fmt(d.prediction.delay_hours, 1)} ч
                              </strong>
                            </div>
                            <div className="factors">
                              {d.prediction.factors.map((x, i) => (
                                <span key={i}>{translate(x)}</span>
                              ))}
                            </div>
                          </section>
                          <section className="card scenario-card">
                            <div className="card-heading">
                              <div>
                                <h3>Сценарии</h3>
                                <p>Оцените влияние изменений</p>
                              </div>
                              <SlidersHorizontal size={18} className="muted" />
                            </div>
                            <div className="scenario-grid">
                              {(
                                [
                                  "none",
                                  "storm",
                                  "congestion",
                                  "closure",
                                ] as const
                              ).map((s, i) => (
                                <button
                                  disabled={busy}
                                  key={s}
                                  className={
                                    d.scenario.kind === s ? "active" : ""
                                  }
                                  onClick={() =>
                                    mutate("/scenario", post({ kind: s }))
                                  }
                                >
                                  {
                                    [
                                      <Activity />,
                                      <CloudSun />,
                                      <Clock3 />,
                                      <Anchor />,
                                    ][i]
                                  }
                                  <span>{scenarios[s]}</span>
                                </button>
                              ))}
                            </div>
                            <button
                              className="text-button"
                              disabled={busy}
                              onClick={() => mutate("/scenario/reset", post())}
                            >
                              <RefreshCw size={14} /> Сбросить сценарий
                            </button>
                            <p className="micro">
                              Сценарии заменяют друг друга. Шторм моделируется
                              локально у Махачкалы.
                            </p>
                          </section>
                        </div>
                      </div>
                      <div className="lower-grid">
                        <section className="card">
                          <div className="card-heading">
                            <div>
                              <h3>Альтернативные маршруты</h3>
                              <p>Доступные варианты оставшегося пути</p>
                            </div>
                            <RouteIcon size={19} className="muted" />
                          </div>
                          {d.alternatives.length ? (
                            d.alternatives.map((a) => (
                              <div className="alternative" key={a.id}>
                                <div>
                                  <strong>{translate(a.label)}</strong>
                                  {!a.feasible && (
                                    <small className="unavailable">
                                      {a.reason
                                        ? translate(a.reason)
                                        : "Маршрут недоступен для выбора"}
                                    </small>
                                  )}
                                  <small>
                                    {a.legs
                                      .map((l) => name(l.origin_id))
                                      .concat(
                                        a.legs.length
                                          ? [
                                              name(
                                                a.legs[a.legs.length - 1]
                                                  .destination_id,
                                              ),
                                            ]
                                          : [],
                                      )
                                      .join(" → ")}
                                  </small>
                                  <div className="alt-stats">
                                    {date(a.eta)} <span>·</span> $
                                    {fmt(a.cost_usd)} <span>·</span>{" "}
                                    {fmt(a.co2_kg)} кг CO₂e
                                  </div>
                                </div>
                                <button
                                  disabled={
                                    busy ||
                                    !a.feasible ||
                                    d.selected_route_id === a.id
                                  }
                                  className="secondary"
                                  onClick={() =>
                                    mutate("/route", post({ route_id: a.id }))
                                  }
                                >
                                  {!a.feasible
                                    ? "Недоступен"
                                    : d.selected_route_id === a.id
                                      ? "Выбран"
                                      : "Выбрать"}
                                </button>
                              </div>
                            ))
                          ) : (
                            <div className="empty-small">
                              Доступных альтернатив нет
                            </div>
                          )}
                          <p className="micro">
                            Выбор моделируется локально и не является
                            бронированием.
                          </p>
                        </section>
                        <section className="card">
                          <div className="card-heading">
                            <div>
                              <h3>Стоимость и экология</h3>
                              <p>Только оставшаяся часть маршрута</p>
                            </div>
                            <Leaf size={19} className="teal" />
                          </div>
                          <div className="impact-total">
                            <div>
                              <small>СТОИМОСТЬ</small>
                              <strong>
                                {r.legs.length ? "$" + fmt(r.cost_usd) : "—"}
                              </strong>
                            </div>
                            <div>
                              <small>ВЫБРОСЫ CO₂e</small>
                              <strong>
                                {r.legs.length ? fmt(r.co2_kg) + " кг" : "—"}
                              </strong>
                            </div>
                          </div>
                          {r.legs.map((l, i) => (
                            <div className="leg-row" key={i}>
                              <span>
                                {lookup(modes, l.mode) ?? l.mode} · {name(l.origin_id)}{" "}
                                → {name(l.destination_id)}
                              </span>
                              <strong>{fmt(l.co2_kg)} кг</strong>
                            </div>
                          ))}
                          <p className="micro">
                            Иллюстрационная оценка только транспорта, без
                            терминалов, хранения и порожних рейсов; не
                            сертифицирована ISO/GLEC. Коэффициенты:{" "}
                            {r.emissions.factor_version}.
                          </p>
                        </section>
                      </div>
                      <div className="source-note">
                        <CircleHelp size={15} /> Погода: {d.weather.source}
                        {d.weather.stale ? " (устарела)" : ""}, базовый ветер{" "}
                        {fmt(d.weather.wind_mps, 1)} м/с
                        {d.scenario.wind_mps != null &&
                          ` · ветер сценария: ${fmt(d.scenario.wind_mps, 1)} м/с`}{" "}
                        · прогноз: {d.prediction.data_source},{" "}
                        {d.prediction.model_version}. Расписание и показатели
                        демонстрационные.
                      </div>
                    </>
                  )}
                </>
              )}
              {page === "documents" && d && form && displayedForm && (
                <>
                  <div className="page-heading">
                    <div>
                      <div className="eyebrow">
                        ДОКУМЕНТЫ <span>/</span> {d.shipment.id}
                      </div>
                      <h1>
                        Предварительная декларация
                        <span className="heading-dot">.</span>
                      </h1>
                      <p>
                        Подготовка, проверка и демонстрационное рассмотрение
                        документов
                      </p>
                    </div>
                    <select
                      value={selected}
                      disabled={busy}
                      onChange={(e) => switchShipment(e.target.value)}
                    >
                      {dashboard.shipments.map((s) => (
                        <option key={s.shipment.id}>{s.shipment.id}</option>
                      ))}
                    </select>
                  </div>
                  <div className="notice">
                    <ShieldCheck size={20} />
                    <div>
                      <strong>
                        Демонстрационный процесс, не таможенное оформление
                      </strong>
                      <p>
                        Статус «Принята в демо» не означает официального решения
                        таможни. Изменение документов может повлиять на
                        готовность груза и маршрут.
                      </p>
                    </div>
                  </div>
                  <div className="document-grid">
                    <section className="card form-card">
                      <div className="card-heading">
                        <div>
                          <h3>Данные декларации</h3>
                          <p>
                            {d.preclearance
                              ? `Версия ${d.preclearance.version} · ${lookup(statuses, d.preclearance.status) ?? d.preclearance.status}`
                              : "Новая декларация"}
                          </p>
                        </div>
                        <span className="outline-tag">
                          {d.shipment.container_id}
                        </span>
                      </div>
                      <div className="form-grid">
                        {(
                          [
                            ["shipper", "Отправитель", 200],
                            ["consignee", "Получатель", 200],
                            ["description", "Описание груза", 2000],
                            ["container_id", "Номер контейнера", 32],
                            ["mass_kg", "Масса, кг", AMOUNT_MAX.mass_kg],
                            ["value_usd", "Стоимость, USD", AMOUNT_MAX.value_usd],
                            [
                              "origin_country",
                              "Страна происхождения (2 буквы)",
                              8,
                            ],
                            ["hs_code", "Код ТН ВЭД (6–10 цифр)", 32],
                          ] as const
                        ).map(([key, label, limit]) => (
                          <label key={key}>
                            {label}
                            <input
                              disabled={busy || submitted}
                              type={
                                key === "mass_kg" || key === "value_usd"
                                  ? "number"
                                  : "text"
                              }
                              min={
                                key === "mass_kg" || key === "value_usd"
                                  ? 0
                                  : undefined
                              }
                              max={
                                key === "mass_kg" || key === "value_usd"
                                  ? limit
                                  : undefined
                              }
                              maxLength={
                                key === "mass_kg" || key === "value_usd"
                                  ? undefined
                                  : limit
                              }
                              value={displayedForm[key]}
                              onChange={(e) =>
                                setForm({
                                  ...form,
                                  [key]:
                                    key === "mass_kg" || key === "value_usd"
                                      ? toAmount(e.target.value, limit)
                                      : e.target.value,
                                })
                              }
                            />
                          </label>
                        ))}
                      </div>
                      <h4>Сопроводительные документы</h4>
                      {displayedForm.documents.map((doc, i) => (
                        <div className="doc-block" key={doc.kind}>
                          <strong>{lookup(docs, doc.kind) ?? doc.kind}</strong>
                          <div className="form-grid three">
                            {(
                              [
                                ["number", "Номер", 64],
                                ["container_id", "Контейнер", 32],
                                ["mass_kg", "Масса, кг", AMOUNT_MAX.mass_kg],
                              ] as const
                            ).map(([key, label, limit]) => (
                              <label key={key}>
                                {label}
                                <input
                                  disabled={busy || submitted}
                                  type={key === "mass_kg" ? "number" : "text"}
                                  min={key === "mass_kg" ? 0 : undefined}
                                  max={key === "mass_kg" ? limit : undefined}
                                  maxLength={key === "mass_kg" ? undefined : limit}
                                  value={doc[key]}
                                  onChange={(e) => {
                                    const documents = [...form.documents];
                                    documents[i] = {
                                      ...doc,
                                      [key]:
                                        key === "mass_kg"
                                          ? toAmount(e.target.value, limit)
                                          : e.target.value,
                                    };
                                    setForm({ ...form, documents });
                                  }}
                                />
                              </label>
                            ))}
                          </div>
                        </div>
                      ))}
                      <div className="form-actions">
                        <button
                          disabled={busy || d.preclearance?.status === "submitted"}
                          className="primary"
                          onClick={() =>
                            mutate(
                              "/preclearance",
                              { method: "PUT", body: JSON.stringify(form) },
                              "broker",
                              true,
                            )
                          }
                        >
                          Сохранить черновик
                        </button>
                        <button
                          disabled={busy || !d.preclearance || d.preclearance.status === "submitted" || d.preclearance.status === "needs_revision" ||
                            JSON.stringify(form) !== JSON.stringify(d.preclearance.payload)}
                          className="secondary"
                          onClick={() =>
                            mutate(
                              "/preclearance/validate",
                              post(),
                              "broker",
                              true,
                            )
                          }
                        >
                          Проверить
                        </button>
                        <button
                          disabled={busy || d.preclearance?.status !== "ready" ||
                            JSON.stringify(form) !== JSON.stringify(d.preclearance.payload)}
                          className="secondary"
                          onClick={() => {
                            // One random key per logical submit (shipment +
                            // declaration version + content); a retry of the
                            // same declaration reuses it.
                            const scope = `${selected}|${d.preclearance?.version}|${d.preclearance?.content_hash}`;
                            let entry = submitKey.current;
                            if (!entry || entry.scope !== scope) {
                              entry = { scope, key: newIdempotencyKey() };
                              submitKey.current = entry;
                            }
                            mutate(
                              "/preclearance/submit",
                              {
                                ...post(),
                                headers: {
                                  "Idempotency-Key": entry.key,
                                },
                              },
                              "broker",
                              true,
                            );
                          }}
                        >
                          Отправить
                        </button>
                      </div>
                      <p className="micro">
                        Перед проверкой сохраните изменения. Отправка доступна
                        после успешной проверки.
                      </p>
                      {d.preclearance?.status === "needs_revision" && (
                        <p className="micro">
                          Сохраните новую версию черновика, чтобы продолжить проверку.
                        </p>
                      )}
                      {!!d.preclearance?.validation.issues.length && (
                        <div className="issues">
                          <strong>Замечания проверки</strong>
                          {d.preclearance.validation.issues.map((issue, i) => (
                            <p key={i}>
                              {issue.field}: {issue.message} ({issue.code})
                            </p>
                          ))}
                        </div>
                      )}
                    </section>
                    <div>
                      <section className="card review-card">
                        <div className="card-heading">
                          <div>
                            <h3>Рассмотрение</h3>
                            <p>Действие демонстрационного проверяющего</p>
                          </div>
                          <ClipboardCheck size={19} className="teal" />
                        </div>
                        <label>
                          Комментарий
                          <textarea
                            value={comment}
                            maxLength={1000}
                            onChange={(e) => setComment(e.target.value)}
                            placeholder="Комментарий к решению"
                          />
                        </label>
                        <div className="form-actions">
                          <button
                            className="primary"
                            disabled={
                              busy || !submitted || dirty
                            }
                            onClick={() => review("demo_accepted")}
                          >
                            Принять в демо
                          </button>
                          <button
                            className="secondary"
                            disabled={
                              busy || !submitted || dirty
                            }
                            onClick={() => review("needs_revision")}
                          >
                            На доработку
                          </button>
                        </div>
                      </section>
                      <section className="card audit-card">
                        <div className="card-heading">
                          <div>
                            <h3>Журнал действий</h3>
                            <p>История изменений декларации</p>
                          </div>
                          <button className="text-button" onClick={verify}>
                            Проверить цепочку
                          </button>
                        </div>
                        {verification && (
                          <div
                            className={
                              verification.valid ? "verify-ok" : "warning"
                            }
                          >
                            {verification.valid ? (
                              <Check size={16} />
                            ) : (
                              <TriangleAlert size={16} />
                            )}{" "}
                            {verification.valid
                              ? verification.entries_checked === 0
                                ? "Нет записей для проверки"
                                : "Целостность подтверждена"
                              : "Нарушена целостность"}{" "}
                            · записей: {verification.entries_checked}
                            {verification.first_invalid_sequence != null
                              ? ` · ошибка №${verification.first_invalid_sequence}`
                              : ""}
                          </div>
                        )}
                        <p className="micro">
                          Проверка хеш-цепочки не подтверждает подлинность или
                          решение таможни.
                        </p>
                        {audit.length ? (
                          audit.map((a) => (
                            <div className="audit-entry" key={a.sequence}>
                              <span className="event-dot" />
                              <div>
                                <strong>
                                  #{a.sequence} · {a.action}
                                </strong>
                                <small>
                                  {a.actor} · {date(a.at)}
                                </small>
                                <code>{a.hash}</code>
                              </div>
                            </div>
                          ))
                        ) : (
                          <div className="empty-small">Действий пока нет</div>
                        )}
                      </section>
                    </div>
                  </div>
                </>
              )}
              {page === "model" && (
                <>
                  <div className="page-heading">
                    <div>
                      <div className="eyebrow">
                        АНАЛИТИКА <span>/</span> МОДЕЛЬ
                      </div>
                      <h1>
                        Качество прогноза<span className="heading-dot">.</span>
                      </h1>
                      <p>Оценка на отложенной части синтетической истории</p>
                    </div>
                  </div>
                  {metrics ? (
                    <>
                      <div className="notice">
                        <Activity size={20} />
                        <div>
                          <strong>Демонстрационная модель</strong>
                          <p>
                            Хронологическое разделение данных без перемешивания.
                            Результаты не проверены на реальных перевозках
                            Каспийского региона.
                          </p>
                        </div>
                      </div>
                      <div className="kpi-grid">
                        <Kpi
                          icon={<BarChart3 />}
                          label="MAE МОДЕЛИ"
                          value={fmt(metrics.model_mae_hours, 2)}
                          unit="ч"
                          sub="Ошибка прогноза задержки"
                        />
                        <Kpi
                          icon={<ArrowDownRight />}
                          label="MAE БАЗОВОЙ ОЦЕНКИ"
                          value={fmt(metrics.baseline_mae_hours, 2)}
                          unit="ч"
                          sub="Медиана обучающей выборки"
                        />
                        <Kpi
                          icon={<Activity />}
                          label="ROC AUC"
                          value={fmt(metrics.roc_auc, 3)}
                          sub="Классификация пропуска"
                        />
                        <Kpi
                          icon={<Layers3 />}
                          label="ВЕРСИЯ МОДЕЛИ"
                          value={metrics.model_version}
                          sub="Синтетическая история"
                          small
                        />
                      </div>
                      <section className="card model-card">
                        <h3>Выборки и методология</h3>
                        <div className="metric-row">
                          <span>Обучающая выборка</span>
                          <strong>{fmt(metrics.train_rows)} записей</strong>
                        </div>
                        <div className="metric-row">
                          <span>Тестовая выборка</span>
                          <strong>{fmt(metrics.test_rows)} записей</strong>
                        </div>
                        <div className="metric-row">
                          <span>Граница разделения</span>
                          <strong>{date(metrics.split_at)}</strong>
                        </div>
                        <p className="micro">
                          {metrics.disclaimer} ROC AUC может быть недоступен,
                          если в тестовой выборке только один класс.
                        </p>
                      </section>
                    </>
                  ) : (
                    <div className="empty">Загрузка метрик…</div>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </main>
      {settings && (
        <div className="modal-backdrop" onClick={closeSettings}>
          <div className="modal card" onClick={(e) => e.stopPropagation()}>
            <div className="card-heading">
              <div>
                <h3>Настройки доступа</h3>
                <p>Токены демонстрационных ролей</p>
              </div>
              <button className="icon" onClick={closeSettings}>
                <X size={19} />
              </button>
            </div>
            <label>
              Токен брокера
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                maxLength={512}
                value={brokerDraft}
                onChange={(e) => setBrokerDraft(e.target.value)}
              />
            </label>
            <p className="micro">
              Брокер изменяет сценарии, выбирает маршруты и отправляет
              декларации. Без сохранённого токена используется встроенный
              демо-токен (только для локальных клиентов).
            </p>
            <label>
              Токен проверяющего
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                maxLength={512}
                value={reviewerDraft}
                onChange={(e) => setReviewerDraft(e.target.value)}
              />
            </label>
            <p className="micro">
              Проверяющий принимает демо-решение или возвращает на доработку.
              Токены хранятся только в этой вкладке (sessionStorage) и
              удаляются при её закрытии. Пустое поле — токен не отправляется.
            </p>
            <button
              className="primary"
              onClick={() => {
                const nextBroker = brokerDraft.trim();
                const nextReviewer = reviewerDraft.trim();
                storeToken(TOKEN_KEYS.broker, nextBroker);
                storeToken(TOKEN_KEYS.reviewer, nextReviewer);
                setBroker(nextBroker);
                setReviewer(nextReviewer);
                closeSettings();
              }}
            >
              Сохранить
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
function Kpi({
  icon,
  label,
  value,
  unit,
  sub,
  accent,
  small,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  unit?: string;
  sub: string;
  accent?: string;
  small?: boolean;
}) {
  return (
    <div className="kpi card">
      <div className="kpi-top">
        <span>{label}</span>
        <div className={"kpi-icon " + (accent || "")}>{icon}</div>
      </div>
      <div className={"kpi-value " + (small ? "compact" : "")}>
        {value} {unit && <span>{unit}</span>}
      </div>
      <div className="kpi-sub">{sub}</div>
    </div>
  );
}
function RouteMap({
  ports,
  route,
  current,
}: {
  ports: Port[];
  route: Route;
  current: string;
}) {
  const valid = ports.filter(
    (p) => Number.isFinite(p.lon) && Number.isFinite(p.lat),
  );
  if (!valid.length)
    return <div className="map-empty">Координаты недоступны</div>;
  const minX = Math.min(...valid.map((p) => p.lon)) - 2,
    maxX = Math.max(...valid.map((p) => p.lon)) + 2,
    minY = Math.min(...valid.map((p) => p.lat)) - 2,
    maxY = Math.max(...valid.map((p) => p.lat)) + 2;
  const xy = (p: Port) => ({
    x: 55 + ((p.lon - minX) / (maxX - minX)) * 590,
    y: 35 + ((maxY - p.lat) / (maxY - minY)) * 250,
  });
  return (
    <div className="map-wrap">
      <svg
        viewBox="0 0 700 320"
        role="img"
        aria-label="Схема маршрута по координатам пунктов"
      >
        <defs>
          <pattern
            id="grid"
            width="35"
            height="35"
            patternUnits="userSpaceOnUse"
          >
            <path
              d="M 35 0 L 0 0 0 35"
              fill="none"
              stroke="#24404b"
              strokeWidth=".6"
            />
          </pattern>
          <linearGradient id="sea" x2="1" y2="1">
            <stop stopColor="#153e49" />
            <stop offset="1" stopColor="#0e303c" />
          </linearGradient>
        </defs>
        <rect width="700" height="320" fill="#102833" />
        <rect width="700" height="320" fill="url(#grid)" />
        <path
          d="M410 -10 C455 24 430 53 450 83 C469 113 454 141 443 166 C432 193 439 213 410 235 C387 252 380 280 354 330 L585 330 C562 284 573 256 559 225 C544 190 558 164 540 131 C525 104 535 72 513 45 C497 23 503 7 500 -10Z"
          fill="url(#sea)"
          opacity=".8"
        />
        <text
          x="469"
          y="165"
          fill="#54808a"
          fontSize="10"
          letterSpacing="3"
          transform="rotate(90 469 165)"
        >
          КАСПИЙСКОЕ МОРЕ
        </text>
        {route.legs.map((l, i) => {
          const a = valid.find((p) => p.id === l.origin_id),
            b = valid.find((p) => p.id === l.destination_id);
          if (!a || !b) return null;
          const p = xy(a),
            q = xy(b);
          return (
            <line
              key={i}
              x1={p.x}
              y1={p.y}
              x2={q.x}
              y2={q.y}
              stroke="#45d7c1"
              strokeWidth="2.5"
              strokeDasharray={l.mode === "sea" ? "6 5" : undefined}
            />
          );
        })}
        {valid.map((p) => {
          const pos = xy(p);
          return (
            <g key={p.id}>
              <circle
                cx={pos.x}
                cy={pos.y}
                r={p.id === current ? 10 : 7}
                fill="#39d3bd"
                opacity=".16"
              />
              <circle
                cx={pos.x}
                cy={pos.y}
                r="4"
                fill={p.id === current ? "#58ead1" : "#d7f5ed"}
                stroke="#37cbb5"
                strokeWidth="2"
              />
              <text
                x={pos.x + 10}
                y={pos.y - 9}
                fill="#d7e9e7"
                fontSize="11"
                fontWeight="600"
              >
                {portName(p)}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="map-badge">
        <Ship size={16} />{" "}
        {route.legs.length
          ? `${route.legs.length} плеча маршрута`
          : "Маршрут недоступен"}
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
