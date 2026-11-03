# QubitFlow results summary

Speedup = best-fixed cycles / DAG cycles (>1 means DAG is faster). 'best fixed' is the per-configuration oracle minimum over the four fixed-order policies, a deliberately strong baseline. Timing: `Timing.lsqca_paper` (see docs/ASSUMPTIONS.md); Line-SAM; deterministic simulator, so no run-to-run noise.

## 1. Fixed-order vs DAG-aware scheduling (policy suite)

### Magic states: finite (one state / 15 beats, buffer 2)

| workload | cr_slots | n_banks | best_fixed | dag_demand | dag_W4 | dag_best_vs_best_fixed |
|---|---|---|---|---|---|---|
| adder_large | 2 | 1 | 3359.00 | 3387.00 | 3359.00 | 1.00 |
| adder_large | 2 | 2 | 3357.00 | 3361.00 | 3357.00 | 1.00 |
| adder_large | 2 | 4 | 3357.00 | 3361.00 | 3357.00 | 1.00 |
| adder_large | 4 | 1 | 3353.00 | 3358.00 | 3358.00 | 1.00 |
| adder_large | 4 | 2 | 3351.00 | 3356.00 | 3353.00 | 1.00 |
| adder_large | 4 | 4 | 3350.00 | 3354.00 | 3352.00 | 1.00 |
| adder_small | 2 | 1 | 837.00 | 841.00 | 837.00 | 1.00 |
| adder_small | 2 | 2 | 836.00 | 840.00 | 836.00 | 1.00 |
| adder_small | 2 | 4 | 837.00 | 841.00 | 837.00 | 1.00 |
| adder_small | 4 | 1 | 831.00 | 837.00 | 837.00 | 0.99 |
| adder_small | 4 | 2 | 830.00 | 835.00 | 832.00 | 1.00 |
| adder_small | 4 | 4 | 830.00 | 834.00 | 832.00 | 1.00 |
| high_parallel | 2 | 1 | 8036.00 | 5133.00 | 5879.00 | 1.57 |
| high_parallel | 2 | 2 | 7375.00 | 5041.00 | 5738.00 | 1.46 |
| high_parallel | 2 | 4 | 6956.00 | 5043.00 | 5682.00 | 1.38 |
| high_parallel | 4 | 1 | 6853.00 | 5103.00 | 5333.00 | 1.34 |
| high_parallel | 4 | 2 | 6030.00 | 5050.00 | 5143.00 | 1.19 |
| high_parallel | 4 | 4 | 5648.00 | 5044.00 | 5118.00 | 1.12 |
| multiplier | 2 | 1 | 10482.00 | 10579.00 | 10515.00 | 1.00 |
| multiplier | 2 | 2 | 10482.00 | 10502.00 | 10502.00 | 1.00 |
| multiplier | 2 | 4 | 10482.00 | 10492.00 | 10494.00 | 1.00 |
| multiplier | 4 | 1 | 10482.00 | 10489.00 | 10489.00 | 1.00 |
| multiplier | 4 | 2 | 10482.00 | 10485.00 | 10485.00 | 1.00 |
| multiplier | 4 | 4 | 10482.00 | 10485.00 | 10485.00 | 1.00 |
| qft | 2 | 1 | 11452.00 | 11452.00 | 11452.00 | 1.00 |
| qft | 2 | 2 | 11450.00 | 11450.00 | 11450.00 | 1.00 |
| qft | 2 | 4 | 11450.00 | 11450.00 | 11450.00 | 1.00 |
| qft | 4 | 1 | 11452.00 | 11452.00 | 11452.00 | 1.00 |
| qft | 4 | 2 | 11450.00 | 11450.00 | 11450.00 | 1.00 |
| qft | 4 | 4 | 11450.00 | 11450.00 | 11450.00 | 1.00 |

| workload | geomean_speedup |
|---|---|
| adder_large | 1.00 |
| adder_small | 1.00 |
| high_parallel | 1.34 |
| multiplier | 1.00 |
| qft | 1.00 |

