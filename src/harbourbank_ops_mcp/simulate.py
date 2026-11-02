"""Modelled histories of a delivery process, to compare ways of working before measuring them for real.

A scenario file (YAML, see scenarios/) describes a process: how many changes arrive, how long review,
testing and release take, how often a change breaks production, how long incidents take to detect,
diagnose and fix, and the engineer-hours each activity costs. A run turns that into a timeline of
deployments and incidents, which delivery.metrics measures exactly as it measures the live lab.

Every number in a scenario is an assumption until it is replaced by a measured one. Results are
labelled ILLUSTRATIVE for that reason. The model is deliberately simple:
- changes arrive at random (a Poisson process), around the clock
- durations are log-normal, given as a median and a 90th percentile (90% take less)
- a release fails if any change in it is faulty: big batches fail more often
- service is affected from the moment a failed release lands until it is rolled back or fixed
"""
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

import yaml

from .delivery import LEVEL_RULES, Deployment, Effort, Incident, level, metrics

START = datetime(2026, 1, 5, tzinfo=UTC)   # a Monday; a fixed start keeps runs reproducible
WARM_UP = timedelta(days=28)               # changes made before the window still ship into it
Z90 = 1.2816                               # the 90th percentile of a standard normal distribution


@dataclass
class Run:
    start: datetime
    end: datetime
    deployments: list[Deployment]
    incidents: list[Incident]
    effort: Effort


def load(path: Path) -> dict:
    scenario = yaml.safe_load(path.read_text())
    for key in ("name", "changes_per_week", "review_hours", "test_hours", "release",
                "failure_probability_per_change", "incident", "effort_hours"):
        if key not in scenario:
            raise ValueError(f"{path}: missing {key!r}")
    return scenario


def _duration(rng: random.Random, spec: dict) -> timedelta:
    """Log-normal hours from {median, p90}."""
    mu = math.log(spec["median"])
    sigma = max(math.log(spec["p90"]) - mu, 0) / Z90
    return timedelta(hours=rng.lognormvariate(mu, sigma))


def _next_window(ready: datetime, release: dict) -> datetime:
    """The first release window that `ready` makes, given the approval cut-off before each window."""
    cutoff = timedelta(hours=release.get("approval_cutoff_hours", 0))
    day = ready.replace(hour=release["hour_utc"], minute=0, second=0, microsecond=0)
    while day.weekday() not in release["weekdays"] or day - cutoff < ready:
        day += timedelta(days=1)
    return day


def run(scenario: dict, days: int = 90, seed: int = 1) -> Run:
    rng = random.Random(seed)
    start, end = START, START + timedelta(days=days)
    per_hour = scenario["changes_per_week"] / (7 * 24)
    release, incident = scenario["release"], scenario["incident"]
    effort: dict[str, float] = {}

    def spend(per: str, times: float = 1) -> None:
        for activity, hours in (scenario["effort_hours"].get(per) or {}).items():
            effort[activity] = effort.get(activity, 0) + hours * times

    # Changes and when each is ready to release
    made = start - WARM_UP
    ready: list[tuple[datetime, datetime]] = []
    while True:
        made += timedelta(hours=rng.expovariate(per_hour))
        if made >= end:
            break
        ready.append((made, made + _duration(rng, scenario["review_hours"]) + _duration(rng, scenario["test_hours"])))

    # Releases: each change on its own, or batched into the next window
    batches: dict[datetime, list[datetime]] = {}
    for made_at, ready_at in ready:
        if release["mode"] == "on_demand":
            batches.setdefault(ready_at + _duration(rng, release["deploy_hours"]), []).append(made_at)
        else:
            batches.setdefault(_next_window(ready_at, release), []).append(made_at)

    deployments, incidents = [], []
    p_fault = scenario["failure_probability_per_change"]
    kind = "change" if release["mode"] == "on_demand" else "release"
    for n, (at, changes) in enumerate(sorted(batches.items())):
        if not start <= at < end:   # the warm-up only fills the pipeline
            continue
        dep = Deployment(id=f"D{n:04d}", env="prod", deployed_at=at, changes=tuple(changes), kind=kind,
                         title=f"{len(changes)} change(s)")
        deployments.append(dep)
        spend("per_release")
        spend("per_change", len(changes))
        if rng.random() < 1 - (1 - p_fault) ** len(changes):
            detected = at + _duration(rng, incident["detect_hours"])
            fix = "rollback_hours" if rng.random() < incident["rollback_share"] else "fix_forward_hours"
            restored = detected + _duration(rng, incident["diagnose_hours"]) + _duration(rng, incident[fix])
            incidents.append(Incident(id=f"I{len(incidents):03d}", env="prod", started=at, detected=detected,
                                      restored=restored, caused_by=dep.id, title=f"{dep.title} failed"))
            spend("per_incident")
            spend("per_incident_hour", (restored - at).total_seconds() / 3600)
    return Run(start, end, deployments, incidents, Effort({k: round(v, 2) for k, v in effort.items()}))


