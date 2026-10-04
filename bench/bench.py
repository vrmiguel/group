#!/usr/bin/env python3
"""
Benchmark utility for CPU/memory usage + launch time across four DB clients: DBeaver, pgpad, pgAdmin, and DBGate

In every run, for each app:
  1. quits the app if it is running then wait --cooldown seconds
  2. launches it with ./launchwait, which reports the time until the main window is on screen
  3. waits --settle seconds;
  4. measure the app's process group with `group --csv`;
  5. quit the app.

the app order is rotated every run so that no app is always measured first.
with --checkpoints, steps 3–4 repeat within the same launch. Checkpoint labels
are stored in the scenario column

Outputs are:
  runs.csv         one row per (run, app, checkpoint): startup time and footprint
  processes.csv    one row per process per (run, app, checkpoint), in bytes
  environment.txt  hardware, OS, power state, app versions, tool commits
  screenshots/     with --macros: run<NN>-<app>-<checkpoint>.png
"""

import argparse
import csv
import datetime as dt
import hashlib
import os
import plistlib
import random
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GROUP = os.path.join(HERE, "..", "group")
LAUNCHWAIT = os.path.join(HERE, "launchwait")
GHOSTHAND = "ghosthand"

APPS = {
    "pgpad": "/Applications/pgpad.app",
    "dbeaver": "/Applications/DBeaver.app",
    "pgadmin": "/Applications/pgAdmin 4.app",
    "dbgate": "/Applications/DbGate.app",
}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def bundle_info(app_path):
    with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
        info = plistlib.load(f)
    return info.get("CFBundleIdentifier"), info.get("CFBundleShortVersionString")


def group_csv(app_path):
    """Returns (rows, warnings); rows is empty if the app is not running."""
    r = run([GROUP, "--app", app_path, "--csv"])
    if r.returncode != 0:
        return [], []
    rows = list(csv.DictReader(r.stdout.splitlines()))
    warnings = [l for l in r.stderr.splitlines() if l.startswith("warning")]
    return rows, warnings


def is_running(app_path):
    return bool(group_csv(app_path)[0])