Overall geomean speedup (finite): **1.059**; configs where DAG is slower: 6 / 30

### Magic states: unlimited (isolates memory scheduling)

| workload | cr_slots | n_banks | best_fixed | dag_demand | dag_W4 | dag_best_vs_best_fixed |
|---|---|---|---|---|---|---|
| adder_large | 2 | 1 | 1524.00 | 1971.00 | 1524.00 | 1.00 |
| adder_large | 2 | 2 | 1460.00 | 1781.00 | 1491.00 | 0.98 |
| adder_large | 2 | 4 | 1452.00 | 1722.00 | 1483.00 | 0.98 |
| adder_large | 4 | 1 | 1098.00 | 1227.00 | 1075.00 | 1.02 |
| adder_large | 4 | 2 | 1112.00 | 1147.00 | 996.00 | 1.12 |
| adder_large | 4 | 4 | 1099.00 | 1123.00 | 969.00 | 1.13 |
| adder_small | 2 | 1 | 391.00 | 467.00 | 392.00 | 1.00 |
| adder_small | 2 | 2 | 369.00 | 430.00 | 375.00 | 0.98 |
| adder_small | 2 | 4 | 372.00 | 425.00 | 374.00 | 0.99 |
| adder_small | 4 | 1 | 277.00 | 292.00 | 252.00 | 1.10 |
| adder_small | 4 | 2 | 276.00 | 285.00 | 245.00 | 1.13 |
| adder_small | 4 | 4 | 275.00 | 279.00 | 244.00 | 1.13 |
| high_parallel | 2 | 1 | 5489.00 | 2962.00 | 3379.00 | 1.85 |
| high_parallel | 2 | 2 | 4627.00 | 2593.00 | 2983.00 | 1.78 |
| high_parallel | 2 | 4 | 4171.00 | 2564.00 | 2917.00 | 1.63 |
| high_parallel | 4 | 1 | 4145.00 | 1801.00 | 1944.00 | 2.30 |
| high_parallel | 4 | 2 | 2975.00 | 1496.00 | 1604.00 | 1.99 |
| high_parallel | 4 | 4 | 2236.00 | 1393.00 | 1436.00 | 1.61 |
| multiplier | 2 | 1 | 4890.00 | 5664.00 | 5368.00 | 0.91 |
| multiplier | 2 | 2 | 4505.00 | 5135.00 | 5016.00 | 0.90 |
| multiplier | 2 | 4 | 4303.00 | 4973.00 | 4714.00 | 0.91 |
| multiplier | 4 | 1 | 3134.00 | 3261.00 | 2852.00 | 1.10 |
| multiplier | 4 | 2 | 3139.00 | 2921.00 | 2543.00 | 1.23 |
| multiplier | 4 | 4 | 3116.00 | 2739.00 | 2421.00 | 1.29 |
| qft | 2 | 1 | 2481.00 | 2188.00 | 2298.00 | 1.13 |
| qft | 2 | 2 | 2490.00 | 2151.00 | 2268.00 | 1.16 |
| qft | 2 | 4 | 2481.00 | 2118.00 | 2207.00 | 1.17 |
| qft | 4 | 1 | 2377.00 | 1170.00 | 1095.00 | 2.17 |
| qft | 4 | 2 | 2375.00 | 1067.00 | 1045.00 | 2.27 |
| qft | 4 | 4 | 2375.00 | 1055.00 | 995.00 | 2.39 |

| workload | geomean_speedup |
|---|---|
| adder_large | 1.04 |
| adder_small | 1.05 |
| high_parallel | 1.85 |
| multiplier | 1.05 |
| qft | 1.62 |

Overall geomean speedup (unlimited): **1.278**; configs where DAG is slower: 8 / 30

### Configurations where fixed-order policies produce different schedules

