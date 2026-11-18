import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List
import fire


def has_infra_failure(bug_info: Dict) -> bool:
    """True if any recorded run hit a timeout (124) or OOM kill (exit 137)."""
    for runs in bug_info.get("actions_runs") or []:
        for run in runs or []:
            if run.get("return_code") == 124 or "exitcode '137'" in (
                (run.get("stdout") or "") + (run.get("stderr") or "")
            ):
                return True
    return False


def determine_failure_type(
    filter_status: str, strategy: str, infra_failure: bool = False
) -> str | None:
    """
    Map filter execution status and collection strategy to the 5-type failure taxonomy + PASS:
    - PASS: Normal passing commit (no build or test failures)
    - TYPE_A: Semantic code bug / regression defect
    - TYPE_B: Dependency & external interference issue
    - TYPE_C: Pipeline & build configuration error
    - TYPE_D: Environmental, non-reproducible or flaky test failure
    - TYPE_E: Pre-existing / concurrent codebase breakage
    Returns None for a filter FAIL without infra evidence (unsound, e.g. a
    test_patch that does not compile on the previous commit): not a Type D.
    """
    if filter_status == "FLAKY":
        return "TYPE_D"
    elif filter_status == "FAIL":
        if strategy == "DEPENDENCY_CONFLICT":
            return "TYPE_B"
        elif strategy == "PIPELINE_CONFIG":
            return "TYPE_C"
        else:
            return "TYPE_D" if infra_failure else None
    else:  # NON-FLAKY
        if strategy == "NORMAL_PASS":
            return "PASS"
        elif strategy == "DEPENDENCY_CONFLICT":
            return "TYPE_B"
        elif strategy == "PIPELINE_CONFIG":
            return "TYPE_C"
        elif strategy == "PRE_EXISTING":
            return "TYPE_E"
        else:
            return "TYPE_A"


