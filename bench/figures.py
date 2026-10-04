#!/usr/bin/env python3

import argparse
import csv
import os
import statistics
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from summarize import trace_metrics, OPENING  # noqa: E402

APPS = {"pgpad": "/Applications/pgpad.app", "dbeaver": "/Applications/DBeaver.app",
        "pgadmin": "/Applications/pgAdmin 4.app", "dbgate": "/Applications/DbGate.app"}
LABELS = {"pgpad": "pgpad", "dbeaver": "DBeaver", "pgadmin": "pgAdmin 4", "dbgate": "DbGate"}

# for fig6: process name -> category
CATEGORIES = [
    ("Processo principal", {"pgpad-tauri", "dbeaver", "pgAdmin 4", "DbGate"}),
    ("Conteúdo web (interface)", {"com.apple.WebKit.WebContent",
                                  "pgAdmin 4 Helper (Renderer)", "DbGate Helper (Renderer)"}),
    ("GPU e rede do motor web", {"com.apple.WebKit.GPU", "com.apple.WebKit.Networking",
                                 "pgAdmin 4 Helper", "DbGate Helper (GPU)"}),
    # dbgate has unnamed helpers which are Node.js workers plus Electron's network
    # process, which cant be told apart by name
    ("Servidor e processos auxiliares", {"Python", "DbGate Helper"}),
]
OTHER = "Outros (serviços do sistema)"


def category(name):
    for label, names in CATEGORIES:
        if name in names:
            return label
    return OTHER


def num(x, digits=2):
    return f"{x:.{digits}f}".replace(".", ",")


def write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


def load(folders):
    """Pooled (samples, processes, runs) rows keyed by ((session, run), app)."""
    by_run = defaultdict(lambda: {"samples": [], "processes": [], "run": None})
    for i, folder in enumerate(folders):
        excluded = set()
        path = os.path.join(folder, "excluded.csv")
        if os.path.exists(path):
            with open(path) as f:
                excluded = {(int(e["run"]), e["app"]) for e in csv.DictReader(f)}
        for name, key in (("samples.csv", "samples"),
                          ("samples_processes.csv", "processes"), ("runs.csv", "run")):
            with open(os.path.join(folder, name)) as f:
                for r in csv.DictReader(f):
                    if (int(r["run"]), r["app"]) in excluded:
                        continue
                    k = ((i, int(r["run"])), r["app"])
                    if key == "run":
                        by_run[k]["run"] = r
                    else:
                        by_run[k][key].append(r)
    # keep only complete launches (a runs.csv row and a trace)
    return {k: v for k, v in by_run.items() if v["run"] and v["samples"]}


def stats(xs):
    return (statistics.mean(xs), statistics.stdev(xs) if len(xs) > 1 else 0.0, len(xs))


def bundle_mb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout
    return int(out.split()[0]) * 1024 / 1e6