| workload | cr_slots | n_banks | magic_mode | distinct_fixed_schedules |
|---|---|---|---|---|
| adder_large | 2 | 1 | finite | 2 |
| adder_large | 2 | 2 | finite | 2 |
| adder_large | 2 | 2 | unlimited | 2 |
| adder_large | 2 | 4 | finite | 2 |
| adder_large | 2 | 4 | unlimited | 2 |
| adder_large | 4 | 1 | finite | 4 |
| adder_large | 4 | 1 | unlimited | 4 |
| adder_large | 4 | 2 | finite | 4 |
| adder_large | 4 | 2 | unlimited | 4 |
| adder_large | 4 | 4 | finite | 3 |
| adder_large | 4 | 4 | unlimited | 3 |
| adder_small | 2 | 1 | finite | 2 |
| adder_small | 2 | 2 | finite | 2 |
| adder_small | 2 | 2 | unlimited | 2 |
| adder_small | 2 | 4 | finite | 2 |
| adder_small | 2 | 4 | unlimited | 2 |
| adder_small | 4 | 1 | finite | 4 |
| adder_small | 4 | 1 | unlimited | 4 |
| adder_small | 4 | 2 | finite | 3 |
| adder_small | 4 | 2 | unlimited | 3 |
| adder_small | 4 | 4 | finite | 3 |
| adder_small | 4 | 4 | unlimited | 3 |
| high_parallel | 2 | 1 | finite | 3 |
| high_parallel | 2 | 1 | unlimited | 2 |
| high_parallel | 2 | 2 | finite | 3 |
| high_parallel | 2 | 2 | unlimited | 3 |
| high_parallel | 2 | 4 | finite | 3 |
| high_parallel | 2 | 4 | unlimited | 3 |
| high_parallel | 4 | 1 | finite | 3 |
| high_parallel | 4 | 1 | unlimited | 2 |
| high_parallel | 4 | 2 | finite | 3 |
| high_parallel | 4 | 2 | unlimited | 3 |
| high_parallel | 4 | 4 | finite | 4 |
| high_parallel | 4 | 4 | unlimited | 4 |
| multiplier | 2 | 1 | finite | 3 |
| multiplier | 2 | 1 | unlimited | 2 |
| multiplier | 2 | 2 | finite | 4 |
| multiplier | 2 | 2 | unlimited | 4 |
| multiplier | 2 | 4 | finite | 4 |
| multiplier | 2 | 4 | unlimited | 4 |
| multiplier | 4 | 1 | finite | 4 |
| multiplier | 4 | 1 | unlimited | 4 |
| multiplier | 4 | 2 | finite | 4 |
| multiplier | 4 | 2 | unlimited | 4 |
| multiplier | 4 | 4 | finite | 4 |
| multiplier | 4 | 4 | unlimited | 4 |
| qft | 2 | 1 | finite | 2 |
| qft | 2 | 1 | unlimited | 2 |
| qft | 2 | 2 | finite | 3 |
| qft | 2 | 2 | unlimited | 3 |
| qft | 2 | 4 | finite | 3 |
| qft | 2 | 4 | unlimited | 3 |
| qft | 4 | 1 | finite | 4 |
| qft | 4 | 1 | unlimited | 4 |
| qft | 4 | 2 | finite | 4 |
| qft | 4 | 2 | unlimited | 4 |
| qft | 4 | 4 | finite | 4 |
| qft | 4 | 4 | unlimited | 4 |

## 2. Baseline policies and prefetch-window sweep (2 banks)

### cycles, magic finite

| workload | cr_slots | demand_belady | demand_lru | prefetch_belady_W1 | prefetch_belady_W2 | prefetch_belady_W4 | prefetch_belady_W8 | prefetch_lru_W1 | prefetch_lru_W2 | prefetch_lru_W4 | prefetch_lru_W8 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| adder_large | 4 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 |
| adder_large | 8 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 | 3351 |
| adder_small | 4 | 830 | 830 | 830 | 830 | 830 | 830 | 830 | 830 | 830 | 830 |
| adder_small | 8 | 830 | 830 | 830 | 830 | 830 | 830 | 830 | 830 | 830 | 830 |
| high_parallel | 4 | 6251 | 6472 | 5977 | 6074 | 6030 | 5958 | 6472 | 6472 | 6472 | 6472 |
| high_parallel | 8 | 5492 | 5910 | 5407 | 5368 | 5358 | 5361 | 5909 | 5909 | 5909 | 5909 |
| multiplier | 4 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 |
| multiplier | 8 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 | 10482 |
| qft | 4 | 11452 | 11452 | 11450 | 11450 | 11450 | 11450 | 11450 | 11450 | 11450 | 11450 |
| qft | 8 | 11452 | 11452 | 11450 | 11450 | 11450 | 11450 | 11450 | 11450 | 11450 | 11450 |

