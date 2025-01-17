import collections
import itertools
import json
import logging
import os
import os.path as osp
import tempfile
import warnings
from collections import OrderedDict
from typing import Any, Callable, List, Mapping, Optional, Sequence, Set

import pandas as pd
from sacred.observers import FileStorageObserver

import imitation.util.sacred as sacred_util
from imitation.scripts.config.analyze import analysis_ex
from imitation.util.sacred import dict_get_nested as get


@analysis_ex.capture
def _gather_sacred_dicts(
    source_dirs: Sequence[str], run_name: str, env_name: str, skip_failed_runs: bool
) -> List[sacred_util.SacredDicts]:
    """Helper function for parsing and selecting Sacred experiment JSON files.

    Args:
        source_dirs: A directory containing Sacred FileObserver subdirectories
            associated with the `train_adversarial` Sacred script. Behavior is
            undefined if there are Sacred subdirectories associated with other
            scripts. (Captured argument)
        run_name: If provided, then only analyze results from Sacred directories
            associated with this run name. `run_name` is compared against the
            "experiment.name" key in `run.json`. (Captured argument)
        skip_failed_runs: If True, then filter out runs where the status is FAILED.
            (Captured argument)

    Returns:
        A list of `SacredDicts` corresponding to the selected Sacred directories.
    """
    # e.g. chain.from_iterable([["pathone", "pathtwo"], [], ["paththree"]]) =>
    # ("pathone", "pathtwo", "paththree")
    sacred_dirs = itertools.chain.from_iterable(
        sacred_util.filter_subdirs(source_dir) for source_dir in source_dirs
    )
    sacred_dicts = []

    for sacred_dir in sacred_dirs:
        try:
            sacred_dicts.append(sacred_util.SacredDicts.load_from_dir(sacred_dir))
        except json.JSONDecodeError:
            warnings.warn(f"Invalid JSON file in {sacred_dir}", RuntimeWarning)

    if run_name is not None:
        sacred_dicts = filter(
            lambda sd: get(sd.run, "experiment.name") == run_name, sacred_dicts
        )

    if env_name is not None:
        sacred_dicts = filter(
            lambda sd: get(sd.config, "env_name") == env_name, sacred_dicts
        )

    if skip_failed_runs:
        sacred_dicts = filter(
            lambda sd: get(sd.run, "status") != "FAILED", sacred_dicts
        )

    return list(sacred_dicts)


@analysis_ex.command
def gather_tb_directories() -> dict:
    """Gather Tensorboard directories from a `parallel_ex` run.

    The directories are copied to a unique directory in `/tmp/analysis_tb/` under
    subdirectories matching the Tensorboard events' Ray Tune trial names.

    This function calls the helper `_gather_sacred_dicts`, which captures its arguments
    automatically via Sacred. Provide those arguments to select which Sacred
    results to parse.

    Returns:
      A dict with two keys. "gather_dir" (str) is a path to a /tmp/
      directory containing all the TensorBoard runs filtered from `source_dir`.
      "n_tb_dirs" (int) is the number of TensorBoard directories that were
      filtered.
    """

    os.makedirs("/tmp/analysis_tb", exist_ok=True)
    tmp_dir = tempfile.mkdtemp(dir="/tmp/analysis_tb/")

    tb_dirs_count = 0
    for sd in _gather_sacred_dicts():
        # Expecting a path like "~/ray_results/{run_name}/sacred/1".
        # Want to search for all Tensorboard dirs inside
        # "~/ray_results/{run_name}".
        sacred_dir = sd.sacred_dir.rstrip("/")
        run_dir = osp.dirname(osp.dirname(sacred_dir))
        run_name = osp.basename(run_dir)

        # "tb" is TensorBoard directory built by our codebase. "sb_tb" is Stable
        # Baselines TensorBoard directory. There should be at most one of each
        # directory.
        for basename in ["rl", "tb", "sb_tb"]:
            tb_src_dirs = tuple(
                sacred_util.filter_subdirs(
                    run_dir, lambda path: osp.basename(path) == basename
                )
            )
            if tb_src_dirs:
                assert len(tb_src_dirs) == 1, "expect at most one TB dir of each type"
                tb_src_dir = tb_src_dirs[0]

                symlinks_dir = osp.join(tmp_dir, basename)
                os.makedirs(symlinks_dir, exist_ok=True)

                tb_symlink = osp.join(symlinks_dir, run_name)
                os.symlink(tb_src_dir, tb_symlink)
                tb_dirs_count += 1

    logging.info(f"Symlinked {tb_dirs_count} TensorBoard dirs to {tmp_dir}.")
    logging.info(f"Start Tensorboard with `tensorboard --logdir {tmp_dir}`.")
    return {"n_tb_dirs": tb_dirs_count, "gather_dir": tmp_dir}


def _get_exp_command(sd: sacred_util.SacredDicts) -> str:
    return str(sd.run.get("command"))


def _get_algo_name(sd: sacred_util.SacredDicts) -> str:
    exp_command = _get_exp_command(sd)

    if exp_command == "train_adversarial":
        algo = get(sd.config, "algorithm")
        if algo is not None:
            algo = algo.upper()
        return algo
    elif exp_command == "train_bc":
        return "BC"
    elif exp_command == "train_dagger":
        return "DAgger"
    else:
        return f"??exp_command={exp_command}"


