#!/usr/bin/env python3
"""Draw simple charts from result folders and measurements.json. Needs matplotlib.

Usage: python3 plots.py results/qwen3-4b results/gpt-5.4-mini ...  (PNG files go to results/figures/)
"""

import json
from pathlib import Path
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from report import factor, read, score

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "figures"
# Level -> legend label, color, marker (the marker is a second cue besides color).
LEVELS = {"pod": ("pod spec", "#2a78d6", "o"),
          "pod_image": ("+ image", "#eb6834", "s"),
          "pod_image_node": ("+ image + nœud", "#1baf7a", "^")}
GOOD, PARTIAL, WRONG, NONE = "#0ca30c", "#eda100", "#d03b3b", "#b9b8b2"
TEXT, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({"font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": TEXT, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE})


def model_name(folder):
    return read(folder)[3]["model"]


def bars(ax, folders, value, title, label_format):
    """Horizontal bars: one group per model, one bar per level, value printed at the end."""
    width = 0.8 / len(LEVELS)
    for i, (level, (label, color, _)) in enumerate(LEVELS.items()):
        for k, folder in enumerate(folders):
            v = value(score(folder, level))
            if v is None:
                continue
            y = k - 0.4 + width * (i + 0.5)
            ax.barh(y, v, height=width * 0.9, color=color, label=label if k == 0 else None)
            ax.text(v, y, " " + label_format.format(v), va="center", fontsize=8, color=MUTED)
    ax.set_yticks(range(len(folders)), [model_name(f) for f in folders])
    ax.invert_yaxis()
    ax.set_title(title, loc="left", color=TEXT, fontsize=11)
    ax.grid(axis="x", color=GRID)
    ax.set_axisbelow(True)


def quality(folders):
    fig, axes = plt.subplots(1, 3, figsize=(13, 1.4 + 0.9 * len(folders)), sharey=True)
    bars(axes[0], folders, lambda s: 100 * s["valid"] / s["calls"], "Réponses valides", "{:.0f}%")
    bars(axes[1], folders, lambda s: 100 * s["work_exact"] / s["calls"], "Type de travail exact", "{:.0f}%")
    bars(axes[2], folders, lambda s: 100 * s["within_2"] / s["estimates"] if s["estimates"] else None,
         "Durée à moins de x2 du réel", "{:.0f}%")
    for ax in axes:
        ax.set_xlim(0, 115)
        ax.set_xticks([0, 50, 100], ["0%", "50%", "100%"])
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.1), ncol=3, frameon=False)
    fig.suptitle("Qualité des réponses (12 cas x 3 répétitions par barre)", x=0.01, ha="left", color=TEXT)
    fig.tight_layout()
    fig.savefig(OUT / "1-qualite.png", dpi=150)


def predicted_vs_measured(folders):
    fig, axes = plt.subplots(1, len(folders), figsize=(3.6 * len(folders), 4.2), sharey=True, squeeze=False)
    axes = axes[0]
    for ax, folder in zip(axes, folders):
        rows, _, measured, settings = read(folder)
        for level, (label, color, marker) in LEVELS.items():
            points = [(measured[r["case"]], r["prediction"]["duration_seconds"]) for r in rows
                      if r["level"] == level and r["prediction"] and r["prediction"]["duration_seconds"]]
            ax.scatter([p[0] for p in points], [p[1] for p in points], s=34, color=color, marker=marker,
                       edgecolor=SURFACE, linewidth=1, label=label, zorder=3)
        ax.plot([0.5, 2000], [0.5, 2000], color=MUTED, linewidth=1)
        ax.fill_between([0.5, 2000], [0.25, 1000], [1, 4000], color=GRID, alpha=0.6, linewidth=0)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(0.7, 300)
        ax.set_ylim(0.7, 1000)
        ax.set_xticks([1, 10, 60], ["1 s", "10 s", "60 s"])
        ax.set_yticks([1, 10, 60, 600], ["1 s", "10 s", "60 s", "10 min"])
        ax.minorticks_off()
        ax.set_title(settings["model"], loc="left", color=TEXT, fontsize=11)
        ax.set_xlabel("durée mesurée")
    axes[0].set_ylabel("durée prédite")
    axes[0].legend(loc="upper left", frameon=False, fontsize=8)
    fig.suptitle("Durée prédite vs mesurée. Diagonale = parfait, bande grise = à moins de x2",
                 x=0.01, ha="left", color=TEXT)
    fig.tight_layout()
    fig.savefig(OUT / "2-predit-vs-mesure.png", dpi=150)


