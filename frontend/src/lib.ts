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
  auto_accepted: "Одобрена автоматически",
  accepted: "Принята",
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
  aktau: "Морской порт Актау",
  turkmenbashi: "Морской порт Туркменбаши",
  amirabad: "Порт Амирабад",
};
// Short names for tight places: map labels, shipment cards, timeline rows.
export const portShort: Record<string, string> = {
  rail_hub: "Астрахань",
  makhachkala: "Махачкала",
  olya: "Оля",
  anzali: "Энзели",
  tehran: "Тегеран",
  aktau: "Актау",
  turkmenbashi: "Туркменбаши",
  amirabad: "Амирабад",
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
  "High sea state": "Сильное волнение",
  "Moderate sea state": "Умеренное волнение",
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
  "Readiness after demo cutoff": "Готовность после дедлайна",
  "Readiness within demo cutoff": "Готовность до дедлайна",
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
  "Shipments handled in this window":
    "В окне обрабатываются перевозки",
  "Shipment held: route unavailable":
    "Груз задержан: маршрут недоступен",
  "Backlog carried over": "Накопленная очередь обработки",
  rail_departure: "Отправление по железной дороге",
  port_arrival: "Прибытие в порт",
  sea_departure: "Морское отправление",
  road_departure: "Отправление автотранспортом",
  delivery: "Доставка",
  // Document validation issue messages (preclearance._validate).
  "Field must not be blank": "Поле не должно быть пустым",
  "Use a two-letter country code": "Укажите двухбуквенный код страны",
  "Use 6–10 digits": "Укажите от 6 до 10 цифр",
  "Missing invoice": "Нет инвойса",
  "Missing packing_list": "Нет упаковочного листа",
  "Missing transport": "Нет транспортного документа",
  "Duplicate invoice": "Инвойс указан дважды",
  "Duplicate packing_list": "Упаковочный лист указан дважды",
  "Duplicate transport": "Транспортный документ указан дважды",
  "Container differs from shipment": "Контейнер не совпадает с перевозкой",
  "Container IDs must match": "Номера контейнеров должны совпадать",
  "All masses must be within 1 kg": "Все массы должны совпадать с точностью до 1 кг",
  // Smart-contract rule results (preclearance._RULES).
  OK: "Выполнено",
  "Documents are inconsistent": "Документы не согласованы",
  "HS code requires manual control": "Код ТН ВЭД требует ручного контроля",
  "Value per kg is outside the plausible band":
    "Стоимость за кг вне допустимого диапазона",
  "Shipper is not a trusted trader": "Отправитель не в списке доверенных",
  "Origin country is not on the allowed list":
    "Страна происхождения не в списке разрешённых",
  // 403/404/409 details the API returns (shown in error toasts).
  "Valid bearer token required": "Нужен действующий токен доступа",
  "Broker role required": "Требуется роль логиста",
  "broker role required": "Требуется роль логиста",
  "reviewer role required": "Требуется роль проверяющего",
  "Shipment not found": "Перевозка не найдена",
  "Not found": "Не найдено",
  "Route is no longer feasible; refresh alternatives":
    "Маршрут больше недоступен; обновите альтернативы",
  "Submitted declaration must be reviewed before editing":
    "Отправленную декларацию нельзя менять до решения проверяющего",
  "Save a draft before validating": "Сначала сохраните черновик",
  "Declaration must be ready": "Декларация должна быть готова к отправке",
  "Idempotency key conflicts with revised declaration":
    "Ключ идемпотентности относится к прежней версии декларации",
  "Idempotency key is required": "Нужен ключ идемпотентности",
  "Idempotency key is too long": "Слишком длинный ключ идемпотентности",
  "Only submitted declarations can be reviewed":
    "Рассмотреть можно только отправленную декларацию",
  "Invalid review decision": "Неверное решение проверки",
  "Invalid review comment": "Неверный комментарий проверки",
  "Review comment is too long": "Слишком длинный комментарий",
  "Declaration payload is too large": "Слишком большая декларация",
  "Declaration payload contains invalid text":
    "Декларация содержит недопустимый текст",
  "Revision limit reached for this declaration":
    "Достигнут предел числа версий декларации",
  "Audit limit reached for this declaration":
    "Достигнут предел записей журнала декларации",
  "Stored declaration is unreadable": "Сохранённая декларация повреждена",
  "Stored audit trail is unreadable": "Журнал действий повреждён",
  "Stored submission record is unreadable":
    "Запись об отправке повреждена",
  // Disclaimers returned by the API.
  "Held-out synthetic history; not validated on real Caspian operations.":
    "Отложенная синтетическая выборка; не проверено на реальных каспийских перевозках.",
  "Hash-chain integrity only; not proof of authenticity or customs approval.":
    "Проверяется только целостность хеш-цепочки; это не подтверждение подлинности и не таможенное решение.",
  "Illustrative transport-only estimate; not ISO/GLEC certified.":
    "Иллюстративная оценка только для перевозки; не сертифицирована по ISO/GLEC.",
  "Synthetic forecast: seeded background load and modelled capacities; not real port statistics.":
    "Синтетический прогноз: сгенерированная фоновая загрузка и расчётные мощности; не реальная статистика портов.",
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
    return clip(translate(serverText));
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
  broker: "dev-broker-token",
  reviewer: "dev-reviewer-token",
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
// A new declaration starts pre-filled with illustrative values (a trusted
// shipper, consistent documents), so no typing is needed to try the flow.
export function initialPayload(d: Detail): Payload {
  const numbers: Record<string, string> = {
    invoice: "INV",
    packing_list: "PL",
    transport: "CMR",
  };
  return {
    shipper: "ООО «Волга Экспорт»",
    consignee: "Caspian Trade Co.",
    description: "General machine parts",
    mass_kg: d.shipment.mass_kg,
    value_usd: 45000,
    origin_country: "RU",
    hs_code: "847990",
    container_id: d.shipment.container_id,
    documents: Object.keys(docs).map((kind) => ({
      kind,
      number: `${lookup(numbers, kind) ?? "DOC"}-${d.shipment.id}`,
      container_id: d.shipment.container_id,
      mass_kg: d.shipment.mass_kg,
    })),
  };
}

// Smart-contract rule labels (rule ids come from GET /api/preclearance/ruleset).
export const ruleLabels: Record<string, string> = {
  R1: "Документы согласованы с декларацией",
  R2: "Код ТН ВЭД вне списка ручного контроля",
  R3: "Стоимость за кг в допустимом диапазоне",
  R4: "Отправитель в списке доверенных участников",
  R5: "Страна происхождения в списке разрешённых",
};
export const shortHash = (hash: string) => hash.slice(0, 12);

// Integration review: remaining backend strings shown in the UI (event
// sources, validation issues, 404/409 error details, model disclaimer, audit
// actions). Kept in one block, merged into `translations`.
Object.assign(translations, {
  synthetic: "синтетика",
  "synthetic-model": "модель",
  "open-meteo": "Open-Meteo",
  cache: "кэш",
  observed: "факт",
  forecast: "прогноз",
  save: "Сохранение черновика",
  validate: "Проверка",
  submit: "Отправка",
  review: "Решение проверяющего",
  "Held-out synthetic history; not validated on real Caspian operations.":
    "Отложенная часть синтетической истории; не проверено на реальных перевозках по Каспию.",
  "Field must not be blank": "Поле не должно быть пустым",
  "Use a two-letter country code": "Укажите двухбуквенный код страны",
  "Use 6–10 digits": "Укажите от 6 до 10 цифр",
  "Missing invoice": "Нет инвойса",
  "Missing packing_list": "Нет упаковочного листа",
  "Missing transport": "Нет транспортного документа",
  "Duplicate invoice": "Инвойс указан дважды",
  "Duplicate packing_list": "Упаковочный лист указан дважды",
  "Duplicate transport": "Транспортный документ указан дважды",
  "Container differs from shipment": "Контейнер не совпадает с перевозкой",
  "Container IDs must match": "Номера контейнеров должны совпадать",
  "All masses must be within 1 kg": "Массы должны совпадать с точностью до 1 кг",
  "Revision limit reached for this declaration":
    "Достигнут лимит версий декларации.",
  "Submitted declaration must be reviewed before editing":
    "Отправленную декларацию можно изменить только после решения проверяющего.",
  "Save a draft before validating": "Сохраните черновик перед проверкой.",
  "Declaration payload contains invalid text":
    "Декларация содержит недопустимые символы.",
  "Declaration payload is too large": "Декларация слишком большая.",
  "Idempotency key is required": "Не передан ключ повторной отправки.",
  "Idempotency key is too long": "Ключ повторной отправки слишком длинный.",
  "Idempotency key conflicts with revised declaration":
    "Ключ повторной отправки относится к другой версии декларации.",
  "Declaration must be ready": "Отправить можно только проверенную декларацию.",
  "Invalid review decision": "Недопустимое решение.",
  "Invalid review comment": "Недопустимый комментарий.",
  "Review comment is too long": "Комментарий слишком длинный.",
  "Only submitted declarations can be reviewed":
    "Рассмотреть можно только отправленную декларацию.",
  "Audit limit reached for this declaration":
    "Достигнут лимит записей журнала.",
  "Stored declaration is unreadable": "Сохранённая декларация повреждена.",
  "Stored audit trail is unreadable": "Журнал действий повреждён.",
  "Stored submission record is unreadable": "Запись об отправке повреждена.",
  "Not found": "Не найдено.",
  "Declaration not found": "Декларация не найдена.",
  "Shipment not found": "Перевозка не найдена.",
  "Route is no longer feasible; refresh alternatives":
    "Маршрут больше недоступен; обновите список альтернатив.",
});
const fieldLabels: Record<string, string> = {
  shipper: "Отправитель",
  consignee: "Получатель",
  description: "Описание груза",
  container_id: "Номер контейнера",
  mass_kg: "Масса",
  value_usd: "Стоимость",
  origin_country: "Страна происхождения",
  hs_code: "Код ТН ВЭД",
  documents: "Документы",
};
const docFieldLabels: Record<string, string> = {
  number: "номер",
  container_id: "контейнер",
  mass_kg: "масса",
};
// "documents.1.number" -> "Упаковочный лист: номер".
export function fieldLabel(field: string, documents: { kind: string }[] = []) {
  const m = /^documents\.(\d+)\.(\w+)$/.exec(field);
  if (m) {
    const kind = documents[Number(m[1])]?.kind;
    const doc = lookup(docs, kind) ?? `Документ ${Number(m[1]) + 1}`;
    return `${doc}: ${lookup(docFieldLabels, m[2]) ?? m[2]}`;
  }
  return lookup(fieldLabels, field) ?? field;
}
export const countries: Record<string, string> = {
  RU: "Россия",
  KZ: "Казахстан",
  TM: "Туркменистан",
  IR: "Иран",
  AZ: "Азербайджан",
  UZ: "Узбекистан",
};
// Witness-ledger nodes: names, short chip status and detailed tooltip state.
export const ledgerNodeLabels: Record<string, string> = {
  "witness-1": "Таможня",
  "witness-2": "Порт",
  "witness-3": "Перевозчик",
};
export const ledgerChipLabels: Record<string, string> = {
  in_sync: "подписал",
  behind: "отстаёт",
  diverged: "расхождение",
  invalid_signature: "расхождение",
  unavailable: "недоступен",
};
export const ledgerStateLabels: Record<string, string> = {
  in_sync: "подписал все записи",
  behind: "отстаёт: подписал не все записи",
  diverged: "расхождение: хеши не совпадают с журналом",
  invalid_signature: "расхождение: подпись или ответ узла не прошли проверку",
  unavailable: "узел недоступен",
};
export const ledgerErrorLabels: Record<string, string> = {
  timeout: "нет ответа (таймаут)",
  unreachable: "нет соединения",
  rejected: "узел отклонил ключ",
  bad_reply: "ответ узла изменён или повреждён",
};
export const ledgerStatusNotes: Record<string, string> = {
  not_anchored: "последние записи не закреплены",
  conflict: "локальная цепочка расходится с реестром",
  no_quorum: "кворум недоступен",
  disabled: "реестр отключён",
};
