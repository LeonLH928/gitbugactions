import json
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

import fire
import pygit2
import tqdm
from junitparser import TestCase
from pathlib import Path

from collect_bugs import BugPatch
from gitbugactions.actions.actions import Act, ActCacheDirManager, ActTestsRun
from gitbugactions.docker.client import DockerClient
from gitbugactions.docker.export import create_diff_image
from gitbugactions.test_executor import TestExecutor
from gitbugactions.utils.repo_utils import delete_repo_clone
from gitbugactions.utils.repo_state_manager import RepoStateManager
from run_bug import get_default_actions, get_diff_path


def run_commit(
    bug: BugPatch,
    repo_clone: pygit2.Repository,
    diff_folder_path: str,
    image_name: str,
    test_fn: Callable[[], Optional[List[ActTestsRun]]],
    offline: bool,
    use_default_actions: bool = False,
) -> Optional[List[ActTestsRun]]:
    act_cache_dir = ActCacheDirManager.acquire_act_cache_dir()
    try:
        # FIXME: we shouldn't need to call this here, but we have to do it because the get_default_actions script will inspect the repo state
        RepoStateManager.reset_to_commit(repo_clone, bug.commit)

        default_actions = None
        if use_default_actions:
            default_actions = get_default_actions(
                diff_folder_path, repo_clone, bug.language
            )

        executor = TestExecutor(
            repo_clone,
            bug.language,
            act_cache_dir,
            default_actions,
            runner_image=image_name,
        )

        return test_fn(executor, offline)
    except Exception as e:
        traceback.print_exc()
    finally:
        ActCacheDirManager.return_act_cache_dir(act_cache_dir)


def equal_test_results(old_test_results: List[Dict], new_test_results: List[TestCase]):
    def check_test(old_test: Dict, new_test: TestCase):
        # Different test
        if not (
            old_test["name"] == new_test.name
            and old_test["classname"] == new_test.classname
        ):
            return False
        # Test is passed
        if new_test.is_passed and old_test["results"][0]["result"] == "Passed":
            return True
        if len(old_test["results"]) != len(new_test.result):
            return False
        aux_new_test_result = list(new_test.result)
        for old_result in old_test["results"]:
            for i, new_result in enumerate(aux_new_test_result):
                if old_result["result"] == new_result.__class__.__name__:
                    aux_new_test_result.pop(i)
                    break
            else:
                return False
        return True

    if len(old_test_results) != len(new_test_results):
        return False

    aux_new_test_results = list(new_test_results)
    for old_test in old_test_results:
        for i, new_test in enumerate(aux_new_test_results):
            if check_test(old_test, new_test):
                aux_new_test_results.pop(i)
                break
        else:
            return False
    return True


