"""Integration, end-to-end and security tests against a real Postgres."""
import uuid

import pytest

from core import db, jobs, scenario
from sf import api, world

from .conftest import bearer

DIRECTOR, ENGINEER, OPERATOR, VIEWER, OTHER = bearer("fleet", "director"), bearer("fleet", "engineer"), bearer("fleet", "operator"), bearer("fleet", "viewer"), bearer("other", "director")
ZERO = "00000000-0000-0000-0000-000000000000"


def test_nothing_works_before_a_fleet_is_loaded(client):
    assert client.get(f"/v1/spacecraft/{ZERO}/state", headers=VIEWER).json()["code"] == "no_fleet" and client.post("/v1/planning/contacts", headers=OPERATOR, json={}).json()["code"] == "no_fleet"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=(), tenant=None):
    with db.tx(tenant) as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    load = demo["load"]["result"]
    assert load["passes_48h"] == len(world.passes()) == one("SELECT count(*) AS n FROM passes")["n"] and load["telemetry_rows"] == one("SELECT count(*) AS n FROM telemetry")["n"] - 80 and load["requests_gb"] > load["capacity_ahead_gb"] * 0.5
    assert demo["state"]["status"] == "nominal" and demo["state"]["anomalies"] == [] and len(demo["state"]["next_passes"]) == 4
    pk = demo["pk"]
    assert pk["stored"] == 80 and pk["duplicates_ignored"] == 1 and [a["metric"] for a in pk["anomalies"]] == ["battery_temp_c"] and pk["fixed_limits"]["alarms"] == [] and pk["isolation_forest"]["first_persistent_min"] is None
    assert demo["state2"]["status"] == "anomaly" and demo["state2"]["anomalies"][0]["metric"] == "battery_temp_c" and demo["state2"]["telemetry"][0]["minute"] == world.NOW - 1
    p = demo["plan"]
    assert p["optimised"]["weighted_gb"] > p["first_come"]["weighted_gb"] and p["optimised"]["passes_used"] == one("SELECT count(*) AS n FROM passes WHERE scheduled")["n"] and len(p["schedule"]) + len(p["unused"]) == p["passes_in_window"]
    cdm = demo["cdm"]
    assert cdm["above_action_threshold"] and 1e-4 < cdm["probability"] < 1e-3 and demo["list"]["items"][0]["conjunction_id"] == cdm["conjunction_id"]
    sim = demo["sim"]
    assert sim["after"]["probability"] < 1e-9 and sim["after"]["miss_distance_m"] > 2000 and not sim["current_plan"]["margin_ok"] and sim["with_payload_delayed"]["margin_ok"] and max(x["minutes"] for x in sim["payload_work_before_burn"]) == 72
    assert demo["early"]["code"] == "insufficient_margin" and demo["c1"]["status"] == "pending" and demo["a1"]["status"] == "approved_once" and demo["twice"]["code"] == "same_person" and demo["nope"]["code"] == "forbidden"
    assert demo["a2"]["status"] == "released" and demo["c2"]["status"] == "pending" and demo["b2"]["status"] == "released" and demo["b2"]["uplink"]["pass"] is not None
    assert [x["status"] for x in demo["queue"]["items"]] == ["released", "released"] and {x["checksum"] for x in demo["queue"]["items"]} == {demo["c1"]["checksum"], demo["c2"]["checksum"]}
    assert demo["audit"]["chain_valid"] and {"fleet.loaded", "anomaly.detected", "contacts.planned", "conjunction.registered", "command.queued", "command.approved", "command.released"} <= {e["action"] for e in demo["audit"]["events"]}


def test_packet_validation(client, demo, seeded):
    sat = str(api.sid(seeded["fleet"], 1))
    post = lambda **kw: client.post("/v1/telemetry/packets", headers=OPERATOR, json={"spacecraft_id": sat, "packets": [{"ts": "2026-10-05T17:55:00Z", "subsystem": "eps", "metric": "soc", "value": 0.9, "unit": "ratio", **kw}]})     # noqa: E731
    assert post().json()["stored"] == 1 and post().json()["duplicates_ignored"] == 1 and post(metric="warp_core").status_code == 422
    assert post(ts="2026-10-05T17:55:00").json()["code"] == "timezone_required" and post(ts="2026-10-07T00:00:00Z").json()["code"] == "out_of_window"
    assert client.post("/v1/telemetry/packets", headers=OPERATOR, json={"spacecraft_id": ZERO, "packets": [{"ts": "2026-10-05T17:55:00Z", "subsystem": "eps", "metric": "soc", "value": 0.9, "unit": "ratio"}]}).json()["code"] == "spacecraft_not_found"


