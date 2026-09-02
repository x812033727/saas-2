from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_eval_gate_workflow_only_runs_eval_cases():
    workflow = (ROOT / ".github/workflows/eval-gate.yml").read_text()

    assert "python -m pytest" not in workflow
    assert "pip install -e ./platform" in workflow
    assert 'python -m ticloud.eval.cli run --json --summary-file "$GITHUB_STEP_SUMMARY"' in workflow


def test_composite_action_emits_json_and_github_summary():
    action = (ROOT / "action.yml").read_text()

    assert '--summary-file "$GITHUB_STEP_SUMMARY"' in action
    assert 'python -m ticloud.eval.cli run "${args[@]}" --json' in action
