from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "mirror.yml"


def _step_block(workflow: str, marker: str) -> str:
    return workflow.split(marker, 1)[1].split("\n      - ", 1)[0]


def test_dependency_cache_is_only_enabled_for_changed_catalogs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    setup_python = _step_block(workflow, "- uses: actions/setup-python")
    assert "cache: pip" not in setup_python

    dependency_cache = _step_block(workflow, "- name: Restore mirror dependency cache")
    assert "if: steps.mirror-check.outputs.changed == 'true'" in dependency_cache
    assert "uses: actions/cache@" in dependency_cache
    assert "path: ~/.cache/pip" in dependency_cache
    assert "hashFiles('requirements.txt')" in dependency_cache

    dependency_install = _step_block(workflow, "- name: Install mirror dependencies")
    assert 'mkdir -p "$(pip cache dir)"' in dependency_install


def test_workflows_do_not_use_node20_action_releases() -> None:
    deprecated_refs = {
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/upload-artifact@v4",
        "actions/upload-pages-artifact@v3",
        "actions/deploy-pages@v4",
    }
    workflow_dir = WORKFLOW.parent
    offenders = {
        path.name: sorted(ref for ref in deprecated_refs if ref in path.read_text(encoding="utf-8"))
        for path in workflow_dir.glob("*.yml")
    }

    assert not {name: refs for name, refs in offenders.items() if refs}