def organize_bugs(
    collect_bugs_dir: str,
    export_bugs_dir: str,
    filter_bugs_dir: str,
    output_dir: str,
):
    """
    Organize bugs from collection directories into a structured output directory.

    Args:
        collect_bugs_dir: Directory containing collected bug JSON files
        export_bugs_dir: Directory containing exported bug files
        filter_bugs_dir: Directory containing non-flaky.json, flaky.json, fail.json
        output_dir: Output directory for organized bugs (default: 'data')
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    output_path = Path(output_dir)
    bugs_dir = output_path / "bugs"
    bugs_dir.mkdir(parents=True, exist_ok=True)
    logging.info("=" * 60)
    logging.info("🚀 Bước 5: Tổ chức Taxonomy & Xuất Benchmark Manifest")
    logging.info(f"🚀 Thu thập: {collect_bugs_dir} | Export: {export_bugs_dir} | Filter: {filter_bugs_dir}")
    logging.info(f"🚀 Thư mục đích: {output_path}")
    logging.info("=" * 60)

    # Read all filter files (non-flaky, flaky, fail)
    filter_files = [
        ("non-flaky.json", "NON-FLAKY"),
        ("flaky.json", "FLAKY"),
        ("fail.json", "FAIL"),
    ]

    all_filtered_bugs: List[Dict] = []
    filter_path = Path(filter_bugs_dir)
    for filename, default_status in filter_files:
        filepath = filter_path / filename
        if filepath.exists():
            with open(filepath, "r") as f:
                for line in f:
                    if line.strip():
                        record = json.loads(line)
                        if "status" not in record:
                            record["status"] = default_status
                        all_filtered_bugs.append(record)
            logging.info(f"Loaded records from {filename}")

    logging.info(f"Total filtered commits loaded: {len(all_filtered_bugs)}")

    # Group filtered bugs by repository
    bugs_by_repo: Dict[str, Dict[str, str]] = {}
    infra_by_repo: Dict[str, Dict[str, bool]] = {}
    for item in all_filtered_bugs:
        repo = item["repository"]
        commit = item["commit"]
        status = item.get("status", "NON-FLAKY")
        if repo not in bugs_by_repo:
            bugs_by_repo[repo] = {}
            infra_by_repo[repo] = {}
        bugs_by_repo[repo][commit] = status
        infra_by_repo[repo][commit] = bool(item.get("infra_failure", False))

    manifest = {
        "total_instances": 0,
        "type_distribution": {
            "PASS": 0,
            "TYPE_A": 0,
            "TYPE_B": 0,
            "TYPE_C": 0,
            "TYPE_D": 0,
            "TYPE_E": 0,
        },
        "repositories": {},
    }

    # Process each repository
    for repo, commit_to_status in bugs_by_repo.items():
        logging.info(f"Processing repository: {repo}")
        repo_file = repo.replace("/", "-").lower()

        collect_file = Path(collect_bugs_dir) / f"{repo_file}.json"
        if not collect_file.exists():
            original_repo_file = repo.replace("/", "-")
            collect_file = Path(collect_bugs_dir) / f"{original_repo_file}.json"
            if not collect_file.exists():
                logging.warning(f"Collection file not found for {repo}")
                continue

        collected_bugs = []
        with open(collect_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                bug_info = json.loads(line)
                commit_hash = bug_info["commit_hash"]
                if commit_hash not in commit_to_status:
                    continue

                filter_status = commit_to_status[commit_hash]
                strategy = bug_info.get("strategy", "UNKNOWN")
                infra_failed = has_infra_failure(bug_info) or infra_by_repo.get(
                    repo, {}
                ).get(commit_hash, False)
                failure_type = determine_failure_type(
                    filter_status, strategy, infra_failed
                )
                if failure_type is None:
                    logging.info(
                        f"Skipping unsound {repo}@{commit_hash[:8]} ({strategy}): filter FAIL without infra evidence"
                    )
                    continue

                if failure_type == "TYPE_D" and (
                    len(bug_info.get("dependency_patch", "")) > 0
                    or len(bug_info.get("workflow_patch", "")) > 0
                ):
                    logging.info(
                        f"Skipping mixed TYPE_D {repo}@{commit_hash[:8]}: dependency/workflow patch present"
                    )
                    continue

                change_type = bug_info.get("change_type", "")
                if change_type in ("NON_CODE", "NON_CODE_ONLY") and failure_type not in (
                    "TYPE_B",
                    "TYPE_C",
                    "TYPE_D",
                    "TYPE_E",
                    "PASS",
                ):
                    continue

                bug_info["failure_type"] = failure_type
                bug_info["filter_status"] = filter_status
                collected_bugs.append(bug_info)

                manifest["total_instances"] += 1
                manifest["type_distribution"][failure_type] = (
                    manifest["type_distribution"].get(failure_type, 0) + 1
                )
                manifest["repositories"][repo] = (
                    manifest["repositories"].get(repo, 0) + 1
                )

        if collected_bugs:
            output_file = bugs_dir / f"{repo_file}.json"
            with open(output_file, "w") as f:
                for bug in collected_bugs:
                    f.write(json.dumps(bug) + "\n")
            logging.info(f"Saved {len(collected_bugs)} bugs for {repo}")

            # Copy export directories if present
            export_path = Path(export_bugs_dir)
            if export_path.exists():
                for bug in collected_bugs:
                    commit = bug["commit_hash"]
                    original_repo_file = repo.replace("/", "-")
                    src_dir = export_path / original_repo_file / commit
                    if not src_dir.exists():
                        src_dir = export_path / repo_file / commit
                        if not src_dir.exists():
                            logging.warning(
                                f"Export directory not found for commit {commit}"
                            )
                            continue

                    dst_dir = output_path / repo_file / commit.lower()
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                    logging.info(f"Copied export directory for commit {commit}")

    # Write benchmark manifest
    manifest_file = bugs_dir / "benchmark_manifest.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2)

    logging.info("=" * 60)
    logging.info(f"🎉 Bước 5 hoàn thành! Benchmark Manifest đã được tạo tại:")
    logging.info(f"   ↳ {manifest_file}")
    logging.info(f"🎉 Tổng số instances thu thập được: {manifest['total_instances']}")
    logging.info("🎉 Phân bố theo Taxonomy:")
    for t_name, count in manifest["type_distribution"].items():
        logging.info(f"   ↳ {t_name:10s}: {count} instances")
    logging.info("=" * 60)


if __name__ == "__main__":
    fire.Fire(organize_bugs)
