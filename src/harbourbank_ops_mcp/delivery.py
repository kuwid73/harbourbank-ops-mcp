"""DORA's four delivery metrics, computed the same way from any source of events.

A source supplies two lists: the deployments to an environment (when each one landed, and when each
change it shipped was made) and the incidents there (when each started, was detected and was restored,
and which deployment caused it, if any). Three sources feed this module:

- the live lab: ops.delivery_metrics reads Git, GitHub and the incident records in harbourbank-gitops
- the scenario simulator (simulate.py): modelled histories of a delivery process
- CSV files, e.g. exported from a bank's CI and incident (ITSM) tools, so a baseline can be measured
  on a workstation without sending the data anywhere

Command line:
  hb-delivery report <dir> [--env prod] [--days 90]     metrics from <dir>/deployments.csv + incidents.csv
  hb-delivery simulate <scenario.yaml> [--out <dir>]     one modelled history, written as those CSVs
  hb-delivery compare <scenario.yaml>...                 many modelled histories per scenario, side by side
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class Deployment:
    id: str
    env: str
    deployed_at: datetime
    changes: tuple[datetime, ...]   # when each change it shipped was made (commit time)
    kind: str = "change"
    title: str = ""


@dataclass(frozen=True)
class Incident:
    id: str
    env: str
    started: datetime               # when service was first affected
    detected: datetime              # when someone (or an alert) knew
    restored: datetime | None       # None: still open
    caused_by: str | None = None    # a Deployment id; None for other causes
    title: str = ""


@dataclass
class Effort:
    """Engineer-hours by activity, where a source knows them (the simulator does; Git doesn't)."""
    hours: dict[str, float] = field(default_factory=dict)


# Reference levels, roughly those of the State of DevOps reports (2021-2023 editions differ in the
# detail; check the edition you cite). metric: (higher is better, [(level, threshold), ...] best first).
LEVELS = {
    "deployment_frequency_per_week": (True, [("elite", 7), ("high", 1), ("medium", 0.25)]),
    "lead_time_hours": (False, [("elite", 24), ("high", 24 * 7), ("medium", 24 * 30)]),
    "change_failure_rate": (False, [("elite", 0.05), ("high", 0.10), ("medium", 0.15)]),
    "restore_hours": (False, [("elite", 1), ("high", 24), ("medium", 24 * 7)]),
}
LEVEL_RULES = {
    "deployment_frequency_per_week": "elite: daily or more; high: weekly or more; medium: monthly or more",
    "lead_time_hours": "elite: under a day; high: under a week; medium: under a month",
    "change_failure_rate": "elite: 5% or less; high: 10% or less; medium: 15% or less",
    "restore_hours": "elite: under an hour; high: under a day; medium: under a week",
}
MIN_DEPLOYMENTS = 10    # below this, rates and medians say little
MIN_DAYS = 28           # the levels describe months of steady work, not a burst


def level(metric: str, value: float | None) -> str | None:
    if value is None:
        return None
    higher_is_better, steps = LEVELS[metric]
    for name, threshold in steps:
        if (value >= threshold) if higher_is_better else (value <= threshold):
            return name
    return "low"


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 2)