def filter_bug(
    bug: Dict,
    repo_clone: pygit2.Repository,
    export_path: str,
    offline: bool,
    n_executions: int,
    base_image: str | None = None,
    use_default_actions: bool = False,
) -> str:
    owns_image = False
    try:
        repo_name = bug["repository"].replace("/", "-")
        bug_patch: BugPatch = BugPatch.from_dict(bug, repo_clone)
        diff_folder_path = os.path.join(export_path, repo_name, bug_patch.commit)
        logging.info(
            f"   ↳ [Filter Run] Đang kiểm tra tính lặp lại {n_executions} lần cho "
            f"{bug['repository']}@{bug['commit_hash'][:8]} (strategy: {bug.get('strategy', 'N/A')})..."
        )
        t_filter0 = time.time()

        Act(base_image=base_image)  # Pass base_image to Act initialization
        image_name = f"gitbugactions-run-bug:{str(uuid.uuid4())}"
        docker_client = DockerClient.getInstance()
        # Export is skipped for failing current commits (Type E): fall back to base image
        diff_path = (
            get_diff_path(diff_folder_path) if os.path.isdir(diff_folder_path) else None
        )
        owns_image = diff_path is not None
        if owns_image:
            create_diff_image("gitbugactions:latest", image_name, diff_path)
        else:
            image_name = "gitbugactions:latest"

        previous_commit_runs = []
        previous_commit_with_diff_runs = []
        current_commit_runs = []

        for _ in range(n_executions):
            run = run_commit(
                bug_patch,
                repo_clone,
                diff_folder_path,
                image_name,
                bug_patch.test_previous_commit,
                offline=offline,
                use_default_actions=use_default_actions,
            )
            previous_commit_runs.append(run)

            if len(bug_patch.test_patch) > 0:
                run = run_commit(
                    bug_patch,
                    repo_clone,
                    diff_folder_path,
                    image_name,
                    bug_patch.test_previous_commit_with_diff,
                    offline=offline,
                    use_default_actions=use_default_actions,
                )
                previous_commit_with_diff_runs.append(run)

            run = run_commit(
                bug_patch,
                repo_clone,
                diff_folder_path,
                image_name,
                bug_patch.test_current_commit,
                offline=offline,
                use_default_actions=use_default_actions,
            )
            current_commit_runs.append(run)

        strategy = bug.get("strategy", "")
        is_build_strategy = strategy in (
            "DEPENDENCY_CONFLICT",
            "PIPELINE_CONFIG",
        )

        for runs_group in (
            previous_commit_runs,
            previous_commit_with_diff_runs,
            current_commit_runs,
        ):
            for run_list in runs_group:
                for act_run in run_list or []:
                    if act_run.return_code == 124 or "exitcode '137'" in (
                        (act_run.stdout or "") + (act_run.stderr or "")
                    ):
                        bug["infra_failure"] = True

        curr_empty = all(
            run is None or len(run) == 0 or run[0].tests is None or len(run[0].tests) == 0
            for run in current_commit_runs
        )
        prev_empty = all(
            run is None or len(run) == 0 or run[0].tests is None or len(run[0].tests) == 0
            for run in previous_commit_runs
        )

        # It is a fail if current commit runs had no tests, or (for test-based strategies) if previous had no tests
        if curr_empty or (not is_build_strategy and prev_empty):
            return "FAIL"

        if (
            not is_build_strategy
            and len(bug_patch.test_patch) != 0
            and any(
                run is None or len(run) == 0 or run[0].tests is None or len(run[0].tests) == 0
                for run in previous_commit_with_diff_runs
            )
        ):
            return "FAIL"

        if bug.get("infra_failure"):
            return "FAIL"

        # It is flaky if at least one run produces different test results
        is_flaky = False
        if (
            not is_build_strategy
            and bug.get("actions_runs")
            and len(bug["actions_runs"]) > 0
            and bug["actions_runs"][0]
            and bug["actions_runs"][0][0].get("tests")
        ):
            if any(
                run is None
                or not equal_test_results(
                    bug["actions_runs"][0][0]["tests"], run[0].tests
                )
                for run in previous_commit_runs
            ):
                is_flaky = True

        if (
            not is_flaky
            and not is_build_strategy
            and len(bug_patch.test_patch) != 0
            and bug.get("actions_runs")
            and len(bug["actions_runs"]) > 1
            and bug["actions_runs"][1]
            and bug["actions_runs"][1][0].get("tests")
        ):
            if any(
                run is None
                or not equal_test_results(
                    bug["actions_runs"][1][0]["tests"], run[0].tests
                )
                for run in previous_commit_with_diff_runs
            ):
                is_flaky = True

        if (
            not is_flaky
            and bug.get("actions_runs")
            and len(bug["actions_runs"]) > 2
            and bug["actions_runs"][2]
            and bug["actions_runs"][2][0].get("tests")
        ):
            if any(
                run is None
                or not equal_test_results(
                    bug["actions_runs"][2][0]["tests"], run[0].tests
                )
                for run in current_commit_runs
            ):
                is_flaky = True

        if is_flaky:
            return "FLAKY"
        # It is non-flaky if not empty and all runs are deterministic
        return "NON-FLAKY"
    finally:
        # delete_repo_clone(repo_clone)
        if owns_image:
            docker_client.images.remove(image_name, force=True)


