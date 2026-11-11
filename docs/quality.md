# Demonstration quality scoring

`python -m coppertop.quality DATASET OUT` scores every episode of a LeRobot v3 dataset on two separate axes and writes
`episodes.parquet` (one row per episode, every metric and reason) and `summary.json` (counts, limits, a rolling trend
by episode index). The dataset is read-only; output refuses an existing directory.

* **Capture** asks whether the recording can be trusted: clock gaps, frozen-pose runs, frame-to-frame jumps faster
  than `max_speed`, drift beyond `max_drift` from the episode's median position, and segments shorter than
  `truncated_fraction` of the task median. A capture fault says nothing about the collector, so those episodes get
  no execution score.
* **Execution** compares capture-clean episodes only with others of the same task: robust z-scores (median/MAD, with a
  mean-deviation fallback when most peers are identical) for duration, idle share, longest interior pause and
  smoothness (log dimensionless jerk on lightly smoothed position). The top `flag_quantile` of the composite is
  flagged with its worst reasons; three or more grasps flag regrasping.
* **Grasps** are closing travel relative to each episode's own open width (a third of the dataset's open/closed
  spread). An absolute threshold misreads wider objects as missed grasps; on `lerobot/umi_cup_in_the_wild` it reported
  80 graspless episodes where travel-based detection finds 3. No grasp is reported as ambiguous, never as failure.

Done is not success: no outcome is inferred. Without collector or scene identifiers the trend is by collection order,
not by person. `--position`, `--gripper` and `--state` adapt the layout; `--gripper -1` disables grasp counting.

Measured on `lerobot/umi_cup_in_the_wild` (revision `c16ab60`, signal Parquet only, 1,447 episodes, ~6 s on CPU):
14 capture faults, 81 execution flags, 3 ambiguous. Fixture tests: `tests/test_quality.py`.