distinct schedules among the 10 baseline policies:

| workload | cr_slots | distinct_schedules |
|---|---|---|
| adder_large | 4 | 10 |
| adder_large | 8 | 9 |
| adder_small | 4 | 9 |
| adder_small | 8 | 9 |
| high_parallel | 4 | 6 |
| high_parallel | 8 | 7 |
| multiplier | 4 | 10 |
| multiplier | 8 | 10 |
| qft | 4 | 10 |
| qft | 8 | 9 |

### cycles, magic unlimited

| workload | cr_slots | demand_belady | demand_lru | prefetch_belady_W1 | prefetch_belady_W2 | prefetch_belady_W4 | prefetch_belady_W8 | prefetch_lru_W1 | prefetch_lru_W2 | prefetch_lru_W4 | prefetch_lru_W8 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| adder_large | 4 | 1119 | 1123 | 1091 | 1091 | 1112 | 1112 | 1123 | 1123 | 1123 | 1123 |
| adder_large | 8 | 1097 | 1097 | 1079 | 1079 | 1061 | 1061 | 1095 | 1095 | 1095 | 1095 |
| adder_small | 4 | 280 | 280 | 277 | 277 | 276 | 276 | 280 | 280 | 280 | 280 |
| adder_small | 8 | 275 | 275 | 268 | 268 | 268 | 268 | 272 | 272 | 272 | 272 |
| high_parallel | 4 | 3203 | 3609 | 2924 | 2819 | 2975 | 2878 | 3609 | 3609 | 3609 | 3609 |
| high_parallel | 8 | 1810 | 2730 | 1534 | 1449 | 1548 | 1418 | 2729 | 2729 | 2729 | 2729 |
| multiplier | 4 | 3340 | 3332 | 3145 | 3149 | 3139 | 3134 | 3330 | 3330 | 3330 | 3330 |
| multiplier | 8 | 3167 | 3295 | 3072 | 3069 | 3056 | 3054 | 3289 | 3289 | 3289 | 3289 |
| qft | 4 | 2501 | 2567 | 2396 | 2375 | 2375 | 2375 | 2563 | 2563 | 2563 | 2563 |
| qft | 8 | 2403 | 2403 | 2375 | 2375 | 2375 | 2375 | 2393 | 2393 | 2393 | 2393 |

distinct schedules among the 10 baseline policies:

| workload | cr_slots | distinct_schedules |
|---|---|---|
| adder_large | 4 | 9 |
| adder_large | 8 | 8 |
| adder_small | 4 | 8 |
| adder_small | 8 | 8 |
| high_parallel | 4 | 6 |
| high_parallel | 8 | 7 |
| multiplier | 4 | 10 |
| multiplier | 8 | 10 |
| qft | 4 | 10 |
| qft | 8 | 9 |

## 3. When does memory latency become visible? (magic-state interval sweep, CR=4, 2 banks)

