# Summary: the load effect is mostly bias, the machine explains little, and bad decisions are hard to detect.

import json

import matplotlib.pyplot as plt

from common import save

twins = json.load(open("results/twins.json"))
model = json.load(open("results/placement.json"))

fig, (left, middle, right) = plt.subplots(1, 3, figsize=(15, 4.5))
left.bar(["all tasks\nmixed", "twins\n(same task)"], [twins["effect_60_mixed"], twins["effect_60_twins"]],
         color=["tab:gray", "tab:green"])
left.set_ylabel("extra duration (%)")
left.set_title("1. Machine above 60 % CPU:\nthe apparent effect is mostly bias")

middle.bar(["the task", "the machine\n(on top)"], [twins["variance_task"], twins["variance_machine"]],
           color=["tab:blue", "tab:orange"])
middle.set_ylabel("% of variance of log duration")
middle.set_ylim(0, 100)
middle.set_title("2. What explains\nan instance's duration")

right.bar(["random", "model", "placebo\n(1 h later)"], [0.5, model["auc_model"], model["auc_placebo"]],
          color=["tab:gray", "tab:orange", "tab:green"])
right.set_ylim(0.45, 1)
right.set_ylabel("AUC (1 = perfect)")
right.set_title("3. Detecting a bad decision:\nbarely better than random")

for axis in (left, middle, right):
    for bar in axis.patches:
        axis.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{bar.get_height():.2f}",
                  ha="center", va="bottom")
save("summary")
