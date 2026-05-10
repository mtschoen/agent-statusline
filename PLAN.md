# schoen-claude-status — Plan

## Inbox

- [ ] Parallelize the JSONL walker in `_walk_pace_buckets` (statusline_lib.py).
      Cost-estimator's `analyze-month.py` already uses `concurrent.futures`
      ProcessPoolExecutor over per-file work — pattern-match it. Cold walk is
      currently ~750ms across ~1500 JSONLs single-threaded; parallelizing
      should get it well under 200ms and let us drop or shorten the 60s pace
      cache. After that, evaluate whether a native walker (Rust/C++) is worth
      it — it would ship as an **optional** dependency of schoen-claude-status
      (and cost-estimator) so the install stays light when users don't need
      it. Spec: input = list of JSONL paths + time range, output =
      (trailing_dollars, window_dollars). Pure cost-summing, no statusline
      coupling.
