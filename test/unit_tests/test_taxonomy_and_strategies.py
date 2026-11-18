import json
import os
import shutil
import tempfile
import pytest
from unittest.mock import MagicMock
from unidiff import PatchSet

from gitbugactions.collect_bugs.bug_patch import BugPatch, ChangeType
from gitbugactions.collect_bugs.collection_strategies import (
    DependencyConflictStrategy,
    PipelineConfigStrategy,
    PreExistingFailureStrategy,
    NormalPassStrategy,
    PassPassStrategy,
    FailPassStrategy,
)
from gitbugactions.actions.actions import ActTestsRun
from organize_bugs import determine_failure_type, organize_bugs


class DummyTest:
    def __init__(self, classname: str, name: str, is_passed: bool = True):
        self.classname = classname
        self.name = name
        self.is_passed = is_passed
        self.is_skipped = False
        self.result = []


class DummyActTestsRun:
    def __init__(self, failed: bool, tests=None, failed_tests=None, erroring_tests=None):
        self.failed = failed
        self.tests = tests or []
        self._failed_tests = failed_tests or []
        self._erroring_tests = erroring_tests or []
        self.stderr = ""
        self.stdout = ""
        self.return_code = 1 if failed else 0

    @property
    def failed_tests(self):
        return self._failed_tests

    @property
    def erroring_tests(self):
        return self._erroring_tests

    def asdict(self):
        return {
            "failed": self.failed,
            "tests": [{"name": t.name, "classname": t.classname} for t in self.tests],
            "failed_tests": [{"name": t.name, "classname": t.classname} for t in self.failed_tests],
        }


def test_change_type_classification():
    p_src = PatchSet("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n")
    p_dep = PatchSet("--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n-a\n+b\n")
    p_wf = PatchSet("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n")
    p_doc = PatchSet("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")
    p_empty = PatchSet("")

    assert ChangeType.get_change_type(p_src, p_empty) == ChangeType.SOURCE_ONLY
    assert ChangeType.get_change_type(p_empty, p_empty, dependency_patch=p_dep) == ChangeType.DEPENDENCY_ONLY
    assert ChangeType.get_change_type(p_empty, p_empty, workflow_patch=p_wf) == ChangeType.WORKFLOW_ONLY
    assert ChangeType.get_change_type(p_src, p_doc) == ChangeType.MIXED
    assert ChangeType.get_change_type(p_src, p_empty, dependency_patch=p_dep) == ChangeType.MIXED
    assert ChangeType.get_change_type(p_empty, p_doc) == ChangeType.NON_CODE_ONLY


def test_dependency_conflict_strategy():
    strategy = DependencyConflictStrategy()
    assert strategy.name == "DEPENDENCY_CONFLICT"

    bp = MagicMock(spec=BugPatch)
    bp.dependency_patch = PatchSet("--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n-1.0\n+2.0\n")
    bp.workflow_patch = PatchSet("")
    bp.bug_patch = PatchSet("")
    bp.test_patch = PatchSet("")
    bp.prev_build_failed = True
    bp.prev_commit_failed = False
    bp.curr_commit_passed = True
    bp.actions_runs = [
        [DummyActTestsRun(failed=True, tests=[])],
        None,
        [DummyActTestsRun(failed=False, tests=[DummyTest("AppTest", "test1")])],
    ]

    assert strategy.check(bp) is True

    # Rejects if previous run actually executed tests (not a strict build-only failure)
    bp.actions_runs[0] = [DummyActTestsRun(failed=True, tests=[DummyTest("AppTest", "test0")])]
    assert strategy.check(bp) is False
    bp.actions_runs[0] = [DummyActTestsRun(failed=True, tests=[])]

    # Rejects if mixed with source/test/workflow diffs (mutual exclusivity)
    bp.bug_patch = PatchSet("--- a/App.java\n+++ b/App.java\n@@ -1 +1 @@\n-a\n+b\n")
    assert strategy.check(bp) is False
    bp.bug_patch = PatchSet("")

    # Fails if dependency_patch is empty
    bp.dependency_patch = PatchSet("")
    assert strategy.check(bp) is False


