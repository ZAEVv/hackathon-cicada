import type React from "react";
import type { HubPoint, Leg, Port, Route } from "../types";
import {
  date,
  fmt,
  hours,
  lookup,
  portKinds,
  portName,
  portShortName,
} from "../lib";
import { useSize } from "../motion";
import { reducedMotion } from "../theme";

type P = { x: number; y: number };

// Longitude degrees are narrower than latitude degrees at this latitude.
const K = Math.cos((41 * Math.PI) / 180);
// Window that always stays in view: the sea and every catalog port.
const ROI = { lon0: 45.6, lon1: 55.2, lat0: 35.2, lat1: 47.2 };
// Strips kept clear of markers: the legend sits on top, the badge and the
// disclaimer at the bottom.
const PAD_TOP = 56;
const PAD_BOTTOM = 52;
const LON_C = (ROI.lon0 + ROI.lon1) / 2;
const LAT_C = (ROI.lat0 + ROI.lat1) / 2;

// Hand-traced Caspian shoreline (lon, lat), clockwise from the north-west.
// A schematic outline for orientation, not navigational data.
const SEA: [number, number][] = [
  [47.05, 44.75], [46.85, 45.3], [47.45, 45.72], [48.5, 45.92], [49.3, 46.3],
  [50.2, 46.72], [51.4, 46.95], [52.5, 46.98], [53.15, 46.6], [53.1, 45.95],
  [52.6, 45.42], [51.6, 45.25], [51.25, 44.9], [51.0, 44.55], [50.35, 44.62],
  [50.25, 44.3], [50.85, 44.1], [51.2, 43.5], [51.75, 42.95], [52.45, 42.5],
  [52.75, 41.75], [52.85, 41.25], [52.9, 40.9], [52.7, 40.4], [53.15, 40.0],
  [53.5, 39.4], [53.2, 38.95], [53.8, 38.3], [53.95, 37.5], [53.9, 36.95],
  [53.0, 36.85], [52.0, 36.6], [51.0, 36.72], [50.2, 37.15], [49.46, 37.5],
  [49.0, 37.72], [48.87, 38.4], [48.95, 39.1], [49.35, 39.4], [49.45, 40.05],
  [50.0, 40.25], [50.35, 40.32], [50.15, 40.55], [49.55, 40.65], [49.1, 41.2],
  [48.6, 41.85], [48.2, 42.3], [47.72, 42.85], [47.5, 43.1], [47.45, 43.6],
  [47.62, 44.0], [47.3, 44.3],
];
const LANDS: [string, number, number][] = [
  ["Россия", 43.4, 46.0],
  ["Казахстан", 55.4, 45.4],
  ["Туркменистан", 56.4, 39.6],
  ["Азербайджан", 46.2, 40.2],
  ["Иран", 55.6, 36.5],
];
const LABEL_SIDE: Record<string, "left" | "right"> = {
  rail_hub: "right",
  olya: "left",
  makhachkala: "left",
  anzali: "left",
  tehran: "right",
};

// "Iso" space: degrees with longitude pre-shrunk, so distances are isotropic.
const iso = (lon: number, lat: number): P => ({ x: lon * K, y: lat });
const SEA_ISO = SEA.map(([lon, lat]) => iso(lon, lat));