def quit_app(app_path, timeout=15):
    """Terminates the app's process group with SIGTERM, escalating to SIGKILL.
    A regular quit request wouldn't work here because some of the clients have "are you sure you want to exit?" dialogs
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        rows = group_csv(app_path)[0]
        if not rows:
            return
        if sig == signal.SIGKILL:
            print(f"    {os.path.basename(app_path)} ignored SIGTERM, sending SIGKILL",
                  file=sys.stderr)
        for row in rows:
            try:
                os.kill(int(row["pid"]), sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not is_running(app_path):
                return
            time.sleep(0.25)
    sys.exit(f"error: could not quit {app_path}")


def group_samples(app_path):
    r = subprocess.run([GROUP, "--app", app_path, "--csv"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return list(csv.DictReader(r.stdout.splitlines()))


def ticks_to_seconds(ticks, numer, denom):
    return ticks * numer / denom / 1e9


def cpu_used(prev, cur):
    """CPU seconds the group used between two samples
    """
    prev_t, prev_cpu = prev
    _, cur_cpu = cur
    used = 0.0
    for key, (cpu, start_s) in cur_cpu.items():
        if key in prev_cpu:
            used += max(0.0, cpu - prev_cpu[key][0])
        elif start_s >= prev_t:
            used += cpu
    return used


class Sampler(threading.Thread):
    """Samples an app's process group every `interval` seconds until stops
    """

    def __init__(self, app_path, interval):
        super().__init__(daemon=True)
        self.app_path, self.interval = app_path, interval
        self.phase = "launch"
        self.samples = []    # (t, phase, total_bytes, n_processes, cpu_s, cpu_pct)
        self.processes = []  # (t, phase, pid, start_ticks, name, bytes, cpu_s)
        self._halt = threading.Event()

    def run(self):
        t0 = time.monotonic()
        prev = None
        while not self._halt.is_set():
            t = time.monotonic() - t0
            phase = self.phase
            rows = group_samples(self.app_path)
            if rows:
                numer = int(rows[0]["timebase_numer"])
                denom = int(rows[0]["timebase_denom"])
                sample_s = ticks_to_seconds(
                    max(int(r["sample_mach_ticks"]) for r in rows), numer, denom)
                cpus = {}
                for r in rows:
                    if r["cpu_seconds"]:
                        cpus[(r["pid"], r["start_mach_ticks"])] = (
                            float(r["cpu_seconds"]),
                            ticks_to_seconds(int(r["start_mach_ticks"]), numer, denom))
                cur = (sample_s, cpus)
                pct = ""
                if prev is not None and sample_s > prev[0]:
                    pct = f"{100 * cpu_used(prev, cur) / (sample_s - prev[0]):.1f}"
                total = sum(int(r["footprint_bytes"]) for r in rows)
                cpu_total = sum(c for c, _ in cpus.values())
                self.samples.append((f"{t:.3f}", phase, total, len(rows),
                                     f"{cpu_total:.4f}", pct))
                for r in rows:
                    self.processes.append((f"{t:.3f}", phase, r["pid"],
                                           r["start_mach_ticks"], r["name"],
                                           r["footprint_bytes"], r["cpu_seconds"]))
                prev = cur
            elapsed = time.monotonic() - t0 - t
            self._halt.wait(max(0.0, self.interval - elapsed))

    def stop(self):
        self._halt.set()
        self.join()


class SkipRun(Exception):
    """A launch that cannot be completed (e.g. a cancelled macro): it is
    discarded and recorded in skipped.csv, and the session continues."""


def check_macro(path):
    """Validates a macro without sending input; returns (ok, message)."""
    r = run([GHOSTHAND, "check", path])
    return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[0:1]


def play_macro(path, timeout=300):
    """Replays a macro; returns (exit code, stderr). Ghosthand counts down 3 s first."""
    try:
        r = run([GHOSTHAND, "play", path], timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout} s"
    return r.returncode, r.stderr.strip()


def clipboard_is(text):
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout == text


def set_clipboard(text):
    """Puts `text` on the clipboard and verifies it was not changed meanwhile."""
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    pasted = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    return pasted == text


def screenshot(path):
    subprocess.run(["screencapture", "-x", path], capture_output=True)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def git_rev(path):
    rev = run(["git", "-C", path, "rev-parse", "--short", "HEAD"]).stdout.strip()
    dirty = run(["git", "-C", path, "status", "--porcelain"]).stdout.strip()
    return f"{rev}{' (uncommitted changes)' if dirty else ''}" if rev else "unknown"


def write_environment(path, args, apps, extra=()):
    def sh(*cmd):
        return run(list(cmd)).stdout.strip()

    lines = [
        f"date: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"command: {' '.join(sys.argv)}",
        f"macOS: {sh('sw_vers', '-productVersion')} ({sh('sw_vers', '-buildVersion')})",
        f"model: {sh('sysctl', '-n', 'hw.model')}",
        f"cpu: {sh('sysctl', '-n', 'machdep.cpu.brand_string')}",
        f"memory: {int(sh('sysctl', '-n', 'hw.memsize')) / 2**30:.0f} GiB",
        f"power: {sh('pmset', '-g', 'batt').splitlines()[0]}",
        f"low power mode: {'lowpowermode 1' in sh('pmset', '-g')}",
        f"group/bench commit: {git_rev(os.path.join(HERE, '..'))}",
        f"warm-up launches per app: {args.warmup}, "
        f"settle: {args.settle} s, cooldown: {args.cooldown} s, "
        f"main window threshold: {args.min_width}x{args.min_height}, "
        f"checkpoint settle: {args.checkpoint_settle} s, "
        f"task window: {f'{args.task_window} s' if args.task_window else 'off'}, "
        f"trace: {f'every {args.trace_interval} s' if args.trace else 'off'}",
    ]
    for name, (app_path, _, version) in apps.items():
        lines.append(f"app {name}: {app_path} version {version}")
    lines.extend(extra)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def summarize(results):
    print("\nsummary (mean ± sd, n)")
    for name, observations in results.items():
        if not observations:
            print(f"  {name:8s} no valid runs")
            continue
        sd = lambda xs: statistics.stdev(xs) if len(xs) > 1 else 0.0
        # Each checkpoint repeats the launch timing; count each launch only once.
        startups = list({r["run"]: r["startup_ms"] for r in observations}.values())
        print(f"  {name:8s} startup {statistics.mean(startups):7.0f} ± {sd(startups):5.0f} ms"
              f"   n={len(startups)}")
        for scenario in dict.fromkeys(r["scenario"] for r in observations):
            totals = [r["total_bytes"] / 1e6 for r in observations
                      if r["scenario"] == scenario]
            print(f"    {scenario}: memory {statistics.mean(totals):7.1f} ± "
                  f"{sd(totals):5.1f} MB   n={len(totals)}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apps", default=",".join(APPS),
                   help=f"comma-separated subset of: {', '.join(APPS)}")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--scenario", default="idle",
                   help="label stored with every row (e.g. idle, post-query)")
    p.add_argument("--settle", type=float, default=30,
                   help="seconds to wait after the main window appears")
    p.add_argument("--cooldown", type=float, default=10,
                   help="seconds to wait after quitting, before the next launch")
    p.add_argument("--interactive", action="store_true",
                   help="after --settle, wait for Enter before measuring")
    p.add_argument("--checkpoints",
                   help="ordered comma-separated checkpoints within each launch; "
                        "requires --interactive or --macros (e.g. idle,query)")
    p.add_argument("--macros",
                   help="directory with <app>-<checkpoint>.ghost scripts replayed "
                        "with Ghosthand at each non-idle checkpoint")
    p.add_argument("--query",
                   help="file copied to the clipboard before every macro playback")
    p.add_argument("--task-window", type=float,
                   help="with --macros: measure each macro checkpoint this many "
                        "seconds after the macro starts (countdown included), "
                        "so every app is observed for the same duration; "
                        "replaces --checkpoint-settle for macro checkpoints")
    p.add_argument("--checkpoint-settle", type=float, default=0,
                   help="seconds to wait after each checkpoint confirmation before measuring")
    p.add_argument("--trace", action="store_true",
                   help="sample memory and CPU continuously, from launch until "
                        "the app is quit, into samples.csv and "
                        "samples_processes.csv")
    p.add_argument("--trace-interval", type=float, default=0.25,
                   help="seconds between continuous samples (default 0.25)")
    p.add_argument("--order", choices=["rotate", "random"], default="rotate",
                   help="app order within each round: 'rotate' shifts it by one "
                        "every round (counterbalancing); 'random' draws a new "
                        "order per round from --seed (randomized complete blocks, "
                        "each round being a block)")
    p.add_argument("--seed", type=int,
                   help="seed for --order random (default: drawn from the "
                        "system and recorded, so the schedule is reproducible)")
    p.add_argument("--warmup", type=int, default=1,
                   help="unrecorded launches per app before measuring, so the "
                        "first recorded run is not a cold start")
    p.add_argument("--min-width", type=int, default=800)
    p.add_argument("--min-height", type=int, default=500)
    p.add_argument("--out")
    args = p.parse_args()

    if args.runs < 1 or args.warmup < 0:
        p.error("--runs must be positive and --warmup nonnegative")
    if min(args.settle, args.cooldown, args.checkpoint_settle) < 0:
        p.error("wait durations must be nonnegative")
    if args.trace_interval <= 0:
        p.error("--trace-interval must be positive")
    if args.task_window is not None and (args.task_window <= 0 or not args.macros):
        p.error("--task-window must be positive and requires --macros")
    if args.interactive and args.macros:
        p.error("--interactive and --macros are mutually exclusive")
    if args.query and not args.macros:
        p.error("--query requires --macros")
    if args.checkpoints is not None:
        if not (args.interactive or args.macros):
            p.error("--checkpoints requires --interactive or --macros")
        checkpoints = [label.strip() for label in args.checkpoints.split(",")]
        if any(not label for label in checkpoints) or len(set(checkpoints)) != len(checkpoints):
            p.error("checkpoint labels must be nonempty and unique")
    elif args.macros:
        p.error("--macros requires --checkpoints (e.g. idle,query)")
    else:
        checkpoints = [args.scenario]

    for tool in (GROUP, LAUNCHWAIT):
        if not os.access(tool, os.X_OK):
            sys.exit(f"error: {tool} not built (see README in bench/)")

    names = [n.strip() for n in args.apps.split(",") if n.strip()]
    if not names or len(set(names)) != len(names):
        p.error("--apps must contain unique application names")
    unknown = [n for n in names if n not in APPS]
    if unknown:
        sys.exit(f"error: unknown app(s): {', '.join(unknown)}")
    apps = {n: (APPS[n], *bundle_info(APPS[n])) for n in names}

    # Validate every macro and the query before touching any app.
    macros, prepare, query, extra_env = {}, {}, None, []
    if args.macros:
        if not shutil.which(GHOSTHAND):
            sys.exit(f"error: {GHOSTHAND} not found in PATH")
        for name in names:
            for scenario in checkpoints:
                if scenario == "idle":
                    continue
                path = os.path.join(args.macros, f"{name}-{scenario}.ghost")
                if not os.path.isfile(path):
                    sys.exit(f"error: missing macro {path}")
                ok, msg = check_macro(path)
                if not ok:
                    sys.exit(f"error: invalid macro {path}: {' '.join(msg)}")
                macros[(name, scenario)] = path
                extra_env.append(f"macro {name}/{scenario}: {path} sha256 {sha256(path)}")
        for name in names:
            # optional: played right after launch, before --settle (e.g. maximize)
            path = os.path.join(args.macros, f"{name}-prepare.ghost")
            if os.path.isfile(path):
                ok, msg = check_macro(path)
                if not ok:
                    sys.exit(f"error: invalid macro {path}: {' '.join(msg)}")
                prepare[name] = path
                extra_env.append(f"macro {name}/prepare: {path} sha256 {sha256(path)}")
        extra_env.insert(0, f"ghosthand: {run([GHOSTHAND, '--version']).stdout.strip()}")
        if args.query:
            with open(args.query) as f:
                query = f.read()
            extra_env.append(f"query: {args.query} sha256 {sha256(args.query)}")

    out = args.out or os.path.join(
        HERE, "results", f"{dt.datetime.now():%Y%m%d-%H%M%S}-{args.scenario}")
    os.makedirs(out, exist_ok=True)
    if macros:
        os.makedirs(os.path.join(out, "screenshots"), exist_ok=True)
    # the whole schedule is fixed and saved before collection starts
    if args.order == "random":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
        rng = random.Random(seed)
        schedule = [rng.sample(names, len(names)) for _ in range(args.runs)]
        extra_env.append(f"order: random, seed {seed}")
    else:
        schedule = [names[(i - 1) % len(names):] + names[:(i - 1) % len(names)]
                    for i in range(1, args.runs + 1)]
        extra_env.append("order: rotate")
    with open(os.path.join(out, "schedule.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run", "position", "app"])
        for i, order in enumerate(schedule, 1):
            for pos, name in enumerate(order, 1):
                w.writerow([i, pos, name])
    write_environment(os.path.join(out, "environment.txt"), args, apps, extra_env)

    runs_f = open(os.path.join(out, "runs.csv"), "w", newline="")
    procs_f = open(os.path.join(out, "processes.csv"), "w", newline="")
    runs_w = csv.writer(runs_f)
    procs_w = csv.writer(procs_f)
    runs_w.writerow(["run", "app", "scenario", "timestamp", "startup_ms",
                     "total_bytes", "n_processes", "warnings", "position"])
    procs_w.writerow(["run", "app", "scenario", "timestamp",
                      "pid", "name", "footprint_bytes"])
    if args.trace:
        samples_f = open(os.path.join(out, "samples.csv"), "w", newline="")
        sproc_f = open(os.path.join(out, "samples_processes.csv"), "w", newline="")
        samples_w = csv.writer(samples_f)
        sproc_w = csv.writer(sproc_f)
        samples_w.writerow(["run", "app", "t_seconds", "phase", "total_bytes",
                            "n_processes", "cpu_seconds", "cpu_percent"])
        sproc_w.writerow(["run", "app", "t_seconds", "phase", "pid",
                          "start_mach_ticks", "name", "footprint_bytes", "cpu_seconds"])

    skipped_f = open(os.path.join(out, "skipped.csv"), "w", newline="")
    skipped_w = csv.writer(skipped_f)
    skipped_w.writerow(["run", "app", "timestamp", "reason"])
    n_skipped = 0

    print(f"writing to {out}")
    for _, (app_path, bundle_id, _) in apps.items():
        quit_app(app_path)

    for w in range(1, args.warmup + 1):
        for name, (app_path, bundle_id, _) in apps.items():
            time.sleep(args.cooldown)
            r = run([LAUNCHWAIT, app_path, "--min-width", str(args.min_width),
                     "--min-height", str(args.min_height)])
            if r.returncode != 0:
                sys.exit(f"error: launchwait failed for {name}: {r.stderr.strip()}")
            print(f"  warm-up {w}/{args.warmup}  {name:8s} startup "
                  f"{float(r.stdout.split()[1]):6.0f} ms (not recorded)")
            time.sleep(5)
            quit_app(app_path)

    results = {n: [] for n in names}
    for i in range(1, args.runs + 1):
        for position, name in enumerate(schedule[i - 1], 1):
            app_path, bundle_id, _ = apps[name]
            time.sleep(args.cooldown)

            sampler = Sampler(app_path, args.trace_interval) if args.trace else None

            def mark(phase):
                if sampler:
                    sampler.phase = phase

            def fail(message):
                if sampler:
                    sampler.stop()
                quit_app(app_path)
                sys.exit(message)

            if sampler:
                sampler.start()  # phase "launch" until the main window appears
            r = run([LAUNCHWAIT, app_path, "--min-width", str(args.min_width),
                     "--min-height", str(args.min_height)])
            if r.returncode != 0:
                fail(f"error: launchwait failed for {name}: {r.stderr.strip()}")
            _, startup_ms = r.stdout.split()
            startup_ms = float(startup_ms)

            # rows are kept until the whole launch succeeds, so a skipped
            # launch leaves no partial data behind
            pending_runs, pending_procs, pending_results = [], [], []
            try:
              if name in prepare:
                mark("prepare")
                code, err = play_macro(prepare[name])
                if code != 0:
                    raise SkipRun(f"ghosthand exit {code} in 'prepare': {err}")
              mark("settle")
              time.sleep(args.settle)
              overrun = None
              for scenario in checkpoints:
                if args.interactive:
                    mark(scenario)
                    input(f"  [{name}, run {i}] perform the '{scenario}' "
                          f"scenario, then press Enter to measure... ")
                    mark(f"after-{scenario}")
                    time.sleep(args.checkpoint_settle)
                elif (name, scenario) in macros:
                    if query is not None and not set_clipboard(query):
                        raise SkipRun("clipboard did not keep the query "
                                      "(another app or Universal Clipboard changed it?)")
                    mark(scenario)  # includes Ghosthand's 3 s countdown
                    window_start = time.monotonic()
                    code, err = play_macro(macros[(name, scenario)])
                    if code != 0:
                        # 130 = cancelled, e.g. by external input
                        raise SkipRun(f"ghosthand exit {code} in '{scenario}': "
                                      f"{err.splitlines()[-1] if err else ''}")
                    if query is not None and not clipboard_is(query):
                        # something replaced the query while the macro ran
                        # (e.g. Universal Clipboard), so it may have pasted
                        # the wrong text
                        raise SkipRun("clipboard changed during the macro")
                    mark(f"after-{scenario}")
                    if args.task_window:
                        # same observation length for every app: wait out
                        # whatever is left of the window after the macro
                        remaining = args.task_window - (time.monotonic() - window_start)
                        if remaining < 0:
                            overrun = (f"macro overran the {args.task_window:g} s "
                                       f"task window by {-remaining:.1f} s")
                            print(f"    warning: {overrun}", file=sys.stderr)
                        time.sleep(max(0.0, remaining))
                    else:
                        time.sleep(args.checkpoint_settle)

                rows, warnings = group_csv(app_path)
                if overrun:
                    warnings.append(f"warning: {overrun}")
                    overrun = None
                if not rows:
                    raise SkipRun("the app is no longer running")
                ts = dt.datetime.now().isoformat(timespec="seconds")
                total = sum(int(row["footprint_bytes"]) for row in rows)
                pending_runs.append([i, name, scenario, ts, f"{startup_ms:.0f}",
                                     total, len(rows), " | ".join(warnings), position])
                for row in rows:
                    pending_procs.append([i, name, scenario, ts, row["pid"],
                                          row["name"], row["footprint_bytes"]])
                if macros:
                    # taken after measuring, so it cannot affect the reading
                    screenshot(os.path.join(out, "screenshots",
                                            f"run{i:02d}-{name}-{scenario}.png"))
                pending_results.append({"run": i, "scenario": scenario, "startup_ms": startup_ms, "total_bytes": total})
                print(f"  run {i:2d}/{args.runs}  {name:8s} startup {startup_ms:6.0f} ms"
                      f"  {scenario} memory {total / 1e6:7.1f} MB  ({len(rows)} processes)"
                      f"{'  WARN' if warnings else ''}")

            except SkipRun as e:
                if sampler:
                    sampler.stop()  # its samples are discarded
                quit_app(app_path)
                n_skipped += 1
                skipped_w.writerow([i, name, dt.datetime.now().isoformat(timespec="seconds"),
                                    str(e)])
                skipped_f.flush()
                print(f"  run {i:2d}/{args.runs}  {name:8s} SKIPPED: {e}", file=sys.stderr)
                continue

            runs_w.writerows(pending_runs)
            procs_w.writerows(pending_procs)
            runs_f.flush()
            procs_f.flush()
            results[name].extend(pending_results)
            if sampler:
                sampler.stop()
                for row in sampler.samples:
                    samples_w.writerow([i, name, *row])
                for row in sampler.processes:
                    sproc_w.writerow([i, name, *row])
                samples_f.flush()
                sproc_f.flush()
                peak = max((s[2] for s in sampler.samples), default=0)
                print(f"           trace: {len(sampler.samples)} samples, "
                      f"peak {peak / 1e6:.1f} MB")
            quit_app(app_path)

    runs_f.close()
    procs_f.close()
    skipped_f.close()
    if n_skipped:
        print(f"\n{n_skipped} launch(es) skipped; see skipped.csv")
    if args.trace:
        samples_f.close()
        sproc_f.close()
    summarize(results)


if __name__ == "__main__":
    main()
