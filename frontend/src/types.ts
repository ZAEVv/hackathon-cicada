export type Port = {
  id: string;
  name: string;
  country: string;
  lat: number;
  lon: number;
  kind: string;
};
export type Leg = {
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
export type Route = {
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
export type Event = {
  id: string;
  event_type: string;
  location_id: string;
  planned_at: string | null;
  estimated_at: string | null;
  actual_at: string | null;
  source: string;
  quality_flag: string;
};
export type Doc = {
  kind: string;
  number: string;
  container_id: string;
  mass_kg: number;
};
export type Payload = {
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
export type Declaration = {
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
  clearance?: Clearance | null;
};
export type RuleResult = { rule_id: string; passed: boolean; detail: string };
export type Clearance = {
  ruleset_id: string;
  ruleset_hash: string;
  decision: "auto_accepted" | "manual_review";
  results: RuleResult[];
};
export type Ruleset = {
  id: string;
  ruleset_hash: string;
  rules: { id: string; description: string; parameters: Record<string, unknown> }[];
};
export type Detail = {
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
    wave_height_m?: number | null;
    source: string;
    observed_at: string;
    stale: boolean;
    fallback?: boolean;
    wave_estimated?: boolean;
  };
  scenario: {
    kind: string;
    wind_mps?: number | null;
    wave_height_m?: number | null;
  };
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
export type Dashboard = {
  simulation_now: string;
  synthetic: boolean;
  shipments: Detail[];
  summary: Record<string, number>;
};
export type Metrics = {
  model_version: string;
  train_rows: number;
  test_rows: number;
  split_at: string;
  baseline_mae_hours: number;
  model_mae_hours: number;
  roc_auc: number | null;
  disclaimer: string;
};
export type Audit = {
  sequence: number;
  action: string;
  actor: string;
  at: string;
  hash: string;
  previous_hash: string;
  payload: Record<string, unknown>;
};
export type Verification = {
  valid: boolean;
  entries_checked: number;
  first_invalid_sequence: number | null;
  disclaimer: string;
};
export type HubPoint = {
  at: string;
  offset_hours: number;
  utilization: number;
  load_tph: number;
  capacity_tph: number;
  background_tph: number;
  shipments_tph: number;
  queue_hours: number;
  shipment_ids: string[];
};
export type HubSeries = {
  port_id: string;
  nominal_capacity_tph: number;
  peak_utilization: number;
  peak_at: string;
  max_queue_hours: number;
  status: string;
  bottleneck_count: number;
  points: HubPoint[];
};
export type BottleneckWindow = {
  port_id: string;
  start_at: string;
  end_at: string;
  start_offset_hours: number;
  end_offset_hours: number;
  peak_at: string;
  peak_utilization: number;
  max_queue_hours: number;
  severity: string;
  reasons: string[];
  shipment_ids: string[];
};
export type HubForecast = {
  simulation_now: string;
  synthetic: boolean;
  horizon_hours: number;
  step_hours: number;
  bottleneck_threshold: number;
  busy_threshold: number;
  regional_scenario: string;
  focus_shipment_id: string | null;
  hubs: HubSeries[];
  bottlenecks: BottleneckWindow[];
  background_source: string;
  disclaimer: string;
};

export type SummaryKpi = {
  value: number;
  previous: number | null;
  delta: number | null;
  delta_pct: number | null;
};
export type CorridorSummary = {
  simulation_now: string;
  synthetic: boolean;
  weeks: 4 | 13;
  period_start: string;
  period_end: string;
  weekly: { week_start: string; shipments: number; tonnes: number }[];
  kpis: {
    shipments: SummaryKpi;
    on_time_pct: SummaryKpi;
    avg_delay_hours: SummaryKpi;
    co2e_tonnes: SummaryKpi;
  };
  mode_split: {
    mode: "sea" | "road" | "rail";
    tonne_km: number;
    share_pct: number;
  }[];
  recent_shipments: {
    id: string;
    origin_id: string;
    origin_name: string;
    destination_id: string;
    destination_name: string;
    mass_tonnes: number;
    status: "at_risk" | "docs_pending" | "attention" | "in_transit";
  }[];
  co2e_intensity_kg_per_tonne: number;
  methodology: string;
  disclaimer: string;
};

// Witness ledger (GET /api/shipments/{id}/ledger); nodes are network services or in-process.
export type LedgerNodeState =
  | "in_sync"
  | "behind"
  | "diverged"
  | "invalid_signature"
  | "unavailable";
export type LedgerNode = {
  node_id: string;
  label: string;
  available: boolean;
  last_sequence: number | null;
  head_hash: string | null;
  signature_valid: boolean | null;
  in_sync: boolean;
  state: LedgerNodeState;
  error?: "timeout" | "unreachable" | "rejected" | "bad_reply" | null;
};
export type LedgerStatus = {
  shipment_id: string;
  status: "anchored" | "not_anchored" | "conflict" | "no_quorum" | "disabled";
  reason: string | null;
  anchored: boolean;
  quorum: number;
  nodes_total: number;
  nodes_available: number;
  nodes_in_sync: number;
  attested_sequence: number;
  local_sequence: number;
  mode?: "in_process" | "network";
  nodes: LedgerNode[];
  disclaimer: string;
};
