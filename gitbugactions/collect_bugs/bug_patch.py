import datetime
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import pygit2
from github import Repository
from unidiff import PatchSet

from gitbugactions.actions.action import Action
from gitbugactions.actions.actions import ActTestsRun
from gitbugactions.github_api import GithubAPI
from gitbugactions.test_executor import TestExecutor
from gitbugactions.utils.file_utils import get_patch_file_extensions
from gitbugactions.utils.repo_state_manager import RepoStateManager


class ChangeType(Enum):
    SOURCE_ONLY = 0
    MIXED = 1
    NON_CODE_ONLY = 2
    DEPENDENCY_ONLY = 3
    WORKFLOW_ONLY = 4

    @staticmethod
    def get_change_type(
        bug_patch: PatchSet,
        non_code_patch: PatchSet,
        dependency_patch: Optional[PatchSet] = None,
        workflow_patch: Optional[PatchSet] = None,
    ) -> "ChangeType":
        has_source = len(bug_patch) > 0
        has_dep = dependency_patch is not None and len(dependency_patch) > 0
        has_workflow = workflow_patch is not None and len(workflow_patch) > 0
        has_non_code = len(non_code_patch) > 0

        if has_source and (has_dep or has_workflow or has_non_code):
            return ChangeType.MIXED
        elif has_source:
            return ChangeType.SOURCE_ONLY
        elif has_dep and not has_workflow and not has_non_code:
            return ChangeType.DEPENDENCY_ONLY
        elif has_workflow and not has_dep and not has_non_code:
            return ChangeType.WORKFLOW_ONLY
        elif has_dep or has_workflow:
            return ChangeType.MIXED
        else:
            return ChangeType.NON_CODE_ONLY