function inSea(p: P) {
  let inside = false;
  for (let i = 0, j = SEA_ISO.length - 1; i < SEA_ISO.length; j = i++) {
    const a = SEA_ISO[i];
    const b = SEA_ISO[j];
    if (
      a.y > p.y !== b.y > p.y &&
      p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x
    )
      inside = !inside;
  }
  return inside;
}
const quad = (a: P, c: P, b: P, t: number): P => ({
  x: (1 - t) * (1 - t) * a.x + 2 * (1 - t) * t * c.x + t * t * b.x,
  y: (1 - t) * (1 - t) * a.y + 2 * (1 - t) * t * c.y + t * t * b.y,
});
// Control point for a leg: the gentlest curve that keeps a sea leg over
// water and a land leg over land. `minBend` grows for alternatives so that
// legs shared with the current route do not overlap it.
function control(a: P, b: P, sea: boolean, minBend: number): P {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const at = (bend: number): P => ({
    x: mid.x - dy * bend,
    y: mid.y + dx * bend,
  });
  const fits = (c: P) =>
    [0.3, 0.4, 0.5, 0.6, 0.7].every((t) => inSea(quad(a, c, b, t)) === sea);
  for (let m = minBend; m <= 1.0001; m += 0.1)
    for (const sign of [1, -1]) {
      const c = at(m * sign);
      if (fits(c)) return c;
    }
  return at(minBend);
}

function smoothClosed(pts: P[]) {
  const n = pts.length;
  const f = (v: number) => v.toFixed(1);
  let d = `M${f(pts[0].x)} ${f(pts[0].y)}`;
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % n];
    const p3 = pts[(i + 2) % n];
    d +=
      `C${f(p1.x + (p2.x - p0.x) / 6)} ${f(p1.y + (p2.y - p0.y) / 6)} ` +
      `${f(p2.x - (p3.x - p1.x) / 6)} ${f(p2.y - (p3.y - p1.y) / 6)} ` +
      `${f(p2.x)} ${f(p2.y)}`;
  }
  return d + "Z";
}

