from organize_bugs import determine_failure_type, has_infra_failure


def test_unsound_fail_is_not_type_d():
    # e.g. test_patch does not compile on previous commit
    assert determine_failure_type("FAIL", "FAIL_PASS_BUILD") is None


def test_infra_fail_is_type_d():
    assert determine_failure_type("FAIL", "FAIL_PASS", infra_failure=True) == "TYPE_D"


def test_flaky_is_type_d():
    assert determine_failure_type("FLAKY", "FAIL_PASS") == "TYPE_D"


def test_build_strategies_keep_their_type():
    assert determine_failure_type("FAIL", "DEPENDENCY_CONFLICT") == "TYPE_B"
    assert determine_failure_type("FAIL", "PIPELINE_CONFIG") == "TYPE_C"


def test_has_infra_failure():
    assert has_infra_failure({"actions_runs": [[{"return_code": 124}]]})
    assert has_infra_failure({"actions_runs": [[{"return_code": 1, "stderr": "exitcode '137'"}]]})
    assert not has_infra_failure({"actions_runs": [[{"return_code": 1, "stdout": "", "stderr": ""}], None]})