def test_pipeline_config_strategy():
    strategy = PipelineConfigStrategy()
    assert strategy.name == "PIPELINE_CONFIG"

    bp = MagicMock(spec=BugPatch)
    bp.workflow_patch = PatchSet("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-runs-on: bad\n+runs-on: ubuntu-latest\n")
    bp.dependency_patch = PatchSet("")
    bp.bug_patch = PatchSet("")
    bp.test_patch = PatchSet("")
    bp.prev_build_failed = True
    bp.curr_commit_passed = True
    bp.actions_runs = [
        [DummyActTestsRun(failed=True)],
        None,
        [DummyActTestsRun(failed=False, tests=[DummyTest("AppTest", "test1")])],
    ]

    assert strategy.check(bp) is True

    # Rejects if mixed with source/test/dependency diffs
    bp.dependency_patch = PatchSet("--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n-1\n+2\n")
    assert strategy.check(bp) is False
    bp.dependency_patch = PatchSet("")

    # Fails if workflow_patch is empty
    bp.workflow_patch = PatchSet("")
    assert strategy.check(bp) is False


def test_pre_existing_failure_strategy():
    strategy = PreExistingFailureStrategy()
    assert strategy.name == "PRE_EXISTING"

    bp = MagicMock(spec=BugPatch)
    bp.bug_patch = PatchSet(
        "--- a/src/App.java\n+++ b/src/App.java\n"
        "@@ -10,2 +10,2 @@ public int add(int a, int b) {\n"
        "   // context mentioning testUnrelatedTimeout does not trigger guard\n"
        "-  return a - b;\n"
        "+  return a + b;\n"
    )
    bp.test_patch = PatchSet("")
    bp.dependency_patch = PatchSet("")
    bp.workflow_patch = PatchSet("")
    bp.prev_commit_failed = True
    bp.curr_commit_failed = True

    t_failing = DummyTest("pkg.ServiceTest", "testUnrelatedTimeout", is_passed=False)
    t_fixed = DummyTest("pkg.AppTest", "testAdd", is_passed=False)
    t_fixed_ok = DummyTest("pkg.AppTest", "testAdd", is_passed=True)
    run_prev = DummyActTestsRun(failed=False, tests=[t_failing, t_fixed], failed_tests=[t_failing, t_fixed])
    run_curr = DummyActTestsRun(failed=False, tests=[t_failing, t_fixed_ok], failed_tests=[t_failing])

    bp.actions_runs = [
        [run_prev],
        None,
        [run_curr],
    ]

    # Subset (curr_failed <= prev_failed), other/new tests pass, failing test untouched -> True
    assert strategy.check(bp) is True

    # Rejects if curr introduces a NEW failing test not in prev
    t_new_fail = DummyTest("pkg.AppTest", "testRegression", is_passed=False)
    bp.actions_runs[2] = [DummyActTestsRun(failed=False, tests=[t_failing, t_new_fail], failed_tests=[t_failing, t_new_fail])]
    assert strategy.check(bp) is False
    bp.actions_runs[2] = [run_curr]

    # Rejects if bug_patch or test_patch actually edited the still-failing test (failed fix)
    bp.test_patch = PatchSet(
        "--- a/src/ServiceTest.java\n+++ b/src/ServiceTest.java\n"
        "@@ -5,1 +5,1 @@ void testUnrelatedTimeout() {\n"
        "-  assertEquals(1, 2);\n"
        "+  assertEquals(1, 3);\n"
    )
    assert strategy.check(bp) is False
    bp.test_patch = PatchSet("")

    # Rejects if mixed with dependency_patch
    bp.dependency_patch = PatchSet("--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n-1\n+2\n")
    assert strategy.check(bp) is False
    bp.dependency_patch = PatchSet("")

    # Rejects if curr_commit_failed is False
    bp.curr_commit_failed = False
    assert strategy.check(bp) is False


