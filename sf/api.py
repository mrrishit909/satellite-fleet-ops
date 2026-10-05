"""Public API (modular monolith). Blueprint services map to: orbit-service (passes), telemetry-ingestion, spacecraft-state,
anomaly-service, contact-planner, conjunction-service, command-service (dual approval) and a scenario endpoint for a manoeuvre.
Not built: SGP4/TLEs, CCSDS packets, a mission scheduler for payload jobs, the uplink itself.

    uvicorn sf.api:app          python -m core.jobs sf.api      # the worker
"""
import datetime
import hashlib
import json
import uuid
from typing import Literal

import numpy as np
from fastapi import Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"fleet:read", "jobs:read"}
OPERATE = READ | {"telemetry:write", "planning:run", "conjunctions:write", "scenarios:run", "commands:create"}
PERMISSIONS = {"viewer": READ, "operator": OPERATE, "engineer": READ | {"commands:approve"}, "director": OPERATE | {"commands:approve", "fleet:load", "audit:read"}}
app = create_app("satellite-fleet-ops", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
EPOCH = datetime.datetime(2026, 10, 5, tzinfo=datetime.UTC)
LOADED_TO = world.NOW - 10          # telemetry in the record stops here; the demo delivers the last ten minutes as packets
SUBSYSTEM = {"battery_voltage_v": "eps", "battery_temp_c": "eps", "soc": "eps", "solar_current_a": "eps", "bus_current_a": "eps", "panel_temp_c": "thermal", "obc_temp_c": "obc", "wheel_speed_rpm": "adcs"}
UNIT = {"battery_voltage_v": "V", "battery_temp_c": "degC", "soc": "ratio", "solar_current_a": "A", "bus_current_a": "A", "panel_temp_c": "degC", "obc_temp_c": "degC", "wheel_speed_rpm": "rpm"}


def ts(minute):
    return EPOCH + datetime.timedelta(minutes=float(minute))


def minute(t):
    return (t - EPOCH).total_seconds() / 60


def sid(t, k):
    return uuid.uuid5(t, f"sat-{k}")


def gid(t, k):
    return uuid.uuid5(t, f"station-{k}")


def worker_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def fleet(c):
    f = c.execute("SELECT * FROM fleets").fetchone()
    if not f:
        raise Problem(409, "no_fleet", "load a fleet first: POST /v1/fleet:load")
    return f, world.build(f["model_seed"]), world.passes(f["model_seed"])


def craft(c, spacecraft_id):
    s = c.execute("SELECT * FROM spacecraft WHERE id = %s", [spacecraft_id]).fetchone()
    if not s:
        raise Problem(404, "spacecraft_not_found")
    return s


def series(c, spacecraft_id):
    """The stored housekeeping record of one spacecraft, metric -> array by minute (NaN where missing)."""
    rows = c.execute("SELECT metric, minute, value FROM telemetry WHERE spacecraft_id = %s", [spacecraft_id]).fetchall()
    n = max(r["minute"] for r in rows) + 1
    out = {m: np.full(n, np.nan) for m in world.METRICS}
    for r in rows:
        out[r["metric"]][r["minute"]] = r["value"]
    return out


def requests(c):
    return {r["ordinal"]: {"data_gb": r["data_gb"], "priority": r["priority"], "deadline": r["deadline_min"], "id": r["spacecraft_id"], "name": r["name"]}
            for r in c.execute("SELECT s.ordinal, s.name, r.* FROM downlink_requests r JOIN spacecraft s ON s.id = r.spacecraft_id ORDER BY s.ordinal")}


def scheduled(c, k):
    return [(p["aos_min"], p["los_min"]) for p in c.execute("SELECT aos_min, los_min FROM passes p JOIN spacecraft s ON s.id = p.spacecraft_id WHERE s.ordinal = %s AND p.scheduled ORDER BY aos_min", [k])]


def payload_plan(c, t, sat):
    """The standing payload plan less whatever a released payload_delay command has moved."""
    ops = world.default_ops(sat)
    for cmd in c.execute("SELECT args FROM commands WHERE spacecraft_id = %s AND command_type = 'payload_delay' AND status = 'released'", [sid(t, sat["index"])]):
        a = cmd["args"]
        ops = [(s, e) for s, e in ops if s != a["start_min"]] + [(a["start_min"] + a["delay_min"], a["end_min"] + a["delay_min"])]
    return ops


# ---------------------------------------------------------------- fleet and orbit-service

class LoadIn(BaseModel):
    seed: int = Field(17, ge=0, le=10 ** 6)


@app.post("/v1/fleet:load", status_code=202, tags=["fleet"], summary="(+) Load a synthetic 12-spacecraft fleet: orbits, four stations, 48 h of passes, 18 h of housekeeping telemetry, the downlink request queue")
def load_fleet(body: LoadIn, ctx: Ctx = Depends(auth("fleet:load")), idem: str | None = IdemKey):
    def work(c):
        if c.execute("SELECT 1 FROM fleets LIMIT 1").fetchone():
            raise Problem(409, "fleet_exists", "this tenant already has a fleet")
        return 202, jobs.enqueue(c, ctx, "fleet.load", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("fleet.load")
def load_job(c, job):
    t, w, ps = job["tenant_id"], world.build(job["payload"]["seed"]), world.passes(job["payload"]["seed"])
    fid = uuid.uuid5(t, "fleet")
    c.execute("INSERT INTO fleets (id, tenant_id, name, model_seed, epoch, now_min) VALUES (%s,%s,'Riverbend Orbital constellation',%s,%s,%s)", [fid, t, w["seed"], EPOCH, world.NOW])
    db.load(c, "spacecraft", ["id", "tenant_id", "fleet_id", "ordinal", "norad_id", "name", "mission", "status", "battery_wh"], [(sid(t, s["index"]), t, fid, s["index"], s["norad_id"], s["name"], "earth observation", "nominal", s["battery_wh"]) for s in w["sats"]])
    db.load(c, "orbits", ["id", "tenant_id", "spacecraft_id", "epoch", "alt_km", "inc_deg", "raan_deg", "phase_deg", "source"], [(uuid.uuid7(), t, sid(t, s["index"]), EPOCH, s["alt_km"], s["inc_deg"], s["raan_deg"], s["phase_deg"], "synthetic elements") for s in w["sats"]])
    db.load(c, "ground_stations", ["id", "tenant_id", "fleet_id", "ordinal", "name", "lat", "lon", "alt_m", "antennas"], [(gid(t, g["index"]), t, fid, g["index"], g["name"], g["lat"], g["lon"], g["alt_m"], Jsonb([{"band": "X", "diameter_m": 3.7}])) for g in w["stations"]])
    db.load(c, "passes", ["id", "tenant_id", "spacecraft_id", "station_id", "ordinal", "aos", "los", "max_el_deg", "quality", "aos_min", "los_min", "gb"],
            [(uuid.uuid5(t, f"pass-{p['index']}"), t, sid(t, p["sat"]), gid(t, p["station"]), p["index"], ts(p["aos"]), ts(p["los"]), p["max_el"], round(min(1.0, p["max_el"] / 60), 2), p["aos"], p["los"], p["gb"]) for p in ps])
    db.load(c, "downlink_requests", ["id", "tenant_id", "spacecraft_id", "data_gb", "priority", "deadline", "deadline_min"], [(uuid.uuid7(), t, sid(t, s["index"]), round(s["data_gb"], 1), s["priority"], ts(s["deadline"]), s["deadline"]) for s in w["sats"]])

    def rows():
        for s in w["sats"]:
            tm = world.telemetry(w, s, payload_ops=world.default_ops(s), anomaly=world.ANOMALY if s["index"] == world.ANOMALY["sat"] else None, upto=LOADED_TO)
            for m in world.METRICS:
                for k in range(LOADED_TO):
                    yield sid(t, s["index"]), ts(k), SUBSYSTEM[m], m, round(float(tm[m][k]), 4), UNIT[m], t, k
    db.load(c, "telemetry", ["spacecraft_id", "ts", "subsystem", "metric", "value", "unit", "tenant_id", "minute"], rows())
    out = {"fleet_id": fid, "clock": ts(world.NOW), "spacecraft": [{"spacecraft_id": sid(t, s["index"]), "name": s["name"], "alt_km": round(s["alt_km"]), "inc_deg": s["inc_deg"]} for s in w["sats"]], "stations": [g["name"] for g in w["stations"]],
           "passes_48h": len(ps), "passes_ahead": sum(p["aos"] >= world.NOW for p in ps), "telemetry_rows": LOADED_TO * len(world.METRICS) * len(w["sats"]), "telemetry_minutes": LOADED_TO, "requests_gb": round(sum(s["data_gb"] for s in w["sats"]), 1),
           "capacity_ahead_gb": round(sum(p["gb"] for p in ps if p["aos"] >= world.NOW), 1)}
    audit.record(c, worker_ctx(job), "fleet.loaded", "fleet", fid, {"spacecraft": len(w["sats"]), "passes": len(ps), "telemetry_rows": out["telemetry_rows"]})
    return out


@app.get("/v1/spacecraft/{spacecraft_id}/state", tags=["spacecraft"], summary="Latest housekeeping values, battery state, open anomalies, the next passes and the downlink request")
def state(spacecraft_id: uuid.UUID, ctx: Ctx = Depends(auth("fleet:read"))):
    with db.tx(ctx.tenant_id) as c:
        f, w, ps = fleet(c)
        s = craft(c, spacecraft_id)
        latest = c.execute("SELECT DISTINCT ON (metric) metric, value, unit, minute FROM telemetry WHERE spacecraft_id = %s ORDER BY metric, minute DESC", [spacecraft_id]).fetchall()
        anomalies = c.execute("SELECT metric, since, since_min, z_now, direction, detected_at FROM anomalies WHERE spacecraft_id = %s ORDER BY since", [spacecraft_id]).fetchall()
        nxt = c.execute("SELECT g.name AS station, p.aos, p.los, p.max_el_deg, p.gb, p.scheduled FROM passes p JOIN ground_stations g ON g.id = p.station_id WHERE p.spacecraft_id = %s AND p.aos_min >= %s ORDER BY p.aos_min LIMIT 4", [spacecraft_id, f["now_min"]]).fetchall()
        rq = c.execute("SELECT data_gb, priority, deadline FROM downlink_requests WHERE spacecraft_id = %s", [spacecraft_id]).fetchone()
        orbit = c.execute("SELECT alt_km, inc_deg, raan_deg, source FROM orbits WHERE spacecraft_id = %s ORDER BY epoch DESC LIMIT 1", [spacecraft_id]).fetchone()
    sat = w["sats"][s["ordinal"]]
    r = world.positions(sat, np.array([f["now_min"] * 60.0]))
    return jsonable_encoder({"spacecraft_id": spacecraft_id, "name": s["name"], "status": "anomaly" if anomalies else s["status"], "clock": ts(f["now_min"]), "orbit": orbit, "in_sun_now": bool(world.in_sun(r, w["sun"])[0]), "position_eci_km": [round(float(v)) for v in r[0]],
                             "telemetry": latest, "soc": next((x["value"] for x in latest if x["metric"] == "soc"), None), "anomalies": anomalies, "next_passes": nxt, "downlink_request": rq, "payload_ops_ahead": sum(a >= f["now_min"] for a, _ in world.default_ops(sat)),
                             "propagation": "circular two-body orbit from stored elements; not SGP4"})


# ---------------------------------------------------------------- telemetry-ingestion and anomaly-service

class Packet(BaseModel):
    ts: datetime.datetime
    subsystem: str = Field(max_length=16)
    metric: Literal[tuple(world.METRICS)]
    value: float
    unit: str = Field(max_length=8)


class PacketsIn(BaseModel):
    spacecraft_id: uuid.UUID
    packets: list[Packet] = Field(min_length=1, max_length=5000)


@app.post("/v1/telemetry/packets", tags=["telemetry"], summary="Housekeeping packets for one spacecraft (a resend is harmless), then anomaly detection over its whole record")
def packets(body: PacketsIn, ctx: Ctx = Depends(auth("telemetry:write")), idem: str | None = IdemKey):
    def work(c):
        f, w, _ = fleet(c)
        s = craft(c, body.spacecraft_id)
        if any(p.ts.tzinfo is None for p in body.packets):
            raise Problem(422, "timezone_required", "timestamps must carry a UTC offset")
        if any(not 0 <= minute(p.ts) <= f["now_min"] for p in body.packets):
            raise Problem(422, "out_of_window", "packets must be timestamped within the record, up to the fleet clock")
        rows = [(s["id"], p.ts.replace(second=0, microsecond=0), p.subsystem, p.metric, p.value, p.unit, ctx.tenant_id, int(minute(p.ts))) for p in body.packets]
        stored = sum(bool(c.execute("INSERT INTO telemetry (spacecraft_id, ts, subsystem, metric, value, unit, tenant_id, minute) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING minute", r).fetchone()) for r in rows)
        tm = series(c, s["id"])
        full = {m: np.nan_to_num(v, nan=np.nanmedian(v)) for m, v in tm.items()}
        period = world.period_min(w["sats"][s["ordinal"]])
        found = engine.detect(full, period)
        c.execute("DELETE FROM anomalies WHERE spacecraft_id = %s", [s["id"]])
        for a in found:
            c.execute("INSERT INTO anomalies (id, tenant_id, spacecraft_id, metric, since, since_min, z_now, direction, model_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [uuid.uuid7(), ctx.tenant_id, s["id"], a["metric"], ts(a["since_min"]), a["since_min"], a["z_now"], a["direction"], engine.VERSION])
        c.execute("UPDATE spacecraft SET status = %s WHERE id = %s", ["anomaly" if found else "nominal", s["id"]])
        if found:
            audit.record(c, ctx, "anomaly.detected", "spacecraft", s["id"], {"spacecraft": s["name"], "metrics": [a["metric"] for a in found]})
        truth = world.ANOMALY if s["ordinal"] == world.ANOMALY["sat"] else None
        metric = found[0]["metric"] if found else "battery_temp_c"
        bins, _ = engine.normal(full[metric], period)
        n = len(full[metric])
        ph = engine.phases(n, period)
        trace = {"metric": metric, "from_min": max(0, n - 8 * 60), "step_min": 5, "value": [round(float(v), 3) for v in full[metric][max(0, n - 8 * 60)::5]], "normal": [round(float(bins[ph[m]]), 3) for m in range(max(0, n - 8 * 60), n, 5)]}
        return 200, {"spacecraft": s["name"], "received": len(rows), "stored": stored, "duplicates_ignored": len(rows) - stored, "record_minutes": len(tm["soc"]), "anomalies": [{**a, "since": ts(a["since_min"])} for a in found], "trace": trace,
                     "fixed_limits": {"rule": "red limits per metric", "alarms": engine.limit_alarms(full)}, "isolation_forest": engine.forest(full), "model": engine.VERSION,
                     "how": "each metric against its own orbit-phase normal from the first 12 hours; anomalous when 4 noise units off for 10 minutes running",
                     "simulation_truth": {"metric": truth["metric"], "onset": ts(truth["onset"]), "per_hour": truth["per_hour"]} if truth else None}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- contact-planner

class PlanIn(BaseModel):
    horizon_h: int = Field(24, ge=2, le=30)


@app.post("/v1/planning/contacts", status_code=201, tags=["planning"], summary="Schedule the passes ahead so that priority-weighted data lands before each deadline, one contact per antenna and per spacecraft at a time; the obvious first-come schedule beside it")
def plan_contacts(body: PlanIn, ctx: Ctx = Depends(auth("planning:run")), idem: str | None = IdemKey):
    def work(c):
        f, w, ps = fleet(c)
        rq, end = requests(c), f["now_min"] + body.horizon_h * 60
        window = [p for p in ps if p["aos"] >= f["now_min"] and p["los"] <= end]
        chosen, info = engine.plan(window, rq, f["now_min"])
        simple = engine.greedy(window, rq, f["now_min"])
        mine, theirs = engine.describe(ps, chosen, rq), engine.describe(ps, simple, rq)
        c.execute("UPDATE passes SET scheduled = false")
        c.execute("UPDATE passes SET scheduled = true WHERE ordinal = ANY(%s)", [chosen])
        pid = uuid.uuid7()
        summary = {"horizon_h": body.horizon_h, "passes_in_window": len(window), "solver": info, "optimised": {k: v for k, v in mine.items() if k != "rows"}, "first_come": {k: v for k, v in theirs.items() if k != "rows"}}
        c.execute("INSERT INTO contact_plans (id, tenant_id, fleet_id, horizon_h, summary, pass_ordinals, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)", [pid, ctx.tenant_id, f["id"], body.horizon_h, Jsonb(summary), chosen, ctx.actor_id])
        audit.record(c, ctx, "contacts.planned", "contact_plan", pid, {"passes": len(chosen), "weighted_gb": mine["weighted_gb"], "first_come_weighted_gb": theirs["weighted_gb"]})
        name = {k: v["name"] for k, v in rq.items()}
        conflicts = sum(1 for a in window for b in window if a["index"] < b["index"] and a["station"] == b["station"] and a["aos"] < b["los"] + world.SLEW_MIN and b["aos"] < a["los"] + world.SLEW_MIN)
        return 201, {"plan_id": pid, **summary, "overlapping_pass_pairs": conflicts, "per_spacecraft": [{**r, "name": name[r["sat"]], "first_come_gb": next(x["delivered_gb"] for x in theirs["rows"] if x["sat"] == r["sat"])} for r in mine["rows"]],
                     "schedule": [{"pass": p["index"], "spacecraft": name[p["sat"]], "station": w["stations"][p["station"]]["name"], "aos": ts(p["aos"]), "los": ts(p["los"]), "aos_min": p["aos"], "los_min": p["los"], "gb": p["gb"], "max_el": p["max_el"]} for p in window if p["index"] in set(chosen)],
                     "unused": [{"pass": p["index"], "spacecraft": name[p["sat"]], "station": w["stations"][p["station"]]["name"], "aos_min": p["aos"], "los_min": p["los"]} for p in window if p["index"] not in set(chosen)]}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- conjunction-service

class ConjunctionIn(BaseModel):
    spacecraft_id: uuid.UUID
    object_id: str = Field(max_length=40)
    tca: datetime.datetime
    miss_distance_m: float = Field(gt=0, le=50000)
    sigma_m: float = Field(gt=0, le=5000, description="Combined position uncertainty at closest approach, one standard deviation")
    radius_m: float = Field(5.0, gt=0, le=50, description="Combined hard-body radius")


@app.post("/v1/conjunctions", status_code=201, tags=["conjunctions"], summary="(+) Register a screening message and score it")
def conjunction(body: ConjunctionIn, ctx: Ctx = Depends(auth("conjunctions:write")), idem: str | None = IdemKey):
    def work(c):
        f, w, _ = fleet(c)
        s = craft(c, body.spacecraft_id)
        if body.tca.tzinfo is None:
            raise Problem(422, "timezone_required")
        if minute(body.tca) <= f["now_min"]:
            raise Problem(422, "tca_in_the_past")
        pc = world.conjunction_probability(body.miss_distance_m, body.sigma_m, body.radius_m)
        cid = uuid.uuid7()
        data = {"sigma_m": body.sigma_m, "radius_m": body.radius_m, "method": "2-D Gaussian miss with isotropic combined uncertainty (no covariance from a CDM)"}
        c.execute("INSERT INTO conjunctions (id, tenant_id, spacecraft_id, object_id, tca, miss_distance_m, probability, data, tca_min) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [cid, ctx.tenant_id, s["id"], body.object_id, body.tca, body.miss_distance_m, pc, Jsonb(data), int(minute(body.tca))])
        audit.record(c, ctx, "conjunction.registered", "conjunction", cid, {"spacecraft": s["name"], "object": body.object_id, "probability": pc})
        return 201, {"conjunction_id": cid, "spacecraft": s["name"], "object_id": body.object_id, "tca": body.tca, "hours_to_tca": round((minute(body.tca) - f["now_min"]) / 60, 1), "miss_distance_m": body.miss_distance_m, "probability": pc, "above_action_threshold": pc >= 1e-4, **data}
    return run(ctx, idem, body, work)


@app.get("/v1/conjunctions", tags=["conjunctions"], summary="Close approaches at or above a collision probability")
def conjunctions(risk_min: float = Query(1e-5, ge=0, le=1), ctx: Ctx = Depends(auth("fleet:read"))):
    with db.tx(ctx.tenant_id) as c:
        rows = c.execute("SELECT j.id AS conjunction_id, s.name AS spacecraft, j.spacecraft_id, j.object_id, j.tca, j.miss_distance_m, j.probability, j.data FROM conjunctions j JOIN spacecraft s ON s.id = j.spacecraft_id WHERE j.probability >= %s ORDER BY j.probability DESC", [risk_min]).fetchall()
    return jsonable_encoder({"items": rows, "action_threshold": 1e-4})


# ---------------------------------------------------------------- scenario-service

class ManeuverIn(BaseModel):
    conjunction_id: uuid.UUID
    delta_v_mps: float = Field(0.05, gt=0, le=2)
    burn_at: datetime.datetime
    delay_payload_min: int = Field(0, ge=0, le=24 * 60, description="Move the payload campaign that overlaps the burn window this many minutes later")


@app.post("/v1/scenarios/orbit-maneuver", tags=["scenarios"], summary="What a manoeuvre would do: the miss distance and probability after it, and whether the battery has the margin to burn under the current payload plan or a delayed one")
def maneuver_scenario(body: ManeuverIn, ctx: Ctx = Depends(auth("scenarios:run"))):
    with db.tx(ctx.tenant_id) as c:
        f, w, ps = fleet(c)
        j = c.execute("SELECT * FROM conjunctions WHERE id = %s", [body.conjunction_id]).fetchone()
        if not j:
            raise Problem(404, "conjunction_not_found")
        s = craft(c, j["spacecraft_id"])
        if body.burn_at.tzinfo is None:
            raise Problem(422, "timezone_required")
        burn = int(minute(body.burn_at))
        if not f["now_min"] < burn < j["tca_min"]:
            raise Problem(422, "bad_burn_time", "the burn must lie between now and the time of closest approach")
        soc_now = c.execute("SELECT value FROM telemetry WHERE spacecraft_id = %s AND metric = 'soc' ORDER BY minute DESC LIMIT 1", [s["id"]]).fetchone()["value"]
        ops, dl = payload_plan(c, ctx.tenant_id, w["sats"][s["ordinal"]]), scheduled(c, s["ordinal"])
    sat, now, end = w["sats"][s["ordinal"]], f["now_min"], j["tca_min"] + 60
    overlap = [(a, b) for a, b in ops if a < burn <= b + 180 and a >= now]                        # payload work in the three hours before the burn
    delayed = [(a, b) if (a, b) not in overlap else (a + body.delay_payload_min, b + body.delay_payload_min) for a, b in ops] if body.delay_payload_min else ops
    cur = engine.soc_forecast(w, sat, soc_now, now, end, dl, ops)
    alt = engine.soc_forecast(w, sat, soc_now, now, end, dl, delayed)
    lead = j["tca_min"] - burn
    miss_after = world.shifted_miss(float(j["miss_distance_m"]), body.delta_v_mps, lead, sat["alt_km"])
    pc_after = world.conjunction_probability(miss_after, float(j["data"]["sigma_m"]), float(j["data"]["radius_m"]))
    at = lambda arr: round(float(arr[burn - now]), 3)        # noqa: E731
    return jsonable_encoder({"spacecraft": s["name"], "conjunction_id": j["id"], "object_id": j["object_id"], "tca": j["tca"], "burn_at": ts(burn), "lead_min": lead, "delta_v_mps": body.delta_v_mps,
                             "before": {"miss_distance_m": float(j["miss_distance_m"]), "probability": float(j["probability"])}, "after": {"miss_distance_m": round(miss_after), "probability": pc_after},
                             "margin_rule": f"state of charge at ignition at least {world.SOC_BURN:.0%}", "soc_now": round(soc_now, 3), "payload_work_before_burn": [{"start": ts(a), "end": ts(b), "minutes": b - a} for a, b in overlap],
                             "current_plan": {"soc_at_burn": at(cur), "margin_ok": at(cur) >= world.SOC_BURN, "soc_min_to_tca": round(float(cur.min()), 3)},
                             "with_payload_delayed": {"delay_min": body.delay_payload_min, "soc_at_burn": at(alt), "margin_ok": at(alt) >= world.SOC_BURN, "soc_min_to_tca": round(float(alt.min()), 3)} if body.delay_payload_min else None,
                             "soc_forecast": {"from": ts(now), "step_min": 5, "current_plan": [round(float(v), 3) for v in cur[::5]], "with_payload_delayed": [round(float(v), 3) for v in alt[::5]] if body.delay_payload_min else None}})


# ---------------------------------------------------------------- command-service (dual approval)

class CommandIn(BaseModel):
    spacecraft_id: uuid.UUID
    command_type: Literal["payload_delay", "maneuver"]
    args: dict = Field(description="payload_delay: {start_min, end_min, delay_min}; maneuver: {delta_v_mps, conjunction_id}")
    scheduled_for: datetime.datetime


@app.post("/v1/commands", status_code=201, tags=["commands"], summary="Queue a command. It needs two different approvers before release; a manoeuvre is refused unless the battery margin holds under the plan as it stands")
def command(body: CommandIn, ctx: Ctx = Depends(auth("commands:create")), idem: str | None = IdemKey):
    def work(c):
        f, w, ps = fleet(c)
        s = craft(c, body.spacecraft_id)
        if body.scheduled_for.tzinfo is None:
            raise Problem(422, "timezone_required")
        when = int(minute(body.scheduled_for))
        if when <= f["now_min"]:
            raise Problem(422, "scheduled_in_the_past")
        sat = w["sats"][s["ordinal"]]
        if body.command_type == "payload_delay":
            if not {"start_min", "end_min", "delay_min"} <= set(body.args) or not any(a == body.args["start_min"] for a, _ in world.default_ops(sat)):
                raise Problem(422, "bad_args", "payload_delay needs start_min, end_min, delay_min of an existing payload operation")
        else:
            if not {"delta_v_mps", "conjunction_id"} <= set(body.args):
                raise Problem(422, "bad_args", "maneuver needs delta_v_mps and conjunction_id")
            j = c.execute("SELECT * FROM conjunctions WHERE id = %s", [body.args["conjunction_id"]]).fetchone()
            if not j or j["spacecraft_id"] != s["id"]:
                raise Problem(404, "conjunction_not_found")
            soc_now = c.execute("SELECT value FROM telemetry WHERE spacecraft_id = %s AND metric = 'soc' ORDER BY minute DESC LIMIT 1", [s["id"]]).fetchone()["value"]
            soc = engine.soc_forecast(w, sat, soc_now, f["now_min"], when + 1, scheduled(c, s["ordinal"]), payload_plan(c, ctx.tenant_id, sat))[-1]
            if soc < world.SOC_BURN:                                                       # the safety gate: no manoeuvre on a battery that will not have the margin
                raise Problem(409, "insufficient_margin", f"state of charge at ignition would be {soc:.0%}, below the {world.SOC_BURN:.0%} rule; delay the payload work first")
        cid = uuid.uuid7()
        checksum = hashlib.sha256(json.dumps({"spacecraft": str(s["id"]), "type": body.command_type, "args": body.args, "at": body.scheduled_for.isoformat()}, sort_keys=True).encode()).hexdigest()
        c.execute("INSERT INTO commands (id, tenant_id, spacecraft_id, command_type, args, scheduled_for, checksum, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", [cid, ctx.tenant_id, s["id"], body.command_type, Jsonb(body.args), body.scheduled_for, checksum, ctx.actor_id])
        audit.record(c, ctx, "command.queued", "command", cid, {"spacecraft": s["name"], "type": body.command_type, "checksum": checksum[:16]})
        return 201, {"command_id": cid, "spacecraft": s["name"], "command_type": body.command_type, "status": "pending", "approvals_needed": 2, "scheduled_for": body.scheduled_for, "checksum": checksum}
    return run(ctx, idem, body, work)


@app.post("/v1/commands/{command_id}/approve", tags=["commands"], summary="One of two approvals. The second, by a different person, releases the command onto the next pass before its time")
def approve(command_id: uuid.UUID, ctx: Ctx = Depends(auth("commands:approve")), idem: str | None = IdemKey):
    def work(c):
        f, w, ps = fleet(c)
        cmd = c.execute("SELECT cm.*, s.name, s.ordinal FROM commands cm JOIN spacecraft s ON s.id = cm.spacecraft_id WHERE cm.id = %s FOR UPDATE OF cm", [command_id]).fetchone()
        if not cmd:
            raise Problem(404, "command_not_found")
        if cmd["status"] not in ("pending", "approved_once"):
            raise Problem(409, "not_approvable", f"this command is already {cmd['status']}")
        if ctx.actor_id in (cmd["created_by"], cmd["approved_by"]):
            raise Problem(409, "same_person", "the person who queued a command, or already approved it, cannot approve it again")
        if cmd["status"] == "pending":
            c.execute("UPDATE commands SET status = 'approved_once', approved_by = %s WHERE id = %s", [ctx.actor_id, command_id])
            audit.record(c, ctx, "command.approved", "command", command_id, {"spacecraft": cmd["name"], "approval": 1})
            return 200, {"command_id": command_id, "status": "approved_once", "approvals": 1, "approvals_needed": 2}
        when = minute(cmd["scheduled_for"])
        up = next((p for p in ps if p["sat"] == cmd["ordinal"] and f["now_min"] <= p["aos"] and p["los"] <= when), None)
        if not up:
            raise Problem(409, "no_uplink_pass", "no pass over a station before the scheduled time")
        c.execute("UPDATE commands SET status = 'released', second_approved_by = %s, released_at = now(), uplink_pass = %s WHERE id = %s", [ctx.actor_id, up["index"], command_id])
        audit.record(c, ctx, "command.released", "command", command_id, {"spacecraft": cmd["name"], "type": cmd["command_type"], "approval": 2, "uplink_pass": up["index"], "checksum": cmd["checksum"][:16]})
        return 200, {"command_id": command_id, "status": "released", "approvals": 2, "uplink": {"pass": up["index"], "station": w["stations"][up["station"]]["name"], "aos": ts(up["aos"])}, "checksum": cmd["checksum"]}
    return run(ctx, idem, {}, work)


@app.get("/v1/commands", tags=["commands"], summary="(+) The command queue")
def commands(ctx: Ctx = Depends(auth("fleet:read"))):
    with db.tx(ctx.tenant_id) as c:
        rows = c.execute("SELECT cm.id AS command_id, s.name AS spacecraft, cm.command_type, cm.args, cm.status, cm.scheduled_for, cm.checksum, cm.created_at, cm.released_at, cm.uplink_pass FROM commands cm JOIN spacecraft s ON s.id = cm.spacecraft_id ORDER BY cm.created_at").fetchall()
    return jsonable_encoder({"items": rows})
