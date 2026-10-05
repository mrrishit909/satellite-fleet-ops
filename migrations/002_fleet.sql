-- Domain model. Names follow the blueprint's schema; additions are marked (+). Orbits are Keplerian elements, not TLEs.

CREATE TABLE fleets (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants UNIQUE, name text NOT NULL, model_seed int NOT NULL, epoch timestamptz NOT NULL, now_min int NOT NULL);   -- (+) the record's start and clock
SELECT enable_tenant_rls('fleets');

CREATE TABLE spacecraft (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, fleet_id uuid NOT NULL REFERENCES fleets ON DELETE CASCADE, ordinal int NOT NULL, norad_id bigint NOT NULL, name text NOT NULL, mission text NOT NULL, status text NOT NULL,
  battery_wh real NOT NULL, UNIQUE (fleet_id, ordinal));
SELECT enable_tenant_rls('spacecraft');

CREATE TABLE orbits (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, spacecraft_id uuid NOT NULL REFERENCES spacecraft ON DELETE CASCADE, epoch timestamptz NOT NULL, alt_km real NOT NULL, inc_deg real NOT NULL, raan_deg real NOT NULL, phase_deg real NOT NULL,
  source text NOT NULL);   -- (+) stands in for tle_sets
SELECT enable_tenant_rls('orbits');

CREATE TABLE ground_stations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, fleet_id uuid NOT NULL REFERENCES fleets ON DELETE CASCADE, ordinal int NOT NULL, name text NOT NULL, lat numeric NOT NULL, lon numeric NOT NULL, alt_m numeric NOT NULL,
  antennas jsonb NOT NULL);
SELECT enable_tenant_rls('ground_stations');

CREATE TABLE passes (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, spacecraft_id uuid NOT NULL REFERENCES spacecraft ON DELETE CASCADE, station_id uuid NOT NULL REFERENCES ground_stations, ordinal int NOT NULL, aos timestamptz NOT NULL, los timestamptz NOT NULL,
  max_el_deg numeric NOT NULL, quality numeric NOT NULL, aos_min real NOT NULL, los_min real NOT NULL, gb real NOT NULL, scheduled boolean NOT NULL DEFAULT false);   -- (+) minutes, capacity, whether the current plan uses it
SELECT enable_tenant_rls('passes');

CREATE TABLE telemetry (spacecraft_id uuid NOT NULL REFERENCES spacecraft ON DELETE CASCADE, ts timestamptz NOT NULL, subsystem text NOT NULL, metric text NOT NULL, value double precision NOT NULL, unit text NOT NULL, quality smallint NOT NULL DEFAULT 1,
  tenant_id uuid NOT NULL REFERENCES tenants, minute int NOT NULL, PRIMARY KEY (spacecraft_id, ts, subsystem, metric));
SELECT enable_tenant_rls('telemetry');

CREATE TABLE anomalies (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, spacecraft_id uuid NOT NULL REFERENCES spacecraft ON DELETE CASCADE, metric text NOT NULL, since timestamptz NOT NULL, since_min int NOT NULL, z_now real NOT NULL, direction text NOT NULL,
  model_version text NOT NULL, detected_at timestamptz NOT NULL DEFAULT now(), UNIQUE (spacecraft_id, metric));   -- (+)
SELECT enable_tenant_rls('anomalies');

CREATE TABLE downlink_requests (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, spacecraft_id uuid NOT NULL UNIQUE REFERENCES spacecraft ON DELETE CASCADE, data_gb real NOT NULL, priority int NOT NULL CHECK (priority BETWEEN 1 AND 3), deadline timestamptz NOT NULL,
  deadline_min int NOT NULL);   -- (+) the payload request queue
SELECT enable_tenant_rls('downlink_requests');

CREATE TABLE contact_plans (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, fleet_id uuid NOT NULL REFERENCES fleets ON DELETE CASCADE, horizon_h int NOT NULL, summary jsonb NOT NULL, pass_ordinals int[] NOT NULL, created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());   -- (+)
SELECT enable_tenant_rls('contact_plans');

CREATE TABLE conjunctions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, spacecraft_id uuid NOT NULL REFERENCES spacecraft ON DELETE CASCADE, object_id text NOT NULL, tca timestamptz NOT NULL, miss_distance_m numeric NOT NULL, probability numeric NOT NULL,
  data jsonb NOT NULL, tca_min int NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
SELECT enable_tenant_rls('conjunctions');

CREATE TABLE commands (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, spacecraft_id uuid NOT NULL REFERENCES spacecraft ON DELETE CASCADE, command_type text NOT NULL CHECK (command_type IN ('payload_delay','maneuver')), args jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved_once','released','rejected')), approved_by uuid, second_approved_by uuid, scheduled_for timestamptz NOT NULL, checksum text NOT NULL,
  created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), released_at timestamptz, uplink_pass int);   -- (+) second approver, creator, the pass that carries the uplink
SELECT enable_tenant_rls('commands');
