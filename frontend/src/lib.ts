import type { Detail, Payload, Port, Route } from "./types";

export const fmt = (n: number | null | undefined, digits = 0) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(
        n,
      );
export const date = (s: string | null | undefined) => {
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
// Durations: hours up to two days, then days.
export const hours = (h: number | null | undefined) =>
  h == null || !Number.isFinite(h)
    ? "—"
    : h >= 48
      ? `${fmt(h / 24, 1)} дн`
      : `${fmt(h, h < 10 ? 1 : 0)} ч`;
// Own-property lookup: server strings such as "constructor" or "__proto__"
// must not resolve to inherited Object.prototype members.
export const lookup = (
  table: Record<string, string>,
  key: unknown,
): string | undefined =>
  typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key)
    ? table[key]
    : undefined;
export const riskLabels: Record<string, string> = {
  low: "Низкий",
  medium: "Средний",
  high: "Высокий",
};
export const statuses: Record<string, string> = {
  draft: "Черновик",
  validation_failed: "Есть замечания",
  ready: "Готова к отправке",
  submitted: "На рассмотрении",
  demo_accepted: "Принята в демо",
  needs_revision: "На доработке",
};
export const scenarios: Record<string, string> = {
  none: "Базовый",
  storm: "Шторм",
  congestion: "Очередь",
  closure: "Закрытие",
};
export const modes: Record<string, string> = {
  sea: "Море",
  road: "Авто",
  rail: "Ж/д",
};
export const docs: Record<string, string> = {
  invoice: "Инвойс",
  packing_list: "Упаковочный лист",
  transport: "Транспортный документ",
};
export const portNames: Record<string, string> = {
  rail_hub: "Астраханский ж/д терминал",
  makhachkala: "Порт Махачкала",
  olya: "Порт Оля",
  anzali: "Порт Энзели",
  tehran: "Сухой порт Тегеран",
};
// Short names for tight places: map labels, shipment cards, timeline rows.
export const portShort: Record<string, string> = {
  rail_hub: "Астрахань",
  makhachkala: "Махачкала",
  olya: "Оля",
  anzali: "Энзели",
  tehran: "Тегеран",
};
export const portKinds: Record<string, string> = {
  terminal: "Ж/д терминал",
  port: "Порт",
  destination: "Пункт назначения",
};
export const cargoNames: Record<string, string> = {
  general: "Генеральный груз",
};
export const translations: Record<string, string> = {
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
  "Synthetic classifier estimate; not guaranteed calibrated":
    "Оценка синтетического классификатора; калибровка не гарантирована",
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
  "Background peak load": "Суточный пик фоновой загрузки",
  "Storm limits berth operations": "Шторм ограничивает работу причалов",
  "Scenario congestion at terminal": "Очередь в терминале по сценарию",
  "Sea service closed: cargo accumulates":
    "Морская линия закрыта: груз накапливается",
  "Diverted flow from closed service":
    "Переток грузопотока с закрытой линии",
  "Demo shipments handled in this window":
    "В окне обрабатываются демо-перевозки",
  "Shipment held: route unavailable":
    "Груз задержан: маршрут недоступен",
  "Backlog carried over": "Накопленная очередь обработки",
  rail_departure: "Отправление по железной дороге",
  port_arrival: "Прибытие в порт",
  sea_departure: "Морское отправление",
  road_departure: "Отправление автотранспортом",
  delivery: "Доставка",
};
export const translate = (value: string) => lookup(translations, value) ?? value;
export const portName = (port: Port) => lookup(portNames, port.id) ?? port.name;
export const portShortName = (port: Port) =>
  lookup(portShort, port.id) ?? port.name;
export const riskLabel = (risk: string) => lookup(riskLabels, risk) ?? risk;
export const riskClass = (risk: string) =>
  lookup(riskLabels, risk) !== undefined ? risk : "";
export function scheduleStatus(route: Route): string | null {
  if (route.missed_connection) return "Стыковка пропущена";
  if (!route.feasible) return "Маршрут недоступен";
  return null;
}
// One tone per shipment: a schedule problem outranks the model's risk class.
export const shipmentTone = (item: Detail): "low" | "medium" | "high" => {
  if (scheduleStatus(item.current_route)) return "high";
  const risk = riskClass(item.prediction.risk);
  return risk === "high" || risk === "medium" ? risk : "low";
};
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
export async function api<T>(
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
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
export const TOKEN_KEYS = {
  broker: "sct-broker",
  reviewer: "sct-reviewer",
} as const;
export const DEFAULT_TOKENS = {
  broker: "demo-broker-token",
  reviewer: "demo-reviewer-token",
} as const;
// Tokens live only in sessionStorage (cleared with the tab); storage access
// can throw (private mode, blocked storage), so every call is guarded.
export function loadToken(key: string, fallback: string): string {
  try {
    const value = sessionStorage.getItem(key);
    return value === null ? fallback : value.trim();
  } catch {
    return fallback;
  }
}
export function storeToken(key: string, value: string) {
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
export const AMOUNT_MAX = {
  mass_kg: 1_000_000,
  value_usd: 1_000_000_000_000,
} as const;
// Number inputs: empty/NaN/Infinity/negative become 0 (rejected by the
// server as "must be > 0"), values above the server cap are clamped.
export const toAmount = (raw: string, max: number) => {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.min(n, max) : 0;
};
export const post = (body?: unknown): RequestInit => ({
  method: "POST",
  ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
});
export function initialPayload(d: Detail): Payload {
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
