"""High-level operations shared by the command line (cli.py) and the web API (server.py)."""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .data_loader import load_dataset
from .greedy import run_greedy
from .ip_model import run_ip
from .models import Dataset, Solution
from .reporting import (comparison_markdown, export_solution, metrics,
                        validation_markdown)
from .sensitivity import run_sensitivity, sensitivity_markdown
from .verification import verify_solution

ALGORITHMS = {"greedy": run_greedy, "ip": run_ip}


def load(cfg: Config) -> Dataset:
    return load_dataset(cfg.data_dir)


def validate(cfg: Config) -> tuple[Dataset, Path]:
    ds = load(cfg)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.output_dir / "validation_report.md"
    path.write_text(validation_markdown(ds), encoding="utf-8")
    return ds, path


def solve(cfg: Config, algorithm: str, dataset: Dataset | None = None):
    """Run one algorithm, verify it, and export its reports."""
    ds = dataset or load(cfg)
    solution: Solution = ALGORITHMS[algorithm](ds, cfg)
    errors = verify_solution(solution, ds, cfg)
    paths = export_solution(solution, ds, cfg, errors, cfg.output_dir, algorithm)
    return ds, solution, errors, paths


def compare(cfg: Config):
    ds = load(cfg)
    _, g, g_err, _ = solve(cfg, "greedy", ds)
    _, i, i_err, _ = solve(cfg, "ip", ds)
    path = cfg.output_dir / "comparison_report.md"
    path.write_text(comparison_markdown(g, i, ds, g_err, i_err), encoding="utf-8")
    return ds, (g, g_err), (i, i_err), path


def sensitivity(cfg: Config):
    ds = load(cfg)
    result = run_sensitivity(ds, cfg)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.output_dir / "sensitivity_report.md"
    path.write_text(sensitivity_markdown(result), encoding="utf-8")
    return result, path


def summary_line(solution: Solution, ds: Dataset, errors: list[str]) -> str:
    m = metrics(solution, ds)
    return (f"{solution.algorithm}: status {solution.status}, verification "
            f"{'PASSED' if not errors else 'FAILED (' + str(len(errors)) + ' issues)'}, "
            f"served {m['requests_served']}/{m['requests_served'] + m['requests_unmatched']}, "
            f"booked cabs used {m['existing_cabs_used']}, new cabs {m['new_cabs']} "
            f"({m['shared_new_cabs']} shared), cost Rs {m['new_cab_cost']:,.2f}")
