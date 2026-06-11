import subprocess

from mac.project_inception import C26_PROJECT_NAME, run_c26_project_inception_proof
from mac.services import ControlPlane


def test_c26_project_inception_proof_covers_epic_review_parallel_slack_and_demo(tmp_path):
    cp = ControlPlane.in_memory()
    repo, _head = _seed_git_repo(tmp_path)

    proof = run_c26_project_inception_proof(cp, project_path=str(repo))

    assert proof["schema"] == "mac.project_inception_proof.v1"
    assert proof["ready"] is True
    assert all(proof["checks"].values())
    assert proof["project"]["name"] == C26_PROJECT_NAME
    assert proof["task_count"] == proof["completed_task_count"]
    assert proof["task_count"] >= 14
    assert len({item["agent_id"] for item in proof["parallel_fanout"]}) >= 3
    assert proof["slack"]["delivery"]["delivered"] > 0
    assert proof["slack"]["message_count"] > 0
    assert proof["demo_request"]["build_commands"] == [
        "cd %s" % repo,
        "make smoke",
        "make run",
    ]

    project = cp.get_project(C26_PROJECT_NAME)
    tasks = {task["id"]: task for task in project["tasks"]}
    assert proof["epic_task_id"] in tasks
    assert proof["plan_task_id"] in tasks
    assert proof["review_task_id"] in tasks
    assert proof["revised_plan_task_id"] in tasks
    assert all(task["state"] == "completed" for task in project["tasks"])

    demo_task = tasks[proof["implementation_task_ids"]["demo_story"]]
    assert "Slack" in demo_task["description"]
    assert "make smoke" in demo_task["description"]
    assert "feedback" in demo_task["description"]
    assert demo_task["metadata"]["execution_contract"]["required_changed_files"] == [
        "README.md",
        "docs/demo-story.md",
    ]


def test_c26_project_inception_proof_records_actual_repository_state(tmp_path):
    repo, head = _seed_git_repo(tmp_path)

    proof = run_c26_project_inception_proof(ControlPlane.in_memory(), project_path=str(repo))

    state = proof["project"]["repository_state"]
    assert state["available"] is True
    assert state["head_sha"] == head
    assert state["dirty"] is False
    assert state["tracked_file_count"] == 1


def _seed_git_repo(tmp_path):
    repo = tmp_path / "c26"
    repo.mkdir()
    (repo / "README.md").write_text("# c26\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "mac-tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "mac tests"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed c26"], cwd=repo, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, head
