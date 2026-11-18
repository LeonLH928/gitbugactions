import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

import dateutil.parser
import fire
import pygit2
import tqdm
from github import (
    GithubException,
    PaginatedList,
    PullRequest,
    Repository,
    UnknownObjectException,
)
from nltk.stem import PorterStemmer
from nltk.tokenize import wordpunct_tokenize
from unidiff import PatchSet

from gitbugactions.actions.action import Action
from gitbugactions.actions.actions import Act, ActCacheDirManager, ActTestsRun
from gitbugactions.actions.workflow import GitHubWorkflow
from gitbugactions.actions.workflow_factory import GitHubWorkflowFactory
from gitbugactions.collect_bugs.bug_patch import BugPatch
from gitbugactions.collect_bugs.collection_strategies import *
from gitbugactions.collect_bugs.test_config import TestConfig
from gitbugactions.github_api import GithubAPI
from gitbugactions.test_executor import TestExecutor
from gitbugactions.utils.actions_utils import get_default_github_actions
from gitbugactions.utils.file_reader import GitShowFileReader
from gitbugactions.utils.file_utils import FileType, get_file_type
from gitbugactions.utils.repo_utils import clone_repo, delete_repo_clone
from gitbugactions.utils.repo_state_manager import RepoStateManager
from gitbugactions.actions.templates.template_workflows import TemplateWorkflowManager