def interpolate(points, t):
    """Linear interpolation of (t, v) points at time t (None outside the range)."""
    if t < points[0][0] or t > points[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folders", nargs="+", metavar="folder")
    p.add_argument("--out", required=True)
    p.add_argument("--preview", action="store_true")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    launches = load(args.folders)
    apps = [a for a in APPS if any(app == a for (_, app) in launches)]
    metrics = defaultdict(list)   # app -> [trace metrics per run]
    startups = defaultdict(list)
    for (run, app), v in launches.items():
        m = trace_metrics(v["samples"], v["processes"])
        m["run"] = run
        metrics[app].append(m)
        startups[app].append(int(v["run"]["startup_ms"]) / 1000)

    # --- table with every metric
    keys = [("startup", "Tempo de inicialização (s)"), ("peak_mb", "Pico de memória (MB)"),
            ("task_mean_mb", "Memória média na tarefa (MB)"),
            ("end_mb", "Memória ao final da tarefa (MB)"),
            ("cpu_seconds", "Tempo de processamento (s)"),
            ("end_cpu_pct", "Processamento ao final (%)")]
    table = {}
    for app in apps:
        for key, _ in keys:
            xs = startups[app] if key == "startup" else [m[key] for m in metrics[app]]
            table[(app, key)] = stats(xs)
    write(os.path.join(args.out, "tabela-metricas.csv"),
          ["Métrica"] + [f"{LABELS[a]} (média)" for a in apps]
          + [f"{LABELS[a]} (desvio padrão)" for a in apps] + ["n"],
          [[label] + [num(table[(a, k)][0]) for a in apps]
           + [num(table[(a, k)][1]) for a in apps]
           + ["/".join(str(table[(a, k)][2]) for a in apps)]
           for k, label in keys])

    # --- fig1: installed size
    sizes = {a: bundle_mb(APPS[a]) for a in apps}
    write(os.path.join(args.out, "fig1-tamanho-instalado.csv"),
          ["Aplicação", "Tamanho instalado (MB)"],
          [[LABELS[a], num(sizes[a], 1)] for a in apps])

    # --- fig2, fig3, fig4
    write(os.path.join(args.out, "fig2-inicializacao.csv"),
          ["Aplicação", "Média (s)", "Desvio padrão (s)", "n"],
          [[LABELS[a], num(table[(a, "startup")][0]), num(table[(a, "startup")][1]),
            table[(a, "startup")][2]] for a in apps])
    mem = [("peak_mb", "Pico"), ("task_mean_mb", "Média na tarefa"), ("end_mb", "Ao final")]
    write(os.path.join(args.out, "fig3-memoria.csv"),
          ["Aplicação"] + [f"{l} (MB)" for _, l in mem] + [f"{l} DP (MB)" for _, l in mem],
          [[LABELS[a]] + [num(table[(a, k)][0], 1) for k, _ in mem]
           + [num(table[(a, k)][1], 1) for k, _ in mem] for a in apps])
    write(os.path.join(args.out, "fig4-cpu.csv"),
          ["Aplicação", "Tempo de processamento (s)", "DP (s)",
           "Processamento ao final (%)", "DP (%)"],
          [[LABELS[a], num(table[(a, "cpu_seconds")][0]), num(table[(a, "cpu_seconds")][1]),
            num(table[(a, "end_cpu_pct")][0]), num(table[(a, "end_cpu_pct")][1])]
           for a in apps])

    # --- fig5: representative run = peak closest to the app's median peak
    chosen = {}
    for app in apps:
        median_peak = statistics.median(m["peak_mb"] for m in metrics[app])
        best = min(metrics[app], key=lambda m: abs(m["peak_mb"] - median_peak))
        chosen[app] = best["run"]
    series_mem, series_cpu, task_start = {}, {}, {}
    for app in apps:
        samples = launches[(chosen[app], app)]["samples"]
        series_mem[app] = [(float(s["t_seconds"]), int(s["total_bytes"]) / 1e6) for s in samples]
        series_cpu[app] = [(float(s["t_seconds"]), float(s["cpu_percent"]))
                           for s in samples if s["cpu_percent"]]
        task_start[app] = next(float(s["t_seconds"]) for s in samples
                               if s["phase"] not in OPENING)
    t_max = max(pts[-1][0] for pts in series_mem.values())
    grid = [i * 0.25 for i in range(int(t_max / 0.25) + 1)]
    for name, series in (("fig5-memoria-tempo.csv", series_mem),
                         ("fig5-cpu-tempo.csv", series_cpu)):
        rows = []
        for t in grid:
            vals = [interpolate(series[a], t) for a in apps]
            rows.append([num(t)] + ["" if v is None else num(v, 1) for v in vals])
        write(os.path.join(args.out, name), ["Tempo (s)"] + [LABELS[a] for a in apps], rows)
    write(os.path.join(args.out, "fig5-execucoes.csv"),
          ["Aplicação", "Sessão", "Execução", "Pico (MB)", "Mediana dos picos (MB)",
           "Início da tarefa (s)"],
          [[LABELS[a], chosen[a][0] + 1, chosen[a][1],
            num(next(m["peak_mb"] for m in metrics[a] if m["run"] == chosen[a]), 1),
            num(statistics.median(m["peak_mb"] for m in metrics[a]), 1),
            num(task_start[a])] for a in apps])

    # --- fig6: memory by process category at each run's last sample, averaged
    cats = [c for c, _ in CATEGORIES] + [OTHER]
    per_app = {}
    for app in apps:
        per_run = []
        for (run, a), v in launches.items():
            if a != app:
                continue
            last_t = max(float(r["t_seconds"]) for r in v["processes"])
            totals = defaultdict(float)
            for r in v["processes"]:
                if float(r["t_seconds"]) == last_t:
                    totals[category(r["name"])] += int(r["footprint_bytes"]) / 1e6
            per_run.append(totals)
        per_app[app] = {c: statistics.mean(t[c] for t in per_run) for c in cats}
    write(os.path.join(args.out, "fig6-processos.csv"),
          ["Aplicação"] + [f"{c} (MB)" for c in cats],
          [[LABELS[a]] + [num(per_app[a][c], 1) for c in cats] for a in apps])

    print(f"wrote CSVs to {args.out}  (runs per app: "
          + ", ".join(f"{LABELS[a]} {len(metrics[a])}" for a in apps) + ")")

    if args.preview:
        previews(args.out, apps, table, sizes, series_mem, series_cpu, task_start,
                 per_app, cats)


def previews(out, apps, table, sizes, series_mem, series_cpu, task_start, per_app, cats):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    arial = "/System/Library/Fonts/Supplemental/Arial.ttf"
    if os.path.exists(arial):
        font_manager.fontManager.addfont(arial)
        plt.rcParams["font.family"] = "Arial"
    plt.rcParams.update({"font.size": 10, "axes.linewidth": 1.5,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": False, "figure.facecolor": "white",
                         "axes.facecolor": "white", "savefig.dpi": 200})
    grays = ["#222222", "#666666", "#999999", "#cccccc"]
    names = [LABELS[a] for a in apps]
    pdir = os.path.join(out, "previews")
    os.makedirs(pdir, exist_ok=True)

    def bars(ax, values, errors, ylabel, fmt):
        b = ax.bar(names, values, yerr=errors, color="#555555", capsize=4,
                   error_kw={"elinewidth": 1})
        ax.bar_label(b, labels=[fmt(v) for v in values], padding=3, fontsize=9)
        ax.set_ylabel(ylabel)
        ax.tick_params(width=1.5)

    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    bars(ax, [sizes[a] for a in apps], None, "Tamanho instalado (MB)",
         lambda v: num(v, 1))
    fig.tight_layout(); fig.savefig(os.path.join(pdir, "fig1.png")); plt.close(fig)

    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    bars(ax, [table[(a, "startup")][0] for a in apps],
         [table[(a, "startup")][1] for a in apps], "Tempo de inicialização (s)",
         lambda v: num(v))
    fig.tight_layout(); fig.savefig(os.path.join(pdir, "fig2.png")); plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.3, 3.4))
    keys = [("peak_mb", "Pico"), ("task_mean_mb", "Média na tarefa"), ("end_mb", "Ao final")]
    width = 0.26
    for j, (k, label) in enumerate(keys):
        xs = [i + (j - 1) * width for i in range(len(apps))]
        ax.bar(xs, [table[(a, k)][0] for a in apps], width,
               yerr=[table[(a, k)][1] for a in apps], color=grays[j], capsize=3,
               label=label, error_kw={"elinewidth": 1})
    ax.set_xticks(range(len(apps)), names)
    ax.set_ylabel("Memória (MB)")
    ax.legend(frameon=False, ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=9)
    ax.tick_params(width=1.5)
    fig.tight_layout(); fig.savefig(os.path.join(pdir, "fig3.png")); plt.close(fig)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7, 3.2))
    bars(a1, [table[(a, "cpu_seconds")][0] for a in apps],
         [table[(a, "cpu_seconds")][1] for a in apps], "Tempo de processamento (s)",
         lambda v: num(v))
    bars(a2, [table[(a, "end_cpu_pct")][0] for a in apps],
         [table[(a, "end_cpu_pct")][1] for a in apps], "Processamento ao final (%)",
         lambda v: num(v, 1))
    for ax, letter in ((a1, "A"), (a2, "B")):
        ax.text(-0.18, 1.02, letter, transform=ax.transAxes, fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(os.path.join(pdir, "fig4.png")); plt.close(fig)

    # fig5: small multiples, one panel per app on shared axes, with the task
    # window (from the start of the macro, 18 s) shaded
    t_end = max(pts[-1][0] for pts in series_mem.values())
    for series, ylabel, fname in ((series_mem, "Memória (MB)", "fig5.png"),
                                  (series_cpu, "Processamento (%)", "fig5-cpu.png")):
        fig, axes = plt.subplots(2, 2, figsize=(6.6, 4.6), sharex=True, sharey=True)
        for ax, a, letter in zip(axes.flat, apps, "ABCD"):
            ax.axvspan(task_start[a], task_start[a] + 18, color="#e6e6e6", linewidth=0)
            t, v = zip(*series[a])
            ax.plot(t, v, color="black", linewidth=1.2)
            ax.set_title(LABELS[a], fontsize=10, loc="center")
            ax.text(0.0, 1.06, letter, transform=ax.transAxes, fontsize=11,
                    fontweight="bold")
            ax.set_xlim(0, t_end)
            ax.tick_params(width=1.5)
        for ax in axes[:, 0]:
            ax.set_ylabel(ylabel)
        for ax in axes[1, :]:
            ax.set_xlabel("Tempo desde a abertura (s)")
        fig.tight_layout(); fig.savefig(os.path.join(pdir, fname)); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 3.4))
    colors = ["#222222", "#555555", "#888888", "#bbbbbb", "#e0e0e0"]
    bottom = [0.0] * len(apps)
    for c, color in zip(cats, colors):
        vals = [per_app[a][c] for a in apps]
        ax.bar(names, vals, bottom=bottom, color=color, label=c, edgecolor="white",
               linewidth=0.5)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("Memória ao final da tarefa (MB)")
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.tick_params(width=1.5)
    fig.tight_layout(); fig.savefig(os.path.join(pdir, "fig6.png")); plt.close(fig)
    print(f"wrote previews to {pdir}")


if __name__ == "__main__":
    main()
