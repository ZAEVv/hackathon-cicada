import { useState } from "react";
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
import "./minimal.css";

// Illustrative demo figures for layout only. Not real operational data.
const months = [
  "янв", "фев", "мар", "апр", "май", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек",
];
const volume = [412, 438, 401, 467, 489, 512, 498, 536, 547, 571, 560, 602];

const kpis = [
  { label: "Перевозок за месяц", value: "1 284", delta: "+6,2%", up: true },
  { label: "Вовремя доставлено", value: "91,7%", delta: "−1,4 п.п.", up: false },
  { label: "Выбросы CO₂e, т", value: "318,6", delta: "−3,9%", up: true },
  { label: "Средняя задержка, ч", value: "7,3", delta: "+0,8 ч", up: false },
];

const modes = [
  { label: "Море", value: 46, cls: "c1", Icon: Ship },
  { label: "Авто", value: 33, cls: "c2", Icon: Truck },
  { label: "Ж/д", value: 21, cls: "c3", Icon: Train },
];

const shipments = [
  { id: "SC-20417", route: "Махачкала → Энзели", mass: "18,4 т", status: "В пути", tone: "ok" },
  { id: "SC-20419", route: "Астрахань → Тегеран", mass: "9,1 т", status: "Очередь", tone: "warn" },
  { id: "SC-20421", route: "Оля → Энзели", mass: "22,7 т", status: "Доставлено", tone: "done" },
  { id: "SC-20424", route: "Махачкала → Тегеран", mass: "6,3 т", status: "Документы", tone: "warn" },
  { id: "SC-20428", route: "Астрахань → Оля", mass: "14,0 т", status: "В пути", tone: "ok" },
];

const W = 640;
const H = 220;
const PAD = 18;

function linePath(values: number[]) {
  const min = Math.min(...values) * 0.95;
  const max = Math.max(...values) * 1.02;
  return values
    .map((v, i) => {
      const x = PAD + (i * (W - PAD * 2)) / (values.length - 1);
      const y = H - PAD - ((v - min) / (max - min)) * (H - PAD * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

export function MinimalDashboard() {
  const [range, setRange] = useState<6 | 12>(12);
  const series = volume.slice(-range);
  const labels = months.slice(-range);
  const total = series.reduce((a, b) => a + b, 0);

  const R = 58;
  const C = 2 * Math.PI * R;
  let offset = 0;

  return (
    <div className="md">
      <div className="md-head">
        <div>
          <h1>Обзор перевозок</h1>
          <p>Сводка по коридору за последние {range} месяцев</p>
        </div>
        <div className="md-seg" role="group" aria-label="Период">
          <button
            className={range === 6 ? "on" : ""}
            onClick={() => setRange(6)}
          >
            6 мес.
          </button>
          <button
            className={range === 12 ? "on" : ""}
            onClick={() => setRange(12)}
          >
            12 мес.
          </button>
        </div>
      </div>

      <section className="md-kpis">
        {kpis.map((k) => {
          const Trend = k.up ? TrendingUp : TrendingDown;
          return (
            <article className="md-card md-kpi" key={k.label}>
              <span className="md-muted">{k.label}</span>
              <strong>{k.value}</strong>
              <span className={"md-delta " + (k.up ? "up" : "down")}>
                <Trend size={14} aria-hidden /> {k.delta}
              </span>
            </article>
          );
        })}
      </section>

      <section className="md-grid">
        <article className="md-card md-chart">
          <div className="md-card-head">
            <div>
              <h2>Объём перевозок</h2>
              <p className="md-muted">Всего за период: {total.toLocaleString("ru-RU")} ед.</p>
            </div>
          </div>
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Линия объёма перевозок по месяцам">
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
            <path d={linePath(series)} className="md-line" />
          </svg>
          <div className="md-axis">
            {labels.map((l) => (
              <span key={l}>{l}</span>
            ))}
          </div>
        </article>

        <article className="md-card md-donut">
          <h2>Доля по видам транспорта</h2>
          <p className="md-muted">В объёме перевозок</p>
          <div className="md-donut-wrap">
            <svg viewBox="0 0 160 160" aria-hidden>
              <circle cx="80" cy="80" r={R} className="md-track" />
              {modes.map((m) => {
                const len = (m.value / 100) * C;
                const el = (
                  <circle
                    key={m.label}
                    cx="80"
                    cy="80"
                    r={R}
                    className={"md-seg-arc " + m.cls}
                    strokeDasharray={`${len} ${C - len}`}
                    strokeDashoffset={-offset}
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
            {modes.map(({ label, value, cls, Icon }) => (
              <li key={label}>
                <span className={"md-dot " + cls} />
                <Icon size={15} aria-hidden />
                <span>{label}</span>
                <strong>{value}%</strong>
              </li>
            ))}
          </ul>
        </article>
      </section>

      <article className="md-card">
        <div className="md-card-head">
          <div>
            <h2>Последние перевозки</h2>
            <p className="md-muted">Статусы обновляются по данным симуляции</p>
          </div>
          <Container size={18} className="md-muted" aria-hidden />
        </div>
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
              {shipments.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{s.id}</td>
                  <td>{s.route}</td>
                  <td className="num">{s.mass}</td>
                  <td>
                    <span className={"md-badge " + s.tone}>{s.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </article>

      <footer className="md-foot">
        <span>
          <Clock3 size={14} aria-hidden /> Демо-данные, не для операционных решений
        </span>
        <span>
          <Leaf size={14} aria-hidden /> Оценка CO₂e только для транспорта
        </span>
      </footer>
    </div>
  );
}