def test_type_a_mutual_exclusivity_and_build_guard():
    t_pass = DummyTest("pkg.AppTest", "testOk", is_passed=True)
    t_fail = DummyTest("pkg.AppTest", "testOk", is_passed=False)

    # PASS_PASS
    pp = PassPassStrategy()
    bp = MagicMock(spec=BugPatch)
    bp.bug_patch = PatchSet("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n")
    bp.test_patch = PatchSet("--- a/test_app.py\n+++ b/test_app.py\n@@ -1 +1 @@\n-x\n+y\n")
    bp.dependency_patch = PatchSet("")
    bp.workflow_patch = PatchSet("")
    bp.prev_commit_passed = True
    bp.prev_with_diff_failed = True
    bp.curr_commit_passed = True
    bp.actions_runs = [
        [DummyActTestsRun(failed=False, tests=[t_pass])],
        [DummyActTestsRun(failed=False, tests=[t_fail], failed_tests=[t_fail])],
        [DummyActTestsRun(failed=False, tests=[t_pass])],
    ]
    assert pp.check(bp) is True

    # Rejects TYPE_A mixed with dependency_patch or workflow_patch
    bp.dependency_patch = PatchSet("--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-a\n+b\n")
    assert pp.check(bp) is False
    bp.dependency_patch = PatchSet("")

    # Rejects TYPE_A if curr had a build crash (run.failed=True)
    bp.actions_runs[2] = [DummyActTestsRun(failed=True, tests=[t_pass])]
    assert pp.check(bp) is False
    bp.actions_runs[2] = [DummyActTestsRun(failed=False, tests=[t_pass])]

    # FAIL_PASS rejects workflow_patch mixing and build crash on curr
    fp = FailPassStrategy()
    bp.test_patch = PatchSet("")
    bp.prev_commit_failed = True
    bp.actions_runs = [
        [DummyActTestsRun(failed=False, tests=[t_fail], failed_tests=[t_fail])],
        None,
        [DummyActTestsRun(failed=False, tests=[t_pass])],
    ]
    assert fp.check(bp) is True
    bp.workflow_patch = PatchSet("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n")
    assert fp.check(bp) is False


def test_normal_pass_strategy():
    strategy = NormalPassStrategy()
    assert strategy.name == "NORMAL_PASS"

    bp = MagicMock(spec=BugPatch)
    bp.curr_commit_passed = True
    bp.prev_commit_passed = True

    t_pass = DummyTest("pkg.MainTest", "testFast", is_passed=True)
    run_prev = DummyActTestsRun(failed=False, tests=[t_pass], failed_tests=[])
    run_curr = DummyActTestsRun(failed=False, tests=[t_pass], failed_tests=[])

    bp.actions_runs = [
        [run_prev],
        None,
        [run_curr],
    ]

    assert strategy.check(bp) is True

    # Rejects if previous had 0 tests (e.g. empty actions_runs[0])
    bp.prev_commit_passed = True
    bp.actions_runs = [[], None, [run_curr]]
    assert strategy.check(bp) is False

    # Rejects if prev_with_diff (actions_runs[1]) failed
    bp.prev_with_diff_failed = True
    bp.actions_runs = [[run_prev], [DummyActTestsRun(failed=True)], [run_curr]]
    assert strategy.check(bp) is False

    # Rejects if previous failed
    bp.prev_with_diff_failed = False
    bp.prev_commit_passed = False
    bp.actions_runs = [[run_prev], None, [run_curr]]
    assert strategy.check(bp) is False


def test_determine_failure_type():
    assert determine_failure_type("FLAKY", "PASS_PASS") == "TYPE_D"
    assert determine_failure_type("FAIL", "DEPENDENCY_CONFLICT") == "TYPE_B"
    assert determine_failure_type("FAIL", "PIPELINE_CONFIG") == "TYPE_C"
    assert determine_failure_type("FAIL", "UNKNOWN") is None
    assert determine_failure_type("FAIL", "UNKNOWN", infra_failure=True) == "TYPE_D"
    assert determine_failure_type("NON-FLAKY", "NORMAL_PASS") == "PASS"
    assert determine_failure_type("NON-FLAKY", "DEPENDENCY_CONFLICT") == "TYPE_B"
    assert determine_failure_type("NON-FLAKY", "PIPELINE_CONFIG") == "TYPE_C"
    assert determine_failure_type("NON-FLAKY", "PRE_EXISTING") == "TYPE_E"
    assert determine_failure_type("NON-FLAKY", "PASS_PASS") == "TYPE_A"
    assert determine_failure_type("NON-FLAKY", "FAIL_PASS") == "TYPE_A"


