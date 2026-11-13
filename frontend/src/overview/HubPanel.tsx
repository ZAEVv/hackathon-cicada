import { useEffect, useState, type CSSProperties } from "react";
import { Gauge, Pause, Play, TriangleAlert } from "lucide-react";
import type { HubForecast, HubPoint } from "../types";
import { api, date, fmt, hours, lookup, scenarios, translate } from "../lib";
import { reducedMotion } from "../theme";
import { CountUp } from "../motion";

export const HUB_STEP = 3;

export type HubTone = "low" | "medium" | "high";

export const hubTone = (
  utilization: number,
  forecast: Pick<HubForecast, "bottleneck_threshold" | "busy_threshold">,
): HubTone =>
  utilization >= forecast.bottleneck_threshold
    ? "high"
    : utilization >= forecast.busy_threshold
      ? "medium"
      : "low";

export const percent = (utilization: number) =>
  `${fmt(Math.round(utilization * 100))}%`;

// Load of every node at one hour of the horizon, keyed by port id. A Map, so
// server ids such as "constructor" cannot hit Object.prototype.
export function loadAt(
  forecast: HubForecast | null,
  hour: number,
): Map<string, HubPoint> {
  const result = new Map<string, HubPoint>();
  if (!forecast) return result;
  for (const hub of forecast.hubs) {
    const point =
      hub.points.find((p) => p.offset_hours === hour) ??
      hub.points[Math.min(hour, hub.points.length - 1)];
    if (point) result.set(hub.port_id, point);
  }
  return result;
}

// The forecast depends on every shipment's scenario and chosen route, so it is
// reloaded whenever the dashboard object is replaced (each refresh/mutation)
// and whenever another shipment is selected.
export function useHubForecast(
  token: string,
  shipmentId: string,
  refreshKey: unknown,
) {
  const [forecast, setForecast] = useState<HubForecast | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!shipmentId) return;
    let active = true;
    api<HubForecast>(
      "/hubs/forecast?shipment_id=" + encodeURIComponent(shipmentId),
      {},
      token,
    )
      .then((result) => {
        if (!active) return;
        setForecast(result);
        setError("");
      })
      .catch((e: Error) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [token, shipmentId, refreshKey]);
  return { forecast, error };
}

const atOffset = (forecast: HubForecast, hour: number) =>
  new Date(
    new Date(forecast.simulation_now).getTime() + hour * 3600_000,
  ).toISOString();

export function HubTimeBar({
  forecast,
  hour,
  onHour,
}: {
  forecast: HubForecast;
  hour: number;
  onHour: (hour: number) => void;
}) {
  const [playing, setPlaying] = useState(false);
  const max = forecast.horizon_hours;
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => {
      onHour(hour + HUB_STEP > max ? 0 : hour + HUB_STEP);
    }, 900);
    return () => clearInterval(timer);
  }, [playing, hour, max, onHour]);
  const now = loadAt(forecast, hour);
  const hot = [...now.values()].filter(
    (p) => p.utilization >= forecast.bottleneck_threshold,
  ).length;
  return (
    <div className="hub-bar">
      {!reducedMotion() && (
        <button
          type="button"
          className={"icon-btn hub-play" + (playing ? " on" : "")}
          onClick={() => setPlaying((v) => !v)}
          aria-label={playing ? "Пауза" : "Проиграть прогноз на 72 часа"}
          title={playing ? "Пауза" : "Проиграть прогноз"}
        >
          {playing ? <Pause size={16} /> : <Play size={16} />}
        </button>
      )}
      <label className="hub-slider">
        <span className="tool-label">Горизонт прогноза</span>
        <input
          type="range"
          min={0}
          max={max}
          step={HUB_STEP}
          value={hour}
          onChange={(e) => {
            setPlaying(false);
            onHour(Number(e.target.value));
          }}
          aria-valuetext={`+${hour} ч, ${date(atOffset(forecast, hour))}`}
        />
      </label>
      <div className="hub-when" aria-live="off">
        <strong key={hour} className="hub-tick">
          +{hour} ч · {date(atOffset(forecast, hour))}
        </strong>
        <small className={hot ? "hub-high" : ""}>
          {hot
            ? `Перегружено узлов: ${hot}`
            : "Перегруженных узлов нет"}
        </small>
      </div>
    </div>
  );
}

