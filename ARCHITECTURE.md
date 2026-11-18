# GitBugActions Architecture & Core Logic Guide

This document describes the architectural layout, core pipeline workflows, and key logic files of the **GitBugActions** framework.

---

## 1. System Architecture Overview

GitBugActions extracts reproducible bugs and failure benchmarks from GitHub repositories that use GitHub Actions. It executes tests in local containers using **`nektos/act`** (via Podman/Docker) to simulate GitHub Actions CI environments.

### 5-Step Pipeline Architecture

```mermaid
flowchart TD
    subgraph S1["Step 1: Repository Collection"]
        CR["collect_repos.py"] --> Crawler["gitbugactions/crawler.py"]
        Crawler --> GH["GitHub API (Search Repos)"]
        Crawler --> Clone1["Clone & Check .github/workflows/"]
        Clone1 --> Act1["Act (Execute Workflow on HEAD)"]
        Act1 --> ReposJSON[("data/repos/*.json<br>(Passing CI Repos)")]
    end

    subgraph S2["Step 2: Bug Mining & Classification"]
        CB["collect_bugs.py"] --> ReposJSON
        CB --> Collector["PatchCollector<br>(Git History Traversal)"]
        Collector --> PatchDiff["Extract Diff:<br>bug_patch, test_patch,<br>dep_patch, workflow_patch"]
        PatchDiff --> TE["TestExecutor (Run 3 Act Passes)"]
        TE --> Run0["actions_runs[0]: previous_commit"]
        TE --> Run1["actions_runs[1]: previous + test_patch"]
        TE --> Run2["actions_runs[2]: current_commit"]
        Run0 & Run1 & Run2 --> Strat["Collection Strategies<br>(collection_strategies.py)"]
        Strat --> OutBugs[("data/out_bugs/*.json<br>(Candidate Bug Patches)")]
    end

    subgraph S3["Step 3: Container Diff Export"]
        EB["export_bugs.py"] --> OutBugs
        EB --> ActExp["Run Act on current_commit"]
        ActExp --> DiffExp["Extract Container Layers<br>(gitbugactions/docker/export.py)"]
        DiffExp --> ExportDir[("data/export/&lt;repo&gt;/&lt;commit&gt;/<br>(Diff tarball + workflow YAML)")]
    end

    subgraph S4["Step 4: Reproducibility & Flakiness Filter"]
        FB["filter_bugs.py"] --> OutBugs
        FB --> ExportDir
        FB --> ActLoop["Re-run Act n_executions times<br>(in isolated container with diff applied)"]
        ActLoop --> FlakyCheck{"Compare runs across executions"}
        FlakyCheck -- "Toggles Pass/Fail" --> FlakyJSON[("flaky.json (Type D1)")]
        FlakyCheck -- "Deterministic" --> NonFlakyJSON[("non-flaky.json")]
        FlakyCheck -- "Crashes / Timeout / OOM" --> FailJSON[("fail.json (Type D2 / Unsound)")]
    end

    subgraph S5["Step 5: Taxonomy Organization & Manifest Generation"]
        OB["organize_bugs.py"] --> OutBugs
        OB --> ExportDir
        OB --> NonFlakyJSON & FlakyJSON & FailJSON
        OB --> Classify["determine_failure_type()<br>+ has_infra_failure()"]
        Classify --> Manifest[("data/bugs/benchmark_manifest.json<br>+ data/bugs/*.json")]
    end
```

---

## 2. Execution Engine & Act Abstraction Layer

The execution engine translates repository test commands into local Docker/Podman containers via `nektos/act`.