def latency(folders):
    fig, ax = plt.subplots(figsize=(8, 1.4 + 0.9 * len(folders)))
    bars(ax, folders, lambda s: s["median_ms"] / 1000 if s["median_ms"] else None,
         "Latence d'un appel, médiane, modèle déjà chargé", "{:.1f} s")
    ax.set_xlabel("secondes par appel (GPT : inclut l'aller-retour réseau)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.08), ncol=3, frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "3-latence.png", dpi=150)


def measured_durations(folder):
    runs = json.loads((Path(folder) / "measurements.json").read_text())["cases"]
    cases = json.loads((Path(folder) / "cases.json").read_text())
    labels = {}
    for c in cases:
        command = c["pod"]["spec"]["containers"][0]["command"]
        text = command[-1] if command[:2] == ["sh", "-c"] else " ".join(command)
        text = text.replace(" --temp-path /tmp --metrics-brief", "")
        labels[c["id"]] = c["id"] + "  " + (text if len(text) <= 46 else text[:44] + "..")
    order = sorted(runs, key=lambda c: statistics.median(r["seconds"] for r in runs[c]))
    fig, ax = plt.subplots(figsize=(9, 0.9 + 0.4 * len(order)))
    for k, case in enumerate(order):
        seconds = [r["seconds"] for r in runs[case]]
        ax.scatter(seconds, [k] * len(seconds), s=36, color="#2a78d6", edgecolor=SURFACE, linewidth=1.5, zorder=3)
        ax.text(max(seconds) * 1.15, k, f"{statistics.median(seconds):.1f} s", va="center", fontsize=8, color=MUTED)
    ax.set_xscale("log")
    ax.set_xlim(0.7, 300)
    ax.set_xticks([1, 10, 60], ["1 s", "10 s", "60 s"])
    ax.minorticks_off()
    ax.set_yticks(range(len(order)), [labels[c] for c in order], fontsize=8, family="monospace")
    ax.invert_yaxis()
    ax.grid(axis="x", color=GRID)
    ax.set_title("Durée mesurée dans Docker, 1 point = 1 exécution", loc="left", color=TEXT)
    fig.tight_layout()
    fig.savefig(OUT / "4-durees-mesurees.png", dpi=150)


def duration_grid(folders):
    columns = [(f, level) for f in folders for level in LEVELS]
    _, references, measured, _ = read(folders[0])
    cases = sorted(measured)
    fig, ax = plt.subplots(figsize=(1.8 + 0.75 * len(columns), 1.8 + 0.38 * len(cases)))
    for j, (folder, level) in enumerate(columns):
        rows = read(folder)[0]
        for i, case in enumerate(cases):
            estimates = [r["prediction"]["duration_seconds"] for r in rows
                         if r["case"] == case and r["level"] == level and r["prediction"]]
            numbers = [e for e in estimates if e is not None]
            if not numbers:
                mark, color = ("null" if estimates else "invalide"), NONE
            else:
                f = factor(statistics.median(numbers), measured[case])
                mark = f"x{f:.1f}" if f < 100 else "x99+"
                color = GOOD if f <= 1.25 else PARTIAL if f <= 2 else WRONG
            ax.add_patch(plt.Rectangle((j + 0.04, i + 0.06), 0.92, 0.88, color=color, linewidth=0))
            ax.text(j + 0.5, i + 0.5, mark, ha="center", va="center", fontsize=7,
                    color="#ffffff" if color in [GOOD, WRONG] else TEXT)
    ax.set_xlim(0, len(columns))
    ax.set_ylim(len(cases), 0)
    short = {"pod": "pod", "pod_image": "+image", "pod_image_node": "+nœud"}
    ax.set_xticks([j + 0.5 for j in range(len(columns))],
                  [f"{model_name(f)}\n{short[l]}" for f, l in columns], fontsize=7)
    ax.xaxis.tick_top()
    ax.set_yticks([i + 0.5 for i in range(len(cases))],
                  [f"{c}  {measured[c]:5.1f} s{'  timer' if references[c]['timer'] else ''}" for c in cases],
                  fontsize=8, family="monospace")
    for side in ["left", "bottom", "top"]:
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    ax.set_title("Erreur de durée par cas (médiane des 3 répétitions)\n"
                 "vert <= x1.25, orange <= x2, rouge > x2 ; timer = la durée est écrite dans le spec",
                 loc="left", color=TEXT, fontsize=10, pad=34)
    fig.tight_layout()
    fig.savefig(OUT / "5-erreur-par-cas.png", dpi=150)


def main():
    folders = sys.argv[1:]
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    quality(folders)
    predicted_vs_measured(folders)
    latency(folders)
    measured_durations(folders[0])
    duration_grid(folders)
    print(f"Figures: {OUT}")


if __name__ == "__main__":
    main()
