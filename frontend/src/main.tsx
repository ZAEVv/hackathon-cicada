import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownRight,
  BarChart3,
  Bell,
  Check,
  ClipboardCheck,
  Clock3,
  FileText,
  Layers3,
  Map,
  Moon,
  Settings2,
  ShieldCheck,
  Sun,
  TriangleAlert,
  X,
} from "lucide-react";
import "./style.css";
import { MinimalDashboard } from "./minimal/MinimalDashboard";
import { Overview } from "./overview/Overview";
import { useSpotlight } from "./motion";
import { applyTheme, loadTheme, switchTheme, type Theme } from "./theme";
import type {
  Audit,
  Dashboard,
  Declaration,
  Detail,
  Metrics,
  Payload,
  Port,
  Verification,
} from "./types";
import {
  AMOUNT_MAX,
  DEFAULT_TOKENS,
  TOKEN_KEYS,
  api,
  date,
  docs,
  fmt,
  initialPayload,
  loadToken,
  lookup,
  newIdempotencyKey,
  portName,
  portShortName,
  post,
  statuses,
  storeToken,
  toAmount,
} from "./lib";

const PAGES = [
  { id: "overview", label: "Обзор", title: "Обзор перевозок", Icon: Map },
  {
    id: "documents",
    label: "Документы",
    title: "Предварительная декларация",
    Icon: FileText,
  },
  { id: "model", label: "Модель", title: "Качество модели", Icon: BarChart3 },
  { id: "minimal", label: "Сводка", title: "Сводный дашборд", Icon: Layers3 },
] as const;
type Page = (typeof PAGES)[number]["id"];

// Brand mark: a route between two nodes.
function Logo({ size = 30 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M8 23c5 0 5.5-3.6 8-7s3-7 8-7"
        stroke="currentColor"
        strokeWidth="3.4"
        strokeLinecap="round"
      />
      <circle cx="8" cy="23" r="3.6" fill="currentColor" />
      <circle cx="24" cy="9" r="3.6" fill="currentColor" />
    </svg>
  );
}

