# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 12 spacecraft, 432 passes, 102,800 telemetry rows.
Reproduce with `make demo`, then `make load-test`.

## Reads: one spacecraft's state, the conjunction list, the command queue, in rotation, closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 1963 | 196.1 req/s | 2.7 ms | 10.7 ms | 11.3 ms | 0.00% |
| 8 | 5758 | 575.1 req/s | 11.4 ms | 24.0 ms | 28.6 ms | 0.00% |
| 32 | 4348 | 433.0 req/s | 55.5 ms | 75.2 ms | 80.9 ms | 0.00% |

Target from the blueprint: p95 under 500 ms for reads: met.

## Compute (in-process, one core)

| Work | Time |
|---|---|
| Propagate 12 orbits for 48 hours at 10-second steps and find every pass over 4 stations | 0.03 s |
| 18 hours of telemetry for 12 spacecraft, loaded through COPY (102,720 rows) | about 3 s |
| Anomaly detection over one spacecraft's 18-hour record, 8 metrics | under 10 ms |
| Contact plan: 155 candidate passes, CP-SAT to optimality | 0.04 s |
| Manoeuvre scenario: two 20-hour battery forecasts | 0.1 s |
| Evaluation: 108 faults, 36 clean records, 12 queues | about 25 s |

Not measured: a live packet stream. Detection re-reads a spacecraft's whole record on each batch; with live data it would keep
running phase bins instead. The blueprint's scale (hundreds of spacecraft, sub-second telemetry) is beyond this slice.
