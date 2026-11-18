import datetime
import fire
import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid

from pathlib import Path
from typing import Optional, Callable
from gitbugactions.docker.client import DockerClient
from github import Repository

from gitbugactions.actions.actions import (
    Act,
    ActCacheDirManager,
    ActCheckCodeFailureStrategy,
    GitHubActions,
)
from gitbugactions.actions.templates.template_workflows import (
    TemplateWorkflowManager,
)
from gitbugactions.crawler import RepoCrawler, RepoStrategy
from gitbugactions.infra.infra_checkers import is_infra_file
from gitbugactions.utils.repo_utils import clone_repo, delete_repo_clone


def run_workflow(repo_path, workflow, language, base_image=None):
    """
    Common utility to run a GitHub Actions workflow

    Args:
        repo_path: Path to the repository
        workflow: Workflow to run
        language: Repository language
        base_image: Base image to use for building the runner

    Returns:
        ActTestsRun: Result of running the workflow
    """
    act_cache_dir = ActCacheDirManager.acquire_act_cache_dir()
    try:
        # Create the GitHub Actions runner
        actions = GitHubActions(repo_path, language, base_image=base_image)

        # Act creates names for the containers by hashing the content of the workflows
        # To avoid conflicts between threads, we randomize the name
        actions.test_workflows[0].doc["name"] = str(uuid.uuid4())
        actions.save_workflows()

        # Run the workflow
        return actions.run_workflow(workflow, act_cache_dir=act_cache_dir)
    finally:
        ActCacheDirManager.return_act_cache_dir(act_cache_dir)


class CollectReposStrategy(RepoStrategy):
    def __init__(
        self,
        data_path: str,
        use_template_workflows: bool = True,
        max_repos: Optional[int] = None,
    ):
        self.data_path = data_path
        self.uuid = str(uuid.uuid1())
        self.use_template_workflows = use_template_workflows
        self.max_repos = max_repos
        self.counter = 0
        self.success_counter = 0
        self.lock = threading.Lock()

    def save_data(self, data: dict, repo):
        """
        Saves the data json to a file with the name of the repository
        """
        repo_name = repo.full_name.replace("/", "-")
        data_path = os.path.join(self.data_path, repo_name + ".json")
        with open(data_path, "w") as f:
            json.dump(data, f, indent=4)

    def handle_repo(self, repo: Repository):
        with self.lock:
            self.counter += 1
            idx = self.counter
        target_str = f"/{self.max_repos}" if self.max_repos else ""
        logging.info(
            f"⏳ [Repo {idx}{target_str}] Bắt đầu xử lý: {repo.full_name} "
            f"({repo.stargazers_count} ⭐, lang: {repo.language})"
        )

        repo_path = os.path.join(
            tempfile.gettempdir(), self.uuid, repo.full_name.replace("/", "-")
        )

        # Handle repositories without a language detected
        lang = repo.language
        if lang is None and isinstance(getattr(repo, "get_languages", lambda: None)(), dict):
            lang = next(iter(repo.get_languages()), None)
        if lang is None:
            logging.info(f"   ↳ ⏭️ [Repo {idx}{target_str}] Bỏ qua {repo.full_name}: không xác định được ngôn ngữ")
            return

        data = {
            "repository": repo.full_name,
            "stars": repo.stargazers_count,
            "language": lang.strip().lower(),
            "size": repo.size,
            "clone_url": repo.clone_url,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "clone_success": False,
            "number_of_actions": 0,
            "number_of_test_actions": 0,
            "actions_successful": False,
            "using_template_workflow": False,  # Track if we used a template workflow
        }

        t_clone = time.time()
        logging.info(f"   ↳ 📥 [Repo {idx}{target_str}] Đang clone {repo.full_name} ({repo.size} KB)...")
        repo_clone = clone_repo(repo.clone_url, repo_path)
        logging.info(f"   ↳ 📦 [Repo {idx}{target_str}] Clone xong trong {time.time() - t_clone:.1f}s")

        try:
            data["clone_success"] = True

            actions = GitHubActions(repo_path, lang)
            data["number_of_actions"] = len(actions.workflows)
            data["actions_build_tools"] = [
                x.get_build_tool() for x in actions.workflows
            ]
            data["number_of_test_actions"] = len(actions.test_workflows)
            data["actions_test_build_tools"] = [
                x.get_build_tool() for x in actions.test_workflows
            ]
            actions.save_workflows()
            logging.info(
                f"   ↳ 📋 [Repo {idx}{target_str}] Workflows: {len(actions.workflows)} tổng số, "
                f"{len(actions.test_workflows)} test workflow(s)"
            )

            # Check if we have test workflows, otherwise use a template if enabled
            if len(actions.test_workflows) == 0 and self.use_template_workflows:
                logging.info(
                    f"   ↳ 🛠️ [Repo {idx}{target_str}] Không có test workflow, tạo template cho {repo.full_name}"
                )

                # Create a template workflow
                TemplateWorkflowManager.create_temp_workflow(repo_path, lang)
                # Create a new actions instance to include our template
                actions = GitHubActions(repo_path, lang)
                data["using_template_workflow"] = True
                actions.save_workflows()

            # If we have at least one test workflow, run it
            if len(actions.test_workflows) >= 1:
                wf_path = actions.test_workflows[0].path
                logging.info(
                    f"   ↳ 🏃 [Repo {idx}{target_str}] Chạy test workflow '{wf_path}' qua Act "
                    f"({len(actions.test_workflows)} workflows tìm thấy)..."
                )

                act_run = run_workflow(
                    repo_path,
                    actions.test_workflows[0],
                    lang,
                )
                data["actions_successful"] = not act_run.failed
                data["actions_run"] = act_run.asdict()
                res_str = "SUCCESS" if not act_run.failed else "FAILED"
                num_tests = len(act_run.tests) if act_run.tests else 0
                logging.info(
                    f"   ↳ 🏁 [Repo {idx}{target_str}] Act hoàn tất trong {act_run.elapsed_time:.1f}s | "
                    f"Kết quả: {res_str} | Tests: {num_tests}"
                )
            else:
                logging.info(
                    f"   ↳ ℹ️ [Repo {idx}{target_str}] Không tìm thấy test workflow nào, bỏ qua chạy test"
                )

            delete_repo_clone(repo_clone)
            self.save_data(data, repo)
            with self.lock:
                if data["actions_successful"]:
                    self.success_counter += 1
                curr_saved = self.success_counter
            logging.info(
                f"   ↳ 💾 [Repo {idx}{target_str}] Đã lưu metadata | "
                f"Repos CI/CD thành công: {curr_saved}/{idx}"
            )
        except Exception as e:
            logging.error(
                f"   ↳ ❌ [Repo {idx}{target_str}] Lỗi khi xử lý {repo.full_name}: {traceback.format_exc()}"
            )

            delete_repo_clone(repo_clone)
            self.save_data(data, repo)