| workload | magic_interval | dag_W4 | dag_demand | demand_lru | prefetch_belady_W4 |
|---|---|---|---|---|---|
| adder_large | 1 | 996 | 1147 | 1123 | 1112 |
| adder_large | 2 | 996 | 1147 | 1123 | 1112 |
| adder_large | 4 | 1101 | 1227 | 1187 | 1176 |
| adder_large | 8 | 1809 | 1820 | 1803 | 1799 |
| adder_large | 15 | 3353 | 3356 | 3351 | 3351 |
| adder_small | 1 | 245 | 285 | 280 | 276 |
| adder_small | 2 | 245 | 285 | 280 | 276 |
| adder_small | 4 | 266 | 307 | 296 | 292 |
| adder_small | 8 | 454 | 458 | 453 | 452 |
| adder_small | 15 | 832 | 835 | 830 | 830 |
| high_parallel | 1 | 1604 | 1561 | 3609 | 3065 |
| high_parallel | 2 | 1610 | 1513 | 3609 | 2940 |
| high_parallel | 4 | 1836 | 1772 | 3705 | 3020 |
| high_parallel | 8 | 3002 | 2784 | 4679 | 4199 |
| high_parallel | 15 | 5143 | 5050 | 6472 | 6030 |
| multiplier | 1 | 2618 | 2926 | 3332 | 3139 |
| multiplier | 2 | 2651 | 2929 | 3332 | 3139 |
| multiplier | 4 | 3199 | 3315 | 3532 | 3337 |
| multiplier | 8 | 5599 | 5621 | 5618 | 5596 |
| multiplier | 15 | 10485 | 10485 | 10482 | 10482 |
| qft | 1 | 1113 | 1133 | 2567 | 2375 |
| qft | 2 | 1560 | 1573 | 2567 | 2375 |
| qft | 4 | 3057 | 3057 | 3063 | 3061 |
| qft | 8 | 6109 | 6109 | 6111 | 6109 |
| qft | 15 | 11450 | 11450 | 11452 | 11450 |

## 4. DAG scheduler ablation (unlimited magic states, 2 banks)

| workload | cr_slots | dag_W0_cw0.0 | dag_W0_cw0.25 | dag_W0_cw1.0 | dag_W2_cw0.0 | dag_W2_cw0.25 | dag_W2_cw1.0 | dag_W4_cw0.0 | dag_W4_cw0.25 | dag_W4_cw1.0 | dag_W4_noevict | dag_W4_tol16 | dag_W4_tol4 | dag_W4_tol64 | dag_W4_urgcrit | dag_W4_urglevel | dag_W8_cw0.0 | dag_W8_cw0.25 | dag_W8_cw1.0 | dag_W8_noevict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| adder_large | 2 | 1781 | 1781 | 1781 | 1491 | 1491 | 1491 | 1491 | 1491 | 1491 | 1781 | 1493 | 1491 | 1494 | 1491 | 1580 | 1491 | 1491 | 1491 | 1781 |
| adder_large | 4 | 1147 | 1147 | 1147 | 996 | 996 | 996 | 996 | 996 | 996 | 1147 | 996 | 996 | 996 | 996 | 990 | 996 | 996 | 996 | 1147 |
| adder_small | 2 | 430 | 430 | 430 | 375 | 375 | 375 | 375 | 375 | 375 | 430 | 376 | 375 | 373 | 375 | 395 | 375 | 375 | 375 | 430 |
| adder_small | 4 | 285 | 285 | 285 | 245 | 245 | 245 | 245 | 245 | 245 | 285 | 245 | 245 | 245 | 245 | 245 | 245 | 245 | 245 | 285 |
| high_parallel | 2 | 2593 | 2593 | 2593 | 2983 | 2983 | 2983 | 2983 | 2983 | 2983 | 2593 | 3008 | 2983 | 3024 | 2983 | 3010 | 2983 | 2983 | 2983 | 2593 |
| high_parallel | 4 | 1496 | 1496 | 1496 | 1581 | 1581 | 1581 | 1604 | 1604 | 1604 | 1496 | 1599 | 1604 | 1563 | 1604 | 1575 | 1594 | 1594 | 1594 | 1496 |
| multiplier | 2 | 5135 | 5135 | 5135 | 5016 | 5016 | 5016 | 5016 | 5016 | 5016 | 5135 | 4857 | 4995 | 4715 | 5016 | 5038 | 5016 | 5016 | 5016 | 5135 |
| multiplier | 4 | 2921 | 2921 | 2921 | 2606 | 2606 | 2606 | 2543 | 2543 | 2543 | 2921 | 2594 | 2573 | 2608 | 2543 | 2623 | 2543 | 2543 | 2543 | 2921 |
| qft | 2 | 2151 | 2151 | 2151 | 2268 | 2268 | 2268 | 2268 | 2268 | 2268 | 2151 | 2259 | 2268 | 2248 | 2268 | 2294 | 2268 | 2268 | 2268 | 2151 |
| qft | 4 | 1067 | 1067 | 1067 | 1045 | 1045 | 1045 | 1045 | 1045 | 1045 | 1072 | 1045 | 1045 | 1059 | 1045 | 1062 | 1045 | 1045 | 1045 | 1072 |

