# Evaluation

Everything here is measured on synthetic fleets generated with seeds the demo does not use (`(101, 102, 103)`). Reproduce with
`python -m sf.evaluate` (about a minute). The numbers say how well the methods recover what the simulator hid. They are not a claim
about a real spacecraft: real housekeeping has modes, heater cycles and sensor quirks this generator does not have, and real
orbits are not circles.

## 1. Is a slow drift caught, and how soon?

108 faults injected across 36 spacecraft: a ramp on the battery temperature (0.2 to 0.9 °C/h), the battery voltage (-0.03 to -0.12 V/h) or the
on-board computer temperature (0.4 to 1.5 °C/h), starting between hour 13 and hour 28 of a 36-hour record. Detection learns each
metric's normal by orbit phase from the first 12 hours. The obvious rule is a fixed red limit per metric.

| metric | faults | caught by hour 36 | minutes to detection (median) | red limit tripped by hour 36 | red limit, minutes (median) |
|---|---|---|---|---|---|
| battery_temp_c | 36 | 36 | 140 | 16 | 780 |
| battery_voltage_v | 36 | 36 | 195 | 0 | - |
| obc_temp_c | 36 | 36 | 165 | 20 | 620 |

Wrong metrics flagged alongside a fault: 0. On the 36 clean records: 0 alarms from this method, 0 from the red limits,
5 from an Isolation Forest fitted on the first 12 hours (the blueprint's suggestion). The forest never fired on the faults either: a slow
drift of one metric does not make a minute's vector of eight look isolated.

## 2. Does the contact plan beat first come, first served?

12 random request queues (12 requests of 15 to 65 GB, priority 1 to 3, deadlines 8 to 26 hours out) over the next 24 hours of passes.
One contact per antenna at a time with a 3-minute slew, one per spacecraft at a time, nothing credited after its deadline.

| fleet | queue | solver | priority-weighted GB, plan | same, first come | requests met, plan | first come | GB delivered, plan | first come |
|---|---|---|---|---|---|---|---|---|
| 101 | 0 | optimal | 549 | 464 | 1 | 1 | 220 | 202 |
| 101 | 1 | optimal | 432 | 393 | 1 | 2 | 220 | 212 |
| 101 | 2 | optimal | 534 | 513 | 1 | 1 | 221 | 220 |
| 101 | 3 | optimal | 468 | 440 | 2 | 3 | 213 | 205 |
| 102 | 0 | optimal | 526 | 506 | 0 | 1 | 232 | 229 |
| 102 | 1 | optimal | 433 | 383 | 2 | 2 | 209 | 202 |
| 102 | 2 | optimal | 458 | 431 | 2 | 1 | 204 | 202 |
| 102 | 3 | optimal | 436 | 418 | 1 | 1 | 190 | 188 |
| 103 | 0 | optimal | 338 | 308 | 0 | 0 | 198 | 196 |
| 103 | 1 | optimal | 465 | 424 | 0 | 2 | 222 | 217 |
| 103 | 2 | optimal | 568 | 481 | 0 | 2 | 219 | 210 |
| 103 | 3 | optimal | 399 | 357 | 1 | 2 | 177 | 170 |

The plan delivers **9.6%** more priority-weighted data than first come, first served, and meets
11 requests against 18. Raw gigabytes barely move: the passes are the limit, and the plan's gain is in which data they carry.