def test_conjunction_and_scenario_validation(client, demo, seeded):
    sat = str(api.sid(seeded["fleet"], 4))
    body = {"spacecraft_id": sat, "object_id": "TEST", "tca": "2026-10-06T20:00:00Z", "miss_distance_m": 3000, "sigma_m": 100}
    low = client.post("/v1/conjunctions", headers=OPERATOR, json=body).json()
    assert low["probability"] < 1e-9 and not low["above_action_threshold"] and client.get("/v1/conjunctions?risk_min=0.0001", headers=VIEWER).json()["items"][0]["spacecraft"] == "RBO-03"
    assert client.post("/v1/conjunctions", headers=OPERATOR, json={**body, "tca": "2026-10-05T10:00:00Z"}).json()["code"] == "tca_in_the_past" and client.post("/v1/conjunctions", headers=OPERATOR, json={**body, "miss_distance_m": -1}).status_code == 422
    sim = lambda **kw: client.post("/v1/scenarios/orbit-maneuver", headers=OPERATOR, json={"conjunction_id": low["conjunction_id"], "delta_v_mps": 0.05, "burn_at": "2026-10-06T15:00:00Z", **kw})     # noqa: E731
    assert sim().json()["with_payload_delayed"] is None and sim(burn_at="2026-10-06T21:00:00Z").json()["code"] == "bad_burn_time" and sim(conjunction_id=ZERO).status_code == 404


def test_command_rules(client, demo, seeded):
    sat = str(api.sid(seeded["fleet"], 4))
    post = lambda **kw: client.post("/v1/commands", headers=OPERATOR, json={"spacecraft_id": sat, "command_type": "payload_delay", "args": {"start_min": world.default_ops(world.build()["sats"][4])[20][0], "end_min": 0, "delay_min": 60}, "scheduled_for": "2026-10-06T06:00:00Z", **kw})     # noqa: E731
    assert post(args={"delay_min": 5}).json()["code"] == "bad_args" and post(scheduled_for="2026-10-05T01:00:00Z").json()["code"] == "scheduled_in_the_past" and post(command_type="maneuver", args={"delta_v_mps": 0.1, "conjunction_id": ZERO}).status_code == 404
    cid = post().json()["command_id"]
    approve = lambda who: client.post(f"/v1/commands/{cid}/approve", headers=who).json()     # noqa: E731
    assert approve(VIEWER)["code"] == "forbidden" and approve(DIRECTOR)["status"] == "approved_once" and approve(DIRECTOR)["code"] == "same_person" and approve(ENGINEER)["status"] == "released"
    assert approve(ENGINEER)["code"] == "not_approvable" and client.post(f"/v1/commands/{ZERO}/approve", headers=ENGINEER).status_code == 404
    assert one("SELECT approved_by <> second_approved_by AS two FROM commands WHERE id = %s", [cid])["two"]


def test_roles(client, demo, seeded):
    sat = str(api.sid(seeded["fleet"], 0))
    assert client.get(f"/v1/spacecraft/{sat}/state").status_code == 401 and client.get("/v1/commands", headers={"Authorization": "Bearer nope"}).status_code == 401
    for path, payload in (("/v1/planning/contacts", {}), ("/v1/conjunctions", {}), ("/v1/commands", {}), ("/v1/telemetry/packets", {})):
        assert client.post(path, headers=VIEWER, json=payload).status_code == 403, path
        assert client.post(path, headers=ENGINEER, json=payload).status_code == 403, path
    assert client.post("/v1/fleet:load", headers=OPERATOR, json={}).status_code == 403 and client.get("/v1/audit", headers=OPERATOR).status_code == 403 and client.get("/v1/audit", headers=DIRECTOR).status_code == 200


def test_another_operator_sees_nothing(client, demo, seeded):
    sat = str(api.sid(seeded["fleet"], 6))
    assert client.get(f"/v1/spacecraft/{sat}/state", headers=OTHER).json()["code"] == "no_fleet" and client.get("/v1/conjunctions", headers=OTHER).json()["items"] == [] and client.get("/v1/commands", headers=OTHER).json()["items"] == []
    for table in ("fleets", "spacecraft", "orbits", "ground_stations", "passes", "telemetry", "anomalies", "downlink_requests", "contact_plans", "conjunctions", "commands"):
        assert one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["other"])["n"] == 0 < one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["fleet"])["n"], table
    with pytest.raises(Exception, match="row-level security"), db.tx(seeded["other"]) as c:
        c.execute("INSERT INTO fleets (id, tenant_id, name, model_seed, epoch, now_min) VALUES (%s,%s,'x',1,now(),0)", [uuid.uuid4(), seeded["fleet"]])


def test_idempotent_writes(client, demo):
    body = {"horizon_h": 24}
    again = client.post("/v1/planning/contacts", headers={**OPERATOR, "Idempotency-Key": "demo-1:plan"}, json=body)
    assert again.status_code == 201 and again.headers["Idempotent-Replay"] == "true" and again.json()["plan_id"] == demo["plan"]["plan_id"]
    assert client.post("/v1/planning/contacts", headers={**OPERATOR, "Idempotency-Key": "demo-1:plan"}, json={"horizon_h": 12}).json()["code"] == "idempotency_key_reused"
    assert client.post("/v1/fleet:load", headers=DIRECTOR, json={"seed": 17}).json()["code"] == "fleet_exists"