class PatchCollector:
    CLONE_SEM = threading.Semaphore(16)

    def __init__(self, repo: Repository, **kwargs):
        self.repo: Repository = repo
        lang = repo.language
        if lang is None and isinstance(getattr(repo, "get_languages", lambda: None)(), dict):
            lang = next(iter(repo.get_languages()), "unknown")
        self.language = (lang or "unknown").strip().lower()
        self.cloned = False
        self.clone_lock = threading.Lock()
        self.default_github_actions = None
        self.filter_on_commit_message = kwargs.get("filter_on_commit_message", True)
        self.filter_on_commit_time_start = kwargs.get(
            "filter_on_commit_time_start", None
        )
        self.filter_on_commit_time_end = kwargs.get("filter_on_commit_time_end", None)
        self.pull_requests = kwargs.get("pull_requests", False)
        self.filter_linked_to_pr = kwargs.get("filter_linked_to_pr", None)
        self.max_commits = kwargs.get("max_commits", None)
        self.cap_normal_pass = kwargs.get("cap_normal_pass", False)
        self.normal_pass_count = 0

    def __clone_repo(self):
        # Too many repos cloning at the same time lead to errors
        with PatchCollector.CLONE_SEM:
            with self.clone_lock:
                if self.cloned:
                    return
                self.delete_repo()
                repo_path = os.path.join(
                    tempfile.gettempdir(), self.repo.full_name.replace("/", "-")
                )
                repo_path = os.path.join(repo_path, str(uuid.uuid4()))
                logging.info(f"Cloning {self.repo.full_name} - {self.repo.clone_url}")
                self.repo_clone: pygit2.Repository = clone_repo(
                    self.repo.clone_url, repo_path
                )
                # Set gc.auto to 0 to avoid "too many open files" bug
                subprocess.run(
                    f"git config gc.auto 0",
                    cwd=repo_path,
                    shell=True,
                    capture_output=True,
                )
                self.first_commit = self.repo_clone.revparse_single(
                    str(self.repo_clone.head.target)
                )
                self.cloned = True

    def __is_bug_fix(self, commit: pygit2.Commit):
        tokens = wordpunct_tokenize(commit.message)
        stemmer = PorterStemmer()
        tokens = [stemmer.stem(token) for token in tokens]
        bug_fix_keywords = {"fix", "resolv", "patch", "repair", "correct", "workaround"}
        return any(keyword in tokens for keyword in bug_fix_keywords)

    def __get_patches(self, repo_clone, commit, previous_commit):
        diff = repo_clone.diff(str(previous_commit.id), str(commit.id))
        patch: PatchSet = PatchSet(diff.patch)
        bug_patch: PatchSet = PatchSet("")
        test_patch: PatchSet = PatchSet("")
        dependency_patch: PatchSet = PatchSet("")
        workflow_patch: PatchSet = PatchSet("")
        non_code_patch: PatchSet = PatchSet("")

        for p in patch:
            source_type = get_file_type(self.language, p.source_file)
            target_type = get_file_type(self.language, p.target_file)
            if source_type == FileType.TESTS or target_type == FileType.TESTS:
                test_patch.append(p)
            elif source_type == FileType.SOURCE or target_type == FileType.SOURCE:
                bug_patch.append(p)
            elif (
                source_type == FileType.DEPENDENCY
                or target_type == FileType.DEPENDENCY
            ):
                dependency_patch.append(p)
            elif (
                source_type == FileType.WORKFLOW
                or target_type == FileType.WORKFLOW
            ):
                workflow_patch.append(p)
            else:
                non_code_patch.append(p)

        return (
            bug_patch,
            test_patch,
            non_code_patch,
            dependency_patch,
            workflow_patch,
        )

    @staticmethod
    def _get_incoming_diff(repo_clone, previous_commit) -> Tuple[Optional[str], str]:
        """Return (previous_parent_hash, raw diff prev~1 -> prev) for Stage 1 leak-free input."""
        try:
            prev_parent = repo_clone.revparse_single(str(previous_commit.id) + "~1")
        except KeyError:
            return None, ""
        diff = repo_clone.diff(str(prev_parent.id), str(previous_commit.id))
        return str(prev_parent.id), (diff.patch or "")

    def __test_patch(
        self,
        bug: BugPatch,
        act_cache_dir: str,
    ) -> List[Optional[List[ActTestsRun]]]:
        test_patch_runs = [None, None, None]
        self.__clone_repo()

        new_repo_path = os.path.join(tempfile.gettempdir(), str(uuid.uuid4()))
        shutil.copytree(self.repo_clone.workdir, new_repo_path, symlinks=True)
        repo_clone = pygit2.Repository(os.path.join(new_repo_path, ".git"))

        try:
            executor = TestExecutor(
                repo_clone,
                self.language,
                act_cache_dir,
                self.default_github_actions,
            )

            def all_runs_crashed(x):
                return x is None or all(map(lambda act_run: act_run.failed, x))

            # Previous commit
            act_runs = bug.test_previous_commit(executor)
            if act_runs is None:
                return test_patch_runs
            prev_tests_count = sum(len(r.tests) for r in act_runs)
            allows_prev_crash = (
                prev_tests_count > 0
                or len(bug.dependency_patch) > 0
                or len(bug.workflow_patch) > 0
            )
            if all_runs_crashed(act_runs) and not allows_prev_crash:
                return test_patch_runs
            test_patch_runs[0] = act_runs

            # Previous commit with diff
            if len(bug.test_patch) > 0:
                act_runs = bug.test_previous_commit_with_diff(executor)
                if act_runs is not None:
                    test_patch_runs[1] = act_runs

            # Current commit
            act_runs = bug.test_current_commit(executor)
            if act_runs is not None:
                test_patch_runs[2] = act_runs
        finally:
            delete_repo_clone(repo_clone)

        return test_patch_runs

    def __get_related_commit_info(self, commit_hex: str):
        self.__clone_repo()

        commit = self.repo_clone.revparse_single(commit_hex)
        matches = re.findall("#[0-9]+", commit.message)
        issues = []

        if len(matches) > 0:
            github = GithubAPI()
            # We need to get the repo again to use the current token
            repo = github.get_repo(self.repo.full_name)
        else:
            return []

        for match in matches:
            match_id = int(match[1:])
            try:
                # GitHub's REST API considers every pull request an issue
                # https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#get-an-issue
                issue = repo.get_issue(match_id)
                is_pull_request = issue.pull_request is not None
                comments, labels, review_comments = [], [], None

                if is_pull_request:
                    review_comments = []
                    pull_request = issue.as_pull_request()
                    for comment in pull_request.get_review_comments():
                        review_comments.append(comment.body)

                for comment in issue.get_comments():
                    comments.append(comment.body)

                for label in issue.get_labels():
                    labels.append(
                        {"name": label.name, "description": label.description}
                    )

                issues.append(
                    {
                        "id": match_id,
                        "title": issue.title,
                        "body": issue.body,
                        "comments": comments,
                        "labels": labels,
                        "is_pull_request": is_pull_request,
                        "review_comments": review_comments,
                    }
                )
            except (UnknownObjectException, GithubException):
                continue

        return issues

    def __get_used_actions(self, commit: str) -> Set[Action]:
        """
        Get the actions used by the workflows declared in the commit version.
        Use git show to avoid checking out the whole version
        """
        actions: Set[Action] = set()
        reader = GitShowFileReader(commit, self.repo_clone.workdir)

        # Read workflows directory listing
        workflows_listing = reader.read_file(".github/workflows")
        if workflows_listing is None:
            return actions

        # Get the workflows paths
        workflow_paths = workflows_listing.split("\n")

        # Get the actions used by each workflow
        for workflow_path in workflow_paths:
            # Skip empty lines and non yaml files
            if workflow_path == "" or not (
                workflow_path.endswith(".yml") or workflow_path.endswith(".yaml")
            ):
                continue

            workflow_path = f".github/workflows/{workflow_path}"
            try:
                workflow: GitHubWorkflow = GitHubWorkflowFactory.create_workflow(
                    workflow_path, self.language, reader
                )
                actions.update(workflow.get_actions())
            except Exception:
                continue

        return actions

    def __get_template_actions(self) -> Set[Action]:
        """
        Get the actions used by the template workflow.
        """
        workflow_path = TemplateWorkflowManager.create_temp_workflow(
            self.repo_clone.workdir, self.language
        )
        if workflow_path is None:
            return set()
        try:
            workflow: GitHubWorkflow = GitHubWorkflowFactory.create_workflow(
                workflow_path, self.language
            )
            return workflow.get_actions()
        finally:
            TemplateWorkflowManager.remove_temp_workflow(workflow_path)

    def get_possible_patches(self):
        self.__clone_repo()
        if len(list(self.repo_clone.references.iterator())) == 0:
            return

        commit_to_patches: Dict[str, List[BugPatch]] = {}
        commits = list(self.repo_clone.walk(self.repo_clone.head.target))
        if self.max_commits:
            # walk() is newest-first: cap per-repo cost, keep most recent history
            commits = commits[: self.max_commits]

        try:
            if self.pull_requests:
                pulls: PaginatedList[PullRequest] = self.repo.get_pulls()
                for pull in pulls:
                    pull_commits = pull.get_commits()
                    for pull_commit in pull_commits:
                        commits.append(self.repo_clone.get(pull_commit.sha))

            for commit in commits:
                # Deferred: dependency/workflow commits (Type B/C) bypass the message filter
                msg_rejected = (
                    self.filter_on_commit_message and not self.__is_bug_fix(commit)
                )

                commit_time = datetime.datetime.fromtimestamp(
                    int(commit.commit_time), datetime.UTC
                )
                if (
                    self.filter_on_commit_time_start
                    and commit_time < self.filter_on_commit_time_start
                ):
                    continue

                if (
                    self.filter_on_commit_time_end
                    and commit_time > self.filter_on_commit_time_end
                ):
                    continue

                # Filter based on whether commit is linked to a PR
                if self.filter_linked_to_pr:
                    issues = self.__get_related_commit_info(str(commit.id))
                    has_pr = any(issue["is_pull_request"] for issue in issues)
                    if not has_pr:
                        continue

                try:
                    previous_commit = self.repo_clone.revparse_single(
                        str(commit.id) + "~1"
                    )
                except KeyError:
                    # The current commit is the first one
                    continue

                (
                    bug_patch,
                    test_patch,
                    non_code_patch,
                    dependency_patch,
                    workflow_patch,
                ) = self.__get_patches(self.repo_clone, commit, previous_commit)
                if msg_rejected and len(dependency_patch) == 0 and len(workflow_patch) == 0:
                    continue
                if (
                    len(bug_patch) == 0
                    and len(non_code_patch) == 0
                    and len(dependency_patch) == 0
                    and len(workflow_patch) == 0
                ):
                    logging.info(
                        f"Skipping commit {self.repo.full_name} {str(commit.id)}: no bug, dependency, or workflow patch"
                    )
                    continue

                actions: Set[Action] = set()
                actions.update(self.__get_used_actions(str(commit.id)))
                actions.update(self.__get_used_actions(str(previous_commit.id)))

                prev_parent_hash, incoming_diff = self._get_incoming_diff(
                    self.repo_clone, previous_commit
                )
                if prev_parent_hash is None:
                    continue
                bp = BugPatch(
                    self.repo,
                    commit,
                    previous_commit,
                    bug_patch,
                    test_patch,
                    non_code_patch,
                    actions,
                    dependency_patch=dependency_patch,
                    workflow_patch=workflow_patch,
                    previous_parent_hash=prev_parent_hash,
                    incoming_diff=incoming_diff,
                )

                if str(previous_commit.id) in commit_to_patches:
                    commit_to_patches[str(previous_commit.id)].append(bp)
                else:
                    commit_to_patches[str(previous_commit.id)] = [bp]
                RepoStateManager.reset_to_commit(self.repo_clone, commit.id)
        finally:
            RepoStateManager.reset_to_commit(self.repo_clone, self.first_commit.id)

        # We remove the merges since when multiple bug patches point to the same
        # previous commit, merges tend to only add useless diffs to another commit
        # that already fixes the bug.
        # https://github.com/gitbugactions/gitbugactions/issues/40
        for previous_commit, grouped_patches in commit_to_patches.items():
            if len(grouped_patches) > 1:
                commit_to_patches[previous_commit] = list(
                    filter(
                        lambda patch: not patch.commit_message.startswith("Merge "),
                        grouped_patches,
                    )
                )

        patches: List[BugPatch] = sum(commit_to_patches.values(), [])
        patches.sort(key=lambda x: x.commit_timestamp)
        # Creates list without duplicates. Duplicates are patches with the same diff
        # We sort the list in order to keep the oldest patch
        patches = list(set(patches))
        # We sort again to return the patches in chronological order
        patches.sort(key=lambda x: x.commit_timestamp)
        return patches

    def set_default_github_actions(self):
        if not self.cloned:
            self.__clone_repo()
        self.default_github_actions = get_default_github_actions(
            self.repo_clone, self.first_commit, self.language
        )

    def test_patch(self, bug_patch: BugPatch):
        # Optionally cap NORMAL_PASS at 2 per repo and skip pure non-code commits once 1 PASS is collected
        if (
            self.cap_normal_pass
            and self.normal_pass_count >= 1
            and bug_patch.change_type.name == "NON_CODE_ONLY"
        ):
            return False
        act_cache_dir = ActCacheDirManager.acquire_act_cache_dir()

        try:
            logging.info(
                f"   ↳ [Act Test] Đang test patch {bug_patch.commit[:8]} "
                f"trên {bug_patch.repo.full_name} (chờ Act runner)..."
            )
            t_test_start = time.time()
            test_patch_runs = self.__test_patch(
                bug_patch,
                act_cache_dir,
            )
            bug_patch.actions_runs = test_patch_runs
            strategy = PatchCollector.check_runs(bug_patch)
            if strategy == "NORMAL_PASS":
                if self.cap_normal_pass and self.normal_pass_count >= 2:
                    strategy = None
                else:
                    self.normal_pass_count += 1
            t_test_dur = time.time() - t_test_start
            if strategy is None:
                logging.info(
                    f"   ↳ [Act Test] Commit {bug_patch.commit[:8]} không thỏa mãn taxonomy strategy nào "
                    f"({t_test_dur:.1f}s)"
                )
                return False
            else:
                bug_patch.strategy_used = strategy
                bug_patch.issues = self.__get_related_commit_info(bug_patch.commit)
                logging.info(
                    f"   ↳ [Act Test] Commit {bug_patch.commit[:8]} KHỚP strategy '{strategy}'! "
                    f"({t_test_dur:.1f}s)"
                )
                return True
        finally:
            ActCacheDirManager.return_act_cache_dir(act_cache_dir)

    @staticmethod
    def check_runs(bug_patch: BugPatch) -> Optional[str]:
        for strategy in TestConfig.strategies:
            if strategy.check(bug_patch):
                return strategy.name

        return None

    def delete_repo(self):
        if self.cloned:
            delete_repo_clone(self.repo_clone)
        self.cloned = False

    def analyze_specific_commits(self, commit_shas: List[str]) -> List[BugPatch]:
        """Analyze specific commits instead of scanning through all repository commits.

        Args:
            commit_shas (List[str]): List of commit SHAs to analyze

        Returns:
            List[BugPatch]: List of bug patches found in the specified commits
        """
        self.__clone_repo()
        if len(list(self.repo_clone.references.iterator())) == 0:
            return []

        bug_patches: List[BugPatch] = []

        try:
            for commit_sha in commit_shas:
                try:
                    # Get the commit and its parent
                    commit = self.repo_clone.revparse_single(commit_sha)

                    # Make sure the commit has a parent
                    if len(commit.parents) == 0:
                        logging.warning(f"Commit {commit_sha} has no parent, skipping")
                        continue

                    previous_commit = commit.parents[0]

                    # Get patches and actions
                    (
                        bug_patch,
                        test_patch,
                        non_code_patch,
                        dependency_patch,
                        workflow_patch,
                    ) = self.__get_patches(self.repo_clone, commit, previous_commit)

                    # Skip if there's no bug, dependency, or workflow patch
                    if (
                        len(bug_patch) == 0
                        and len(non_code_patch) == 0
                        and len(dependency_patch) == 0
                        and len(workflow_patch) == 0
                    ):
                        logging.info(
                            f"Skipping commit {self.repo.full_name} {str(commit.id)}: no bug patch"
                        )
                        continue

                    # Get actions used in the commit and its parent
                    actions: Set[Action] = set()
                    actions.update(self.__get_used_actions(str(commit.id)))
                    actions.update(self.__get_used_actions(str(previous_commit.id)))

                    prev_parent_hash, incoming_diff = self._get_incoming_diff(
                        self.repo_clone, previous_commit
                    )
                    if prev_parent_hash is None:
                        continue
                    # Create a BugPatch object
                    patch = BugPatch(
                        self.repo,
                        commit,
                        previous_commit,
                        bug_patch,
                        test_patch,
                        non_code_patch,
                        actions,
                        dependency_patch=dependency_patch,
                        workflow_patch=workflow_patch,
                        previous_parent_hash=prev_parent_hash,
                        incoming_diff=incoming_diff,
                    )

                    bug_patches.append(patch)
                    RepoStateManager.reset_to_commit(self.repo_clone, commit.id)

                except Exception as e:
                    logging.error(f"Error analyzing commit {commit_sha}: {e}")
                    continue
        finally:
            # Reset to the original state
            RepoStateManager.reset_to_commit(self.repo_clone, self.first_commit.id)

        return bug_patches


