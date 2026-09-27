import math
import re

import pytest

from ticloud.eval import cli


@pytest.mark.parametrize("boundary", [0, 1])
def test_task1_cli_accepts_min_score_boundaries_without_touching_db(monkeypatch, boundary):
    calls = []

    def fake_init_db():
        calls.append("init_db")

    def fake_run_cases(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "init_db", fake_init_db)
    monkeypatch.setattr(cli, "run_cases", fake_run_cases)

    assert cli.main(["run", "--min-score", str(boundary)]) == 0

    assert calls == [
        "init_db",
        {
            "job_id": None,
            "min_score_override": float(boundary),
            "json_output": False,
            "summary_file": None,
        },
    ]


@pytest.mark.parametrize("raw", ["-0.000001", "1.000001", "nan", "not-a-number"])
def test_task1_cli_rejects_bad_min_score_before_db_init(monkeypatch, raw, capsys):
    monkeypatch.setattr(cli, "init_db", lambda: pytest.fail("init_db must not run for invalid CLI input"))
    monkeypatch.setattr(cli, "run_cases", lambda **_: pytest.fail("run_cases must not run for invalid CLI input"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--min-score", raw])

    assert exc.value.code == 2
    assert "argument --min-score" in capsys.readouterr().err


@pytest.mark.parametrize("override", [-0.000001, 1.000001, math.nan])
def test_task1_run_cases_rejects_bad_direct_override_before_session(monkeypatch, override):
    monkeypatch.setattr(cli, "get_session", lambda: pytest.fail("get_session must not run for invalid override"))

    with pytest.raises(ValueError, match="min_score_override must be between 0 and 1"):
        cli.run_cases(min_score_override=override)


def test_task1_composite_action_uses_env_for_inputs_inside_run_blocks():
    action = (cli.Path(__file__).resolve().parents[2] / "action.yml").read_text()
    run_blocks = re.findall(r"(?ms)^\s+run: \|\n((?:\s{8}.*\n?)+)", action)

    assert run_blocks, "expected composite action to contain shell run blocks"
    for block in run_blocks:
        assert "${{ inputs." not in block

    assert 'git+https://github.com/x812033727/saas-2.git@${TICLOUD_ACTION_REF}#subdirectory=platform' in action
    assert '[ -n "$TICLOUD_ACTION_MIN_SCORE" ] && args+=(--min-score "$TICLOUD_ACTION_MIN_SCORE")' in action
    assert '[ -n "$TICLOUD_ACTION_JOB" ] && args+=(--job "$TICLOUD_ACTION_JOB")' in action
