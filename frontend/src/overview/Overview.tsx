import type React from "react";
import { useEffect, useRef, useState } from "react";
import {
  Activity,
  Anchor,
  ArrowRight,
  ChevronDown,
  CircleHelp,
  Clock3,
  CloudSun,
  Container,
  Gauge,
  Leaf,
  Map as MapIcon,
  RefreshCw,
  RotateCcw,
  Route as RouteIcon,
  Search,
  Ship,
  Split,
  Tag,
  TriangleAlert,
} from "lucide-react";
import type { Dashboard, Detail, Port } from "../types";
import {
  cargoNames,
  date,
  fmt,
  hours,
  lookup,
  modes,
  riskClass,
  riskLabel,
  scenarios,
  scheduleStatus,
  shipmentTone,
  translate,
} from "../lib";
import { CountUp, Ring } from "../motion";
import { reducedMotion } from "../theme";
import { LegGantt, modeIcon } from "./LegGantt";
import { RouteMap } from "./RouteMap";
import { HubPanel, HubTimeBar, loadAt, useHubForecast } from "./HubPanel";

const SCENARIOS = [
  { kind: "none", Icon: Activity, tone: "green" },
  { kind: "storm", Icon: CloudSun, tone: "cyan" },
  { kind: "congestion", Icon: Clock3, tone: "amber" },
  { kind: "closure", Icon: Anchor, tone: "violet" },
] as const;

type Sort = "id" | "risk" | "eta";

// Stagger index for the entrance animation.
const order = (n: number) => ({ "--i": n }) as React.CSSProperties;

// Last bell press already acted on; survives remounts of the page so that
// coming back to the overview does not re-apply an old request.
let handledPing = 0;