def set_test_config(
    normalize_non_code_patch: bool = True,
    strategies: Tuple[str] = (
        # Structural strategies first: first match wins in check_runs()
        "PIPELINE_CONFIG",
        "DEPENDENCY_CONFLICT",
        "PASS_PASS",
        "FAIL_PASS",
        "PRE_EXISTING",
    ),
):
    TestConfig.normalize_non_code_patch = normalize_non_code_patch
    strategy_instances = [s() for s in CollectionStrategy.__subclasses__()]

    TestConfig.strategies = []
    for strategy in strategies:
        matched = [s for s in strategy_instances if s.name == strategy]
        if matched:
            TestConfig.strategies.append(matched[0])


def parse_commit_url(commit_url: str) -> Tuple[str, str]:
    """Extract repository name and commit SHA from a GitHub commit URL.

    Args:
        commit_url (str): GitHub commit URL in the format "https://github.com/owner/repo/commit/sha"

    Returns:
        Tuple[str, str]: A tuple containing (repository_name, commit_sha)
    """
    parts = commit_url.strip().split("/")
    if len(parts) >= 5 and parts[2] == "github.com" and parts[5] == "commit":
        repo_name = f"{parts[3]}/{parts[4]}"
        commit_sha = parts[6]
        return repo_name, commit_sha
    raise ValueError(f"Invalid GitHub commit URL format: {commit_url}")


