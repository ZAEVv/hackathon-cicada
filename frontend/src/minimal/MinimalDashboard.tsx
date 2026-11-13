import { useEffect, useState, type CSSProperties } from "react";
import { CountUp } from "../motion";
import {
  Clock3,
  Container,
  Leaf,
  Ship,
  Train,
  TrendingDown,
  TrendingUp,
  Truck,
} from "lucide-react";
import { api, fmt, lookup, portShort } from "../lib";
import type { CorridorSummary, SummaryKpi } from "../types";
import "./minimal.css";

type Weeks = 4 | 13;

const modeMeta = {
  sea: { label: "Море", cls: "c1", Icon: Ship },
  road: { label: "Авто", cls: "c2", Icon: Truck },
  rail: { label: "Ж/д", cls: "c3", Icon: Train },
} as const;

const statusMeta = {
  at_risk: { label: "Риск срыва", tone: "bad" },
  docs_pending: { label: "Документы", tone: "warn" },
  attention: { label: "Внимание", tone: "warn" },
  in_transit: { label: "В пути", tone: "ok" },
} as const;

type KpiDef = {
  label: string;
  key: keyof CorridorSummary["kpis"];
  digits: number;
  unit?: string;
  // Delta text and whether an increase is an improvement.
  delta: (k: SummaryKpi) => string;
  upIsGood: boolean;
};

const signed = (n: number, digits: number, unit: string) =>
  `${n > 0 ? "+" : n < 0 ? "−" : ""}${fmt(Math.abs(n), digits)}${unit}`;

const kpiDefs: KpiDef[] = [
  {
    label: "Перевозок за период",
    key: "shipments",
    digits: 0,
    delta: (k) => (k.delta_pct == null ? "—" : signed(k.delta_pct, 1, "%")),
    upIsGood: true,
  },
  {
    label: "Вовремя доставлено",
    key: "on_time_pct",
    digits: 1,
    unit: "%",
    delta: (k) => signed(k.delta ?? 0, 1, " п.п."),
    upIsGood: true,
  },
  {
    label: "Выбросы CO₂e, т",
    key: "co2e_tonnes",
    digits: 1,
    delta: (k) => (k.delta_pct == null ? "—" : signed(k.delta_pct, 1, "%")),
    upIsGood: false,
  },
  {
    label: "Средняя задержка, ч",
    key: "avg_delay_hours",
    digits: 1,
    delta: (k) => signed(k.delta ?? 0, 1, " ч"),
    upIsGood: false,
  },
];

const W = 640;
const H = 220;
const PAD = 18;

// Chart points in viewBox units. A flat series (min == max) sits at mid-height.
function linePoints(values: number[]) {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const min = lo * 0.95;
  const span = hi * 1.02 - min;
  return values.map((v, i) => ({
    x: values.length > 1 ? PAD + (i * (W - PAD * 2)) / (values.length - 1) : W / 2,
    y: hi > lo && span > 0 ? H - PAD - ((v - min) / span) * (H - PAD * 2) : H / 2,
  }));
}
const linePath = (pts: { x: number; y: number }[]) =>
  pts.length < 2
    ? ""
    : pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");

// "2026-11-05" -> "05.11"
const weekLabel = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
const dateRu = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;

function Skeleton({ h, w = "100%" }: { h: number; w?: string }) {
  return <span className="md-skel" style={{ height: h, width: w }} aria-hidden />;
}

