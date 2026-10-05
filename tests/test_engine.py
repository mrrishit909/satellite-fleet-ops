"""Unit tests for the orbit model, the telemetry simulator, detection, planning and the conjunction arithmetic: no database."""
import numpy as np
import pytest

from sf import engine, world

W = world.build()
PS = world.passes()


def test_orbits_and_passes_are_plausible():
    sat = W["sats"][0]
    t = np.arange(0, 6000, 10.0)
    r = world.positions(sat, t)
    assert np.allclose(np.linalg.norm(r, axis=1), world.R_EARTH + sat["alt_km"])                   # circular
    period = 2 * np.pi * np.sqrt((world.R_EARTH + sat["alt_km"]) ** 3 / world.MU)
    assert 5400 < period < 5900 and np.allclose(world.positions(sat, np.array([0.0])), world.positions(sat, np.array([period])), atol=1)
    assert 300 < len(PS) < 600 and all(3 <= p["minutes"] <= 15 and p["max_el"] >= world.MIN_EL for p in PS) and PS == sorted(PS, key=lambda p: p["aos"])
    polar = [p for p in PS if p["station"] == 0]
    assert len(polar) > len([p for p in PS if p["station"] == 3])                                   # Svalbard sees the sun-synchronous orbits every revolution
    assert all(p["sat"] < 8 for p in polar)                                                          # 51.6 degrees never reaches 78 north


def test_sun_and_power_balance():
    sat = W["sats"][9]
    t = np.arange(0, 2 * 5700, 60.0)
    sun = world.in_sun(world.positions(sat, t), W["sun"])
    assert 0.5 < sun.mean() < 0.8                                                                    # a 51.6-degree orbit spends about a third of each revolution in eclipse
    assert world.in_sun(world.positions(W["sats"][3], t), W["sun"]).mean() > sun.mean()             # dawn-dusk sun-synchronous orbits see more sun
    tm = world.telemetry(W, sat, payload_ops=world.default_ops(sat), upto=600)
    assert (tm["soc"] <= 1).all() and tm["soc"].min() > 0.5 and np.corrcoef(tm["panel_temp_c"], tm["solar_current_a"])[0, 1] > 0.9
    busy = world.telemetry(W, sat, payload_ops=[(0, 600)], upto=600)
    assert busy["soc"][-1] < tm["soc"][-1]                                                           # payload on all the time drains the battery


def test_detection_finds_the_warming_battery_and_nothing_else():
    hot, clean = W["sats"][world.ANOMALY["sat"]], W["sats"][1]
    tm = world.telemetry(W, hot, payload_ops=world.default_ops(hot), anomaly=world.ANOMALY, upto=world.NOW)
    found = engine.detect(tm, world.period_min(hot))
    assert [a["metric"] for a in found] == ["battery_temp_c"] and found[0]["direction"] == "rising" and world.ANOMALY["onset"] <= found[0]["since_min"] <= world.ANOMALY["onset"] + 150
    assert engine.limit_alarms(tm) == [] and engine.detect(world.telemetry(W, clean, payload_ops=world.default_ops(clean), upto=world.NOW), world.period_min(clean)) == []
    assert engine.detect(tm, world.period_min(hot), upto=world.ANOMALY["onset"]) == []                                       # nothing before it starts


def test_plan_respects_stations_and_spacecraft_and_beats_first_come():
    rq = {s["index"]: {"data_gb": s["data_gb"], "priority": s["priority"], "deadline": s["deadline"]} for s in W["sats"]}
    chosen, info = engine.plan(PS, rq, world.NOW)
    simple = engine.greedy(PS, rq, world.NOW)
    mine, theirs = engine.describe(PS, chosen, rq), engine.describe(PS, simple, rq)
    assert info["status"] in ("OPTIMAL", "FEASIBLE") and mine["weighted_gb"] >= theirs["weighted_gb"] and all(r["delivered_gb"] <= r["requested_gb"] for r in mine["rows"])
    for pick in (chosen, simple):
        used = sorted((PS[i] for i in pick), key=lambda p: p["aos"])
        for key, gap in (("station", world.SLEW_MIN), ("sat", 0)):
            for v in {p[key] for p in used}:
                mine_ = [p for p in used if p[key] == v]
                assert all(b["aos"] >= a["los"] + gap for a, b in zip(mine_, mine_[1:])), key
        assert all(p["aos"] >= world.NOW and p["los"] <= rq[p["sat"]]["deadline"] for p in used)


def test_conjunction_arithmetic():
    assert world.conjunction_probability(0, 120, 5) > world.conjunction_probability(180, 120, 5) > world.conjunction_probability(1000, 120, 5)
    assert world.conjunction_probability(180, 120, 5) == pytest.approx(2.8e-4, rel=0.1) and world.conjunction_probability(5000, 120, 5) < 1e-12
    assert world.shifted_miss(180, 0.05, 300, 550) == pytest.approx(np.hypot(180, 3 * 0.05 * 300 * 60)) and world.shifted_miss(180, 0.0, 300, 550) == 180


def test_soc_forecast_shows_the_campaign_cost():
    sat = W["sats"][world.CAMPAIGN["sat"]]
    ops = world.default_ops(sat)
    cur = engine.soc_forecast(W, sat, 0.95, world.NOW, world.BURN + 1, [], ops)
    alt = engine.soc_forecast(W, sat, 0.95, world.NOW, world.BURN + 1, [], [o for o in ops if o[0] != world.CAMPAIGN["start"]])
    assert cur[-1] < world.SOC_BURN <= alt[-1]