Variants ranked by geomean slowdown relative to the best variant per (workload, CR):

| policy_id | geomean_slowdown_vs_best_variant |
|---|---|
| dag_W4_tol64 | 1.029 |
| dag_W4_tol16 | 1.033 |
| dag_W8_cw0.0 | 1.034 |
| dag_W8_cw0.25 | 1.034 |
| dag_W8_cw1.0 | 1.034 |
| dag_W4_cw0.25 | 1.034 |
| dag_W4_cw1.0 | 1.034 |
| dag_W4_cw0.0 | 1.034 |
| dag_W4_urgcrit | 1.034 |
| dag_W4_tol4 | 1.035 |
| dag_W2_cw1.0 | 1.035 |
| dag_W2_cw0.0 | 1.035 |
| dag_W2_cw0.25 | 1.035 |
| dag_W4_urglevel | 1.051 |
| dag_W0_cw0.25 | 1.090 |
| dag_W0_cw0.0 | 1.090 |
| dag_W0_cw1.0 | 1.090 |
| dag_W4_noevict | 1.091 |
| dag_W8_noevict | 1.091 |

## 5. Seed variability (random Clifford+T, 5 seeds, CR=4, 2 banks, unlimited magic)

| policy_id | mean | std | min | max |
|---|---|---|---|---|
| dag_W4 | 1481.4 | 56.8 | 1435 | 1579 |
| dag_demand | 1491.2 | 45.1 | 1462 | 1568 |
| demand_belady | 2839.6 | 34.6 | 2784 | 2875 |
| demand_lru | 3010.0 | 45.0 | 2957 | 3063 |
| prefetch_belady_W4 | 2323.2 | 43.3 | 2263 | 2360 |
| prefetch_lru_W4 | 2996.2 | 41.9 | 2950 | 3043 |

DAG speedup over best fixed per seed: 1.63, 1.54, 1.57, 1.51, 1.63  (mean 1.58, std 0.05)

## 6. Transfer traffic and area (policy suite, unlimited magic, CR=4, 2 banks)

| workload | dag_W4 | dag_demand | demand_belady | demand_lru | prefetch_belady_W4 | prefetch_lru_W4 |
|---|---|---|---|---|---|---|
| adder_large | 216 | 210 | 142 | 154 | 154 | 154 |
| adder_small | 46 | 44 | 34 | 34 | 34 | 34 |
| high_parallel | 954 | 776 | 2194 | 2420 | 2492 | 2420 |
| multiplier | 1080 | 934 | 416 | 446 | 446 | 446 |
| qft | 342 | 330 | 106 | 166 | 140 | 166 |

Physical-qubit estimate depends on architecture only (not on policy):

| workload | cr_slots | 1 | 2 | 4 |
|---|---|---|---|---|
| adder_large | 2 | 22654 | 26510 | 29884 |
| adder_large | 4 | 23618 | 27474 | 30848 |
| adder_small | 2 | 16388 | 18798 | 24100 |
| adder_small | 4 | 17352 | 19762 | 25064 |
| high_parallel | 2 | 29884 | 31812 | 37596 |
| high_parallel | 4 | 30848 | 32776 | 38560 |
| multiplier | 2 | 21208 | 24100 | 29884 |
| multiplier | 4 | 22172 | 25064 | 30848 |
| qft | 2 | 17352 | 20244 | 24100 |
| qft | 4 | 18316 | 21208 | 25064 |
