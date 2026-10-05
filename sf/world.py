"""An invented fleet: 12 small satellites in low Earth orbit, four ground stations, 48 hours of orbits, the passes between them,
a minute-by-minute power and thermal record for each spacecraft, payload downlink requests and one close approach.
Everything is a pure function of a seed. Time is in minutes from the start of the record; the demo stands at NOW."""
import functools

import numpy as np

MU = 398600.4418            # km^3/s^2
R_EARTH = 6371.0
OMEGA_E = 7.2921159e-5      # rad/s
HOURS = 48
NOW = 18 * 60
STEP_S = 10
MIN_EL = 10.0
SLEW_MIN = 3
RATE_GB_PER_MIN = 0.3       # downlink rate
SOC_FLOOR, SOC_BURN = 0.30, 0.80          # never below 30%; a manoeuvre needs 80% at ignition (attitude control, thruster heaters, a safe-mode reserve)
STATIONS = [("Svalbard", 78.23, 15.39), ("Fairbanks", 64.86, -147.85), ("Punta Arenas", -53.16, -70.91), ("Singapore", 1.35, 103.82)]
ANOMALY = {"sat": 6, "metric": "battery_temp_c", "onset": 14 * 60, "per_hour": 0.6}           # battery warming on one spacecraft
CONJUNCTION = {"sat": 2, "object": "DEBRIS 2019-006AJ", "tca": 38 * 60, "miss_m": 180.0, "sigma_m": 120.0, "radius_m": 5.0}
CAMPAIGN = {"sat": 2, "start": 31 * 60 + 30, "end": 31 * 60 + 30 + 72}                         # a long imaging campaign on the same spacecraft, shortly before a manoeuvre would have to burn
BURN = 33 * 60                                                                                  # five hours before closest approach
METRICS = ["battery_voltage_v", "battery_temp_c", "soc", "solar_current_a", "bus_current_a", "panel_temp_c", "obc_temp_c", "wheel_speed_rpm"]


@functools.lru_cache(maxsize=8)
def build(seed=17):
    r = np.random.default_rng(seed)
    sats = []
    for k in range(12):
        sats.append({"index": k, "name": f"RBO-{k + 1:02d}", "norad_id": 58000 + 37 * k, "alt_km": float(r.uniform(500, 600)), "inc_deg": 97.6 if k < 8 else 51.6, "raan_deg": float(r.uniform(0, 360)), "phase_deg": float(r.uniform(0, 360)),
                     "battery_wh": 100.0, "solar_w": float(r.uniform(58, 70)), "bus_w": float(r.uniform(18, 24)), "payload_w": 48.0, "downlink_w": 36.0,
                     "data_gb": float(r.uniform(15, 65)), "priority": int(r.integers(1, 4)), "deadline": int(NOW + r.uniform(8, 26) * 60)})
    sun = np.array([0.6, 0.75, 0.28])
    sun /= np.linalg.norm(sun)
    return {"seed": seed, "sats": sats, "sun": sun, "stations": [{"index": i, "name": n, "lat": la, "lon": lo, "alt_m": 0} for i, (n, la, lo) in enumerate(STATIONS)]}


def period_min(sat):
    return float(2 * np.pi * np.sqrt((R_EARTH + sat["alt_km"]) ** 3 / MU) / 60)


def positions(sat, t_s):
    """Circular two-body orbit (no drag, no J2; not SGP4). -> ECI positions [n, 3] in km for times t_s (seconds)"""
    a = R_EARTH + sat["alt_km"]
    n = np.sqrt(MU / a ** 3)
    i, om = np.radians(sat["inc_deg"]), np.radians(sat["raan_deg"])
    u = np.radians(sat["phase_deg"]) + n * t_s
    p, q = np.array([np.cos(om), np.sin(om), 0.0]), np.array([-np.sin(om) * np.cos(i), np.cos(om) * np.cos(i), np.sin(i)])
    return a * (np.cos(u)[:, None] * p + np.sin(u)[:, None] * q)


def eci_to_ecef(r_eci, t_s):
    th = OMEGA_E * t_s
    c, s = np.cos(th), np.sin(th)
    return np.stack([c * r_eci[:, 0] + s * r_eci[:, 1], -s * r_eci[:, 0] + c * r_eci[:, 1], r_eci[:, 2]], axis=1)