def test_organize_bugs_multitype_manifest():
    temp_dir = tempfile.mkdtemp(prefix="test_organize_multitype_")
    try:
        collect_dir = os.path.join(temp_dir, "collect")
        export_dir = os.path.join(temp_dir, "export")
        filter_dir = os.path.join(temp_dir, "filter")
        output_dir = os.path.join(temp_dir, "output")

        os.makedirs(collect_dir)
        os.makedirs(export_dir)
        os.makedirs(filter_dir)

        repo = "acme/safeci"
        repo_file = "acme-safeci"

        # Mock collected commits
        commits = [
            {"repository": repo, "commit_hash": "sha_a", "strategy": "PASS_PASS", "change_type": "SOURCE_ONLY"},
            {"repository": repo, "commit_hash": "sha_b", "strategy": "DEPENDENCY_CONFLICT", "change_type": "DEPENDENCY_ONLY"},
            {"repository": repo, "commit_hash": "sha_c", "strategy": "PIPELINE_CONFIG", "change_type": "WORKFLOW_ONLY"},
            {"repository": repo, "commit_hash": "sha_d", "strategy": "PASS_PASS", "change_type": "SOURCE_ONLY"},
            {"repository": repo, "commit_hash": "sha_e", "strategy": "PRE_EXISTING", "change_type": "SOURCE_ONLY"},
            {"repository": repo, "commit_hash": "sha_pass", "strategy": "NORMAL_PASS", "change_type": "SOURCE_ONLY"},
        ]

        with open(os.path.join(collect_dir, f"{repo_file}.json"), "w") as f:
            for c in commits:
                f.write(json.dumps(c) + "\n")

        # Mock filter outputs
        with open(os.path.join(filter_dir, "non-flaky.json"), "w") as f:
            f.write(json.dumps({"repository": repo, "commit": "sha_a"}) + "\n")
            f.write(json.dumps({"repository": repo, "commit": "sha_b"}) + "\n")
            f.write(json.dumps({"repository": repo, "commit": "sha_c"}) + "\n")
            f.write(json.dumps({"repository": repo, "commit": "sha_e"}) + "\n")
            f.write(json.dumps({"repository": repo, "commit": "sha_pass"}) + "\n")

        with open(os.path.join(filter_dir, "flaky.json"), "w") as f:
            f.write(json.dumps({"repository": repo, "commit": "sha_d"}) + "\n")

        # Run organize_bugs
        organize_bugs(collect_dir, export_dir, filter_dir, output_dir)

        manifest_path = os.path.join(output_dir, "bugs", "benchmark_manifest.json")
        assert os.path.exists(manifest_path)

        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        assert manifest["total_instances"] == 6
        assert manifest["type_distribution"]["TYPE_A"] == 1
        assert manifest["type_distribution"]["TYPE_B"] == 1
        assert manifest["type_distribution"]["TYPE_C"] == 1
        assert manifest["type_distribution"]["TYPE_D"] == 1
        assert manifest["type_distribution"]["TYPE_E"] == 1
        assert manifest["type_distribution"]["PASS"] == 1

        # Check organized repo file
        saved_file = os.path.join(output_dir, "bugs", f"{repo_file}.json")
        assert os.path.exists(saved_file)
        with open(saved_file, "r") as f:
            saved_lines = [json.loads(line) for line in f if line.strip()]
        assert len(saved_lines) == 6
        types = {x["commit_hash"]: x["failure_type"] for x in saved_lines}
        assert types["sha_a"] == "TYPE_A"
        assert types["sha_b"] == "TYPE_B"
        assert types["sha_c"] == "TYPE_C"
        assert types["sha_d"] == "TYPE_D"
        assert types["sha_e"] == "TYPE_E"
        assert types["sha_pass"] == "PASS"

    finally:
        shutil.rmtree(temp_dir)


