# Hybrid ATPG-Guided DFT Framework

## Overview
This repository implements a **Hybrid Design-for-Testability (DFT) Framework** developed for Master's research. It addresses the limitations of conventional static testability measures (like SCOAP) by using a dynamic, **ATPG-Guided** approach to improve fault coverage in complex sequential circuits.

The methodology is divided into two stages:
* **Stage 1:** Inversion-Based Test Point Insertion (TPI) targeting Random-Pattern-Resistant (RPR) faults.
* **Stage 2:** Graph-based Partial Scan insertion for loop breaking (Sequential Depth Reduction).

*Note: This repository currently focuses on the implementation of Stage 1.*

## Key Features
* **Closed-Loop Profiling:** Uses Synopsys TetraMAX to identify actual RPR faults rather than estimating them.
* **Cone Intersection Heuristic:** A custom Python engine that performs backward topological traversal (Backward Cone Tracing) to find the "Reconvergent Root" nodes responsible for blocking multiple faults.
* **Automated Insertion:** Automatically generates TCL scripts to insert XOR-based inversion logic within Synopsys Design Compiler.

## Prerequisites
* **Language:** Python 3.x
* **EDA Tools:**
    * Synopsys Design Compiler (`dc_shell`)
    * Synopsys TetraMAX (`tmax`)
* **Library:** SAED 32nm Standard Cell Library (or compatible)

## Workflow (Stage 1)

1.  **Initial Profiling (TetraMAX):**
    Run the profiling script to identify "Hard" faults (Aborted/Undetected).
    ```bash
    tmax -shell -f scripts/step1_profile.tcl
    ```

2.  **Node Selection (Python):**
    Parse the failure report and run the Intersection Analysis to find the best TPI locations.
    ```bash
    python3 scripts/stage1_tpi_selector.py
    ```
    *Output:* Generates `insert_tpi_logic.tcl`.

3.  **Physical Insertion (Design Compiler):**
    Apply the TPI logic to the netlist.
    ```bash
    dc_shell -f scripts/step1_insert.tcl
    ```

## Algorithm Details
The node selection logic utilizes a **Greedy Intersection Heuristic**:
1.  **Map:** Maps undetected faults to physical netlist nodes.
2.  **Trace:** Performs recursive backward traversal to identify the "Fan-In Cone" of every victim fault.
3.  **Score:** Calculates a *Fault Impact Score* based on the overlap of these cones.
4.  **Select:** Inserts Inversion Logic (XOR) at the nodes with the highest intersection score.

## Author
[Your Name]
Master's Thesis Research
[University Name]