```mermaid
classDiagram
    class TestExecutor {
        +Repository repo_clone
        +str language
        +str act_cache_dir
        +GitHubActions default_actions
        +run_tests(keep_containers, offline, timeout) List~ActTestsRun~
        +reset_repo()
    }

    class GitHubActions {
        +str repo_path
        +str language
        +List~GitHubWorkflow~ workflows
        +List~GitHubWorkflow~ test_workflows
        +run_workflow(workflow, act_cache_dir) ActTestsRun
        +save_workflows()
        +delete_workflows()
    }

    class GitHubWorkflow {
        <<abstract>>
        +str path
        +dict doc
        +get_test_results(repo_path) List~TestCase~
        +get_build_tool() str
        +_is_test_command(cmd) bool
        +instrument_offline_execution()
    }

    class Act {
        +run_act(workflow, repo_path, act_cache_dir) ActTestsRun
    }

    class ActTestsRun {
        +int return_code
        +str stdout
        +str stderr
        +List~TestCase~ tests
        +List~TestCase~ failed_tests
        +List~TestCase~ erroring_tests
        +bool failed
        +float elapsed_time
    }

    TestExecutor --> GitHubActions : instruments & executes
    GitHubActions --> GitHubWorkflow : parses & manipulates
    GitHubActions --> Act : invokes CLI
    Act --> ActTestsRun : returns test output & JUnit XML
```

---

## 3. Core Files & Where to Check Key Logic

Below is the directory map of the most critical files, what they do, and where to inspect specific logic:

### Root Pipeline Scripts

| File | Primary Responsibility | Where to Check Core Logic |
| :--- | :--- | :--- |
| [`collect_repos.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/collect_repos.py) | **Step 1**: Searches GitHub for repositories, clones them, and runs Act on `HEAD` to verify test workflows pass. | • `CollectReposStrategy.handle_repo()` (clone & language check)<br>• `run_workflow()` (Act execution on candidate repo) |
| [`collect_bugs.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/collect_bugs.py) | **Step 2**: Traverses Git history, isolates patches into types (source, test, dep, workflow), executes 3 Act test phases, and matches collection strategies. | • `PatchCollector.get_possible_patches()` (commit diff separation & commit-message filter)<br>• `PatchCollector.test_patch()` (runs 3 Act phases)<br>• `PatchCollector.check_runs()` (checks matched strategy) |
| [`export_bugs.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/export_bugs.py) | **Step 3**: Builds/runs `current_commit` and extracts changed filesystem layers into a diff tarball. | • `export_bug_containers()` (orchestrates container run)<br>• `create_exported_containers()` (extracts layers & workflow YAML) |
| [`filter_bugs.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/filter_bugs.py) | **Step 4**: Validates reproducibility and detects flaky tests or environment crashes over `n_executions` identical re-runs. | • `filter_bug()` (builds diff image and executes repeated test runs)<br>• `equal_test_results()` (compares test outcomes across runs to flag flakiness) |
| [`organize_bugs.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/organize_bugs.py) | **Step 5**: Consolidates bugs, validates soundness, determines the final failure taxonomy type, and writes `benchmark_manifest.json`. | • `determine_failure_type()` (maps filter status + strategy to `TYPE_A`–`TYPE_E` or `PASS`)<br>• `has_infra_failure()` (verifies exit 124 timeout or exit 137 OOM) |
| [`run_batch.sh`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/run_batch.sh) | Driver shell script executing Steps 1 to 5 sequentially with specified flags, strategies, and cache settings. | Batch pipeline arguments and strategy list. |

---

### Internal Package: `gitbugactions/`

| File / Submodule | Purpose & Logic |
| :--- | :--- |
| [`gitbugactions/collect_bugs/collection_strategies.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/collect_bugs/collection_strategies.py) | **Strategy Taxonomy Engine**: Defines each strategy's criteria.<br>• `PassPassStrategy` & `FailPassStrategy` (`TYPE_A`)<br>• `DependencyConflictStrategy` (`TYPE_B`)<br>• `PipelineConfigStrategy` (`TYPE_C`)<br>• `PreExistingFailureStrategy` (`TYPE_E`)<br>• `NormalPassStrategy` (`PASS`) |
| [`gitbugactions/collect_bugs/bug_patch.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/collect_bugs/bug_patch.py) | **Patch Data Structure & Git State Manager**: Holds patch diffs (`bug_patch`, `test_patch`, `dependency_patch`, `workflow_patch`). Manages pygit2 commit reset, patch application, and property helpers (`prev_commit_failed`, `curr_commit_passed`, etc.). |
| [`gitbugactions/test_executor.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/test_executor.py) | **Local Test Harness**: Coordinates repository state preparation, GitHub Actions workflow instrumentations, container execution via Act, and JUnit XML test report extraction. |
| [`gitbugactions/actions/actions.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/actions/actions.py) | **Act CLI Wrapper & Result Parser**: Manages concurrent act cache directories (`ActCacheDirManager`), spawns the Act CLI, parses test result objects, and categorizes run outcomes (`ActTestsFailureStrategy`). |
| [`gitbugactions/actions/workflow_factory.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/actions/workflow_factory.py) | **Workflow Parser Factory**: Detects workflow language and instantiates language-specific workflow handlers (`MavenWorkflow`, `PytestWorkflow`, `GoWorkflow`, `NpmWorkflow`, etc.). |
| [`gitbugactions/actions/<lang>/`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/actions) | **Language-Specific Handlers**: Contains custom parsers that locate and parse test reports (e.g. `surefire-reports` for Maven, `.junitxml` for pytest, Mocha/Jest reporters). |
| [`gitbugactions/docker/export.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/docker/export.py) | **Container Diff Utilities**: Exports Docker/Podman container filesystem layers and tarballs to persist the exact environment state. |
| [`gitbugactions/utils/file_utils.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/gitbugactions/utils/file_utils.py) | **File Path & Type Classifier**: `get_file_type()` determines whether a file belongs to `SOURCE`, `TESTS`, `DEPENDENCY` (`pom.xml`, `package.json`, `requirements*.txt`, `setup.py`), `WORKFLOW` (`.github/workflows/*`), or `NON_CODE`. |

