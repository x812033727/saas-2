import subprocess


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def changed_files() -> list[str]:
    return [
        line.strip()
        for line in git("diff", "--name-only", "HEAD").splitlines()
        if line.strip() and line.strip() != ".gitignore"
    ]


def test_task1_changes_exactly_one_allowed_document_file():
    files = changed_files()
    assert len(files) == 1, f"expected exactly one changed file excluding .gitignore, got {files}"

    changed = files[0]
    assert changed == "README.md" or changed.endswith(".py"), (
        "changed file must be README.md or a Python file containing a docstring, "
        f"got {changed}"
    )


def test_task1_has_no_python_logic_diff():
    diff = git("diff", "--", "*.py")
    if not diff:
        return

    changed = changed_files()
    assert len(changed) == 1 and changed[0].endswith(".py")
    added_or_removed = [
        line
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert added_or_removed, "expected a visible docstring-only Python diff"

    allowed_text_markers = ('"""', "'''", "#")
    assert all(
        line[1:].lstrip().startswith(allowed_text_markers) or not line[1:].strip()
        for line in added_or_removed
    ), f"Python diff appears to include non-documentation logic lines: {added_or_removed}"


def test_task1_diff_is_small():
    numstat = git("diff", "--numstat", "HEAD").splitlines()
    relevant = [line for line in numstat if not line.endswith("\t.gitignore")]
    total_changed_lines = 0
    for line in relevant:
        added, deleted, _path = line.split("\t", 2)
        total_changed_lines += int(added) + int(deleted)

    assert total_changed_lines > 0, "expected one small documentation change, got no diff"
    assert total_changed_lines <= 6, f"expected a single small change, got {total_changed_lines} changed lines"