class CollectInfraReposStrategy(CollectReposStrategy):
    def __init__(self, data_path: str, use_template_workflows: bool = False):
        super().__init__(data_path, use_template_workflows)
        if use_template_workflows:
            logging.warning("use_template_workflows is not supported for infra repos")

    def test_actions(self, data: dict, repo: Repository, repo_path: str):
        actions = GitHubActions(repo_path, repo.language)
        data["number_of_actions"] = len(actions.workflows)
        data["actions_build_tools"] = [x.get_build_tool() for x in actions.workflows]
        data["number_of_test_actions"] = len(actions.test_workflows)
        data["actions_test_build_tools"] = [
            x.get_build_tool() for x in actions.test_workflows
        ]
        data["actions_run"] = []
        actions.save_workflows()

        if len(actions.workflows) >= 1:
            logging.info(f"Running actions for {repo.full_name}")

            for workflow in actions.workflows:
                # Act creates names for the containers by hashing the content of the workflows
                # To avoid conflicts between threads, we randomize the name
                workflow.doc["name"] = str(uuid.uuid4())
                actions.save_workflows()

                act_cache_dir = ActCacheDirManager.acquire_act_cache_dir()
                try:
                    act_run = actions.run_workflow(
                        workflow,
                        act_cache_dir=act_cache_dir,
                        act_fail_strategy=ActCheckCodeFailureStrategy(),
                    )
                finally:
                    ActCacheDirManager.return_act_cache_dir(act_cache_dir)

                data["actions_run"].append(act_run.asdict())
                if not act_run.failed:
                    data["actions_successful"] = True
                    break
            else:
                data["actions_successful"] = False

    def handle_repo(self, repo: Repository):
        logging.info(f"Cloning {repo.full_name} - {repo.clone_url}")
        repo_path = os.path.join(
            tempfile.gettempdir(), self.uuid, repo.full_name.replace("/", "-")
        )

        data = {
            "repository": repo.full_name,
            "stars": repo.stargazers_count,
            "language": (
                repo.language.strip().lower() if repo.language is not None else ""
            ),
            "size": repo.size,
            "clone_url": repo.clone_url,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "clone_success": False,
            "number_of_actions": -1,
            "number_of_test_actions": -1,
            "actions_successful": None,
        }

        repo_clone = clone_repo(repo.clone_url, repo_path)
        data["clone_success"] = True

        infra_files = 0
        for root, _, files in os.walk(repo_path):
            for f in files:
                if is_infra_file(Path(os.path.join(root, f))):
                    infra_files += 1

        data["infra_files"] = infra_files
        if infra_files == 0:
            delete_repo_clone(repo_clone)
            self.save_data(data, repo)
            return

        try:
            self.test_actions(data, repo, repo_path)
            delete_repo_clone(repo_clone)
            self.save_data(data, repo)
        except Exception as e:
            logging.error(
                f"Error while processing {repo.full_name}: {traceback.format_exc()}"
            )

            delete_repo_clone(repo_clone)
            self.save_data(data, repo)


