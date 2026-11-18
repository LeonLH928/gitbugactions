from abc import ABC, abstractmethod
from typing import List

from gitbugactions.actions.actions import ActTestsRun
from gitbugactions.collect_bugs.bug_patch import BugPatch


class CollectionStrategy(ABC):
    @staticmethod
    def _diff_tests(run_failed: List[ActTestsRun], run_passed: List[ActTestsRun]):
        flat_failed_tests = sum(
            map(
                lambda act_run: act_run.failed_tests
                + getattr(act_run, "erroring_tests", []),
                run_failed,
            ),
            [],
        )
        flat_tests = sum(map(lambda act_run: act_run.tests, run_passed), [])
        fixed, not_fixed = [], []

        for failed_test in flat_failed_tests:
            for test in flat_tests:
                if (
                    failed_test.classname == test.classname
                    and failed_test.name == test.name
                    and test.is_passed
                ):
                    fixed.append(failed_test)
                    break
            else:
                not_fixed.append(failed_test)

        return fixed, not_fixed

    @staticmethod
    def _check_tests_were_fixed(
        run_failed: List[ActTestsRun], run_passed: List[ActTestsRun]
    ):
        _, not_fixed = CollectionStrategy._diff_tests(run_failed, run_passed)
        return len(not_fixed) == 0

    @staticmethod
    def _number_of_tests(runs):
        return sum(map(lambda act_run: len(act_run.tests), runs))

    @staticmethod
    def _has_patch(patch) -> bool:
        return patch is not None and len(patch) > 0

    @staticmethod
    def _is_pure_source(bug_patch: BugPatch) -> bool:
        return (
            CollectionStrategy._has_patch(getattr(bug_patch, "bug_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "dependency_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "workflow_patch", None))
        )

    @staticmethod
    def _no_build_crash(runs) -> bool:
        return runs is not None and not any(map(lambda run: run.failed, runs))

    @staticmethod
    def _all_failed_test_ids(runs) -> set:
        failed = set()
        for act_run in runs or []:
            for t in act_run.failed_tests + getattr(act_run, "erroring_tests", []):
                failed.add((t.classname, t.name))
        return failed

    @abstractmethod
    def check(self, bug_patch: BugPatch) -> bool:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass


class UnknownStrategy(CollectionStrategy):
    def check(self, bug_patch: BugPatch) -> bool:
        return False

    @property
    def name(self) -> str:
        return "UNKNOWN"


class PassPassStrategy(CollectionStrategy):
    def check(self, bug_patch: BugPatch) -> bool:
        return (
            CollectionStrategy._is_pure_source(bug_patch)
            # previous commit passed cleanly with >0 tests
            and bug_patch.prev_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[0])
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[0]) > 0
            # previous commit with new tests failed
            and bug_patch.prev_with_diff_failed
            # current commit passed cleanly
            and bug_patch.curr_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[2])
            # test patch is not empty and not removals only
            and len(bug_patch.test_patch) > 0
            and not (
                bug_patch.test_patch.removed > 0 and bug_patch.test_patch.added == 0
            )
            # check if tests from previous commit w/diff were fixed
            and CollectionStrategy._check_tests_were_fixed(
                bug_patch.actions_runs[1], bug_patch.actions_runs[2]
            )
            # previous commit should have at most the same number of tests as current commit
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[0])
            <= CollectionStrategy._number_of_tests(bug_patch.actions_runs[2])
            # current commit should have same number of tests as previous commit w/ tests
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[2])
            == CollectionStrategy._number_of_tests(bug_patch.actions_runs[1])
        )

    @property
    def name(self) -> str:
        return "PASS_PASS"


class FailPassStrategy(CollectionStrategy):
    def check(self, bug_patch: BugPatch) -> bool:
        return (
            CollectionStrategy._is_pure_source(bug_patch)
            # previous commit failed
            and bug_patch.prev_commit_failed
            # no changes have been made in the tests
            and len(bug_patch.test_patch) == 0
            # current commit passed cleanly
            and bug_patch.curr_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[2])
            # check if tests from previous commit were fixed
            and CollectionStrategy._check_tests_were_fixed(
                bug_patch.actions_runs[0], bug_patch.actions_runs[2]
            )
            # previous commit should have same number of tests as current commit
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[0])
            == CollectionStrategy._number_of_tests(bug_patch.actions_runs[2])
        )

    @property
    def name(self) -> str:
        return "FAIL_PASS"


class FailFailStrategy(CollectionStrategy):
    def check(self, bug_patch: BugPatch) -> bool:
        return (
            # previous commit failed
            bug_patch.prev_commit_failed
            # current commit failed
            and bug_patch.curr_commit_failed
            # at least one test was fixed
            and len(
                CollectionStrategy._diff_tests(
                    bug_patch.actions_runs[0], bug_patch.actions_runs[2]
                )[0]
            )
            > 0
            # previous commit should have same number of tests as current commit
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[0])
            == CollectionStrategy._number_of_tests(bug_patch.actions_runs[2])
        ) or (
            # previous commit with diff failed
            bug_patch.prev_with_diff_failed
            # current commit failed
            and bug_patch.curr_commit_failed
            # at least one test was fixed
            and len(
                CollectionStrategy._diff_tests(
                    bug_patch.actions_runs[1], bug_patch.actions_runs[2]
                )[0]
            )
            > 0
            # previous commit w/test diff should have same number of tests as current commit
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[1])
            == CollectionStrategy._number_of_tests(bug_patch.actions_runs[2])
        )

    @property
    def name(self) -> str:
        return "FAIL_FAIL"


