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
        # Java / JVM
        "pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties", "settings.gradle",
        "settings.gradle.kts", "gradle-wrapper.properties", "libs.versions.toml", "ivy.xml", "build.xml",
        # JS / TS
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml",
        "bun.lockb", "bun.lock", "npm-shrinkwrap.json", ".nvmrc", ".node-version", ".npmrc", ".yarnrc.yml",
        # Python
        "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "pyproject.toml",
        "pipfile", "pipfile.lock", "poetry.lock", "uv.lock", "pdm.lock", "environment.yml",
        "conda.yml", "conda.yaml", "constraints.txt", ".python-version", "tox.ini",
        # Go
        "go.mod", "go.sum", "go.work", "go.work.sum",
        # Rust
        "cargo.toml", "cargo.lock", "rust-toolchain", "rust-toolchain.toml",
        # C# / .NET
        "packages.config", "packages.lock.json", "directory.build.props", "directory.build.targets",
        "directory.packages.props", "global.json", "nuget.config",
        # C / C++
        "cmakelists.txt", "cmakepresets.json", "cmakeuserpresets.json", "conanfile.txt", "conanfile.py",
        "conan.lock", "vcpkg.json", "vcpkg-configuration.json", "meson.build", "meson_options.txt",
        "makefile", "gnumakefile", "configure.ac", "build.bazel", "workspace", "workspace.bazel", "module.bazel",
    }
    raw_file_name = normalized_path.split("/")[-1]
    file_name = raw_file_name.lower()
    if (
        file_name in dependency_filenames
        or "/requirements/" in f"/{normalized_path.lower()}"
        or ("requirements" in file_name and file_name.endswith((".txt", ".in")))
        or file_name.endswith((".csproj", ".fsproj", ".vbproj", ".sln", ".props", ".targets"))
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
    test_dir_keywords = {
        "test",
        "tests",
        "__tests__",
        "unittest",
        "unittests",
        "unit_tests",
        "spec",
        "specs",
        "__specs__",
        "__mocks__",
        "testing",
        "e2e",
        "integration_tests",
        "integration-tests",
        "functional_tests",
    }

    dir_segments = [seg.lower() for seg in normalized_path.split("/")[:-1] if seg]
    if language in [
        "java",
        "python",
        "javascript",
        "rust",
        "typescript",
        "c#",
        "c++",
        "c",
        "go",
    ]:
        if any(
            seg in test_dir_keywords
            or seg.endswith((".tests", ".unittests", ".integrationtests"))
            for seg in dir_segments
        ):
            return FileType.TESTS

    if language == "python":
        if file_name == "conftest.py" or (
            file_name.endswith(".py")
            and (file_name.startswith("test_") or file_name.endswith(("_test.py", "_tests.py")))
        ):
            return FileType.TESTS
    elif language == "java":
        if raw_file_name.endswith(".java") and re.match(
            r"^(Test.*|.*(Test|Tests|TestCase)|IT.*|.*(IT|ITCase))\.java$",
            raw_file_name,
        ):
            return FileType.TESTS
    elif language == "go":
        if file_name.endswith("_test.go"):
            return FileType.TESTS
    elif language in ["javascript", "typescript"]:
        if re.search(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$", file_name):
            return FileType.TESTS
    elif language == "rust":
        if file_name.endswith(("_test.rs", "_tests.rs")):
            return FileType.TESTS
    elif language == "c#":
        if raw_file_name.endswith(("Test.cs", "Tests.cs")):
            return FileType.TESTS
    elif language in ["c", "c++"] and file_path != "/dev/null":
        if re.search(
            r"(^test_.*|.*(\.test|_test|_tests|Test|Tests))\.(cpp|c|cc|cxx|hpp|hh|hxx|h)$",
            raw_file_name,
        ):
            return FileType.TESTS

    if get_file_extension(file_path) in language_extensions.get(language, set()):
        return FileType.SOURCE
    else:
        return FileType.NON_SOURCE
