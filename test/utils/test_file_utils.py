import pytest
from gitbugactions.utils.file_utils import get_file_type, FileType


@pytest.mark.parametrize(
    "language, file_path, expected",
    [
        # Test for source files
        ("python", "src/main.py", FileType.SOURCE),
        ("java", "src/Main.java", FileType.SOURCE),
        ("c++", "src/main.cpp", FileType.SOURCE),
        ("c", "src/main.c", FileType.SOURCE),
        ("typescript", "src/app.ts", FileType.SOURCE),
        ("go", "src/main.go", FileType.SOURCE),
        # Test for test files
        ("python", "tests/test_main.py", FileType.TESTS),
        ("python", "src/__tests__/test_main.py", FileType.TESTS),
        ("java", "tests/TestMain.java", FileType.TESTS),
        ("c++", "tests/main_test.cpp", FileType.TESTS),
        ("c++", "src/main.test.cpp", FileType.TESTS),
        ("c", "src/main_test.c", FileType.TESTS),
        ("go", "src/main_test.go", FileType.TESTS),
        ("javascript", "src/app.test.js", FileType.TESTS),
        ("typescript", "src/app.test.ts", FileType.TESTS),
        ("python", "src/test_main.py", FileType.TESTS),
        ("python", "src/conftest.py", FileType.TESTS),
        ("java", "src/MainTest.java", FileType.TESTS),
        ("typescript", "src/app.spec.tsx", FileType.TESTS),
        ("c#", "src/UserServiceTests.cs", FileType.TESTS),
        ("c++", "src/test_math.cpp", FileType.TESTS),
        ("rust", "src/lib_test.rs", FileType.TESTS),
        # Test for non-source files
        ("python", "docs/readme.txt", FileType.NON_SOURCE),
        ("java", "assets/image.png", FileType.NON_SOURCE),
        ("c++", "build/output.o", FileType.NON_SOURCE),
        ("c++", "/dev/null", FileType.NON_SOURCE),
        # Test for dependency files
        ("python", "requirements.txt", FileType.DEPENDENCY),
        ("python", "pyproject.toml", FileType.DEPENDENCY),
        ("python", "uv.lock", FileType.DEPENDENCY),
        ("java", "pom.xml", FileType.DEPENDENCY),
        ("java", "gradle/libs.versions.toml", FileType.DEPENDENCY),
        ("javascript", "package.json", FileType.DEPENDENCY),
        ("typescript", "bun.lock", FileType.DEPENDENCY),
        ("rust", "Cargo.toml", FileType.DEPENDENCY),
        ("go", "go.mod", FileType.DEPENDENCY),
        ("c#", "Directory.Packages.props", FileType.DEPENDENCY),
        ("c++", "conanfile.py", FileType.DEPENDENCY),
        ("c++", "vcpkg-configuration.json", FileType.DEPENDENCY),
        # Test for workflow files
        ("python", ".github/workflows/ci.yml", FileType.WORKFLOW),
        ("java", ".github/workflows/build.yaml", FileType.WORKFLOW),
        ("javascript", "action.yml", FileType.WORKFLOW),
    ],
)
def test_get_file_type(language, file_path, expected):
    assert get_file_type(language, file_path) == expected
