# gto-moca-density-experiment — Executive Overview

**Status:** experiment design in progress; no measurements yet
**Repo:** `rossoctl/gto-moca-density-experiment`
**Date:** 2026-10-29


## Argument

We can reduce the amount of resources used in a cloud-managed
inference service by separating and dense-packing the "hands" —
sandboxes where AI tool calls execute.

### Canonical workload

* We identify a "canonical workload" and argue that it represents a
  typical AI workflow. For us this is swebench (or a subset).

* We record the execution of this canonical workload as a reproducible
  sequence that can be played back.

* We consider the resources used by just the tool calls during the
  replay of this sequence; not the LLM use or the harness.

### Density

* We define our target measure, "density" as "how many concurrent
  copies of the canonical workload replay can we run in a unit of
  compute resource".

* Our basic unit for compute resource is the smallest VM that can
  successfully run the canonical workload. Our baseline density is
  "1".

* We hold that the SBX0 architecture described by Paolo - in which
  sandboxes only run while executing tool calls - achieves higher
  densities compared to other architectures that pre-allocate compute
  resources and hold them for the entirety of the workload. This is
  because in normal use the sandboxes that run tool calls are mostly
  idle and unused.

* This number (how much denser SBX0 is than the baseline) is what we
  measure and present as proof of the dense-packing argument.

## Method

### Workload sequence

* The workload sequence is a predetermined and replayable set of tool
  calls. We use SWE-bench.

* We run Rong's swebench workload and record the tool calls. Rong is
  supplying "trajectories" -- timed sequences of tool calls while
  running SWE-bench.

* Test of correctness: replaying the sequence should at the very least
  produce the same exit codes for the tool calls as the original. In
  addition, the final state of the sandbox that the sequence is run on
  should match the state of the original sandbox. Rong is supplying
  "patch files" that are the outcomes of SWE-bench. We compare the
  generated patch files to our reference.

* Insight: the timing of the replay doesn't matter; it's
  predetermined. "Success" for a replay means that it produced the
  correct output


### Metrics to measure

Frontend:

* Time-to-interactive (TTI) percentiles 

* Time for tools to run, P50, P99? Timeout if tool calls take longer
  than the timeline permits? Treat it as failure?

* Success rate, "success" means that the tool calls all returned exit
  code 0, next level "success" means that the swebench repo is correct
  verified by checker tool

* Latency histogram
* other?

Backend:

* CPU: percentage, run-queue length, PSI cpu, ?
* Memory: free/available, page cache, swap in/out etc
* Disk/SSD/NVMe bandwidth and IOPS (snapshot restores), other?
* network: bandwidth, PPS (packet per second),other?

### Baseline measurement

* The baseline compute unit is the smallest virtual machine (with the
  fewest resources) that can run the workload sequence successfully.

* Standardized hardware for this experiment is many times larger than
  the baseline compute unit.  Our baseline number is "how many
  concurrent workloads can we pack into the hardware we have
  available, if we preallocate the resources for each run".  This
  number can be deduced with a division, so I would argue that we
  don't need to run the baseline experiment.

* We could do a more refined measurement and allow qemu virtual
  machines to overprovision memory. I would say "if time allows".
  Either case, our baseline is the number "B" of concurrent
  experimental workloads that can be run in our hardware.

### Dense-packing measurement (SBX0)

* This is the number we are after. We deploy SBX0 on hardware
  identical to what was used for the baseline. We find the number D of
  concurrent workloads that we can successfully run on this setup. We
  had better hope that D > B.

## Observations and details

* This system excludes LLM measurements and harness measurements
  (obvious)

* There is a major departure in the experimental method from earlier
  approaches tried by Paolo. Our experimental framework no longer
  includes and/or relies on an LLM. It's completely predictable and
  only involves sandboxes.

* Randomize timings (slightly) and offset job starts: to avoid
  artificial utilization peaks caused by synchronized tool calls.

## Work items [WIP]

* create a replayable sequence of tool calls by one of the methods
  described earlier. Prove that the sequence replay is
  correct. Demonstrate this re-run in a "normal" environment (a normal
  virtual machine). Develop a process to demonstrate the correctness
  of the run.

* Re-run this sequence in a mocactl sandbox environment. This means
  that every tool call in the sequence is prefixed (manually!) with
  sbx. Demonstrate correct execution.

* Create a scaling experiment that runs multiple copies of the replay
  sequence concurrently. My preferred approach to this is a "steady
  state" experiment with workload arrivals generated by an exponential
  distribution. This can be calculated to result in a predictable
  "workload pressure", and we can then calculate average workload
  delay and/or success rate.

## Questions [WIP]

* No LLM means the experiment is no longer subject to
  pi/claude/whatever vagaries. We run the experiments
  deterministically

* Where does the container based sandbox experiment come into this
  play?


