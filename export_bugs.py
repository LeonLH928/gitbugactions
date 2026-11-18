import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import fire
import tqdm
import yaml
from docker.models.containers import Container

from collect_bugs import BugPatch
from gitbugactions.actions.actions import Act, ActCacheDirManager, ActTestsRun
from gitbugactions.docker.client import DockerClient
from gitbugactions.docker.export import extract_diff
from gitbugactions.test_executor import TestExecutor
from gitbugactions.utils.actions_utils import get_default_github_actions
from gitbugactions.utils.repo_utils import clone_repo, delete_repo_clone

diff_file_lock = threading.Lock()


def create_exported_containers(
    repo_full_name: str,
    runs: List[ActTestsRun],
    bug_patch: BugPatch,
    export_commit: str,
    export_path: str,
):
    commit_hash = bug_patch.commit
    docker_client = DockerClient.getInstance()

    for run in runs:
        # FIXME: some workflows have chars that are not allowed in docker container names
        # and act splits the name at 64 characters
        filter_name = f"act-{run.workflow_name.replace(' ', '-')}"
        filters = {"name": filter_name[:63]}
        containers: List[Container] = docker_client.containers.list(filters=filters)
        if not containers:
            clean_name = run.workflow_name.replace(" ", "-")
            containers = [
                c
                for c in docker_client.containers.list(all=True)
                if clean_name[:20] in c.name or "act" in c.name
            ]
        if run.failed and bug_patch.strategy_used != "PRE_EXISTING" and len(run.tests) == 0:
            logging.error(
                f"Run failed while exporting container {export_commit} from {repo_full_name}@{commit_hash} ({run.workflow_name}).\n"
                "Run stdout:\n"
                f"{run.stdout}\n\n"
                "Run stderr:\n"
                f"{run.stderr}"
            )

            for container in containers:
                container.stop()
                container.remove(v=True, force=True)
            return

        for container in containers:
            container.stop()
            diff_folder_path = os.path.join(
                export_path, repo_full_name.replace("/", "-"), export_commit
            )
            with diff_file_lock:
                if not os.path.exists(diff_folder_path):
                    os.makedirs(diff_folder_path)
                else:
                    # Container already being saved. This may happen if bugs
                    # were collected from two consecutive commits
                    container.remove(v=True, force=True)
                    continue
            diff_file_path = os.path.join(diff_folder_path, container.name)
            extract_diff(
                container.id, diff_file_path, ignore_paths=["/tmp/*", "*.act-result*"]
            )
            container.remove(v=True, force=True)

            workflows = os.path.join(diff_folder_path, "workflow")
            os.mkdir(workflows)
            with open(os.path.join(workflows, f"{run.workflow_name}.yml"), "w") as f:
                yaml.dump(run.workflow.doc, f)
        # FIXME: we only consider a single workflow per commit
        break


def export_bug_containers(
    bug: Dict,
    export_path: str,
    base_image: str | None = None,
    use_default_actions: bool = False,
):
    TestExecutor.toggle_cleanup(False)
    repo_full_name = bug["repository"]
    commit_hash = bug["commit_hash"]
    diff_folder_path = os.path.join(
        export_path, repo_full_name.replace("/", "-"), commit_hash
    )
    if os.path.exists(diff_folder_path) and os.listdir(diff_folder_path):
        logging.info(
            f"   ↳ [Export Engine] ⚡ Bỏ qua vì đã tồn tại container diff cho {repo_full_name}@{commit_hash[:8]}"
        )
        return

    logging.info(f"   ↳ [Export Engine] Bắt đầu trích xuất container diff cho {repo_full_name}@{commit_hash[:8]}...")
    t0 = time.time()
    temp_path = os.path.join(tempfile.gettempdir(), str(uuid.uuid4()))
    repo_clone = clone_repo(f"https://github.com/{repo_full_name}", temp_path)
    first_commit = repo_clone.revparse_single(str(repo_clone.head.target))
    default_actions = None
    if use_default_actions:
        default_actions = get_default_github_actions(
            repo_clone, first_commit, bug["language"]
        )

    act_cache_dir = ActCacheDirManager.acquire_act_cache_dir()
    bug_patch: BugPatch = BugPatch.from_dict(bug, repo_clone)
    try:
        executor = TestExecutor(
            repo_clone,
            bug["language"],
            act_cache_dir,
            default_actions,
            base_image=base_image,
        )
        runs = bug_patch.test_current_commit(executor, keep_containers=True)
        create_exported_containers(
            repo_full_name, runs, bug_patch, bug["commit_hash"], export_path
        )
        logging.info(
            f"   ↳ [Export Engine] Trích xuất container diff thành công cho {repo_full_name}@{commit_hash[:8]} "
            f"trong {time.time() - t0:.1f}s"
        )
    finally:
        ActCacheDirManager.return_act_cache_dir(act_cache_dir)
        delete_repo_clone(repo_clone)


