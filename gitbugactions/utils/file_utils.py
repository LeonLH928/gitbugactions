import os
from enum import Enum
from typing import List

from unidiff import PatchSet
import re


class FileType(Enum):
    SOURCE = 0
    TESTS = 1
    NON_SOURCE = 2
    DEPENDENCY = 3
    WORKFLOW = 4


def get_file_extension(file_path: str) -> str:
    return file_path.split(".")[-1] if "." in file_path else file_path.split(os.sep)[-1]


def get_patch_file_extensions(patch: PatchSet) -> List[str]:
    return list(
        {get_file_extension(p.source_file) for p in patch}.union(
            {get_file_extension(p.target_file) for p in patch}
        )
    )


def get_file_type(language: str, file_path: str) -> FileType:
    normalized_path = file_path.replace(os.sep, "/")
    if (
        ".github/workflows" in normalized_path
        or ".github/actions" in normalized_path
        or normalized_path.endswith(("/action.yml", "/action.yaml", "action.yml", "action.yaml"))
    ):
        return FileType.WORKFLOW

    dependency_filenames = {
        "pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties", "settings.gradle",
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
        "requirements.txt", "requirements-dev.txt", "setup.cfg", "pyproject.toml",
        "pipfile", "pipfile.lock", "poetry.lock", "environment.yml",
        "go.mod", "go.sum",
        "cargo.toml", "cargo.lock",
        "packages.config", "directory.build.props",
        "cmakelists.txt", "conanfile.txt", "vcpkg.json"
    }
    file_name = file_path.split(os.sep)[-1].lower()
    if (
        file_name in dependency_filenames
        or file_name == "setup.py"
        or "/requirements/" in f"/{normalized_path.lower()}"
        or (file_name.startswith("requirements") and file_name.endswith((".txt", ".in")))
        or any(file_name.endswith(ext) for ext in [".csproj", ".fsproj", ".sln"])
    ):
        return FileType.DEPENDENCY

    language_extensions = {
        "java": {"java"},
        "python": {"py"},
        "go": {"go"},
        "javascript": {"js", "cjs", "mjs", "jsx"},
        "rust": {"rs"},
        "typescript": {"ts", "tsx"},
        "c#": {"cs"},
        "c++": {"cpp", "cc", "cxx", "hpp", "hh", "hxx", "c", "h"},
        "c": {"cpp", "cc", "cxx", "hpp", "hh", "hxx", "c", "h"},
    }
    test_keywords = {
        "test",
        "tests",
        "__tests__",
        "Test",
        "Tests",
        "unittest",
        "unittests",
    }

    if language in [
        "java",
        "python",
        "javascript",
        "rust",
        "typescript",
        "c#",
        "c++",
        "c",
    ]:
        if any([keyword in file_path.split(os.sep) for keyword in test_keywords]):
            return FileType.TESTS
    if language in ["go"]:
        if "_test.go" in file_path:
            return FileType.TESTS
    if language in ["javascript"]:
        if ".test.js" in file_path:
            return FileType.TESTS
    if language in ["typescript"]:
        if ".test.ts" in file_path:
            return FileType.TESTS
    if language in ["c", "c++"] and file_path != "/dev/null":
        cpp_test_pattern = re.compile(r"(\.test|_test)\.(cpp|c|cc|cxx|hpp|hxx)$")
        if cpp_test_pattern.search(file_path):
            return FileType.TESTS

    if get_file_extension(file_path) in language_extensions[language]:
        return FileType.SOURCE
    else:
        return FileType.NON_SOURCE