export function HubPanel({
  forecast,
  error,
  hour,
  name,
  short,
  onHour,
  onSelect,
  index = 0,
}: {
  forecast: HubForecast | null;
  error: string;
  hour: number;
  name: (id: string) => string;
  short: (id: string) => string;
  onHour: (hour: number) => void;
  onSelect: (id: string) => void;
  index?: number;
}) {
  const [all, setAll] = useState(false);
  const order = { "--i": index } as CSSProperties;
  if (!forecast)
    return (
      <section className="card hub-card reveal" style={order}>
        <div className="card-heading">
          <div>
            <h3>Узкие места · 72 ч</h3>
            <p>{error ? "Прогноз загрузки недоступен" : "Загрузка прогноза…"}</p>
          </div>
          <Gauge size={18} className="muted" />
        </div>
      </section>
    );
  const snap = (h: number) =>
    Math.min(forecast.horizon_hours, Math.round(h / HUB_STEP) * HUB_STEP);
  const peakOffset = (at: string) =>
    snap(
      (new Date(at).getTime() - new Date(forecast.simulation_now).getTime()) /
        3600_000,
    );
  const windows = forecast.bottlenecks;
  const shown = all ? windows : windows.slice(0, 4);
  const buckets: number[] = [];
  for (let h = 0; h < forecast.horizon_hours; h += HUB_STEP) buckets.push(h);
  const regional = forecast.regional_scenario;
  const nowIdx = Math.max(0, Math.min(buckets.length - 1, Math.floor(hour / HUB_STEP)));
  return (
    <section className="card hub-card reveal" style={order}>
      <div className="card-heading">
        <div>
          <h3>Узкие места · 72 ч</h3>
          <p>
            Прогноз загрузки узлов: синтетический фон + демо-перевозки · порог{" "}
            {percent(forecast.bottleneck_threshold)}
          </p>
        </div>
        <span className={"pill " + (windows.length ? "high" : "")}>
          <i />
          {windows.length ? `Окон: ${windows.length}` : "Без перегрузок"}
        </span>
      </div>
      {regional !== "none" && (
        <p className="hub-regional">
          <TriangleAlert size={14} /> Региональное условие:{" "}
          <strong>{lookup(scenarios, regional) ?? regional}</strong>
          {forecast.focus_shipment_id
            ? ` — сценарий перевозки ${forecast.focus_shipment_id}`
            : ""}
        </p>
      )}

      <div
        className="hub-heat"
        role="group"
        aria-label="Загрузка узлов по трёхчасовым интервалам"
      >
        {forecast.hubs.map((hub, row) => (
          <div className="hub-heat-row" key={hub.port_id} style={{ "--r": row } as CSSProperties}>
            <span className="hub-heat-name" title={name(hub.port_id)}>
              {short(hub.port_id)}
            </span>
            <div className="hub-heat-cells">
              <span className="hub-heat-now" style={{ "--idx": nowIdx, "--n": buckets.length } as CSSProperties} aria-hidden="true" />
              {buckets.map((start) => {
                const slice = hub.points.filter(
                  (p) =>
                    p.offset_hours >= start &&
                    p.offset_hours < start + HUB_STEP,
                );
                const peak = Math.max(0, ...slice.map((p) => p.utilization));
                const tone = hubTone(peak, forecast);
                return (
                  <button
                    type="button"
                    key={start}
                    className={
                      "hub-cell hub-" +
                      tone +
                      (start === Math.min(hour, forecast.horizon_hours - HUB_STEP)
                        ? " now"
                        : "")
                    }
                    style={{ opacity: 0.3 + 0.7 * Math.min(1, peak) }}
                    // 8 hubs x 24 cells: keyboard users move through
                    // time with the slider instead of ~200 tab stops.
                    tabIndex={-1}
                    onClick={() => onHour(start)}
                    title={`${name(hub.port_id)} · +${start} ч · ${percent(peak)}`}
                    aria-label={`${name(hub.port_id)}, +${start} ч, загрузка ${percent(peak)}`}
                  />
                );
              })}
            </div>
            <span className={"hub-heat-peak hub-" + hubTone(hub.peak_utilization, forecast)}>
              <CountUp value={Math.round(hub.peak_utilization * 100)} />%
            </span>
          </div>
        ))}
        <div className="hub-heat-row hub-heat-axis" aria-hidden="true">
          <span />
          <div className="hub-heat-ticks">
            <span>сейчас</span>
            <span>+24 ч</span>
            <span>+48 ч</span>
            <span>+72 ч</span>
          </div>
          <span>пик</span>
        </div>
      </div>

      {windows.length ? (
        <ul className="hub-list">
          {shown.map((w, i) => (
            <li
              key={w.port_id + w.start_at}
              className={"hub-item hub-" + (w.severity === "high" ? "high" : "medium")}
              style={{ "--i": i % 4 } as CSSProperties}
            >
              <button
                type="button"
                className="hub-item-main"
                onClick={() => onHour(peakOffset(w.peak_at))}
                title="Показать пик на карте"
              >
                <span className="hub-item-top">
                  <strong>{name(w.port_id)}</strong>
                  <b><CountUp value={Math.round(w.peak_utilization * 100)} />%</b>
                </span>
                <span className="hub-item-when">
                  {date(w.start_at)} — {date(w.end_at)} · через{" "}
                  {hours(w.start_offset_hours)} · ожидание до{" "}
                  {hours(w.max_queue_hours)}
                </span>
                <span className="hub-item-why">
                  {w.reasons.map(translate).join(" · ")}
                </span>
              </button>
              <span className="hub-item-bar" style={{ "--p": Math.min(1, w.peak_utilization) } as CSSProperties} aria-hidden="true" />
              {w.shipment_ids.length > 0 && (
                <span className="hub-item-ships">
                  <small>Затронуты:</small>
                  {w.shipment_ids.map((id) => (
                    <button
                      type="button"
                      key={id}
                      className="chip mono hub-ship"
                      onClick={() => onSelect(id)}
                    >
                      {id}
                    </button>
                  ))}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <div className="empty-small">
          В ближайшие 72 ч узких мест не прогнозируется
        </div>
      )}
      {windows.length > 4 && (
        <button
          type="button"
          className="text-button"
          onClick={() => setAll((v) => !v)}
        >
          {all ? "Свернуть" : `Показать все (${windows.length})`}
        </button>
      )}
      <p className="hub-note">
        Синтетические данные: фоновая загрузка и мощности узлов смоделированы
        (seed-детерминированно) и не являются статистикой портов.
        {error ? " Не удалось обновить прогноз — показаны прежние данные." : ""}
      </p>
    </section>
  );
}