class BugPatch:
    def __init__(
        self,
        repo: Repository,
        commit: pygit2.Commit,
        previous_commit: pygit2.Commit,
        bug_patch: PatchSet,
        test_patch: PatchSet,
        non_code_patch: PatchSet,
        actions: Set[Action],
        dependency_patch: Optional[PatchSet] = None,
        workflow_patch: Optional[PatchSet] = None,
        previous_parent_hash: Optional[str] = None,
        incoming_diff: str = "",
    ):
        self.repo: Repository = repo
        lang = repo.language
        if lang is None and isinstance(getattr(repo, "get_languages", lambda: None)(), dict):
            lang = next(iter(repo.get_languages()), "unknown")
        self.language: str = (lang or "unknown").lower().strip()
        self.commit: str = str(commit.id)
        self.commit_message: str = commit.message
        self.commit_timestamp: str = (
            datetime.datetime.fromtimestamp(
                int(commit.commit_time), datetime.UTC
            ).isoformat()
            + "Z"
        )
        self.previous_commit: str = str(previous_commit.id)
        self.previous_commit_message: str = previous_commit.message
        self.previous_commit_timestamp: str = (
            datetime.datetime.fromtimestamp(
                int(previous_commit.commit_time), datetime.UTC
            ).isoformat()
            + "Z"
        )
        self.time_to_patch: str = str(
            datetime.datetime.fromtimestamp(int(commit.commit_time), datetime.UTC)
            - datetime.datetime.fromtimestamp(
                int(previous_commit.commit_time), datetime.UTC
            )
        )
        self.bug_patch: PatchSet = self.__clean_patch(bug_patch)
        self.bug_patch_file_extensions: List[str] = get_patch_file_extensions(
            self.bug_patch
        )
        self.test_patch: PatchSet = self.__clean_patch(test_patch)
        self.test_patch_file_extensions: List[str] = get_patch_file_extensions(
            self.test_patch
        )
        self.dependency_patch: PatchSet = self.__clean_patch(
            dependency_patch or PatchSet("")
        )
        self.dependency_patch_file_extensions: List[str] = get_patch_file_extensions(
            self.dependency_patch
        )
        self.workflow_patch: PatchSet = self.__clean_patch(
            workflow_patch or PatchSet("")
        )
        self.workflow_patch_file_extensions: List[str] = get_patch_file_extensions(
            self.workflow_patch
        )
        self.non_code_patch: PatchSet = self.__clean_patch(non_code_patch)
        self.non_code_patch_file_extensions: List[str] = get_patch_file_extensions(
            self.non_code_patch
        )
        self.change_type: ChangeType = ChangeType.get_change_type(
            self.bug_patch,
            self.non_code_patch,
            self.dependency_patch,
            self.workflow_patch,
        )
        self.actions: Set[Action] = actions
        self.previous_parent_hash: Optional[str] = previous_parent_hash
        self.incoming_diff: str = incoming_diff
        self.strategy_used: str = "UNKNOWN"
        self.issues = None
        # The actions are grouped by each phase of the strategy used
        self.actions_runs: List[List[ActTestsRun]] = []

    def __flat_failed_tests(self, runs):
        # JUnit <error> (e.g. import/fixture crashes) counts as failing too
        return sum(
            map(lambda act_run: act_run.failed_tests + act_run.erroring_tests, runs),
            [],
        )

    @property
    def prev_commit_passed(self):
        return (
            self.actions_runs[0] is not None
            and len(self.__flat_failed_tests(self.actions_runs[0])) == 0
        )

    @property
    def prev_with_diff_failed(self):
        return (
            self.actions_runs[1] is not None
            and len(self.__flat_failed_tests(self.actions_runs[1])) > 0
        )

    @property
    def curr_commit_passed(self):
        return (
            self.actions_runs[2] is not None
            and len(self.__flat_failed_tests(self.actions_runs[2])) == 0
        )

    @property
    def curr_commit_failed(self):
        return (
            self.actions_runs[2] is not None
            and len(self.__flat_failed_tests(self.actions_runs[2])) > 0
        )

    @property
    def prev_commit_failed(self):
        return (
            self.actions_runs[0] is not None
            and len(self.__flat_failed_tests(self.actions_runs[0])) > 0
        )

    @property
    def prev_build_failed(self):
        return (
            self.actions_runs[0] is not None
            and any(map(lambda run: run.failed, self.actions_runs[0]))
        )

    @property
    def curr_build_passed(self):
        return (
            self.actions_runs[2] is not None
            and not any(map(lambda run: run.failed, self.actions_runs[2]))
            and len(self.__flat_failed_tests(self.actions_runs[2])) == 0
        )

    def get_data(self):
        actions_runs = []

        for runs in self.actions_runs:
            if runs is None:
                actions_runs.append(None)
                continue
            runs_data = []

            for run in runs:
                runs_data.append(run.asdict())
            actions_runs.append(runs_data)

        return {
            "repository": self.repo.full_name,
            "language": self.language,
            "clone_url": self.repo.clone_url,
            "collection_timestamp": datetime.datetime.now(datetime.UTC).isoformat()
            + "Z",
            "commit_hash": self.commit,
            "commit_message": self.commit_message,
            "commit_timestamp": self.commit_timestamp,
            "previous_commit_hash": self.previous_commit,
            "previous_commit_message": self.previous_commit_message,
            "previous_commit_timestamp": self.previous_commit_timestamp,
            "previous_parent_hash": self.previous_parent_hash,
            "incoming_diff": self.incoming_diff,
            "time_to_patch": self.time_to_patch,
            "bug_patch": str(self.bug_patch),
            "bug_patch_file_extensions": self.bug_patch_file_extensions,
            "test_patch": str(self.test_patch),
            "test_patch_file_extensions": self.test_patch_file_extensions,
            "dependency_patch": str(self.dependency_patch),
            "dependency_patch_file_extensions": self.dependency_patch_file_extensions,
            "workflow_patch": str(self.workflow_patch),
            "workflow_patch_file_extensions": self.workflow_patch_file_extensions,
            "non_code_patch": str(self.non_code_patch),
            "non_code_patch_file_extensions": self.non_code_patch_file_extensions,
            "change_type": self.change_type.name,
            "actions_runs": actions_runs,
            "strategy": self.strategy_used,
            "issues": self.issues,
        }

    def __clean_patch(self, patch: PatchSet) -> PatchSet:
        """
        Cleans the patch to be used by pygit2. This is related to issue XXX that causes libgit2 to segfault when one of the paths is /dev/null.
        """
        for file in patch:
            if file.source_file == "/dev/null" and not file.is_added_file:
                file.source_file = file.target_file.replace("b/", "a/", 1)
            elif file.target_file == "/dev/null" and not file.is_removed_file:
                file.target_file = file.source_file.replace("a/", "b/", 1)
        return patch

    def __set_commit(self, repo_clone: pygit2.Repository, commit: str):
        commit = repo_clone.revparse_single(commit)
        RepoStateManager.clean_untracked_files(repo_clone)

        repo_clone.checkout_tree(commit)
        repo_clone.create_tag(
            str(uuid.uuid4()),
            commit.id,
            pygit2.GIT_OBJECT_COMMIT,
            commit.author,
            commit.message,
        )
        repo_clone.set_head(commit.id)

    def __apply_non_code_patch(self, repo_clone: pygit2.Repository):
        # We only apply the non code patch when the bug patch is non-empty
        # Otherwise, we are testing the non code patch alone
        if len(self.non_code_patch) > 0 and len(self.bug_patch) > 0:
            try:
                repo_clone.apply(pygit2.Diff.parse_diff(str(self.non_code_patch)))
                return True
            except pygit2.GitError:
                # Invalid patches
                return False
        return True

    def __apply_test_patch(self, repo_clone: pygit2.Repository):
        try:
            repo_clone.apply(pygit2.Diff.parse_diff(str(self.test_patch)))
            return True
        except pygit2.GitError:
            # Invalid patches
            return False

    def test_previous_commit(
        self,
        executor: TestExecutor,
        offline: bool = False,
        keep_containers: bool = False,
    ) -> Optional[List[ActTestsRun]]:
        executor.reset_repo()
        # Clean .act-result before testing
        RepoStateManager.clean_act_result_dir(executor.repo_clone.workdir)

        self.__set_commit(executor.repo_clone, self.previous_commit)
        if not self.__apply_non_code_patch(executor.repo_clone):
            return None
        return executor.run_tests(offline=offline, keep_containers=keep_containers)

    def test_previous_commit_with_diff(
        self,
        executor: TestExecutor,
        offline: bool = False,
        keep_containers: bool = False,
    ) -> Optional[List[ActTestsRun]]:
        executor.reset_repo()
        # Clean .act-result before testing
        RepoStateManager.clean_act_result_dir(executor.repo_clone.workdir)

        self.__set_commit(executor.repo_clone, self.previous_commit)
        if not self.__apply_non_code_patch(executor.repo_clone):
            return None
        if not self.__apply_test_patch(executor.repo_clone):
            return None
        return executor.run_tests(offline=offline, keep_containers=keep_containers)

    def test_current_commit(
        self,
        executor: TestExecutor,
        offline: bool = False,
        keep_containers: bool = False,
    ) -> Optional[List[ActTestsRun]]:
        executor.reset_repo()
        # Clean .act-result before testing
        RepoStateManager.clean_act_result_dir(executor.repo_clone.workdir)

        self.__set_commit(executor.repo_clone, self.commit)
        return executor.run_tests(offline=offline, keep_containers=keep_containers)

    @staticmethod
    def from_dict(bug: Dict[str, Any], repo_clone: pygit2.Repository) -> "BugPatch":
        github = GithubAPI()
        repo_full_name = bug["repository"]

        bp = BugPatch(
            github.get_repo(repo_full_name),
            repo_clone.revparse_single(bug["commit_hash"]),
            repo_clone.revparse_single(bug["previous_commit_hash"]),
            PatchSet(bug["bug_patch"]),
            PatchSet(bug["test_patch"]),
            PatchSet(bug["non_code_patch"]),
            set(),
            dependency_patch=PatchSet(bug["dependency_patch"])
            if "dependency_patch" in bug
            else None,
            workflow_patch=PatchSet(bug["workflow_patch"])
            if "workflow_patch" in bug
            else None,
            previous_parent_hash=bug.get("previous_parent_hash"),
            incoming_diff=bug.get("incoming_diff", ""),
        )
        bp.strategy_used = bug.get("strategy", "UNKNOWN")
        return bp

    def __remove_patch_index(self, patch: PatchSet) -> str:
        lines = str(patch).split("\n")
        return "\n".join(list(filter(lambda line: not line.startswith("index"), lines)))

    def __hash__(self):
        return hash(
            (
                self.__remove_patch_index(self.bug_patch),
                self.__remove_patch_index(self.test_patch),
                self.__remove_patch_index(self.dependency_patch),
                self.__remove_patch_index(self.workflow_patch),
                self.__remove_patch_index(self.non_code_patch),
            )
        )

    def __eq__(self, __value: object) -> bool:
        if not isinstance(__value, BugPatch):
            return False
        return (
            self.__remove_patch_index(self.bug_patch)
            == self.__remove_patch_index(__value.bug_patch)
            and self.__remove_patch_index(self.test_patch)
            == self.__remove_patch_index(__value.test_patch)
            and self.__remove_patch_index(self.dependency_patch)
            == self.__remove_patch_index(__value.dependency_patch)
            and self.__remove_patch_index(self.workflow_patch)
            == self.__remove_patch_index(__value.workflow_patch)
            and self.__remove_patch_index(self.non_code_patch)
            == self.__remove_patch_index(__value.non_code_patch)
        )

    def __ne__(self, __value: object) -> bool:
        return not self.__eq__(__value)
