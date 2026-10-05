"""Telemetry anomaly detection, battery forecasting, contact planning and the conjunction arithmetic."""
import numpy as np
from ortools.sat.python import cp_model
from sklearn.ensemble import IsolationForest

from . import world

VERSION = "anomaly-residual-1"
LEARN_MIN = 12 * 60          # the first 12 hours of a spacecraft's record set its normal
Z_ALARM, PERSIST = 4.0, 10   # a metric is anomalous when it sits this many noise units off its normal for this many minutes
LIMITS = {"battery_temp_c": (5.0, 30.0), "battery_voltage_v": (14.0, 17.2), "soc": (0.25, 1.01), "panel_temp_c": (-40, 70), "obc_temp_c": (15, 45)}      # the obvious rule: fixed red limits
BINS = 48                    # the orbit is folded into this many phase bins


def phases(n, period):
    return ((np.arange(n) % period) / period * BINS).astype(int) % BINS


def normal(series, period, learn=LEARN_MIN):
    """Each metric's expected value by orbit phase (the first hours folded on the spacecraft's own period) and its noise."""
    ph = phases(len(series), period)
    bins = np.array([np.median(series[:learn][ph[:learn] == b]) for b in range(BINS)])
    resid = series[:learn] - bins[ph[:learn]]
    return bins, max(float(np.std(resid)), 1e-6)


def residual_z(series, period):
    bins, sigma = normal(series, period)
    return (series - bins[phases(len(series), period)]) / sigma


def detect(tm, period, upto=None):
    """Which metrics are off their normal right now, and since when. A run must still be going: an excursion that came and went
    (a payload burst drifting against the phase bins) is not an alarm. State of charge is watched by its limit only: a full
    battery sitting at 100% is not a fault. -> [{metric, since_min, z_now, direction, value_now}]"""
    out = []
    for metric, series in tm.items():
        series = series[:upto] if upto else series
        if metric == "soc" or len(series) <= LEARN_MIN + PERSIST:
            continue
        z = residual_z(series, period)
        off = np.abs(z) > Z_ALARM
        if not off[-PERSIST:].all():
            continue
        since = len(series) - 1
        while since > LEARN_MIN and off[since - 1]:
            since -= 1
        out.append({"metric": metric, "since_min": int(since), "z_now": round(float(z[-1]), 1), "direction": "rising" if z[-1] > 0 else "falling", "value_now": round(float(series[-1]), 3)})
    return out


def limit_alarms(tm, upto=None):
    """The obvious rule: a metric outside its fixed limits. -> [{metric, since_min}]"""
    out = []
    for metric, (lo, hi) in LIMITS.items():
        series = tm[metric][:upto] if upto else tm[metric]
        bad = np.flatnonzero((series < lo) | (series > hi))
        if len(bad):
            out.append({"metric": metric, "since_min": int(bad[0]), "value_now": round(float(series[-1]), 3)})
    return out


def forest(tm, upto=None):
    """Isolation Forest on the minute vectors, fitted on the learning window: the blueprint's suggestion, kept for comparison.
    -> share of the last PERSIST minutes scored anomalous"""
    x = np.column_stack([tm[m][:upto] if upto else tm[m] for m in world.METRICS])
    f = IsolationForest(n_estimators=100, contamination=0.01, random_state=0).fit(x[:LEARN_MIN])
    flags = f.predict(x[LEARN_MIN:]) == -1
    first = int(np.flatnonzero(np.convolve(flags, np.ones(PERSIST), "valid") >= PERSIST)[0] + LEARN_MIN) if (np.convolve(flags, np.ones(PERSIST), "valid") >= PERSIST).any() else None
    return {"flagged_share_last_window": round(float(flags[-PERSIST:].mean()), 2), "first_persistent_min": first}


def soc_forecast(w, sat, soc0, start, end, downlinks, payload_ops):
    """Battery state of charge from `start` to `end` under a plan. -> array per minute from start"""
    tm = world.telemetry(w, sat, downlinks=[(a - start, b - start) for a, b in downlinks], payload_ops=[(a - start, b - start) for a, b in payload_ops], upto=end - start, soc0=soc0)
    return tm["soc"]