# --- many runs per scenario ---------------------------------------------------------------------

def _band(values: list[float]) -> dict | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pick = lambda q: round(values[min(len(values) - 1, int(q * len(values)))], 3)  # noqa: E731
    return {"median": round(median(values), 3), "p10": pick(0.1), "p90": pick(0.9)}


def compare(scenario: dict, days: int = 90, runs: int = 200) -> dict:
    """The spread of each metric over many modelled runs: one run is one possible quarter."""
    keys = {"deployments_per_week": [], "lead_time_median_hours": [], "change_failure_rate": [],
            "restore_median_hours": [], "effort_hours_per_week": []}
    by_activity: dict[str, list[float]] = {}
    for seed in range(runs):
        r = run(scenario, days, seed)
        m = metrics(r.deployments, r.incidents, "prod", r.start, r.end, r.effort)
        keys["deployments_per_week"].append(m["deployment_frequency"]["per_week"])
        keys["lead_time_median_hours"].append(m["lead_time_for_changes_hours"].get("median"))
        keys["change_failure_rate"].append(m["change_failure_rate"]["rate"])
        keys["restore_median_hours"].append(m["time_to_restore_hours"].get("median"))
        weeks = days / 7
        keys["effort_hours_per_week"].append(sum(r.effort.hours.values()) / weeks)
        for activity, hours in r.effort.hours.items():
            by_activity.setdefault(activity, []).append(hours / weeks)
    bands = {k: _band(v) for k, v in keys.items()}
    return {"scenario": scenario["name"], "description": scenario.get("description", "").strip(),
            "illustrative": True, "days": days, "runs": runs, "metrics": bands,
            "levels": {"deployment_frequency": level("deployment_frequency_per_week",
                                                     bands["deployments_per_week"]["median"]),
                       "lead_time": level("lead_time_hours", (bands["lead_time_median_hours"] or {}).get("median")),
                       "change_failure_rate": level("change_failure_rate",
                                                    (bands["change_failure_rate"] or {}).get("median")),
                       "time_to_restore": level("restore_hours", (bands["restore_median_hours"] or {}).get("median"))},
            "effort_hours_per_week_by_activity": {k: _band(v) for k, v in sorted(by_activity.items())},
            "levels_used": LEVEL_RULES}


def _hours(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 60:.0f} min" if value < 1 else f"{value:.1f} h" if value < 48 else f"{value / 24:.1f} d"


def render_comparison(results: list[dict]) -> str:
    def cell(band: dict | None, fmt) -> str:
        return "-" if not band else f"{fmt(band['median'])} ({fmt(band['p10'])}-{fmt(band['p90'])})"

    per_week = lambda v: f"{v:.1f}"   # noqa: E731
    rows = [
        ("Deployments per week", "deployments_per_week", per_week, "deployment_frequency"),
        ("Lead time (median)", "lead_time_median_hours", _hours, "lead_time"),
        ("Change failure rate", "change_failure_rate", lambda v: f"{v:.0%}", "change_failure_rate"),
        ("Time to restore (median)", "restore_median_hours", _hours, "time_to_restore"),
        ("Engineer-hours per week", "effort_hours_per_week", per_week, None),
    ]
    header = ["Metric", *[r["scenario"] for r in results]]
    table = [[label, *[cell(r["metrics"][key], fmt) + (f" {r['levels'][lv]}" if lv and r["levels"][lv] else "")
                       for r in results]] for label, key, fmt, lv in rows]
    activities = sorted({a for r in results for a in r["effort_hours_per_week_by_activity"]})
    table += [[f"  {a}", *[cell(r["effort_hours_per_week_by_activity"].get(a), per_week) for r in results]]
              for a in activities]
    if len(results) > 1:
        base = results[0]["metrics"]["effort_hours_per_week"]["median"]
        table.append(["Effort vs first column", "-", *[
            f"{(r['metrics']['effort_hours_per_week']['median'] - base) / base:+.0%}" for r in results[1:]]])
    widths = [max(len(str(row[i])) for row in [header, *table]) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()  # noqa: E731
    days, runs = results[0]["days"], results[0]["runs"]
    return "\n".join([
        f"ILLUSTRATIVE: modelled, not measured. Median of {runs} runs of {days} days (10th-90th percentile).",
        "Every input is an assumption in the scenario file; replace them with measured values.", "",
        line(header), line(["-" * w for w in widths]), *map(line, table)])