def cleanup_act():
    client = DockerClient.getInstance()
    ancestors = [
        "gitbugactions:latest",
    ]

    # Cleanup containers
    for container in client.containers.list(filters={"ancestor": ancestors}):
        try:
            logging.info(f"Stopping and removing container {container.name}")
            container.stop()
            container.remove(v=True, force=True)
        except Exception as e:
            logging.error(
                f"Error while stopping and removing container {container.name}: {traceback.format_exc()}"
            )

    # Cleanup volumes
    for volume in client.volumes.list():
        if volume.name.startswith("act-") and not volume.name == "act-toolcache":
            try:
                logging.info(f"Removing volume {volume.name}")
                volume.remove(force=True)
            except Exception as e:
                logging.error(
                    f"Error while removing volume {volume.name}: {traceback.format_exc()}"
                )


def collect_repos(
    query: str,
    pagination_freq: Optional[str] = None,
    n_workers: int = 1,
    out_path: str = "./out/",
    base_image: str | None = None,
    use_template_workflows: bool = True,
    cleanup_interval: int = 100,
    enable_cleanup: bool = False,
    max_repos: Optional[int] = None,
    output_path: Optional[str] = None,
):
    """Collect the repositories from GitHub that match the query and have executable
    GitHub Actions workflows with parsable tests.

    Args:
        query (str): Query with the Github searching format (https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories).
        pagination_freq (str, optional): Useful if the number of repos to collect is superior to 1000 results (GitHub limit). The possible values are listed here: https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timeseries-offset-aliases.
                                         For instance, if the value is 'D', each request will be limited to the repos created in a single day, until all the days are obtained.
        n_workers (int, optional): Number of parallel workers. Defaults to 1.
        out_path (str, optional): Folder on which the results will be saved. Defaults to "./out/".
        base_image (str, optional): Base image to use for building the runner image. If None, uses default.
        use_template_workflows (bool, optional): Whether to use template workflows for repos without test workflows. Defaults to True.
        cleanup_interval (int, optional): Number of jobs after which to run the cleanup function. Defaults to 100.
        enable_cleanup (bool, optional): Whether to enable the periodic cleanup function. Defaults to False.
        max_repos (int, optional): Maximum number of repositories to crawl. Defaults to None.
        output_path (str, optional): Alias for out_path.
    """
    out_path = output_path or out_path
    if out_path.endswith(".json"):
        out_path = os.path.dirname(out_path) or "./out/"

    if not Path(out_path).exists():
        os.makedirs(out_path, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    logging.info("=" * 60)
    logging.info("🚀 Bước 1: Bắt đầu thu thập Repositories có CI/CD từ GitHub")
    logging.info(f"🚀 Query: '{query}'")
    logging.info(f"🚀 Workers: {n_workers} | Max repos: {max_repos or 'Unlimited'} | Output: {out_path}")
    logging.info("=" * 60)
    t_step1_start = time.time()

    Act(base_image=base_image)  # Initialize Act with base_image

    # Set up cleanup function if enabled
    cleanup_function = cleanup_act if enable_cleanup else None

    crawler = RepoCrawler(
        query,
        pagination_freq=pagination_freq,
        n_workers=n_workers,
        cleanup_interval=cleanup_interval,
        cleanup_function=cleanup_function,
        max_repos=max_repos,
    )
    strategy = CollectReposStrategy(
        out_path, use_template_workflows=use_template_workflows, max_repos=max_repos
    )
    crawler.get_repos(strategy)

    t_step1_dur = time.time() - t_step1_start
    logging.info("=" * 60)
    logging.info(f"🎉 Bước 1 hoàn thành sau {t_step1_dur:.1f}s!")
    logging.info(
        f"🎉 Tổng repos đã quét: {strategy.counter} | "
        f"Số repos có CI/CD thành công: {strategy.success_counter}"
    )
    logging.info(f"🎉 Metadata lưu tại: {out_path}")
    logging.info("=" * 60)


def main():
    fire.Fire(collect_repos)


if __name__ == "__main__":
    sys.exit(main())
