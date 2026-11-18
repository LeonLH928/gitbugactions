import re
from pathlib import Path
from typing import List

from junitparser import TestCase

from gitbugactions.actions.multi.junitxmlparser import JUnitXMLParser
from gitbugactions.actions.workflow import GitHubWorkflow


class PytestWorkflow(GitHubWorkflow):
    BUILD_TOOL_KEYWORDS = {"pytest", "py.test"}
    # Regex patterns to match pytest commands
    __TESTS_COMMAND_PATTERNS = [
        r"\bpytest\b",
        r"\bpy\.test\b",
        r"python([23](\.\d+)?)?\s+(([^\s]+\s+)*)?-m\s+pytest\b",  # Matches commands that call pytest through python's module option
    ]

    def _is_test_command(self, command) -> bool:
        # Checks if the given command matches any of the tests command patterns
        for pattern in PytestWorkflow.__TESTS_COMMAND_PATTERNS:
            if re.search(pattern, command):
                return True
        return False

    def instrument_test_steps(self, **kwargs):
        if "jobs" in self.doc:
            for _, job in self.doc["jobs"].items():
                if "steps" in job:
                    for step in job["steps"]:
                        if "run" in step and self._is_test_command(step["run"]):
                            if (
                                re.search(r"\bpytest\b", step["run"])
                                and "--junitxml" not in step["run"]
                            ):
                                step["run"] = re.sub(
                                    r"\bpytest\b",
                                    "pytest --junitxml=report.xml",
                                    step["run"],
                                    count=1,
                                )
                            elif (
                                re.search(r"\bpy\.test\b", step["run"])
                                and "--junitxml" not in step["run"]
                            ):
                                step["run"] = re.sub(
                                    r"\bpy\.test\b",
                                    "py.test --junitxml=report.xml",
                                    step["run"],
                                    count=1,
                                )
                            elif (
                                re.search(r"\b(?:py\.test|pytest)\b", step["run"])
                                and "--junitxml" in step["run"]
                            ):
                                step["run"] = re.sub(
                                    r"--junitxml=[^\s]+",
                                    "--junitxml=report.xml",
                                    step["run"],
                                )

    def get_test_results(self, repo_path) -> List[TestCase]:
        parser = JUnitXMLParser()
        return parser.get_test_results(str(Path(repo_path, "report.xml")))

    def get_build_tool(self) -> str:
        return "pytest"