def station_ecef(st):
    la, lo = np.radians(st["lat"]), np.radians(st["lon"])
    return R_EARTH * np.array([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def elevation(r_ecef, st):
    s = station_ecef(st)
    up = s / np.linalg.norm(s)
    rho = r_ecef - s
    return np.degrees(np.arcsin((rho @ up) / np.linalg.norm(rho, axis=1)))


def in_sun(r_eci, sun):
    along = r_eci @ sun
    perp = np.linalg.norm(r_eci - along[:, None] * sun[None, :], axis=1)
    return (along > 0) | (perp > R_EARTH)


@functools.lru_cache(maxsize=8)
def passes(seed=17):
    """Every pass of every spacecraft over every station in the record. -> list of dicts with minutes aos, los, max_el"""
    w, out = build(seed), []
    t = np.arange(0, HOURS * 3600, STEP_S, dtype=float)
    for sat in w["sats"]:
        ecef = eci_to_ecef(positions(sat, t), t)
        for st in w["stations"]:
            el = elevation(ecef, st)
            vis = el >= MIN_EL
            edges = np.flatnonzero(np.diff(vis.astype(int)))
            starts = [0] + [e + 1 for e in edges if vis[e + 1]] if vis[0] else [e + 1 for e in edges if vis[e + 1]]
            for a in starts:
                b = a
                while b < len(vis) and vis[b]:
                    b += 1
                aos, los = t[a] / 60, t[min(b, len(t) - 1)] / 60
                if los - aos >= 3:
                    out.append({"sat": sat["index"], "station": st["index"], "aos": round(float(aos), 1), "los": round(float(los), 1), "max_el": round(float(el[a:b].max()), 1), "minutes": round(float(los - aos), 1), "gb": round(float(los - aos) * RATE_GB_PER_MIN, 1)})
    out.sort(key=lambda p: p["aos"])
    for k, p in enumerate(out):
        p["index"] = k
    return out


def telemetry(w, sat, downlinks=(), payload_ops=(), anomaly=None, upto=HOURS * 60, noise_seed=0, soc0=0.85):
    """Minute-by-minute housekeeping for one spacecraft: a power balance in and out of eclipse, temperatures that follow it,
    plus an injected fault. downlinks / payload_ops: [(start_min, end_min)]. -> dict metric -> array[upto]"""
    r = np.random.default_rng([w["seed"], sat["index"], noise_seed])
    t = np.arange(upto) * 60.0
    sun = in_sun(positions(sat, t), w["sun"])
    busy = lambda spans: np.array([any(a <= m < b for a, b in spans) for m in range(upto)])       # noqa: E731
    dl, pl = busy(downlinks), busy(payload_ops)
    load = sat["bus_w"] + sat["payload_w"] * pl + sat["downlink_w"] * dl
    gen = sat["solar_w"] * sun
    soc = np.empty(upto)
    s = soc0
    for m in range(upto):
        s = min(1.0, s + (gen[m] - load[m]) / 60 / sat["battery_wh"])
        soc[m] = s
    phase = np.linspace(0, 2 * np.pi * upto / 95, upto)
    out = {"soc": soc, "battery_voltage_v": 14.4 + 2.4 * soc + r.normal(0, 0.02, upto), "solar_current_a": gen / 16 + r.normal(0, 0.05, upto), "bus_current_a": load / 16 + r.normal(0, 0.03, upto),
           "battery_temp_c": 18 + 3 * sun + 2.5 * dl + 0.5 * np.sin(phase) + r.normal(0, 0.15, upto), "panel_temp_c": -20 + 70 * sun + r.normal(0, 1.5, upto), "obc_temp_c": 28 + 4 * sun + 1.5 * pl + r.normal(0, 0.3, upto),
           "wheel_speed_rpm": 2400 + 300 * np.sin(phase / 3) + r.normal(0, 15, upto)}
    if anomaly:
        ramp = np.maximum(0, np.arange(upto) - anomaly["onset"]) / 60 * anomaly["per_hour"]
        out[anomaly["metric"]] = out[anomaly["metric"]] + ramp
    return out


def default_ops(sat):
    """The standing payload plan: an imaging burst every orbit, 8 minutes long, plus the campaign on one spacecraft."""
    ops = [(m, m + 8) for m in range(20 + sat["index"] * 7, HOURS * 60, 95)]
    return ops + ([(CAMPAIGN["start"], CAMPAIGN["end"])] if sat["index"] == CAMPAIGN["sat"] else [])


def conjunction_probability(miss_m, sigma_m, radius_m):
    """Chance of collision for a 2-D Gaussian miss with isotropic combined uncertainty and a combined hard-body radius
    (the standard thin-encounter approximation; no covariance from a CDM, so a stated sigma stands in)."""
    from scipy.stats import ncx2
    return float(ncx2.cdf((radius_m / sigma_m) ** 2, 2, (miss_m / sigma_m) ** 2))


def shifted_miss(miss_m, dv_mps, lead_min, alt_km):
    """An along-track burn moves the spacecraft along its orbit by about 3 * dv * dt at the time of closest approach."""
    along = 3 * dv_mps * lead_min * 60
    return float(np.hypot(miss_m, along))