def _return_summaries(sd: sacred_util.SacredDicts) -> dict:
    imit_stats = get(sd.run, "result.imit_stats")
    expert_stats = get(sd.run, "result.expert_stats")

    expert_return_summary = None
    if expert_stats is not None:
        expert_return_summary = _make_return_summary(expert_stats)

    imit_return_summary = None
    if imit_stats is not None:
        imit_return_summary = _make_return_summary(imit_stats, "monitor_")

    if imit_stats is not None and expert_stats is not None:
        # Assuming here that `result.imit_stats` and `result.expert_stats` are
        # formatted correctly.
        imit_expert_ratio = (
            imit_stats["monitor_return_mean"] / expert_stats["return_mean"]
        )
    else:
        imit_expert_ratio = None

    return dict(
        expert_stats=expert_stats,
        imit_stats=imit_stats,
        expert_return_summary=expert_return_summary,
        imit_return_summary=imit_return_summary,
        imit_expert_ratio=imit_expert_ratio,
    )


sd_to_table_entry_type = Mapping[str, Callable[[sacred_util.SacredDicts], Any]]

# This OrderedDict maps column names to functions that get table entries, given the
# row's unique SacredDicts object.
table_entry_fns: sd_to_table_entry_type = collections.OrderedDict(
    [
        ("status", lambda sd: get(sd.run, "status")),
        ("exp_command", _get_exp_command),
        ("algo", _get_algo_name),
        ("env_name", lambda sd: get(sd.config, "env_name")),
        ("n_expert_demos", lambda sd: get(sd.config, "n_expert_demos")),
        ("run_name", lambda sd: get(sd.run, "experiment.name")),
        (
            "expert_return_summary",
            lambda sd: _return_summaries(sd)["expert_return_summary"],
        ),
        (
            "imit_return_summary",
            lambda sd: _return_summaries(sd)["imit_return_summary"],
        ),
        ("imit_expert_ratio", lambda sd: _return_summaries(sd)["imit_expert_ratio"]),
    ]
)


# If `verbosity` is at least the length of this list, then we use all table_entry_fns
# as columns of table.
# Otherwise, use only the subset at index `verbosity`. The subset of columns is
# still arranged in the same order as in the `table_entry_fns` OrderedDict.
table_verbosity_mapping: List[Set[str]] = []

# verbosity 0
table_verbosity_mapping.append(
    {
        "algo",
        "env_name",
        "expert_return_summary",
        "imit_return_summary",
    }
)

# verbosity 1
table_verbosity_mapping.append(table_verbosity_mapping[-1] | {"n_expert_demos"})

# verbosity 2
table_verbosity_mapping.append(
    table_verbosity_mapping[-1]
    | {"status", "imit_expert_ratio", "exp_command", "run_name"}
)


def _get_table_entry_fns_subset(table_verbosity: int) -> sd_to_table_entry_type:
    assert table_verbosity >= 0
    if table_verbosity >= len(table_verbosity_mapping):
        return table_entry_fns
    else:
        keys_subset = table_verbosity_mapping[table_verbosity]
        result = OrderedDict()
        for k, v in table_entry_fns.items():
            if k not in keys_subset:
                continue
            result[k] = v
        return result


@analysis_ex.command
def analyze_imitation(
    csv_output_path: Optional[str],
    tex_output_path: Optional[str],
    print_table: bool,
    table_verbosity: int,
) -> pd.DataFrame:
    """Parse Sacred logs and generate a DataFrame for imitation learning results.

    This function calls the helper `_gather_sacred_dicts`, which captures its arguments
    automatically via Sacred. Provide those arguments to select which Sacred
    results to parse.

    Args:
        csv_output_path: If provided, then save a CSV output file to this path.
        tex_output_path: If provided, then save a LaTeX-format table to this path.
        print_table: If True, then print the dataframe to stdout.
        table_verbosity: Increasing levels of verbosity, from 0 to 2, increase the
            number of columns in the table.

    Returns:
        The DataFrame generated from the Sacred logs.
    """
    table_entry_fns_subset = _get_table_entry_fns_subset(table_verbosity)

    rows = []
    for sd in _gather_sacred_dicts():
        row = OrderedDict()
        for col_name, make_entry_fn in table_entry_fns_subset.items():
            row[col_name] = make_entry_fn(sd)
        rows.append(row)

    df = pd.DataFrame(rows)
    if len(df) > 0:
        df.sort_values(by=["algo", "env_name"], inplace=True)

    display_options = dict(index=False)
    if csv_output_path is not None:
        df.to_csv(csv_output_path, **display_options)
        print(f"Wrote CSV file to {csv_output_path}")
    if tex_output_path is not None:
        s: str = df.to_latex(**display_options)
        with open(tex_output_path, "w") as f:
            f.write(s)
        print(f"Wrote TeX file to {tex_output_path}")

    if print_table:
        print(df.to_string(**display_options))
    return df


def _make_return_summary(stats: dict, prefix="") -> str:
    return "{:3g} ± {:3g} (n={})".format(
        stats[f"{prefix}return_mean"], stats[f"{prefix}return_std"], stats["n_traj"]
    )


def main_console():
    observer = FileStorageObserver(osp.join("output", "sacred", "analyze"))
    analysis_ex.observers.append(observer)
    analysis_ex.run_commandline()


if __name__ == "__main__":  # pragma: no cover
    main_console()
