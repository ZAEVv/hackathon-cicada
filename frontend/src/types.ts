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
    source: string;
    observed_at: string;
    stale: boolean;
  };
  scenario: { kind: string; wind_mps?: number | null };
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
