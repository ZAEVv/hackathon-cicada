import type React from "react";
import { Ship, Train, Truck } from "lucide-react";
import type { Route } from "../types";
import { date, fmt, hours, lookup, modes } from "../lib";

const HOUR = 3600e3;
const ms = (s: string) => new Date(s).getTime();

export const modeIcon = (mode: string) =>
  mode === "sea" ? Ship : mode === "rail" ? Train : Truck;

const dayLabel = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});
const hourLabel = new Intl.DateTimeFormat("ru-RU", {
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

// Remaining legs on a shared UTC time axis: travel as solid bars, waiting
// between legs as dashed segments, acceptance cutoffs as ticks.
export function LegGantt({
  route,
  now,
  short,
}: {
  route: Route;
  now: string;
  short: (id: string) => string;
}) {
  const legs = route.legs.filter(
    (l) =>
      Number.isFinite(ms(l.departure_at)) && Number.isFinite(ms(l.arrival_at)),
  );
  if (!legs.length)
    return (
      <div className="empty-small">
        Маршрут недоступен: плеч для отображения нет
      </div>
    );
  const nowMs = ms(now);
  const hasNow = Number.isFinite(nowMs);
  const start = Math.min(
    hasNow ? nowMs : Infinity,
    ...legs.map((l) => {
      const cutoff = ms(l.cutoff_at);
      return Math.min(
        ms(l.departure_at),
        Number.isFinite(cutoff) ? cutoff : Infinity,
      );
    }),
  );
  const end = Math.max(...legs.map((l) => ms(l.arrival_at)));
  const pad = Math.max((end - start) * 0.05, HOUR);
  const d0 = start - pad;
  const span = end + pad - d0;
  const pos = (t: number) => ((t - d0) / span) * 100;
  const step =
    [3, 6, 12, 24, 48, 96, 192]
      .map((h) => h * HOUR)
      .find((s) => span / s <= 7) ?? 192 * HOUR;
  const ticks: number[] = [];
  for (let t = Math.ceil(d0 / step) * step; t <= d0 + span; t += step)
    ticks.push(t);

  return (
    <div className="gantt-scroll">
      <div
        className="gantt"
        style={
          { "--now": hasNow ? pos(nowMs) / 100 : 0 } as React.CSSProperties
        }
      >
        <div className="g-row g-axis">
          <div className="g-name">Время, UTC</div>
          <div className="g-track">
            {ticks.map((t) => {
              const midnight = new Date(t).getUTCHours() === 0;
              return (
                <span
                  key={t}
                  className={"g-tick" + (midnight ? " day" : "")}
                  style={{ left: pos(t) + "%" }}
                >
                  {midnight ? dayLabel.format(t) : hourLabel.format(t)}
                </span>
              );
            })}
          </div>
        </div>
        {legs.map((leg, i) => {
          const Icon = modeIcon(leg.mode);
          const dep = ms(leg.departure_at);
          const arr = ms(leg.arrival_at);
          const cutoff = ms(leg.cutoff_at);
          const prev = i > 0 ? ms(legs[i - 1].arrival_at) : NaN;
          const wait = Number.isFinite(prev) ? dep - prev : 0;
          return (
            <div
              className="g-row"
              key={i}
              style={{ "--i": i } as React.CSSProperties}
            >
              <div className="g-name">
                <Icon size={16} aria-hidden="true" />
                <div>
                  <strong>
                    {short(leg.origin_id)} → {short(leg.destination_id)}
                  </strong>
                  <small>
                    {lookup(modes, leg.mode) ?? leg.mode} ·{" "}
                    {fmt(leg.distance_km)} км
                  </small>
                </div>
              </div>
              <div className="g-track">
                {wait > 0 && (
                  <span
                    className="g-wait"
                    style={{
                      left: pos(prev) + "%",
                      width: pos(dep) - pos(prev) + "%",
                    }}
                    title={`Ожидание ${hours(wait / HOUR)}`}
                  />
                )}
                {Number.isFinite(cutoff) && (
                  <span
                    className="g-cut"
                    style={{ left: pos(cutoff) + "%" }}
                    title={`Дедлайн приёма: ${date(leg.cutoff_at)}`}
                  />
                )}
                <span
                  className="g-bar"
                  style={{
                    left: pos(dep) + "%",
                    width: Math.max(pos(arr) - pos(dep), 0.8) + "%",
                  }}
                  title={`${date(leg.departure_at)} — ${date(leg.arrival_at)}`}
                />
                <span
                  className="g-dur"
                  style={{ left: pos((dep + arr) / 2) + "%" }}
                >
                  {hours((arr - dep) / HOUR)}
                </span>
                <span className="sr-only">
                  Отправление {date(leg.departure_at)}, прибытие{" "}
                  {date(leg.arrival_at)}
                </span>
              </div>
            </div>
          );
        })}
        {hasNow && (
          <span className="g-now" aria-hidden="true">
            <i>Сейчас</i>
          </span>
        )}
      </div>
    </div>
  );
}
