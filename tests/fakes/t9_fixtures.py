"""Team T9 fixture text.

``POWER_REPORT`` is the header, summary, supply and confidence sections of the real
routed report ``fpga/dfx/build_mint_2026_09/shell_proj/shell_proj.runs/impl_1/
shell_top_power_routed.rpt`` (mps3-nanosoc-platform, Vivado 2024.1, 2026-09-15),
copied read-only and trimmed. ``power_report(total=...)`` rewrites the summary
numbers so tests can tell several reports apart.
"""

from __future__ import annotations

POWER_REPORT = """\
Copyright 1986-2022 Xilinx, Inc. All Rights Reserved. Copyright 2022-2024 Advanced Micro Devices, Inc. All Rights Reserved.
-------------------------------------------------------------------------------------------------------------------------------------------------
| Tool Version     : Vivado v.2024.1 (lin64) Build 5076996 Wed May 22 18:36:09 MDT 2024
| Date             : Tue Sep 15 15:08:16 2026
| Host             : srv03335 running 64-bit Red Hat Enterprise Linux release 8.10 (Ootpa)
| Command          : report_power -file shell_top_power_routed.rpt -pb shell_top_power_summary_routed.pb -rpx shell_top_power_routed.rpx
| Design           : shell_top
| Device           : xcku115-flvb1760-1-c
| Design State     : routed
| Grade            : commercial
| Process          : typical
| Characterization : Production
-------------------------------------------------------------------------------------------------------------------------------------------------

Power Report

Table of Contents
-----------------
1. Summary
1.1 On-Chip Components
1.2 Power Supply Summary
1.3 Confidence Level
2. Settings
2.1 Environment
2.2 Clock Constraints
3. Detailed Reports
3.1 By Hierarchy

1. Summary
----------

+--------------------------+--------------+
| Total On-Chip Power (W)  | {total}        |
| Design Power Budget (W)  | Unspecified* |
| Power Budget Margin (W)  | NA           |
| Dynamic (W)              | {dynamic}        |
| Device Static (W)        | 1.270        |
| Effective TJA (C/W)      | 1.1          |
| Max Ambient (C)          | 83.1         |
| Junction Temperature (C) | {tj}         |
| Confidence Level         | Low          |
| Setting File             | ---          |
| Simulation Activity File | ---          |
| Design Nets Matched      | NA           |
+--------------------------+--------------+
* Specify Design Power Budget using, set_operating_conditions -design_power_budget <value in Watts>


1.1 On-Chip Components
----------------------

+--------------------------+-----------+----------+-----------+-----------------+
| On-Chip                  | Power (W) | Used     | Available | Utilization (%) |
+--------------------------+-----------+----------+-----------+-----------------+
| Clocks                   |     0.041 |        9 |       --- |             --- |
| CARRY8                   |    <0.001 |      166 |     82920 |            0.20 |
| Static Power             |     1.270 |          |           |                 |
| Total                    |     {total} |          |           |                 |
+--------------------------+-----------+----------+-----------+-----------------+


1.2 Power Supply Summary
------------------------

+------------+-------------+-----------+-------------+------------+-------------+-------------+------------+
| Source     | Voltage (V) | Total (A) | Dynamic (A) | Static (A) | Powerup (A) | Budget (A)  | Margin (A) |
+------------+-------------+-----------+-------------+------------+-------------+-------------+------------+
| Vccint     |       0.950 |     0.678 |       0.231 |      0.447 |       NA    | Unspecified | NA         |
| Vccaux     |       1.800 |     0.341 |       0.104 |      0.236 |       NA    | Unspecified | NA         |
| Vccbram    |       0.950 |     0.045 |       0.001 |      0.043 |       NA    | Unspecified | NA         |
+------------+-------------+-----------+-------------+------------+-------------+-------------+------------+


1.3 Confidence Level
--------------------

+-----------------------------+------------+--------------------------------------------------------+
| User Input Data             | Confidence | Details                                                |
+-----------------------------+------------+--------------------------------------------------------+
| Design implementation state | High       | Design is routed                                       |
| Overall confidence level    | Low        |                                                        |
+-----------------------------+------------+--------------------------------------------------------+


2. Settings
-----------

2.1 Environment
---------------

+-----------------------+--------------------------+
| Ambient Temp (C)      | 25.0                     |
| ThetaJA (C/W)         | 1.1                      |
+-----------------------+--------------------------+
"""

TIMING_REPORT = """\
Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
| Command      : report_timing_summary -file timing_rm_nanosoc.rpt
| Design       : shell_top
Timing Summary Report
    WNS(ns)      TNS(ns)
      0.412        0.000
"""


def power_report(*, total: str = "1.711", dynamic: str = "0.442", tj: str = "26.9") -> str:
    return (POWER_REPORT.replace("{total}", total).replace("{dynamic}", dynamic)
            .replace("{tj}", tj))