def greedy(passes, requests, now):
    """The obvious schedule: take each pass in time order if the station and the spacecraft are free and data remains."""
    free_st, free_sat, left, chosen = {}, {}, {k: v["data_gb"] for k, v in requests.items()}, []
    for p in sorted(passes, key=lambda p: p["aos"]):
        if p["aos"] < now or left.get(p["sat"], 0) <= 0 or p["los"] > requests[p["sat"]]["deadline"]:
            continue
        if free_st.get(p["station"], -1e9) + world.SLEW_MIN > p["aos"] or free_sat.get(p["sat"], -1e9) > p["aos"]:
            continue
        chosen.append(p["index"])
        free_st[p["station"]], free_sat[p["sat"]] = p["los"], p["los"]
        left[p["sat"]] -= p["gb"]
    return chosen


def plan(passes, requests, now, seconds=5.0):
    """CP-SAT: pick passes so that priority-weighted data delivered before each deadline is greatest, with one contact per
    station at a time (plus slew), one contact per spacecraft at a time, and no pass credited after its request's deadline.
    requests: sat -> {data_gb, priority, deadline}. -> (chosen pass indices, info)"""
    m = cp_model.CpModel()
    use, per_st, per_sat = {}, {}, {}
    cand = [p for p in passes if p["aos"] >= now and p["los"] <= requests[p["sat"]]["deadline"] and requests[p["sat"]]["data_gb"] > 0]
    for p in cand:
        x = use[p["index"]] = m.NewBoolVar(f"p{p['index']}")
        a, b = int(round(p["aos"] * 10)), int(round(p["los"] * 10))
        per_st.setdefault(p["station"], []).append(m.NewOptionalIntervalVar(a, b - a + world.SLEW_MIN * 10, b + world.SLEW_MIN * 10, x, f"s{p['index']}"))
        per_sat.setdefault(p["sat"], []).append(m.NewOptionalIntervalVar(a, b - a, b, x, f"c{p['index']}"))
    for iv in list(per_st.values()) + list(per_sat.values()):
        m.AddNoOverlap(iv)
    delivered, obj = {}, []
    for sat, rq in requests.items():
        mine = [p for p in cand if p["sat"] == sat]
        d = delivered[sat] = m.NewIntVar(0, int(round(rq["data_gb"] * 10)), f"d{sat}")
        m.Add(d <= sum(int(round(p["gb"] * 10)) * use[p["index"]] for p in mine))
        obj.append(rq["priority"] * d)
    m.Maximize(sum(obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds, solver.parameters.num_search_workers = seconds, 8
    status = solver.Solve(m)
    chosen = [i for i, x in use.items() if solver.Value(x)]
    return chosen, {"status": solver.StatusName(status), "candidates": len(cand), "seconds": round(solver.WallTime(), 2), "objective": solver.ObjectiveValue() / 10, "bound": solver.BestObjectiveBound() / 10}


def describe(passes, chosen, requests):
    """What a schedule delivers. -> per spacecraft and totals"""
    by = {k: 0.0 for k in requests}
    for i in chosen:
        p = passes[i]
        by[p["sat"]] += p["gb"]
    rows = [{"sat": k, "requested_gb": round(v["data_gb"], 1), "priority": v["priority"], "deadline_min": v["deadline"], "delivered_gb": round(float(min(by[k], v["data_gb"])), 1), "met": bool(by[k] >= v["data_gb"] - 1e-9), "passes": int(sum(passes[i]["sat"] == k for i in chosen))} for k, v in requests.items()]
    return {"rows": rows, "passes_used": len(chosen), "delivered_gb": round(sum(r["delivered_gb"] for r in rows), 1), "weighted_gb": round(sum(r["delivered_gb"] * r["priority"] for r in rows), 1), "requests_met": int(sum(r["met"] for r in rows)),
            "contact_minutes": round(sum(passes[i]["minutes"] for i in chosen), 1)}