def test_organize_bugs_type_d_exclusivity_and_step4_infra():
    temp_dir = tempfile.mkdtemp(prefix="test_organize_exclusivity_")
    try:
        collect_dir = os.path.join(temp_dir, "collect")
        export_dir = os.path.join(temp_dir, "export")
        filter_dir = os.path.join(temp_dir, "filter")
        output_dir = os.path.join(temp_dir, "output")

        os.makedirs(collect_dir)
        os.makedirs(export_dir)
        os.makedirs(filter_dir)

        repo = "acme/exclusivity"
        repo_file = "acme-exclusivity"

        commits = [
            # Pure FLAKY -> kept as TYPE_D
            {"repository": repo, "commit_hash": "sha_flaky_pure", "strategy": "PASS_PASS", "change_type": "SOURCE_ONLY", "bug_patch": "diff --git a/a.py b/a.py"},
            # Mixed FLAKY with dependency_patch -> dropped by TYPE_D mutual exclusivity
            {"repository": repo, "commit_hash": "sha_flaky_dep", "strategy": "PASS_PASS", "change_type": "MIXED", "dependency_patch": "diff --git a/pom.xml b/pom.xml"},
            # FAIL with Step 4 infra_failure -> mapped to TYPE_D
            {"repository": repo, "commit_hash": "sha_step4_infra", "strategy": "PASS_PASS", "change_type": "SOURCE_ONLY", "bug_patch": "diff --git a/b.py b/b.py"},
            # PASS on non-code & dependency changes -> kept as PASS
            {"repository": repo, "commit_hash": "sha_pass_noncode", "strategy": "NORMAL_PASS", "change_type": "NON_CODE_ONLY", "non_code_patch": "diff --git a/README.md b/README.md"},
        ]

        with open(os.path.join(collect_dir, f"{repo_file}.json"), "w") as f:
            for c in commits:
                f.write(json.dumps(c) + "\n")

        with open(os.path.join(filter_dir, "flaky.json"), "w") as f:
            f.write(json.dumps({"repository": repo, "commit": "sha_flaky_pure"}) + "\n")
            f.write(json.dumps({"repository": repo, "commit": "sha_flaky_dep"}) + "\n")

        with open(os.path.join(filter_dir, "fail.json"), "w") as f:
            f.write(json.dumps({"repository": repo, "commit": "sha_step4_infra", "infra_failure": True}) + "\n")

        with open(os.path.join(filter_dir, "non-flaky.json"), "w") as f:
            f.write(json.dumps({"repository": repo, "commit": "sha_pass_noncode"}) + "\n")

        organize_bugs(collect_dir, export_dir, filter_dir, output_dir)

        with open(os.path.join(output_dir, "bugs", f"{repo_file}.json"), "r") as f:
            saved = {x["commit_hash"]: x for x in (json.loads(line) for line in f if line.strip())}

        assert "sha_flaky_pure" in saved and saved["sha_flaky_pure"]["failure_type"] == "TYPE_D"
        assert "sha_flaky_dep" not in saved
        assert "sha_step4_infra" in saved and saved["sha_step4_infra"]["failure_type"] == "TYPE_D"
        assert "sha_pass_noncode" in saved and saved["sha_pass_noncode"]["failure_type"] == "PASS"

    finally:
        shutil.rmtree(temp_dir)


def test_bug_patch_incoming_diff_roundtrip(monkeypatch):
    mock_repo = MagicMock()
    mock_repo.full_name = "acme/demo"
    mock_repo.language = "python"
    mock_repo.clone_url = "https://github.com/acme/demo.git"
    mock_github = MagicMock()
    mock_github.get_repo.return_value = mock_repo
    monkeypatch.setattr("gitbugactions.collect_bugs.bug_patch.GithubAPI", lambda: mock_github)

    def make_commit(sha, msg, ts):
        c = MagicMock()
        c.id = sha
        c.message = msg
        c.commit_time = ts
        return c

    mock_repo_clone = MagicMock()
    mock_repo_clone.revparse_single.side_effect = lambda h: (
        make_commit("2222222222222222222222222222222222222222", "Fix bug", 1700000300)
        if h.startswith("2222")
        else make_commit("1111111111111111111111111111111111111111", "Introduce bug", 1700000000)
    )

    raw_data = {
        "repository": "acme/demo",
        "language": "python",
        "clone_url": "https://github.com/acme/demo.git",
        "collection_timestamp": "2026-11-14T00:00:00Z",
        "commit_hash": "2222222222222222222222222222222222222222",
        "commit_message": "Fix bug",
        "commit_timestamp": "2026-11-14T00:05:00Z",
        "previous_commit_hash": "1111111111111111111111111111111111111111",
        "previous_parent_hash": "0000000000000000000000000000000000000000",
        "previous_commit_message": "Introduce bug",
        "previous_commit_timestamp": "2026-11-14T00:00:00Z",
        "time_to_patch": "0:05:00",
        "incoming_diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 0\n",
        "bug_patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n",
        "bug_patch_file_extensions": ["py"],
        "test_patch": "",
        "test_patch_file_extensions": [],
        "dependency_patch": "",
        "dependency_patch_file_extensions": [],
        "workflow_patch": "",
        "workflow_patch_file_extensions": [],
        "non_code_patch": "",
        "non_code_patch_file_extensions": [],
        "change_type": "SOURCE_ONLY",
        "strategy": "FAIL_PASS",
        "actions_runs": [],
    }
    bp = BugPatch.from_dict(raw_data, mock_repo_clone)
    assert bp.previous_parent_hash == "0000000000000000000000000000000000000000"
    assert "-x = 1" in bp.incoming_diff
    assert bp.strategy_used == "FAIL_PASS"

    serialized = bp.get_data()
    assert serialized["previous_parent_hash"] == "0000000000000000000000000000000000000000"
    assert serialized["incoming_diff"] == raw_data["incoming_diff"]