export function Overview({
  dashboard,
  ports,
  selected,
  detail,
  busy,
  name,
  short,
  riskPing,
  token,
  onSelect,
  onRefresh,
  onScenario,
  onScenarioReset,
  onRoute,
  onOpenDocuments,
}: {
  dashboard: Dashboard;
  ports: Port[];
  selected: string;
  detail: Detail | null;
  busy: boolean;
  name: (id: string) => string;
  short: (id: string) => string;
  riskPing: number;
  token: string;
  onSelect: (id: string) => void;
  onRefresh: () => void;
  onScenario: (kind: string) => void;
  onScenarioReset: () => void;
  onRoute: (routeId: string) => void;
  onOpenDocuments: () => void;
}) {
  const [tab, setTab] = useState<"list" | "summary">("list");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("id");
  const [showAlts, setShowAlts] = useState(true);
  const [showLabels, setShowLabels] = useState(true);
  const [showLoad, setShowLoad] = useState(true);
  const [hubHour, setHubHour] = useState(0);
  const hub = useHubForecast(token, selected, dashboard);
  const listRef = useRef<HTMLDivElement>(null);

  // The bell in the top bar asks for the risky shipments first.
  useEffect(() => {
    if (riskPing === handledPing) return;
    handledPing = riskPing;
    setTab("list");
    setSort("risk");
    setQuery("");
    listRef.current?.scrollIntoView({
      behavior: reducedMotion() ? "auto" : "smooth",
      block: "nearest",
    });
  }, [riskPing]);

  const d = detail;
  const r = d?.current_route;
  const { summary, shipments } = dashboard;

  // Same rule the server uses for summary.high_risk.
  const isHigh = (s: Detail) =>
    s.prediction.risk === "high" || !s.current_route.feasible;
  const high = shipments.filter(isHigh).length;
  const medium = shipments.filter(
    (s) => !isHigh(s) && s.prediction.risk === "medium",
  ).length;
  const low = shipments.length - high - medium;
  const share = shipments.length
    ? Math.round((high / shipments.length) * 100)
    : 0;

  const q = query.trim().toLowerCase();
  const rank = (s: Detail) =>
    ({ high: 2, medium: 1, low: 0 })[shipmentTone(s)];
  const items = shipments
    .filter(
      (s) =>
        !q ||
        [
          s.shipment.id,
          s.shipment.container_id,
          s.shipment.reference,
          name(s.shipment.origin_id),
          name(s.shipment.destination_id),
        ].some((v) => v.toLowerCase().includes(q)),
    )
    .sort((a, b) => {
      if (sort === "risk")
        return (
          rank(b) - rank(a) ||
          b.prediction.miss_probability - a.prediction.miss_probability
        );
      if (sort === "eta") {
        const ea = a.current_route.eta
          ? new Date(a.current_route.eta).getTime()
          : Infinity;
        const eb = b.current_route.eta
          ? new Date(b.current_route.eta).getTime()
          : Infinity;
        return ea - eb;
      }
      return a.shipment.id.localeCompare(b.shipment.id);
    });

  return (
    <div className="ov">
      <aside className="ov-left" aria-label="Обзор и список перевозок">
        <div className="panel-head reveal" style={order(0)}>
          <h1>Обзор</h1>
          <button
            type="button"
            className="icon-btn"
            onClick={onRefresh}
            aria-label="Обновить данные"
            title="Обновить данные"
          >
            <RefreshCw size={18} />
          </button>
        </div>

        <section className="block reveal" style={order(1)}>
          <div className="label-row">
            <h2 className="label">Сценарий</h2>
            <button
              type="button"
              className="text-button"
              disabled={busy || !d}
              onClick={onScenarioReset}
            >
              <RotateCcw size={14} /> Сбросить
            </button>
          </div>
          <div className="qa-row">
            {SCENARIOS.map(({ kind, Icon, tone }) => {
              const active = d?.scenario.kind === kind;
              return (
                <button
                  type="button"
                  key={kind}
                  className={"qa tone-" + tone + (active ? " active" : "")}
                  disabled={busy || !d}
                  aria-pressed={active}
                  onClick={() => onScenario(kind)}
                >
                  <span className="qa-bubble">
                    <Icon size={20} />
                  </span>
                  <span className="qa-text">{scenarios[kind]}</span>
                </button>
              );
            })}
          </div>
          <p className="micro">
            Сценарии заменяют друг друга. Шторм моделируется локально у
            текущего порта перевозки (Махачкала или Актау).
          </p>
        </section>

        <section className="block reveal" style={order(2)}>
          <div className="label-row">
            <h2 className="label">Перевозки под риском</h2>
            <span
              className="hint"
              title="Высокий риск модели или пропущенная стыковка"
            >
              <CircleHelp size={15} />
              <span className="sr-only">
                Высокий риск модели или пропущенная стыковка
              </span>
            </span>
          </div>
          <div className="risk-figure">
            <strong>
              <CountUp value={summary.high_risk} />
            </strong>
            <span>из {fmt(summary.total)}</span>
            <span className={"delta " + (high ? "bad" : "good")}>
              {share}%
            </span>
          </div>
          <div
            className="dist"
            role="img"
            aria-label={`Под риском: ${high}, средний риск: ${medium}, в норме: ${low}`}
          >
            {high > 0 && <i className="high" style={{ flexGrow: high }} />}
            {medium > 0 && (
              <i className="medium" style={{ flexGrow: medium }} />
            )}
            {low > 0 && <i className="low" style={{ flexGrow: low }} />}
          </div>
          <ul className="dist-legend">
            <li>
              <i className="high" />
              Под риском <b>{high}</b>
            </li>
            <li>
              <i className="medium" />
              Средний <b>{medium}</b>
            </li>
            <li>
              <i className="low" />В норме <b>{low}</b>
            </li>
          </ul>
        </section>

        <div
          className="seg reveal"
          role="tablist"
          aria-label="Раздел обзора"
          style={
            { "--i": 3, "--idx": tab === "list" ? 0 : 1 } as React.CSSProperties
          }
          onKeyDown={(e) => {
            const order = ["list", "summary"] as const;
            const cur = order.indexOf(tab);
            let next = -1;
            if (e.key === "ArrowRight") next = (cur + 1) % order.length;
            else if (e.key === "ArrowLeft")
              next = (cur - 1 + order.length) % order.length;
            else if (e.key === "Home") next = 0;
            else if (e.key === "End") next = order.length - 1;
            if (next < 0) return;
            e.preventDefault();
            setTab(order[next]);
            e.currentTarget
              .querySelectorAll<HTMLButtonElement>('[role="tab"]')
              [next]?.focus();
          }}
        >
          <span className="seg-goo" aria-hidden="true">
            <i />
          </span>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "list"}
            tabIndex={tab === "list" ? 0 : -1}
            onClick={() => setTab("list")}
          >
            Перевозки <span className="count">{shipments.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "summary"}
            tabIndex={tab === "summary" ? 0 : -1}
            onClick={() => setTab("summary")}
          >
            Сводка
          </button>
        </div>

        {tab === "list" ? (
          <div className="tab-pane" key="list">
            <label className="field">
              <Search size={16} aria-hidden="true" />
              <span className="sr-only">Поиск перевозки</span>
              <input
                type="search"
                placeholder="Номер, контейнер или пункт"
                value={query}
                maxLength={64}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <label className="field select-field">
              <span className="sr-only">Сортировка</span>
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as Sort)}
              >
                <option value="id">По номеру перевозки</option>
                <option value="risk">Сначала под риском</option>
                <option value="eta">По прогнозу прибытия</option>
              </select>
              <ChevronDown size={16} aria-hidden="true" />
            </label>
            <div className="msn-list" ref={listRef}>
              {items.map((item, idx) => {
                const id = item.shipment.id;
                const tone = shipmentTone(item);
                const status = scheduleStatus(item.current_route);
                const events = item.shipment.events;
                const done = events.filter((e) => e.actual_at).length;
                const progress = events.length ? done / events.length : 0;
                const ModeIcon = modeIcon(
                  item.current_route.legs[0]?.mode ?? "sea",
                );
                return (
                  <button
                    type="button"
                    key={id}
                    className={
                      "msn glow reveal" + (selected === id ? " selected" : "")
                    }
                    style={order(idx)}
                    disabled={busy}
                    aria-pressed={selected === id}
                    onClick={() => onSelect(id)}
                  >
                    <span className="msn-top">
                      <span className="msn-ico">
                        <Container size={18} />
                      </span>
                      <strong className="mono">{id}</strong>
                      <span className={"pill " + tone}>
                        <i />
                        {status ||
                          `Риск: ${riskLabel(item.prediction.risk)}`}
                      </span>
                    </span>
                    <span className="msn-route">
                      <span>{short(item.shipment.origin_id)}</span>
                      <span className="msn-line" aria-hidden="true">
                        <ModeIcon size={16} />
                      </span>
                      <span>{short(item.shipment.destination_id)}</span>
                    </span>
                    <span className="msn-foot">
                      <Ring value={progress} size={40} stroke={4} tone={tone} />
                      <span className="msn-meta">
                        <small>{item.shipment.container_id}</small>
                        <span>
                          Этапов пройдено: {done} из {events.length}
                        </span>
                      </span>
                      <strong>{Math.round(progress * 100)}%</strong>
                    </span>
                    <span className="msn-eta">
                      <Clock3 size={14} /> Прибытие:{" "}
                      {date(item.current_route.eta)}
                    </span>
                  </button>
                );
              })}
              {!items.length && (
                <div className="empty-small">
                  По запросу «{query}» перевозок нет.{" "}
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setQuery("")}
                  >
                    Сбросить поиск
                  </button>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="tab-pane tiles" key="summary">
            <div className="tile glow">
              <span>Всего перевозок</span>
              <strong>
                <CountUp value={summary.total} />
              </strong>
              <small>В демонстрационном контуре</small>
            </div>
            <div className="tile glow">
              <span>Высокий риск</span>
              <strong className={high ? "bad" : ""}>
                <CountUp value={summary.high_risk} />
              </strong>
              <small>Риск модели или пропущенная стыковка</small>
            </div>
            <div className="tile glow">
              <span>Выбросы CO₂e</span>
              <strong>
                <CountUp value={summary.co2_kg} /> <em>кг</em>
              </strong>
              <small>Оставшаяся часть пути</small>
            </div>
            <div className="tile glow">
              <span>Источник данных</span>
              <strong>{dashboard.synthetic ? "Демо" : "API"}</strong>
              <small>Фиксированное время симуляции</small>
            </div>
          </div>
        )}
        <p className="demo-note">
          <span className="pulse" /> Синтетические данные. Не для операционных
          решений.
        </p>
      </aside>

      <section className="ov-center" aria-label="Схема маршрута">
        <div className="tools reveal" style={order(1)}>
          <div className="tool-group">
            <span className="tool-label">Вид</span>
            <span className="tool on" title="Геосхема">
              <MapIcon size={16} aria-hidden="true" />
              <span className="tool-name">Геосхема</span>
            </span>
          </div>
          <div className="tool-group">
            <span className="tool-label">Слои</span>
            <div className="tool-items">
              <button
                type="button"
                className={"tool" + (showAlts ? " on" : "")}
                aria-pressed={showAlts}
                title="Альтернативы"
                onClick={() => setShowAlts((v) => !v)}
              >
                <Split size={16} aria-hidden="true" />
                <span className="tool-name">Альтернативы</span>
              </button>
              <button
                type="button"
                className={"tool" + (showLabels ? " on" : "")}
                aria-pressed={showLabels}
                title="Подписи"
                onClick={() => setShowLabels((v) => !v)}
              >
                <Tag size={16} aria-hidden="true" />
                <span className="tool-name">Подписи</span>
              </button>
              <button
                type="button"
                className={"tool" + (showLoad ? " on" : "")}
                aria-pressed={showLoad}
                title="Загрузка узлов"
                onClick={() => setShowLoad((v) => !v)}
              >
                <Gauge size={16} aria-hidden="true" />
                <span className="tool-name">Загрузка узлов</span>
              </button>
            </div>
          </div>
          <div className="tool-group">
            <span className="tool-label">Маршрут</span>
            <span className="tool-text">
              {d
                ? `${d.shipment.id} · ${d.selected_route_id ? "выбранная альтернатива" : "текущий маршрут"}`
                : "Загрузка…"}
            </span>
          </div>
        </div>

        {d && r ? (
          <>
            {showLoad && hub.forecast && (
              <HubTimeBar
                forecast={hub.forecast}
                hour={hubHour}
                onHour={setHubHour}
              />
            )}
            <div className="map-stage reveal" style={order(2)}>
              <RouteMap
                ports={ports}
                route={r}
                alternatives={d.alternatives}
                current={d.shipment.current_location_id}
                showAlternatives={showAlts}
                showLabels={showLabels}
                load={showLoad ? loadAt(hub.forecast, hubHour) : null}
                loadLevels={hub.forecast}
              />
              <div className="map-legend">
                <span>
                  <i className="lg-line" /> Маршрут
                </span>
                {showAlts && d.alternatives.length > 0 && (
                  <span>
                    <i className="lg-line alt" /> Альтернативы
                  </span>
                )}
                <span>
                  <i className="lg-dot" /> Груз сейчас
                </span>
                {showLoad && hub.forecast && (
                  <span className="lg-load">
                    Загрузка: <i className="hub-low" /> норма
                    <i className="hub-medium" /> высокая
                    <i className="hub-high" /> узкое место
                  </span>
                )}
              </div>
              <div className="map-badge">
                <Ship size={16} />{" "}
                {r.legs.length
                  ? `${r.legs.length} плеча маршрута`
                  : "Маршрут недоступен"}
              </div>
              <div className="map-note">
                Схема по координатам · не навигационная карта
              </div>
            </div>

            <div className="legs reveal" style={order(3)}>
              <div className="legs-head">
                <h2>
                  <Anchor size={17} /> Плечи маршрута
                </h2>
                <span className="muted">
                  Оставшийся путь · пунктир — ожидание, засечка — дедлайн
                  приёма
                </span>
              </div>
              <LegGantt
                key={d.shipment.id}
                route={r}
                now={d.simulation_now}
                short={short}
              />
            </div>

            <HubPanel
              forecast={hub.forecast}
              error={hub.error}
              hour={hubHour}
              name={name}
              short={short}
              onHour={(h) => {
                setShowLoad(true);
                setHubHour(h);
              }}
              onSelect={onSelect}
            />

            <section
              className="card timeline-card reveal"
              style={order(4)}
            >
              <div className="card-heading">
                <div>
                  <h3>Хронология перевозки</h3>
                  <p>План, прогноз и фактические события · UTC</p>
                </div>
                <Clock3 size={18} className="muted" />
              </div>
              <div className="timeline-head">
                <span>Событие</span>
                <span>План</span>
                <span>Прогноз</span>
                <span>Факт</span>
              </div>
              {d.shipment.events.length ? (
                d.shipment.events.map((e) => (
                  <div className="timeline-row" key={e.id}>
                    <div className="event-name">
                      <span
                        className={"event-dot" + (e.actual_at ? " done" : "")}
                      />
                      <div>
                        <strong>{translate(e.event_type)}</strong>
                        <small>
                          {name(e.location_id)} · {translate(e.source)} /{" "}
                          {translate(e.quality_flag)}
                        </small>
                      </div>
                    </div>
                    <span data-label="План">{date(e.planned_at)}</span>
                    <span data-label="Прогноз">{date(e.estimated_at)}</span>
                    <span data-label="Факт">{date(e.actual_at)}</span>
                  </div>
                ))
              ) : (
                <div className="empty-small">Событий пока нет</div>
              )}
            </section>
          </>
        ) : (
          <div className="skeleton-stack" aria-busy="true">
            <div className="skeleton map-skeleton" />
            <div className="skeleton" style={{ height: 150 }} />
          </div>
        )}
      </section>

      <aside className="ov-right" aria-label="Детали перевозки">
        {d && r ? (
          <div className="detail" key={d.shipment.id}>
            <div className="panel-head reveal" style={order(1)}>
              <h2>Детали</h2>
              <div className="detail-tags">
                <span className="chip mono">{d.shipment.container_id}</span>
                <span className="chip">
                  {fmt(d.shipment.mass_kg / 1000, 1)} т
                </span>
              </div>
            </div>
            <p className="lead reveal" style={order(2)}>
              Перевозка <b className="mono">{d.shipment.id}</b>: прогноз
              дополнительной задержки {hours(d.prediction.delay_hours)}.
              {scheduleStatus(r) ? ` ${scheduleStatus(r)}.` : ""}
            </p>

            <div className="stack">
              <section className="card glow reveal" style={order(3)}>
                <div className="card-heading">
                  <div>
                    <h3>Анализ риска задержки</h3>
                    <p>На основе текущего сценария</p>
                  </div>
                  <span className={"pill " + riskClass(d.prediction.risk)}>
                    <i /> Риск: {riskLabel(d.prediction.risk)}
                  </span>
                </div>
                <div className="risk-ring">
                  <Ring
                    value={d.prediction.miss_probability}
                    size={132}
                    stroke={10}
                    tone={riskClass(d.prediction.risk) || "low"}
                  >
                    <strong>
                      <CountUp
                        value={d.prediction.miss_probability * 100}
                        digits={1}
                      />
                      %
                    </strong>
                    <small>пропуск</small>
                  </Ring>
                  <ul className="factors">
                    {d.prediction.factors.map((x, i) => (
                      <li key={i}>
                        <i className={"f" + (i % 3)} />
                        {translate(x)}
                      </li>
                    ))}
                  </ul>
                </div>
                <button
                  type="button"
                  className="link-row"
                  onClick={onOpenDocuments}
                >
                  Открыть декларацию перевозки <ArrowRight size={15} />
                </button>
                <p className="micro">
                  Синтетическая оценка модели, не гарантирует калибровку;
                  фактическая доступность определяется расписанием.
                </p>
              </section>

              <section className="facts reveal" style={order(4)}>
                <h3>Перевозка</h3>
                <dl>
                  <div>
                    <dt>Номер</dt>
                    <dd className="mono accent">{d.shipment.id}</dd>
                  </div>
                  <div>
                    <dt>Груз</dt>
                    <dd>
                      {lookup(cargoNames, d.shipment.cargo) ??
                        d.shipment.cargo}
                    </dd>
                  </div>
                  <div>
                    <dt>Статус маршрута</dt>
                    <dd>
                      {scheduleStatus(r) || "По расписанию"}{" "}
                      <i className={"dot " + (scheduleStatus(r) ? "high" : "low")} />
                    </dd>
                  </div>
                  <div>
                    <dt>Сейчас находится</dt>
                    <dd>{name(d.shipment.current_location_id)}</dd>
                  </div>
                  <div>
                    <dt>План доставки</dt>
                    <dd>{date(d.shipment.planned_delivery_at)}</dd>
                  </div>
                  <div>
                    <dt>Прогноз прибытия</dt>
                    <dd>{date(r.eta)}</dd>
                  </div>
                </dl>
              </section>

              <section className="card glow reveal" style={order(5)}>
                <div className="card-heading">
                  <div>
                    <h3>Прогноз доставки</h3>
                    <p>Расчётное прибытие по текущему маршруту</p>
                  </div>
                  <Clock3 size={18} className="muted" />
                </div>
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
                  <span>Готовность к отправке</span>
                  <strong>{date(d.prediction.ready_at)}</strong>
                </div>
                <div className="metric-row">
                  <span>
                    Дедлайн текущего рейса
                    {!r.feasible && r.eta ? " (восстановление)" : ""}
                  </span>
                  <strong>{date(r.legs[0]?.cutoff_at)}</strong>
                </div>
                <div className="metric-row">
                  <span>Доп. прогноз задержки</span>
                  <strong>
                    <CountUp value={d.prediction.delay_hours} digits={1} /> ч
                  </strong>
                </div>
              </section>

              <section className="card glow reveal" style={order(6)}>
                <div className="card-heading">
                  <div>
                    <h3>Альтернативные маршруты</h3>
                    <p>Доступные варианты оставшегося пути</p>
                  </div>
                  <RouteIcon size={18} className="muted" />
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
                            .map((l) => short(l.origin_id))
                            .concat(
                              a.legs.length
                                ? [
                                    short(
                                      a.legs[a.legs.length - 1].destination_id,
                                    ),
                                  ]
                                : [],
                            )
                            .join(" → ")}
                        </small>
                        <div className="alt-stats">
                          {date(a.eta)} <span>·</span> ${fmt(a.cost_usd)}{" "}
                          <span>·</span> {fmt(a.co2_kg)} кг CO₂e
                        </div>
                      </div>
                      <button
                        type="button"
                        disabled={
                          busy || !a.feasible || d.selected_route_id === a.id
                        }
                        className="secondary"
                        onClick={() => onRoute(a.id)}
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
                  <div className="empty-small">Доступных альтернатив нет</div>
                )}
                <p className="micro">
                  Выбор моделируется локально и не является бронированием.
                </p>
              </section>

              <section className="card glow reveal" style={order(7)}>
                <div className="card-heading">
                  <div>
                    <h3>Стоимость и экология</h3>
                    <p>Только оставшаяся часть маршрута</p>
                  </div>
                  <Leaf size={18} className="tone-green-text" />
                </div>
                <div className="impact-total">
                  <div>
                    <small>Стоимость</small>
                    <strong>
                      {r.legs.length ? (
                        <>
                          $<CountUp value={r.cost_usd} />
                        </>
                      ) : (
                        "—"
                      )}
                    </strong>
                  </div>
                  <div>
                    <small>Выбросы CO₂e</small>
                    <strong>
                      {r.legs.length ? (
                        <>
                          <CountUp value={r.co2_kg} /> <em>кг</em>
                        </>
                      ) : (
                        "—"
                      )}
                    </strong>
                  </div>
                </div>
                {r.legs.length > 0 && (
                  <div
                    className="bars"
                    role="img"
                    aria-label={
                      "Выбросы CO₂e по плечам: " +
                      r.legs
                        .map(
                          (l) =>
                            `${short(l.origin_id)} — ${short(l.destination_id)} ${fmt(l.co2_kg)} кг`,
                        )
                        .join("; ")
                    }
                  >
                    {r.legs.map((l, i) => {
                      const max = Math.max(...r.legs.map((x) => x.co2_kg));
                      const ratio = max > 0 ? l.co2_kg / max : 0;
                      const Icon = modeIcon(l.mode);
                      return (
                        <div
                          className={"bar" + (l.co2_kg === max ? " top" : "")}
                          key={i}
                          style={order(i)}
                        >
                          <span className="bar-value">{fmt(l.co2_kg)} кг</span>
                          <span className="bar-track">
                            <i
                              style={
                                {
                                  "--v": Math.max(ratio, 0.06),
                                } as React.CSSProperties
                              }
                            />
                          </span>
                          <span className="bar-label">
                            <Icon size={14} /> {lookup(modes, l.mode) ?? l.mode}
                          </span>
                          <small>
                            {short(l.origin_id)} → {short(l.destination_id)}
                          </small>
                        </div>
                      );
                    })}
                  </div>
                )}
                <p className="micro">
                  Иллюстрационная оценка только транспорта, без терминалов,
                  хранения и порожних рейсов; не сертифицирована ISO/GLEC.
                  Коэффициенты: {r.emissions.factor_version}.
                </p>
              </section>
            </div>

            <div className="source-note">
              <CircleHelp size={15} />
              <span>
                Погода: {translate(d.weather.source)}
                {d.weather.stale ? " (устарела)" : ""}, базовый ветер{" "}
                {fmt(d.weather.wind_mps, 1)} м/с
                {d.weather.wave_height_m != null &&
                  `, волнение ${fmt(d.weather.wave_height_m, 1)} м`}
                {d.scenario.wind_mps != null &&
                  ` · ветер сценария: ${fmt(d.scenario.wind_mps, 1)} м/с`}
                {d.scenario.wave_height_m != null &&
                  ` · волнение сценария: ${fmt(d.scenario.wave_height_m, 1)} м`}{" "}
                · прогноз: {translate(d.prediction.data_source)},{" "}
                {d.prediction.model_version}. Расписание и показатели
                демонстрационные.
              </span>
            </div>
          </div>
        ) : (
          <div className="skeleton-stack" aria-busy="true">
            <div className="skeleton" style={{ height: 34, width: "50%" }} />
            <div className="skeleton" style={{ height: 240 }} />
            <div className="skeleton" style={{ height: 180 }} />
          </div>
        )}
      </aside>
    </div>
  );
}