class FailPassBuildStrategy(CollectionStrategy):
    def check(self, bug_patch: BugPatch) -> bool:
        return (
            (
                # tests failed in the previous commit
                bug_patch.prev_commit_failed
                # or the run failed
                or (
                    bug_patch.actions_runs[0] is not None
                    and any(map(lambda run: run.failed, bug_patch.actions_runs[0]))
                )
            )
            # tests passed in the current commit
            and bug_patch.curr_commit_passed
        ) or (
            (
                # tests failed in the previous commit w/diff
                bug_patch.prev_with_diff_failed
                # or the run failed
                or (
                    bug_patch.actions_runs[1] is not None
                    and any(map(lambda run: run.failed, bug_patch.actions_runs[1]))
                )
            )
            # tests passed in the current commit
            and bug_patch.curr_commit_passed
        )

    @property
    def name(self) -> str:
        return "FAIL_PASS_BUILD"


class DependencyConflictStrategy(CollectionStrategy):
    """
    Type B: Dependency & external package conflict or version breakage (strict build-only & dependency-exclusive).
    """

    def check(self, bug_patch: BugPatch) -> bool:
        dep_only = (
            CollectionStrategy._has_patch(getattr(bug_patch, "dependency_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "workflow_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "bug_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "test_patch", None))
        )
        prev_build_only_failed = (
            bug_patch.prev_build_failed
            and bug_patch.actions_runs[0] is not None
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[0]) == 0
        )
        curr_passed = (
            bug_patch.curr_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[2])
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[2]) > 0
        )
        return dep_only and prev_build_only_failed and curr_passed

    @property
    def name(self) -> str:
        return "DEPENDENCY_CONFLICT"


class PipelineConfigStrategy(CollectionStrategy):
    """
    Type C: CI/CD Pipeline and build configuration errors (strictly workflow-exclusive).
    """

    def check(self, bug_patch: BugPatch) -> bool:
        workflow_only = (
            CollectionStrategy._has_patch(getattr(bug_patch, "workflow_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "bug_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "test_patch", None))
            and not CollectionStrategy._has_patch(getattr(bug_patch, "dependency_patch", None))
        )
        prev_crashed = (
            bug_patch.prev_build_failed
            or getattr(bug_patch, "prev_commit_failed", False)
        )
        curr_passed = (
            bug_patch.curr_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[2])
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[2]) > 0
        )
        return workflow_only and prev_crashed and curr_passed

    @property
    def name(self) -> str:
        return "PIPELINE_CONFIG"


class PreExistingFailureStrategy(CollectionStrategy):
    """
    Type E: Pre-existing / orthogonal codebase breakage.
    - Pure source work (no dependency/workflow changes).
    - Every failing test in current commit was already failing in previous commit (curr ⊆ prev).
    - Current commit does not touch the pre-existing failing test or its target symbol.
    """

    @staticmethod
    def _touches_failing_test(bug_patch: BugPatch, failing_ids: set) -> bool:
        tokens = set()
        for _, tname in failing_ids:
            base = tname.split("[", 1)[0].split("{", 1)[0].split("(", 1)[0].strip().lower()
            if len(base) >= 3:
                tokens.add(base)
            stripped = base[5:] if base.startswith("test_") else (base[4:] if base.startswith("test") else base)
            if len(stripped) >= 3:
                tokens.add(stripped)
        if not tokens:
            return False

        for patch in (getattr(bug_patch, "test_patch", None), getattr(bug_patch, "bug_patch", None)):
            if not patch:
                continue
            for patched_file in patch:
                for hunk in patched_file:
                    header = (hunk.section_header or "").lower()
                    if any(tok in header for tok in tokens):
                        return True
                    for line in hunk:
                        if line.is_added or line.is_removed:
                            val = line.value.lower()
                            if any(tok in val for tok in tokens):
                                return True
        return False

    def check(self, bug_patch: BugPatch) -> bool:
        if not CollectionStrategy._is_pure_source(bug_patch):
            return False
        if not (bug_patch.prev_commit_failed and bug_patch.curr_commit_failed):
            return False

        prev_failed_ids = CollectionStrategy._all_failed_test_ids(bug_patch.actions_runs[0])
        curr_failed_ids = CollectionStrategy._all_failed_test_ids(bug_patch.actions_runs[2])
        if not curr_failed_ids or not curr_failed_ids.issubset(prev_failed_ids):
            return False

        return not self._touches_failing_test(bug_patch, curr_failed_ids)

    @property
    def name(self) -> str:
        return "PRE_EXISTING"


class NormalPassStrategy(CollectionStrategy):
    """
    Normal Passing Commits: Unrestricted across file types so Stage 1 learns safe vs. risky edits.
    """

    def check(self, bug_patch: BugPatch) -> bool:
        curr_passed = (
            bug_patch.curr_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[2])
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[2]) > 0
        )
        prev_passed = (
            bug_patch.prev_commit_passed
            and CollectionStrategy._no_build_crash(bug_patch.actions_runs[0])
            and CollectionStrategy._number_of_tests(bug_patch.actions_runs[0]) > 0
        )
        prev_diff_ok = (
            len(bug_patch.actions_runs) <= 1
            or bug_patch.actions_runs[1] is None
            or (
                not getattr(bug_patch, "prev_with_diff_failed", False)
                and CollectionStrategy._no_build_crash(bug_patch.actions_runs[1])
            )
        )
        return curr_passed and prev_passed and prev_diff_ok

    @property
    def name(self) -> str:
        return "NORMAL_PASS"