export function MinimalDashboard({ token }: { token?: string }) {
  const [range, setRange] = useState<Weeks>(4);
  const [data, setData] = useState<CorridorSummary | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setError("");
    api<CorridorSummary>(`/summary?weeks=${range}`, {}, token)
      .then((d) => {
        if (live) setData(d);
      })
      .catch((e: unknown) => {
        if (live)
          setError(e instanceof Error ? e.message : "Не удалось загрузить сводку.");
      });
    return () => {
      live = false;
    };
  }, [range, token, attempt]);

  // Show stale data while a new period loads, but never data for another period as "current".
  const ready = data && data.weeks === range ? data : null;
  const loading = !ready && !error;

  const R = 58;
  const C = 2 * Math.PI * R;
  let offset = 0;
  // Weekly tonnage: the synthetic history has a constant count per week, so
  // counts stay in the KPI card and the chart shows mass.
  const series = ready ? ready.weekly.map((w) => w.tonnes) : [];
  const points = series.length ? linePoints(series) : [];
  const total = series.reduce((a, b) => a + b, 0);

  return (
    <div className="md">
      <div className="md-head">
        <div>
          <h1>Обзор перевозок</h1>
          <p>Сводка по коридору за последние {range} нед.</p>
          <p className="md-caption">Синтетическая история · не операционные данные</p>
        </div>
        <div className="md-seg" role="group" aria-label="Период">
          <button type="button" className={range === 4 ? "on" : ""} aria-pressed={range === 4} onClick={() => setRange(4)}>
            4 нед.
          </button>
          <button type="button" className={range === 13 ? "on" : ""} aria-pressed={range === 13} onClick={() => setRange(13)}>
            13 нед.
          </button>
        </div>
      </div>

      {error && (
        <article className="md-card md-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>Повторить</button>
        </article>
      )}

      {!error && (
        <>
          <section className="md-kpis" aria-busy={loading}>
            {kpiDefs.map((def) => {
              const k = ready?.kpis[def.key];
              const hasDelta = !!k && k.delta != null;
              const up = hasDelta && (k.delta as number) >= 0;
              const good = up === def.upIsGood;
              const Trend = up ? TrendingUp : TrendingDown;
              return (
                <article className="md-card md-kpi" key={def.key}>
                  <span className="md-muted">{def.label}</span>
                  {k ? (
                    <>
                      <strong><CountUp value={k.value} digits={def.digits} />{def.unit}</strong>
                      {hasDelta ? (
                        <span className={"md-delta " + (good ? "up" : "down")}>
                          <Trend size={14} aria-hidden /> {def.delta(k)}
                        </span>
                      ) : (
                        <span className="md-delta flat">нет периода для сравнения</span>
                      )}
                    </>
                  ) : (
                    <>
                      <Skeleton h={30} w="60%" />
                      <Skeleton h={16} w="40%" />
                    </>
                  )}
                </article>
              );
            })}
          </section>

          <section className="md-grid">
            <article className="md-card md-chart">
              <div className="md-card-head">
                <div>
                  <h2>Объём перевозок, т</h2>
                  <p className="md-muted">
                    {ready
                      ? `Всего за период: ${fmt(total, 1)} т · по неделям`
                      : "Загрузка…"}
                  </p>
                </div>
              </div>
              {ready ? (
                <>
                  <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Масса перевезённых грузов по неделям, т">
                    {[0.25, 0.5, 0.75].map((t) => (
                      <line
                        key={t}
                        x1={PAD}
                        x2={W - PAD}
                        y1={PAD + t * (H - PAD * 2)}
                        y2={PAD + t * (H - PAD * 2)}
                        className="md-grid-line"
                      />
                    ))}
                    <path d={linePath(points)} pathLength={1} className="md-line" />
                    {points.map((p, i) => (
                      <circle key={i} cx={p.x} cy={p.y} r={3.5} className="md-point">
                        <title>{`${weekLabel(ready.weekly[i].week_start)}: ${fmt(series[i], 1)} т`}</title>
                      </circle>
                    ))}
                  </svg>
                  <div className="md-axis">
                    {ready.weekly.map((w) => (
                      <span key={w.week_start}>{weekLabel(w.week_start)}</span>
                    ))}
                  </div>
                </>
              ) : (
                <Skeleton h={H} />
              )}
            </article>

            <article className="md-card md-donut">
              <h2>Доля по видам транспорта</h2>
              <p className="md-muted">По т·км текущих маршрутов</p>
              {ready ? (
                <>
                  <div className="md-donut-wrap">
                    <svg viewBox="0 0 160 160" aria-hidden>
                      <circle cx="80" cy="80" r={R} className="md-track" />
                      {ready.mode_split.map((m) => {
                        const len = (m.share_pct / 100) * C;
                        const el = (
                          <circle
                            key={m.mode}
                            cx="80"
                            cy="80"
                            r={R}
                            className={"md-seg-arc " + modeMeta[m.mode].cls}
                            strokeDasharray={`${len} ${C - len}`}
                            strokeDashoffset={-offset}
                            style={{ "--circumference": C } as CSSProperties}
                            transform="rotate(-90 80 80)"
                          />
                        );
                        offset += len;
                        return el;
                      })}
                      <text x="80" y="86" textAnchor="middle" className="md-donut-total">
                        100%
                      </text>
                    </svg>
                  </div>
                  <ul className="md-legend">
                    {ready.mode_split.map((m) => {
                      const { label, cls, Icon } = modeMeta[m.mode];
                      return (
                        <li key={m.mode}>
                          <span className={"md-dot " + cls} />
                          <Icon size={15} aria-hidden />
                          <span>{label}</span>
                          <strong>{fmt(m.share_pct, 1)}%</strong>
                        </li>
                      );
                    })}
                  </ul>
                </>
              ) : (
                <Skeleton h={190} />
              )}
            </article>
          </section>

          <article className="md-card">
            <div className="md-card-head">
              <div>
                <h2>Последние перевозки</h2>
                <p className="md-muted">Текущие демо-отправки; статус по риску и документам</p>
              </div>
              <Container size={18} className="md-muted" aria-hidden />
            </div>
            {ready ? (
              <div className="md-table-wrap">
                <table className="md-table">
                  <thead>
                    <tr>
                      <th>Номер</th>
                      <th>Маршрут</th>
                      <th className="num">Масса</th>
                      <th>Статус</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ready.recent_shipments.map((s, i) => {
                      const st = statusMeta[s.status] ?? { label: s.status, tone: "warn" };
                      return (
                        <tr key={s.id} style={{ "--i": i } as CSSProperties}>
                          <td className="mono">{s.id}</td>
                          <td>
                            {lookup(portShort, s.origin_id) ?? s.origin_name} →{" "}
                            {lookup(portShort, s.destination_id) ?? s.destination_name}
                          </td>
                          <td className="num">{fmt(s.mass_tonnes, 1)} т</td>
                          <td>
                            <span className={"md-badge " + st.tone}>{st.label}</span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <Skeleton h={120} />
            )}
          </article>
        </>
      )}

      <footer className="md-foot">
        <span>
          <Clock3 size={14} aria-hidden />
          {ready
            ? ` Синтетическая история ${dateRu(ready.period_start)} – ${dateRu(ready.period_end)}, не для операционных решений`
            : " Демо-данные, не для операционных решений"}
        </span>
        <span>
          <Leaf size={14} aria-hidden /> Оценка CO₂e только для транспорта
        </span>
      </footer>
    </div>
  );
}