def filter_bugs(
    bugs_path: str,
    export_path: str,
    res_path: str,
    n_workers: int = 1,
    offline: bool = True,
    n_executions: int = 5,
    base_image: str | None = None,
    use_default_actions: bool = False,
):
    """Creates the list of non-flaky bug-fixes that are able to be reproduced.

    Args:
        bugs_path (str): Folder where the result of collect_bugs is.
        export_path (str): Folder where the result of export_bugs is.
        res_path (str): Folder on which the results will be saved.
        n_workers (int, optional): Number of parallel workers. Defaults to 1.
        offline (bool, optional): If the containers must be isolated from the internet. Defaults to True.
        n_executions (int, optional): Number of times to execute each test. Defaults to 5.
        base_image (str, optional): Base image to use for building the runner image. If None, uses default.
        use_default_actions (bool, optional): Whether to use and collect default GitHub actions from repositories. Defaults to False.
    """
    ActCacheDirManager.init_act_cache_dirs(n_dirs=n_workers)
    executor = ThreadPoolExecutor(max_workers=n_workers)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    logging.info("=" * 60)
    logging.info("🚀 Bước 4: Lọc Flaky & Kiểm định Reproducibility")
    logging.info(f"🚀 N_Executions: {n_executions} lần | Offline: {offline} | Workers: {n_workers}")
    logging.info(f"🚀 Nguồn bugs: {bugs_path} -> Kết quả lọc: {res_path}")
    logging.info("=" * 60)
    t_step4_start = time.time()

    if not os.path.exists(bugs_path):
        logging.warning(f"Bugs path '{bugs_path}' does not exist. No bugs to filter.")
        return

    # Load existing verified commits from res_path if available
    existing_filtered: Dict[Tuple[str, str], str] = {}
    status_stats = {"NON-FLAKY": 0, "FLAKY": 0, "FAIL": 0}
    if os.path.exists(res_path):
        for fname, default_stat in [("non-flaky.json", "NON-FLAKY"), ("flaky.json", "FLAKY"), ("fail.json", "FAIL")]:
            fpath = os.path.join(res_path, fname)
            if os.path.exists(fpath):
                with open(fpath, "r") as f:
                    for line in f:
                        if line.strip():
                            rec = json.loads(line)
                            st = rec.get("status", default_stat)
                            existing_filtered[(rec["repository"], rec["commit"])] = st
                            status_stats[st] = status_stats.get(st, 0) + 1

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        future_to_bug: Dict[Future, Dict] = {}

        for bugs_file in os.listdir(bugs_path):
            if bugs_file == "data.json" or not bugs_file.endswith(".json"):
                continue

            with open(os.path.join(bugs_path, bugs_file), "r") as f:
                bugs = list(filter(lambda line: len(line.strip()) != 0, f.readlines()))
                if len(bugs) == 0:
                    continue
            clone_url = json.loads(bugs[0])["clone_url"]
            repo_clone = pygit2.clone_repository(
                clone_url, os.path.join(tempfile.gettempdir(), str(uuid.uuid4()))
            )

            try:
                for bug in bugs:
                    bug = json.loads(bug)
                    repo_key = (bug["repository"], bug["commit_hash"])
                    if repo_key in existing_filtered:
                        logging.info(
                            f"   ↳ [Filter Run] ⚡ Bỏ qua vì đã kiểm định: {bug['repository']}@{bug['commit_hash'][:8]} ({existing_filtered[repo_key]})"
                        )
                        continue

                    new_repo_path = os.path.join(
                        tempfile.gettempdir(), str(uuid.uuid4())
                    )
                    shutil.copytree(repo_clone.workdir, new_repo_path, symlinks=True)
                    repo_clone_copy = pygit2.Repository(
                        os.path.join(new_repo_path, ".git")
                    )
                    future = executor.submit(
                        filter_bug,
                        bug,
                        repo_clone_copy,
                        export_path,
                        offline,
                        n_executions,
                        base_image,
                        use_default_actions,
                    )
                    future_to_bug[future] = bug
            finally:
                # delete_repo_clone(repo_clone)
                pass

        total_filter = len(future_to_bug)
        logging.info(f"🔬 Tìm thấy {total_filter} commits cần kiểm định flakiness ({n_executions} executions mỗi commit, {len(existing_filtered)} đã cached)...")
        filter_idx = 0

        for future in tqdm.tqdm(as_completed(future_to_bug), total=total_filter, desc="Filtering flakiness"):
            filter_idx += 1
            try:
                bug = future_to_bug[future]
                repository = bug["repository"]
                commit = bug["commit_hash"]
                status = future.result()
            except Exception:
                logging.error(
                    f"   ↳ [{filter_idx}/{total_filter}] ❌ Lỗi kiểm tra flakiness trên {repository}@{commit}: {traceback.format_exc()}"
                )
            else:
                status_stats[status] = status_stats.get(status, 0) + 1
                logging.info(
                    f"   ↳ [{filter_idx}/{total_filter}] {repository}@{commit[:8]} "
                    f"({bug.get('strategy', 'N/A')}) -> KẾT QUẢ: {status} | "
                    f"(Non-Flaky: {status_stats.get('NON-FLAKY', 0)}, Flaky: {status_stats.get('FLAKY', 0)}, Fail: {status_stats.get('FAIL', 0)})"
                )
                if not Path(res_path).exists():
                    Path(res_path).mkdir(parents=True, exist_ok=True)
                record = {
                    "repository": repository,
                    "commit": commit,
                    "strategy": bug.get("strategy", "UNKNOWN"),
                    "status": status,
                }
                if bug.get("infra_failure"):
                    record["infra_failure"] = True
                if status == "NON-FLAKY":
                    with open(os.path.join(res_path, "non-flaky.json"), "a") as f:
                        f.write(json.dumps(record) + "\n")
                elif status == "FAIL":
                    with open(os.path.join(res_path, "fail.json"), "a") as f:
                        f.write(json.dumps(record) + "\n")
                elif status == "FLAKY":
                    with open(os.path.join(res_path, "flaky.json"), "a") as f:
                        f.write(json.dumps(record) + "\n")

    t_step4_dur = time.time() - t_step4_start
    logging.info("=" * 60)
    logging.info(f"🎉 Bước 4 hoàn thành sau {t_step4_dur:.1f}s!")
    logging.info(
        f"🎉 Kết quả kiểm định: NON-FLAKY: {status_stats.get('NON-FLAKY', 0)} | "
        f"FLAKY (Type D1): {status_stats.get('FLAKY', 0)} | "
        f"FAIL (Type D2 / Unsound): {status_stats.get('FAIL', 0)}"
    )
    logging.info(f"🎉 Kết quả lưu tại: {res_path}")
    logging.info("=" * 60)


def main():
    fire.Fire(filter_bugs)


if __name__ == "__main__":
    sys.exit(main())
