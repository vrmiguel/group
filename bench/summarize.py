#!/usr/bin/env python3

import argparse
import csv
import os
import statistics
from collections import defaultdict


def stats(xs):
    return (statistics.mean(xs),
            statistics.stdev(xs) if len(xs) > 1 else 0.0,
            len(xs))

OPENING = {"launch", "prepare", "settle"}

# Mean of (t, value) points, each interval weighted by duration
def time_weighted_mean(points):
    if len(points) < 2:
        return points[0][1] if points else 0.0
    area = sum((t1 - t0) * (v0 + v1) / 2
               for (t0, v0), (t1, v1) in zip(points, points[1:]))
    return area / (points[-1][0] - points[0][0])


def trace_metrics(samples, processes, end_window=2.0, cpu_window=3.0):
    """Per-run metrics from one launch's continuous samples (bench.py --trace).

    samples:   dicts with t_seconds, phase, total_bytes, cpu_percent
    processes: dicts with pid, start_mach_ticks, cpu_seconds

    Returns MB / seconds / percent values:
      peak_mb      highest group memory from launch to the end of the run
      task_mean_mb time-weighted mean from the start of the first macro
                   (including Ghosthand's countdown) to the end
      end_mb       mean over the last `end_window` seconds
      cpu_seconds  total CPU time of the run: each process instance's last
                   cumulative reading (all processes start at launch)
      end_cpu_pct  mean CPU % over the last `cpu_window` seconds
    """
    pts = [(float(s["t_seconds"]), int(s["total_bytes"]) / 1e6) for s in samples]
    t_end = pts[-1][0]
    task = [(float(s["t_seconds"]), int(s["total_bytes"]) / 1e6)
            for s in samples if s["phase"] not in OPENING]
    end = [v for t, v in pts if t >= t_end - end_window]
    cpu_by_process = {}
    for p in processes:
        if p["cpu_seconds"]:
            key = (p["pid"], p["start_mach_ticks"])
            cpu_by_process[key] = max(cpu_by_process.get(key, 0.0),
                                      float(p["cpu_seconds"]))
    end_cpu = [float(s["cpu_percent"]) for s in samples
               if s["cpu_percent"] and float(s["t_seconds"]) >= t_end - cpu_window]
    return {
        "peak_mb": max(v for _, v in pts),
        "task_mean_mb": time_weighted_mean(task or pts),
        "end_mb": statistics.mean(end),
        "cpu_seconds": sum(cpu_by_process.values()),
        "end_cpu_pct": statistics.mean(end_cpu) if end_cpu else 0.0,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out", help="CSV path (default: <folder>/summary.csv)")
    args = p.parse_args()

    with open(os.path.join(args.folder, "runs.csv")) as f:
        rows = list(csv.DictReader(f))

    apps = list(dict.fromkeys(r["app"] for r in rows))
    checkpoints = list(dict.fromkeys(r["scenario"] for r in rows))
    startup = defaultdict(dict)             # app -> run -> seconds
    memory = defaultdict(lambda: defaultdict(dict))  # app -> checkpoint -> run -> MB
    for r in rows:
        startup[r["app"]][r["run"]] = int(r["startup_ms"]) / 1000
        memory[r["app"]][r["scenario"]][r["run"]] = int(r["total_bytes"]) / 1e6

    table = []
    for app in apps:
        table.append((app, "startup (s)", *stats(list(startup[app].values()))))
        for cp in checkpoints:
            table.append((app, f"{cp} (MB)", *stats(list(memory[app][cp].values()))))
        # paired differences between consecutive checkpoints of the same launch
        for a, b in zip(checkpoints, checkpoints[1:]):
            runs = sorted(set(memory[app][a]) & set(memory[app][b]), key=int)
            deltas = [memory[app][b][r] - memory[app][a][r] for r in runs]
            if deltas:
                table.append((app, f"{b} - {a} (MB)", *stats(deltas)))

    samples_path = os.path.join(args.folder, "samples.csv")
    if os.path.exists(samples_path):
        with open(samples_path) as f:
            samples = list(csv.DictReader(f))
        with open(os.path.join(args.folder, "samples_processes.csv")) as f:
            processes = list(csv.DictReader(f))
        by_run = defaultdict(lambda: ([], []))
        for s in samples:
            by_run[(s["app"], s["run"])][0].append(s)
        for p in processes:
            by_run[(p["app"], p["run"])][1].append(p)
        per_app = defaultdict(list)
        for (app, _), (s, p) in by_run.items():
            if s:
                per_app[app].append(trace_metrics(s, p))
        labels = [("peak_mb", "peak (MB)"), ("task_mean_mb", "task mean (MB)"),
                  ("end_mb", "end level (MB)"), ("cpu_seconds", "CPU time (s)"),
                  ("end_cpu_pct", "end CPU (%)")]
        for app in apps:
            for key, label in labels:
                values = [m[key] for m in per_app.get(app, [])]
                if values:
                    table.append((app, label, *stats(values)))
        table.sort(key=lambda row: apps.index(row[0]))  # keep each app together

    print(f"{'app':8s} {'metric':22s} {'mean':>9s} {'sd':>8s} {'n':>3s}")
    for app, metric, mean, sd, n in table:
        print(f"{app:8s} {metric:22s} {mean:9.2f} {sd:8.2f} {n:3d}")

    out = args.out or os.path.join(args.folder, "summary.csv")
    comma = lambda x: f"{x:.2f}".replace(".", ",")
    with open(out, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["app", "metric", "mean", "sd", "n"])
        for app, metric, mean, sd, n in table:
            w.writerow([app, metric, comma(mean), comma(sd), n])
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