def collect_bugs(
    data_path: str,
    results_path="data/out_bugs",
    n_workers=1,
    memory_limit="7g",
    filter_on_commit_message: bool = True,
    filter_on_commit_time_start: str = None,
    filter_on_commit_time_end: str = None,
    normalize_non_code_patch: bool = True,
    strategies: Tuple[str] = (
        "PIPELINE_CONFIG",
        "DEPENDENCY_CONFLICT",
        "PASS_PASS",
        "FAIL_PASS",
        "PRE_EXISTING",
    ),
    pull_requests: bool = False,
    filter_linked_to_pr: bool = None,
    base_image: str | None = None,
    use_default_actions: bool = False,
    commit_list_file: str = None,
    collect_passing_commits: bool = False,
    max_commits: int = None,
    cap_normal_pass: bool = False,
):
    """Collects bug-fixes from the repos listed in `data_path`. The result is saved
    on `results_path`. A file `data.json` is also created with information about
    the repos.

    Args:
        data_path (str): Folder where the result of collect_repos is.
        results_path (str, optional): Folder on which the results will be saved.
                                      Defaults to "data/out_bugs".
        n_workers (int, optional): Number of parallel workers. Defaults to 1.
        memory_limit (str, optional): Memory limit per container (https://docs.docker.com/config/containers/resource_constraints/#limit-a-containers-access-to-memory).
                                      Defaults to "7g".
        filter_on_commit_message (bool, optional): If True, only commits with the word "fix" in the commit message will be considered.
        filter_on_commit_time_start (str, optional): If set, only commits after this date will be considered. The string must follow the format "yyyy-mm-dd HH:MM UTC".
        filter_on_commit_time_end (str, optional): If set, only commits before this date will be considered. The string must follow the format "yyyy-mm-dd HH:MM UTC".
        normalize_non_code_patch (bool, optional): If True, the non-code patch will be applied to previous commits. Defaults to True.
        strategies (Tuple[str], optional): List of strategies to be used.
                                           The available strategies are: "PASS_PASS", "FAIL_PASS", "FAIL_FAIL", "FAIL_PASS_BUILD", "DEPENDENCY_CONFLICT", "PIPELINE_CONFIG", "PRE_EXISTING", "NORMAL_PASS".
        pull_requests (bool, optional): If True, the commits in pull requests will be considered. Defaults to False.
        filter_linked_to_pr (bool, optional): If True, only include commits that are linked to pull requests. If False, only include commits that are not linked to pull requests. If None, include all commits. Defaults to None.
        base_image (str, optional): Base image to use for building the runner image. If None, uses default.
        use_default_actions (bool, optional): Whether to use and collect default GitHub actions from repositories. Defaults to False.
        commit_list_file (str, optional): Path to a JSON file containing a list of commit URLs to analyze. If provided, data_path is ignored. Defaults to None.
        collect_passing_commits (bool, optional): If True, also collects passing commits (NORMAL_PASS) for Stage 1 training. Defaults to False.
    """
    strategies = list(strategies)
    if collect_passing_commits:
        if "NORMAL_PASS" not in strategies:
            strategies.append("NORMAL_PASS")
        filter_on_commit_message = False

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    logging.info("=" * 60)
    logging.info("🚀 Bước 2: Bắt đầu thu thập commits & phân loại Taxonomy")
    logging.info(f"🚀 Nguồn dữ liệu: {data_path} | Kết quả lưu tại: {results_path}")
    logging.info(f"🚀 Strategies: {strategies} | Workers: {n_workers} | Memory: {memory_limit}")
    logging.info("=" * 60)
    t_step2_start = time.time()

    set_test_config(normalize_non_code_patch, tuple(strategies))

    Act.set_memory_limit(memory_limit)
    Act(base_image=base_image)  # Initialize Act with base_image
    github: GithubAPI = GithubAPI(
        per_page=100,
        pool_size=n_workers,
    )
    ActCacheDirManager.init_act_cache_dirs(n_dirs=n_workers)

    kwargs = {
        "filter_on_commit_message": filter_on_commit_message,
        "filter_on_commit_time_start": (
            dateutil.parser.parse(filter_on_commit_time_start)
            if filter_on_commit_time_start is not None
            else None
        ),
        "filter_on_commit_time_end": (
            dateutil.parser.parse(filter_on_commit_time_end)
            if filter_on_commit_time_end is not None
            else None
        ),
        "pull_requests": pull_requests,
        "filter_linked_to_pr": filter_linked_to_pr,
        "max_commits": max_commits,
        "cap_normal_pass": cap_normal_pass,
    }

    patch_collectors: List[Tuple[PatchCollector, Any]] = []

    # Create the results directory if it doesn't exist
    if not os.path.exists(results_path):
        os.makedirs(results_path)

    # Mode: analyze specific commits from a file
    if commit_list_file is not None and os.path.exists(commit_list_file):
        # Read and parse the commit list file
        with open(commit_list_file, "r") as f:
            commit_urls = json.load(f)

        # Group commits by repository to minimize cloning operations
        commits_by_repo = {}
        for commit_url in commit_urls:
            try:
                repo_name, commit_sha = parse_commit_url(commit_url)
                if repo_name not in commits_by_repo:
                    commits_by_repo[repo_name] = []
                commits_by_repo[repo_name].append(commit_sha)
            except ValueError as e:
                logging.error(f"Error parsing commit URL: {e}")
                continue

        logging.info(
            f"Found {len(commits_by_repo)} repositories with {len(commit_urls)} commits to analyze"
        )

        # Process each repository and its commits
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_to_collector: Dict[Future, Tuple[PatchCollector, List[str]]] = {}

            for repo_name, commit_shas in commits_by_repo.items():
                try:
                    repo = github.get_repo(repo_name)
                    patch_collector = PatchCollector(repo, **kwargs)
                    future_to_collector[
                        executor.submit(
                            patch_collector.analyze_specific_commits, commit_shas
                        )
                    ] = (patch_collector, commit_shas)
                except Exception as e:
                    logging.error(f"Error getting repository {repo_name}: {e}")
                    continue

            for future in tqdm.tqdm(
                as_completed(future_to_collector), total=len(future_to_collector)
            ):
                try:
                    patch_collector, _ = future_to_collector[future]
                    result = future.result()
                except Exception:
                    logging.error(
                        f"Error while collecting commits from {patch_collector.repo}: {traceback.format_exc()}"
                    )
                else:
                    patch_collectors.append((patch_collector, result))

    # Default mode: analyze repositories based on data_path
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_to_collector: Dict[Future, PatchCollector] = {}

            if not os.path.exists(data_path):
                logging.error(f"Data path '{data_path}' does not exist.")
                return

            if os.path.isfile(data_path):
                files_to_read = [data_path]
            else:
                files_to_read = [
                    os.path.join(data_path, file)
                    for file in os.listdir(data_path)
                    if file.endswith(".json")
                ]

            logging.info(f"📂 Đã đọc danh sách từ '{data_path}': tìm thấy {len(files_to_read)} files repo metadata")

            for file_path in files_to_read:
                with open(file_path, "r") as f:
                    run = json.loads(f.read())

                    if (
                        (
                            run.get("number_of_test_actions", 0) >= 1
                            or run.get("using_template_workflow", False)
                        )
                        and "actions_run" in run
                        and len(run["actions_run"].get("tests", [])) > 0
                    ):
                        repo = github.get_repo(run["repository"])
                        patch_collector = PatchCollector(repo, **kwargs)
                        future_to_collector[
                            executor.submit(patch_collector.get_possible_patches)
                        ] = patch_collector

            total_collectors = len(future_to_collector)
            logging.info(f"⏳ Bắt đầu đào lịch sử Git cho {total_collectors} repos hợp lệ để tìm candidate bug patches...")
            collector_idx = 0
            for future in tqdm.tqdm(
                as_completed(future_to_collector), total=total_collectors, desc="Mining commits"
            ):
                collector_idx += 1
                try:
                    patch_collector = future_to_collector[future]
                    result = future.result()
                    logging.info(
                        f"   ↳ [{collector_idx}/{total_collectors}] {patch_collector.repo.full_name}: "
                        f"phát hiện {len(result)} candidate bug patches"
                    )
                except Exception:
                    logging.error(
                        f"   ↳ [{collector_idx}/{total_collectors}] Lỗi thu thập commits: {traceback.format_exc()}"
                    )
                else:
                    patch_collectors.append((patch_collector, result))

    data_path = os.path.join(results_path, "data.json")
    repos = {}
    with open(data_path, "w") as fp:
        for patch_collector, bug_patches in patch_collectors:
            repos[patch_collector.repo.full_name] = {
                "clone_url": patch_collector.repo.clone_url,
                "commits": patch_collector.repo.get_commits().totalCount,
                "possible_bug_patches": len(bug_patches),
                "stars": patch_collector.repo.stargazers_count,
                "size": patch_collector.repo.size,
            }
        fp.write(json.dumps(repos))

    # Populate the base cache dir with required actions
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures_to_actions: Dict[Future, Action] = {}
        actions_to_download: Set[Action] = set()
        for patch_collector, bug_patches in patch_collectors:
            for bug_patch in bug_patches:
                actions_to_download.update(bug_patch.actions)

        total_actions = len(actions_to_download)
        logging.info(f"📦 Đang tải và cache {total_actions} GitHub Action dependencies...")

        for action in actions_to_download:
            futures_to_actions[
                executor.submit(ActCacheDirManager.cache_action, action)
            ] = action

        action_idx = 0
        for future in tqdm.tqdm(
            as_completed(futures_to_actions), total=len(futures_to_actions), desc="Caching actions"
        ):
            action_idx += 1
            try:
                future.result()
                if action_idx % 5 == 0 or action_idx == total_actions:
                    logging.info(f"   ↳ Đã cache [{action_idx}/{total_actions}] actions...")
            except Exception:
                logging.error(
                    f"Error while downloading action: {traceback.format_exc()}"
                )
                continue

    # Only collect default GitHub actions if specified
    if use_default_actions:
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_to_collector: Dict[Future, PatchCollector] = {}
            for patch_collector, _ in patch_collectors:
                future_to_collector[
                    executor.submit(patch_collector.set_default_github_actions)
                ] = patch_collector

            for future in tqdm.tqdm(
                as_completed(future_to_collector), total=len(future_to_collector), desc="Setting default actions"
            ):
                try:
                    patch_collector = future_to_collector[future]
                    future.result()
                except Exception:
                    logging.error(
                        f"Error while setting default github actions from {patch_collector.repo}: {traceback.format_exc()}"
                    )
                    continue
    else:
        logging.info(
            "Skipping collection of default GitHub actions as requested by use_default_actions=False"
        )

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        future_to_patches: Dict[Future, Tuple[BugPatch]] = {}
        for patch_collector, bug_patches in patch_collectors:
            for bug_patch in bug_patches:
                future_to_patches[
                    executor.submit(patch_collector.test_patch, bug_patch)
                ] = bug_patch

        total_patches = len(future_to_patches)
        logging.info(f"🧪 Bắt đầu kiểm thử {total_patches} candidate patches bằng Act (workers={n_workers})...")
        patch_idx = 0
        accepted_count = 0
        strategy_stats = {}

        for future in tqdm.tqdm(
            as_completed(future_to_patches), total=total_patches, desc="Testing patches"
        ):
            patch_idx += 1
            try:
                bug_patch = future_to_patches[future]
                is_patch = future.result()
            except Exception:
                logging.error(
                    f"   ↳ [{patch_idx}/{total_patches}] ❌ Lỗi kiểm thử patch: {traceback.format_exc()}"
                )
            else:
                if is_patch:
                    accepted_count += 1
                    strat = getattr(bug_patch, "strategy_used", "UNKNOWN")
                    strategy_stats[strat] = strategy_stats.get(strat, 0) + 1
                    data_path = os.path.join(
                        results_path,
                        bug_patch.repo.full_name.replace("/", "-") + ".json",
                    )
                    with open(data_path, "a") as fp:
                        data = bug_patch.get_data()
                        fp.write((json.dumps(data) + "\n"))
                    logging.info(
                        f"   ↳ [{patch_idx}/{total_patches}] ✅ ACCEPTED: {bug_patch.repo.full_name}@{bug_patch.commit[:8]} "
                        f"(Strategy: {strat}) | Tổng accepted: {accepted_count}"
                    )
                else:
                    logging.info(
                        f"   ↳ [{patch_idx}/{total_patches}] ❌ REJECTED: {bug_patch.repo.full_name}@{bug_patch.commit[:8]}"
                    )

    for patch_collector, _ in patch_collectors:
        patch_collector.delete_repo()

    t_step2_dur = time.time() - t_step2_start
    logging.info("=" * 60)
    logging.info(f"🎉 Bước 2 hoàn thành sau {t_step2_dur:.1f}s!")
    logging.info(f"🎉 Đã kiểm thử: {total_patches} candidate patches | Chấp nhận (Accepted): {accepted_count}")
    if strategy_stats:
        logging.info("🎉 Phân bố chiến lược đã thu thập:")
        for s_name, s_count in strategy_stats.items():
            logging.info(f"   ↳ {s_name}: {s_count}")
    logging.info(f"🎉 Kết quả lưu tại: {results_path}")
    logging.info("=" * 60)


def main():
    fire.Fire(collect_bugs)


if __name__ == "__main__":
    sys.exit(main())