---

## 4. How Data Flows Across the 3 Test Phases in Step 2

In [`collect_bugs.py`](file:///home/tuanlh/Documents/UIT/sem5/test/research/gitbugactions/collect_bugs.py), each candidate commit undergoes up to three distinct local test phases via Act:

```mermaid
sequenceDiagram
    autonumber
    participant Collector as PatchCollector
    participant Executor as TestExecutor
    participant Act as Act Runner (Container)
    participant Strat as CollectionStrategy

    Note over Collector: Commit split into bug_patch, test_patch, dep_patch, workflow_patch

    Collector->>Executor: test_previous_commit()
    Executor->>Act: Run test on previous_commit (HEAD~1)
    Act-->>Executor: actions_runs[0] (Result & JUnit XML)

    alt Has test_patch (len > 0)
        Collector->>Executor: test_previous_commit_with_diff()
        Executor->>Act: Run test on previous_commit + test_patch
        Act-->>Executor: actions_runs[1] (Result & JUnit XML)
    else No test_patch
        Note over Collector: actions_runs[1] = None
    end

    Collector->>Executor: test_current_commit()
    Executor->>Act: Run test on current_commit (HEAD)
    Act-->>Executor: actions_runs[2] (Result & JUnit XML)

    Collector->>Strat: check_runs(bug_patch)
    Note over Strat: Evaluates strategy rules against actions_runs[0..2]
    Strat-->>Collector: Matched Strategy Name (or None)
```

1. **`actions_runs[0]` (`previous_commit`)**: Checks baseline behavior before the commit.
2. **`actions_runs[1]` (`previous_commit + test_patch`)**: Exposes whether new or modified tests fail when running against old source code (used to catch semantic bugs in `PASS_PASS` / `TYPE_A` and dependency breaks in `TYPE_B`).
3. **`actions_runs[2]` (`current_commit`)**: Confirms whether the current commit successfully passes and resolves the issue.