def _spread(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {"median": round(median(ordered), 2), "p90": round(ordered[min(len(ordered) - 1,
                                                                          int(0.9 * len(ordered)))], 2),
            "count": len(ordered)}


def metrics(deployments: list[Deployment], incidents: list[Incident], env: str, start: datetime,
            end: datetime, effort: Effort | None = None) -> dict:
    """The four metrics for one environment and window, with the evidence behind each."""
    deps = sorted((d for d in deployments if d.env == env and start <= d.deployed_at < end),
                  key=lambda d: d.deployed_at)
    incs = sorted((i for i in incidents if i.env == env and start <= i.started < end), key=lambda i: i.started)
    weeks = (end - start).total_seconds() / (7 * 86400)
    ids = {d.id for d in deps}
    from_changes = [i for i in incs if i.caused_by in ids]
    failed = sorted({i.caused_by for i in from_changes})

    lead = [_hours(d.deployed_at - c) for d in deps for c in d.changes]
    restore = [_hours(i.restored - i.started) for i in from_changes if i.restored]
    detect = [_hours(i.detected - i.started) for i in from_changes]
    frequency = round(len(deps) / weeks, 2) if weeks else None
    failure_rate = round(len(failed) / len(deps), 3) if deps else None
    lead_spread, restore_spread = _spread(lead), _spread(restore)

    notes = []
    if len(deps) < MIN_DEPLOYMENTS:
        notes.append(f"only {len(deps)} deployments in the window: treat the rates and medians as anecdotes, "
                     f"not a baseline (at least {MIN_DEPLOYMENTS} are needed)")
    if (end - start) < timedelta(days=MIN_DAYS):
        notes.append(f"the window is only {(end - start).total_seconds() / 86400:.1f} days: too short for a baseline "
                     f"(at least {MIN_DAYS} days), so the levels are indicative only")
    if deps and not from_changes:
        notes.append("no incident was linked to a deployment: either none failed, or failures weren't recorded "
                     "as incidents with their cause")
    if any(i.restored is None for i in incs):
        notes.append("some incidents are still open; they have no restore time yet")

    # Too little history for a baseline: every level says so, so no answer can quote one bare
    baseline = len(deps) >= MIN_DEPLOYMENTS and (end - start) >= timedelta(days=MIN_DAYS)

    def rated(metric: str, value: float | None) -> str | None:
        found = level(metric, value)
        return found if baseline or found is None else f"{found} (indicative)"

    result = {
        **({} if baseline else {"read_first": "not a baseline yet: " + "; ".join(notes[:2])}),
        "env": env,
        "window": {"from": start.isoformat(timespec="minutes"), "to": end.isoformat(timespec="minutes"),
                   "days": round((end - start).total_seconds() / 86400, 1)},
        "deployment_frequency": {"deployments": len(deps), "per_week": frequency,
                                 "by_kind": dict(sorted(_count(d.kind for d in deps).items())),
                                 "level": rated("deployment_frequency_per_week", frequency)},
        "lead_time_for_changes_hours": (lead_spread or {}) | {
            "level": rated("lead_time_hours", lead_spread and lead_spread["median"]),
            "measured": "from each change's commit to the deployment that shipped it"},
        "change_failure_rate": {"failed_deployments": len(failed), "deployments": len(deps), "rate": failure_rate,
                                "level": rated("change_failure_rate", failure_rate), "failed": failed},
        "time_to_restore_hours": (restore_spread or {}) | {
            "level": rated("restore_hours", restore_spread and restore_spread["median"]),
            "time_to_detect": _spread(detect),
            "measured": "from when service was first affected to when it was restored, for incidents "
                        "caused by a deployment"},
        "other_incidents": [{"id": i.id, "title": i.title, "restore_hours": i.restored and
                             _hours(i.restored - i.started)} for i in incs if i.caused_by not in ids],
        "levels_used": LEVEL_RULES,
        "notes": notes,
    }
    if effort and effort.hours:
        total = sum(effort.hours.values())
        result["effort_hours"] = {"total": round(total, 1), "per_week": round(total / weeks, 1) if weeks else None,
                                  "by_activity": {k: round(v, 1) for k, v in sorted(effort.hours.items(),
                                                                                    key=lambda kv: -kv[1])}}
    return result


def _count(items) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item] += 1
    return counts


# --- CSV: the exchange format with other tools ---------------------------------------------------
# deployments.csv: one row per change shipped (a deployment with no listed change has one row with
#   change_made_at empty): deployment_id, env, deployed_at, kind, title, change_made_at
# incidents.csv: incident_id, env, started, detected, restored, caused_by_deployment, title
# effort.csv (optional): activity, hours
# Times are ISO 8601 with a UTC offset, e.g. 2026-10-29T07:53:00Z.

DEPLOYMENT_FIELDS = ["deployment_id", "env", "deployed_at", "kind", "title", "change_made_at"]
INCIDENT_FIELDS = ["incident_id", "env", "started", "detected", "restored", "caused_by_deployment", "title"]


def _time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no UTC offset; write e.g. {value}Z")
    return parsed


def read_csv(directory: Path) -> tuple[list[Deployment], list[Incident], Effort]:
    rows: dict[str, dict] = {}
    with open(directory / "deployments.csv", newline="") as f:
        for row in csv.DictReader(f):
            entry = rows.setdefault(row["deployment_id"], {"row": row, "changes": []})
            if made := _time(row.get("change_made_at", "")):
                entry["changes"].append(made)
    deployments = [Deployment(id=k, env=v["row"]["env"], deployed_at=_time(v["row"]["deployed_at"]),
                              changes=tuple(v["changes"]), kind=v["row"].get("kind") or "change",
                              title=v["row"].get("title", "")) for k, v in rows.items()]
    incidents = []
    if (directory / "incidents.csv").exists():
        with open(directory / "incidents.csv", newline="") as f:
            incidents = [Incident(id=r["incident_id"], env=r["env"], started=_time(r["started"]),
                                  detected=_time(r["detected"]) or _time(r["started"]),
                                  restored=_time(r["restored"]), caused_by=r.get("caused_by_deployment") or None,
                                  title=r.get("title", "")) for r in csv.DictReader(f)]
    effort = Effort()
    if (directory / "effort.csv").exists():
        with open(directory / "effort.csv", newline="") as f:
            effort.hours = {r["activity"]: float(r["hours"]) for r in csv.DictReader(f)}
    return deployments, incidents, effort