function App() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null),
    [ports, setPorts] = useState<Port[]>([]),
    [selected, setSelected] = useState(""),
    [detail, setDetail] = useState<Detail | null>(null),
    [metrics, setMetrics] = useState<Metrics | null>(null);
  const [page, setPage] = useState<Page>("overview"),
    [broker, setBroker] = useState(() =>
      loadToken(TOKEN_KEYS.broker, DEFAULT_TOKENS.broker),
    ),
    [reviewer, setReviewer] = useState(() =>
      loadToken(TOKEN_KEYS.reviewer, DEFAULT_TOKENS.reviewer),
    ),
    [brokerDraft, setBrokerDraft] = useState(""),
    [reviewerDraft, setReviewerDraft] = useState(""),
    [settings, setSettings] = useState(false),
    [theme, setTheme] = useState<Theme>(loadTheme),
    [riskPing, setRiskPing] = useState(0);
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
  useSpotlight();
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
  const short = (id: string) => {
    const port = ports.find((p) => p.id === id);
    return port ? portShortName(port) : id;
  };
  const toggleTheme = (e: React.MouseEvent<HTMLButtonElement>) => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    const rect = e.currentTarget.getBoundingClientRect();
    switchTheme(
      next,
      { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
      () => setTheme(next),
    );
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
  useEffect(() => {
    if (!settings) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeSettings();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [settings]);
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
  const d = detail?.shipment.id === selected ? detail : null,
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
  const pageIndex = PAGES.findIndex((p) => p.id === page);
  const atRisk = dashboard?.summary.high_risk ?? 0;
  return (
    <div className="app">
      <svg
        className="goo-defs"
        width="0"
        height="0"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <filter id="goo" colorInterpolationFilters="sRGB">
            <feGaussianBlur in="SourceGraphic" stdDeviation="5" result="blur" />
            <feColorMatrix
              in="blur"
              mode="matrix"
              values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -8"
            />
          </filter>
        </defs>
      </svg>
      <aside className="rail">
        <div className="rail-logo" title="Smart Caspian Transit">
          <Logo />
        </div>
        <nav
          className="rail-nav"
          aria-label="Разделы"
          style={{ "--idx": pageIndex } as React.CSSProperties}
        >
          <span className="nav-goo" aria-hidden="true">
            <i />
            <i />
          </span>
          {PAGES.map(({ id, label, title, Icon }) => (
            <button
              type="button"
              key={id}
              className={"rail-btn" + (page === id ? " active" : "")}
              aria-current={page === id ? "page" : undefined}
              aria-label={title}
              title={title}
              onClick={() => setPage(id)}
            >
              <Icon size={20} />
              <span className="rail-text">{label}</span>
            </button>
          ))}
          <button
            type="button"
            className="rail-btn settings"
            aria-label="Настройки доступа"
            title="Настройки доступа"
            onClick={openSettings}
          >
            <Settings2 size={20} />
            <span className="rail-text">Доступ</span>
          </button>
        </nav>
      </aside>
      <header className="topbar">
        <div className="brand">
          <span className="brand-logo">
            <Logo size={28} />
          </span>
          <strong>SMART CASPIAN TRANSIT</strong>
          <span className="brand-page">{PAGES[pageIndex].title}</span>
        </div>
        <div className="top-right">
          <span
            className="live"
            title="Синтетические данные. Не для операционных решений."
          >
            <span className="pulse" />
            <span className="live-text">Демо-данные</span>
          </span>
          <span className="clock">
            <Clock3 size={15} /> {date(dashboard?.simulation_now)}
          </span>
          <button
            type="button"
            className="theme-toggle"
            role="switch"
            aria-checked={theme === "light"}
            aria-label="Светлая тема"
            title={theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему"}
            onClick={toggleTheme}
          >
            <span className="tt-goo" aria-hidden="true">
              <i />
              <i />
            </span>
            <Moon size={17} />
            <Sun size={17} />
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label={`Перевозки под риском: ${atRisk}. Показать их первыми`}
            title="Показать перевозки под риском"
            onClick={() => {
              setPage("overview");
              setRiskPing((n) => n + 1);
            }}
          >
            <Bell size={19} />
            {atRisk > 0 && <span className="badge">{fmt(atRisk)}</span>}
          </button>
          <button
            type="button"
            className="avatar"
            onClick={openSettings}
            aria-label="Настройки доступа"
            title="Настройки доступа"
          >
            SC
          </button>
        </div>
      </header>
      <main className={"stage" + (page === "overview" ? " fixed" : "")}>
        {error && (
          <div className="error" role="alert">
            <TriangleAlert size={18} />
            <span>{error}</span>
            <button
              type="button"
              aria-label="Закрыть сообщение"
              onClick={() => setError("")}
            >
              <X size={16} />
            </button>
          </div>
        )}
        {loading ? (
          <div className="skeleton-stack" aria-busy="true" aria-label="Загрузка данных">
            <div className="skeleton" style={{ height: 56 }} />
            <div className="skeleton map-skeleton" />
            <div className="skeleton" style={{ height: 140 }} />
          </div>
        ) : !dashboard ? (
          <div className="empty">
            <TriangleAlert />
            <h2>Нет соединения с сервером</h2>
            <p>Проверьте, что API запущен на localhost:8000.</p>
            <button
              type="button"
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
          <div className="page" key={page}>
            {page === "overview" && (
              <Overview
                dashboard={dashboard}
                ports={ports}
                selected={selected}
                detail={d}
                busy={busy}
                name={name}
                short={short}
                riskPing={riskPing}
                token={authToken}
                onSelect={switchShipment}
                onRefresh={() => refresh().catch((e) => setError(e.message))}
                onScenario={(kind) => mutate("/scenario", post({ kind }))}
                onScenarioReset={() => mutate("/scenario/reset", post())}
                onRoute={(routeId) =>
                  mutate("/route", post({ route_id: routeId }))
                }
                onOpenDocuments={() => setPage("documents")}
              />
            )}
            {page === "documents" &&
              (d && form && displayedForm ? (
                <div className="content">
                  <div className="page-heading reveal">
                    <div>
                      <div className="eyebrow">
                        Документы <span>/</span> {d.shipment.id}
                      </div>
                      <h1>Предварительная декларация</h1>
                      <p>
                        Подготовка, проверка и демонстрационное рассмотрение
                        документов
                      </p>
                    </div>
                    <label>
                      <span className="sr-only">Перевозка</span>
                      <select
                        value={selected}
                        disabled={busy}
                        onChange={(e) => switchShipment(e.target.value)}
                      >
                        {dashboard.shipments.map((s) => (
                          <option key={s.shipment.id}>{s.shipment.id}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div
                    className="notice reveal"
                    style={{ "--i": 1 } as React.CSSProperties}
                  >
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
                    <section
                      className="card form-card reveal"
                      style={{ "--i": 2 } as React.CSSProperties}
                    >
                      <div className="card-heading">
                        <div>
                          <h3>Данные декларации</h3>
                          <p>
                            {d.preclearance
                              ? `Версия ${d.preclearance.version} · ${lookup(statuses, d.preclearance.status) ?? d.preclearance.status}`
                              : "Новая декларация"}
                          </p>
                        </div>
                        <span className="outline-tag mono">
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
                          type="button"
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
                          type="button"
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
                          type="button"
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
                      <section
                        className="card review-card reveal"
                        style={{ "--i": 3 } as React.CSSProperties}
                      >
                        <div className="card-heading">
                          <div>
                            <h3>Рассмотрение</h3>
                            <p>Действие демонстрационного проверяющего</p>
                          </div>
                          <ClipboardCheck size={19} className="muted" />
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
                            type="button"
                            className="secondary"
                            disabled={
                              busy || !submitted || dirty
                            }
                            onClick={() => review("demo_accepted")}
                          >
                            Принять в демо
                          </button>
                          <button
                            type="button"
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
                      <section
                        className="card audit-card reveal"
                        style={{ "--i": 4 } as React.CSSProperties}
                      >
                        <div className="card-heading">
                          <div>
                            <h3>Журнал действий</h3>
                            <p>История изменений декларации</p>
                          </div>
                          <button
                            type="button"
                            className="text-button"
                            onClick={verify}
                          >
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
                            )}
                            <span>
                              {verification.valid
                                ? verification.entries_checked === 0
                                  ? "Нет записей для проверки"
                                  : "Целостность подтверждена"
                                : "Нарушена целостность"}{" "}
                              · записей: {verification.entries_checked}
                              {verification.first_invalid_sequence != null
                                ? ` · ошибка №${verification.first_invalid_sequence}`
                                : ""}
                            </span>
                          </div>
                        )}
                        <p className="micro">
                          Проверка хеш-цепочки не подтверждает подлинность или
                          решение таможни.
                        </p>
                        {audit.length ? (
                          audit.map((a) => (
                            <div className="audit-entry" key={a.sequence}>
                              <span className="event-dot done" />
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
                </div>
              ) : (
                <div className="content skeleton-stack" aria-busy="true">
                  <div className="skeleton" style={{ height: 72 }} />
                  <div className="skeleton" style={{ height: 420 }} />
                </div>
              ))}
            {page === "minimal" && (
              <div className="content">
                <MinimalDashboard token={authToken} />
              </div>
            )}
            {page === "model" && (
              <div className="content">
                <div className="page-heading reveal">
                  <div>
                    <div className="eyebrow">
                      Аналитика <span>/</span> Модель
                    </div>
                    <h1>Качество прогноза</h1>
                    <p>Оценка на отложенной части синтетической истории</p>
                  </div>
                </div>
                {metrics ? (
                  <>
                    <div
                      className="notice reveal"
                      style={{ "--i": 1 } as React.CSSProperties}
                    >
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
                        index={2}
                        icon={<BarChart3 />}
                        label="MAE модели"
                        value={fmt(metrics.model_mae_hours, 2)}
                        unit="ч"
                        sub="Ошибка прогноза задержки"
                      />
                      <Kpi
                        index={3}
                        icon={<ArrowDownRight />}
                        label="MAE базовой оценки"
                        value={fmt(metrics.baseline_mae_hours, 2)}
                        unit="ч"
                        sub="Медиана обучающей выборки"
                      />
                      <Kpi
                        index={4}
                        icon={<Activity />}
                        label="ROC AUC"
                        value={fmt(metrics.roc_auc, 3)}
                        sub="Классификация пропуска"
                      />
                      <Kpi
                        index={5}
                        icon={<Layers3 />}
                        label="Версия модели"
                        value={metrics.model_version}
                        sub="Синтетическая история"
                        small
                      />
                    </div>
                    <section
                      className="card glow model-card reveal"
                      style={{ "--i": 6 } as React.CSSProperties}
                    >
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
                  <div className="skeleton-stack" aria-busy="true">
                    <div className="skeleton" style={{ height: 72 }} />
                    <div className="skeleton" style={{ height: 130 }} />
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </main>
      {settings && (
        <div className="modal-backdrop" onClick={closeSettings}>
          <div
            className="modal card"
            role="dialog"
            aria-modal="true"
            aria-label="Настройки доступа"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="card-heading">
              <div>
                <h3>Настройки доступа</h3>
                <p>Токены демонстрационных ролей</p>
              </div>
              <button
                type="button"
                className="icon"
                aria-label="Закрыть"
                onClick={closeSettings}
              >
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
              type="button"
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
  index = 0,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  unit?: string;
  sub: string;
  accent?: string;
  small?: boolean;
  index?: number;
}) {
  return (
    <div
      className="kpi card glow reveal"
      style={{ "--i": index } as React.CSSProperties}
    >
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

applyTheme(loadTheme());
createRoot(document.getElementById("root")!).render(<App />);