export function RouteMap({
  ports,
  route,
  alternatives,
  current,
  showAlternatives,
  showLabels,
  load = null,
  loadLevels = null,
}: {
  ports: Port[];
  route: Route;
  alternatives: Route[];
  current: string;
  showAlternatives: boolean;
  showLabels: boolean;
  // Hub load at the chosen hour, keyed by port id (null: layer off).
  load?: Map<string, HubPoint> | null;
  loadLevels?: { bottleneck_threshold: number; busy_threshold: number } | null;
}) {
  const [ref, { w, h }] = useSize<HTMLDivElement>();
  const valid = ports.filter(
    (p) => Number.isFinite(p.lon) && Number.isFinite(p.lat),
  );
  const ready = valid.length > 0 && w > 0 && h > PAD_TOP + PAD_BOTTOM;
  const innerH = h - PAD_TOP - PAD_BOTTOM;
  const scale = ready
    ? Math.min(
        w / ((ROI.lon1 - ROI.lon0) * K),
        innerH / (ROI.lat1 - ROI.lat0),
      )
    : 1;
  const midY = PAD_TOP + innerH / 2;
  const toPx = (p: P): P => ({
    x: w / 2 + (p.x - LON_C * K) * scale,
    y: midY - (p.y - LAT_C) * scale,
  });
  const at = (lon: number, lat: number) => toPx(iso(lon, lat));
  const f = (v: number) => v.toFixed(1);

  const legPath = (leg: Leg, minBend: number) => {
    const a = valid.find((p) => p.id === leg.origin_id);
    const b = valid.find((p) => p.id === leg.destination_id);
    if (!a || !b) return null;
    const ai = iso(a.lon, a.lat);
    const bi = iso(b.lon, b.lat);
    const A = toPx(ai);
    const B = toPx(bi);
    const C = toPx(control(ai, bi, leg.mode === "sea", minBend));
    return {
      d: `M${f(A.x)} ${f(A.y)}Q${f(C.x)} ${f(C.y)} ${f(B.x)} ${f(B.y)}`,
      tail: `Q${f(C.x)} ${f(C.y)} ${f(B.x)} ${f(B.y)}`,
    };
  };

  const currentLegs = ready
    ? route.legs.map((leg) => ({ leg, path: legPath(leg, 0.08) }))
    : [];
  // One continuous path through the whole route for the travelling dot.
  let runner = "";
  currentLegs.forEach(({ leg, path }, i) => {
    if (!path) return;
    const joined =
      i > 0 &&
      runner !== "" &&
      route.legs[i - 1].destination_id === leg.origin_id;
    runner += joined ? path.tail : path.d;
  });

  const lonSpan = w / 2 / (K * scale);
  const latTop = LAT_C + midY / scale;
  const latBottom = LAT_C - (h - midY) / scale;
  const step = scale < 24 ? 4 : 2;
  const meridians: number[] = [];
  const parallels: number[] = [];
  if (ready) {
    for (
      let lon = Math.ceil((LON_C - lonSpan) / step) * step;
      lon <= LON_C + lonSpan;
      lon += step
    )
      meridians.push(lon);
    for (
      let lat = Math.ceil(latBottom / step) * step;
      lat <= latTop;
      lat += step
    )
      parallels.push(lat);
  }
  const loadTone = (utilization: number) =>
    utilization >= (loadLevels?.bottleneck_threshold ?? 0.85)
      ? "high"
      : utilization >= (loadLevels?.busy_threshold ?? 0.7)
        ? "medium"
        : "low";
  const onRoute = new Set(
    route.legs.flatMap((l) => [l.origin_id, l.destination_id]),
  );

  return (
    <div className="map" ref={ref}>
      {!valid.length && <div className="map-empty">Координаты недоступны</div>}
      {ready && (
        <>
          <svg
            className="map-svg"
            width={w}
            height={h}
            viewBox={`0 0 ${f(w)} ${f(h)}`}
            role="img"
            aria-label="Схема маршрута по координатам пунктов"
          >
            <g className="map-grid">
              {meridians.map((lon) => {
                const x = at(lon, LAT_C).x;
                // Meridians sit close together; label every other one
                // when there is no room for each.
                const labelled =
                  step * K * scale >= 44 || (lon / step) % 2 === 0;
                return (
                  <g key={"m" + lon}>
                    <line x1={x} x2={x} y1={0} y2={h} />
                    {labelled && (
                      <text x={x + 5} y={14}>
                        {lon}°E
                      </text>
                    )}
                  </g>
                );
              })}
              {parallels.map((lat) => {
                const y = at(LON_C, lat).y;
                return (
                  <g key={"p" + lat}>
                    <line x1={0} x2={w} y1={y} y2={y} />
                    <text x={6} y={y - 5}>
                      {lat}°N
                    </text>
                  </g>
                );
              })}
            </g>
            <path
              className="map-sea"
              d={smoothClosed(SEA_ISO.map(toPx))}
            />
            {scale >= 26 && (
              <g className="map-names">
                {LANDS.map(([label, lon, lat]) => {
                  const p = at(lon, lat);
                  if (p.x < 50 || p.x > w - 50 || p.y < 30 || p.y > h - 14)
                    return null;
                  return (
                    <text key={label} x={p.x} y={p.y} textAnchor="middle">
                      {label}
                    </text>
                  );
                })}
                <text
                  className="map-sea-name"
                  x={at(51.15, 42.6).x}
                  y={at(51.15, 42.6).y}
                  textAnchor="middle"
                >
                  Каспийское море
                </text>
              </g>
            )}
            {load && (
              // Plain fills and strokes only: no filters on the map (repaint trails).
              <g className="map-load">
                {valid.map((port) => {
                  const point = load.get(port.id);
                  if (!point) return null;
                  const p = at(port.lon, port.lat);
                  const share = Math.min(1, point.utilization);
                  const size = 13 + 13 * Math.sqrt(Math.min(1, point.load_tph / 60));
                  const ring = 2 * Math.PI * size;
                  return (
                    <g
                      key={port.id}
                      className={"hub-" + loadTone(point.utilization)}
                    >
                      <circle className="load-halo" cx={p.x} cy={p.y} r={size} />
                      <circle
                        className="load-ring"
                        cx={p.x}
                        cy={p.y}
                        r={size}
                        strokeDasharray={`${f(ring * share)} ${f(ring)}`}
                        transform={`rotate(-90 ${f(p.x)} ${f(p.y)})`}
                      />
                    </g>
                  );
                })}
              </g>
            )}
            {showAlternatives &&
              alternatives.map((alt, k) => (
                <g
                  key={alt.id}
                  className={"map-alt" + (alt.feasible ? "" : " off")}
                >
                  {alt.legs.map((leg, i) => {
                    const path = legPath(leg, 0.22 + 0.14 * k);
                    return path ? (
                      <path
                        key={i}
                        d={path.d}
                        className={leg.mode === "sea" ? "sea" : ""}
                      />
                    ) : null;
                  })}
                </g>
              ))}
            <g key={route.id} className="map-route">
              {currentLegs.map(({ leg, path }, i) =>
                path ? (
                  <g key={i} style={{ "--i": i } as React.CSSProperties}>
                    <path className="route-glow" d={path.d} pathLength={1} />
                    <path
                      className={
                        "route-line" + (leg.mode === "sea" ? " sea" : "")
                      }
                      d={path.d}
                    />
                  </g>
                ) : null,
              )}
              {runner && !reducedMotion() && (
                <g>
                  <circle className="route-runner-halo" r="9" />
                  <circle className="route-runner" r="4.5" />
                  <animateMotion
                    dur="7s"
                    repeatCount="indefinite"
                    path={runner}
                    calcMode="spline"
                    keyTimes="0;1"
                    keySplines="0.4 0 0.6 1"
                  />
                </g>
              )}
            </g>
          </svg>
          {valid.map((port, i) => {
            const p = at(port.lon, port.lat);
            const isCurrent = port.id === current;
            // Own-property lookup: a server id like "constructor" must not hit
            // Object.prototype (same rule as lib.ts lookup()).
            const side = Object.prototype.hasOwnProperty.call(LABEL_SIDE, port.id)
              ? LABEL_SIDE[port.id]
              : port.lon < LON_C
                ? "left"
                : "right";
            const departs = route.legs.find((l) => l.origin_id === port.id);
            const arrives = route.legs.find(
              (l) => l.destination_id === port.id,
            );
            const tipSide =
              p.x < 120 ? " tip-start" : p.x > w - 120 ? " tip-end" : "";
            const hub = load?.get(port.id);
            const hubText = hub
              ? `Загрузка ${fmt(Math.round(hub.utilization * 100))}% · ожидание ${hours(hub.queue_hours)}`
              : "";
            return (
              <button
                type="button"
                key={port.id}
                className={
                  "mk kind-" +
                  (lookup(portKinds, port.kind) ? port.kind : "port") +
                  (isCurrent ? " current" : "") +
                  (onRoute.has(port.id) ? " on" : "") +
                  (p.y < 96 ? " tip-below" : "") +
                  tipSide
                }
                style={
                  { left: p.x, top: p.y, "--i": i } as React.CSSProperties
                }
                aria-label={`${portName(port)}${isCurrent ? ", груз сейчас здесь" : ""}${hubText ? ", " + hubText : ""}`}
              >
                <span className="mk-dot" />
                {showLabels && (
                  <span className={"mk-label " + side}>
                    {portShortName(port)}
                  </span>
                )}
                <span className="mk-tip" role="tooltip">
                  <strong>{portName(port)}</strong>
                  <small>
                    {lookup(portKinds, port.kind) ?? port.kind} · {port.country}
                  </small>
                  {isCurrent && <em>Груз сейчас здесь</em>}
                  {arrives && <small>Прибытие: {date(arrives.arrival_at)}</small>}
                  {departs && (
                    <small>Отправление: {date(departs.departure_at)}</small>
                  )}
                  {hub && (
                    <small className={"mk-load hub-" + loadTone(hub.utilization)}>
                      {hubText}
                      <br />
                      на {date(hub.at)} · синтетика
                    </small>
                  )}
                </span>
              </button>
            );
          })}
        </>
      )}
    </div>
  );
}