def write_csv(directory: Path, deployments: list[Deployment], incidents: list[Incident],
              effort: Effort | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)

    def iso(t: datetime | None) -> str:
        return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if t else ""

    with open(directory / "deployments.csv", "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(DEPLOYMENT_FIELDS)
        for d in deployments:
            for made in d.changes or (None,):
                out.writerow([d.id, d.env, iso(d.deployed_at), d.kind, d.title, iso(made)])
    with open(directory / "incidents.csv", "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(INCIDENT_FIELDS)
        for i in incidents:
            out.writerow([i.id, i.env, iso(i.started), iso(i.detected), iso(i.restored), i.caused_by or "", i.title])
    if effort and effort.hours:
        with open(directory / "effort.csv", "w", newline="") as f:
            out = csv.writer(f)
            out.writerow(["activity", "hours"])
            out.writerows(sorted(effort.hours.items()))


# --- text output ------------------------------------------------------------------------------

def _fmt_hours(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} days"


def summary_rows(m: dict) -> list[tuple[str, str, str]]:
    lead, restore = m["lead_time_for_changes_hours"], m["time_to_restore_hours"]
    rate = m["change_failure_rate"]
    rows = [("Deployment frequency", f"{m['deployment_frequency']['per_week']} per week "
             f"({m['deployment_frequency']['deployments']} in {m['window']['days']:g} days)",
             m["deployment_frequency"]["level"] or "-"),
            ("Lead time for changes", f"median {_fmt_hours(lead.get('median'))}, p90 {_fmt_hours(lead.get('p90'))}",
             lead["level"] or "-"),
            ("Change failure rate", "-" if rate["rate"] is None else
             f"{rate['rate']:.0%} ({rate['failed_deployments']} of {rate['deployments']})", rate["level"] or "-"),
            ("Time to restore", f"median {_fmt_hours(restore.get('median'))}, p90 {_fmt_hours(restore.get('p90'))}",
             restore["level"] or "-")]
    if "effort_hours" in m:
        rows.append(("Engineer effort", f"{m['effort_hours']['per_week']} hours per week", ""))
    return rows


def _table(header: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()  # noqa: E731
    return "\n".join([line(header), line(["-" * w for w in widths]), *map(line, rows)])


def render(m: dict, title: str = "") -> str:
    text = [title] if title else []
    text.append(_table(["Metric", "Value", "Level"], [list(r) for r in summary_rows(m)]))
    if "effort_hours" in m:
        text.append("\nEffort by activity (hours per week):")
        weeks = m["window"]["days"] / 7
        text += [f"  {k}: {v / weeks:.1f}" for k, v in m["effort_hours"]["by_activity"].items()]
    text += [f"\nNote: {n}" for n in m["notes"]]
    return "\n".join(text)


# --- command line -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    from . import simulate  # here, not at the top: simulate imports this module

    parser = argparse.ArgumentParser(prog="hb-delivery", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="metrics from deployments.csv and incidents.csv in a directory")
    report.add_argument("directory", type=Path)
    report.add_argument("--env", default="prod")
    report.add_argument("--days", type=int, help="window ending at the newest deployment (default: all)")
    sim = sub.add_parser("simulate", help="one modelled history of a scenario, written as CSV")
    sim.add_argument("scenario", type=Path)
    sim.add_argument("--days", type=int, default=90)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--out", type=Path, help="directory for the CSV files")
    cmp = sub.add_parser("compare", help="many modelled histories per scenario, summarised side by side")
    cmp.add_argument("scenarios", type=Path, nargs="+")
    cmp.add_argument("--days", type=int, default=90)
    cmp.add_argument("--runs", type=int, default=200)
    for p in (report, sim, cmp):
        p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    if args.command == "report":
        deployments, incidents, effort = read_csv(args.directory)
        if not deployments:
            sys.exit("no deployments in deployments.csv")
        end = max(d.deployed_at for d in deployments) + timedelta(seconds=1)
        start = end - timedelta(days=args.days) if args.days else min(d.deployed_at for d in deployments)
        m = metrics(deployments, incidents, args.env, start, end, effort)
        print(json.dumps(m, indent=2) if args.json else render(m, f"{args.directory} ({args.env})"))
    elif args.command == "simulate":
        scenario = simulate.load(args.scenario)
        run = simulate.run(scenario, args.days, args.seed)
        if args.out:
            write_csv(args.out, run.deployments, run.incidents, run.effort)
        m = metrics(run.deployments, run.incidents, "prod", run.start, run.end, run.effort)
        print(json.dumps(m, indent=2) if args.json else
              render(m, f"{scenario['name']} (one modelled run, seed {args.seed}; ILLUSTRATIVE)"))
    else:
        results = [simulate.compare(simulate.load(p), args.days, args.runs) for p in args.scenarios]
        print(json.dumps(results, indent=2) if args.json else simulate.render_comparison(results))


if __name__ == "__main__":
    main()