def export_bugs(
    dataset_path: str = None,
    output_folder_path: str = None,
    bugs_path: str = None,
    output_path: str = None,
    base_image: str | None = None,
    use_default_actions: bool = False,
):
    """Export the containers (reproducible environment) for the bug-fixes collected by collect_bugs.

    Args:
        dataset_path (str, optional): Folder where the result of collect_bugs is. Defaults to "data/out_bugs".
        output_folder_path (str, optional): Folder on which the results will be saved. Defaults to "data/export".
        bugs_path (str, optional): Alias for dataset_path.
        output_path (str, optional): Alias for output_folder_path.
        base_image (str, optional): Base image to use for building the runner image. If None, uses default.
        use_default_actions (bool, optional): Whether to use and collect default GitHub actions from repositories. Defaults to False.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    dataset_path = dataset_path or bugs_path or "data/out_bugs"
    output_folder_path = output_folder_path or output_path or "data/export"

    logging.info("=" * 60)
    logging.info("🚀 Bước 3: Đóng băng container & xuất Diff Layers")
    logging.info(f"🚀 Nguồn: {dataset_path} -> Thư mục xuất: {output_folder_path}")
    logging.info("=" * 60)
    t_step3_start = time.time()

    if not os.path.exists(dataset_path):
        logging.warning(
            f"Dataset path '{dataset_path}' does not exist. No bugs to export."
        )
        return

    # FIXME: export_bugs is not working with multiple workers
    n_workers = 1
    ActCacheDirManager.init_act_cache_dirs(n_dirs=n_workers)
    executor = ThreadPoolExecutor(max_workers=n_workers)
    futures = []
    futures_to_bug = {}

    Act(base_image=base_image)

    for jsonl_path in os.listdir(dataset_path):
        if jsonl_path == "log.out" or jsonl_path == "data.json":
            continue

        with open(os.path.join(dataset_path, jsonl_path), "r") as jsonl:
            lines = jsonl.readlines()
            for line in lines:
                bug = json.loads(line)
                futures.append(
                    executor.submit(
                        export_bug_containers,
                        bug,
                        output_folder_path,
                        base_image,
                        use_default_actions,
                    )
                )
                futures_to_bug[futures[-1]] = bug

    total_export = len(futures)
    logging.info(f"📦 Tìm thấy {total_export} bugs cần xuất container diff layers...")
    export_idx = 0
    exported_count = 0

    for future in tqdm.tqdm(as_completed(futures), total=total_export, desc="Exporting containers"):
        export_idx += 1
        bug = futures_to_bug[future]
        try:
            future.result()
            exported_count += 1
            logging.info(
                f"   ↳ [{export_idx}/{total_export}] ✅ EXPORTED container cho {bug['repository']}@{bug['commit_hash'][:8]}"
            )
        except Exception as e:
            logging.error(
                f"   ↳ [{export_idx}/{total_export}] ❌ Ngoại lệ tại {bug['repository']}@{bug['commit_hash'][:8]}: {traceback.format_exc()}"
            )
            continue

    executor.shutdown()

    t_step3_dur = time.time() - t_step3_start
    logging.info("=" * 60)
    logging.info(f"🎉 Bước 3 hoàn thành sau {t_step3_dur:.1f}s!")
    logging.info(f"🎉 Đã xuất thành công: {exported_count}/{total_export} container layers sang {output_folder_path}")
    logging.info("=" * 60)


def main():
    fire.Fire(export_bugs)


if __name__ == "__main__":
    sys.exit(main())
