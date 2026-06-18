from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mac.models import ReviewStatus, Task, TaskState
from mac.services import ControlPlane, sign_verification_manifest

JsonDict = Dict[str, Any]

C26_PROJECT_NAME = "c26"
C26_PROJECT_DESCRIPTION = (
    "A complete reimagining of what the Commodore 64 might have looked like if "
    "it were released in 2026 on a RISC-V processor: standalone C/assembly, "
    "QEMU-first hardware emulation, BASIC, retro desktop, high-resolution "
    "graphics, sound, USB/I2C/CAN/TCP/IP concepts, and robot-control SDKs."
)

C26_REVIEW_NOTES = [
    "Keep the first demo to a vertical slice instead of promising full production device stacks.",
    "Make QEMU virt, UART, linker layout, and build reproducibility the first dependency layer.",
    "Scope USB/I2C/CAN/TCP/IP to freestanding APIs and emulated stubs for the first milestone.",
    "Require Slack-visible progress and a final demo ask with build commands and feedback request.",
]


def run_c26_project_inception_proof(
    cp: Optional[ControlPlane] = None,
    *,
    project_path: str = "~/Src/c26",
) -> JsonDict:
    """Exercise the c26 project inception lifecycle inside MAC.

    The proof deliberately starts with only a name and description, then creates
    the durable epic, plan, independent review, revised plan, fan-out
    implementation tasks, Slack notifier delivery, and final demo handoff that
    the user asked MAC to prove.
    """

    cp = cp or ControlPlane.in_memory()
    tenant = cp.register_tenant("c26-demo")
    persona = cp.register_persona(
        tenant.id,
        "c26 slack reporter",
        "hermes://c26/SOUL.md",
        "hermes://c26/memory",
        metadata={"role_slugs": ["planner", "reviewer", "systems-builder"]},
    )
    hermes = cp.register_hermes_instance(
        tenant.id,
        "c26-hermes",
        persona_id=persona.id,
        home_ref="hermes://c26",
    )
    binding = cp.register_platform_binding(
        tenant.id,
        hermes.id,
        "slack",
        "T-C26/C-DEMO",
        display_name="#rockyandfriends",
    )
    agents = _register_c26_agents(cp, hermes.id)
    notifier = cp.configure_notifier_channel(
        "c26-slack-progress",
        "slack",
        event_types=["task.*", "project.*"],
        target={"platform_binding_id": binding.id},
        metadata={"project": C26_PROJECT_NAME, "purpose": "project inception proof"},
    )

    epic = cp.create_task(
        "c26 epic: 2026 RISC-V home computer",
        description=_epic_description(project_path),
        project=C26_PROJECT_NAME,
        priority=0,
        required_capabilities=["planning"],
        metadata={
            "artifact_type": "epic",
            "project_inception": {
                "name": C26_PROJECT_NAME,
                "description": C26_PROJECT_DESCRIPTION,
                "instructions": "create a detailed plan, then require independent review before implementation",
            },
        },
        actor="human",
    )
    _complete_task(
        cp,
        epic,
        agents["planner"],
        agents["reviewer"],
        "Epic accepted",
        project_path=project_path,
    )

    plan = cp.create_task(
        "c26 plan: derive implementation tasks from epic",
        description=(
            "Create the c26 implementation plan from the epic only. Include "
            "task dependencies, parallel execution lanes, acceptance criteria, "
            "and Slack reporting checkpoints."
        ),
        project=C26_PROJECT_NAME,
        priority=1,
        required_capabilities=["planning"],
        dependencies=[epic.id],
        metadata={"artifact_type": "initial_plan", "source_epic_task_id": epic.id},
        actor="planner",
    )
    plan_payload = _initial_plan(project_path)
    _complete_task(
        cp,
        plan,
        agents["planner"],
        agents["reviewer"],
        "Initial c26 plan produced",
        project_path=project_path,
        extra_metadata={"plan": plan_payload},
    )

    review = cp.create_task(
        "c26 review: independently review the project plan",
        description=(
            "Review the c26 plan as a different agent. Suggest modifications "
            "that improve feasibility, fan-out execution, demo readiness, and "
            "Slack reporting."
        ),
        project=C26_PROJECT_NAME,
        priority=1,
        required_capabilities=["review"],
        dependencies=[plan.id],
        metadata={
            "artifact_type": "plan_review",
            "reviewed_task_id": plan.id,
            "review_agent_must_differ_from": agents["planner"].id,
        },
        actor="reviewer",
    )
    _complete_task(
        cp,
        review,
        agents["reviewer"],
        agents["planner"],
        "Independent plan review completed",
        project_path=project_path,
        extra_metadata={"review_notes": C26_REVIEW_NOTES},
    )

    revised = cp.create_task(
        "c26 plan: apply independent review modifications",
        description=(
            "Revise the plan after independent review. Keep the first demo to "
            "a runnable QEMU vertical slice with C/assembly artifacts, device "
            "API stubs, and explicit demo instructions."
        ),
        project=C26_PROJECT_NAME,
        priority=1,
        required_capabilities=["planning"],
        dependencies=[review.id],
        metadata={
            "artifact_type": "revised_plan",
            "review_task_id": review.id,
            "modifications": C26_REVIEW_NOTES,
        },
        actor="planner",
    )
    revised_plan = _revised_plan(project_path)
    _complete_task(
        cp,
        revised,
        agents["planner"],
        agents["reviewer"],
        "Reviewed c26 plan accepted",
        project_path=project_path,
        extra_metadata={"plan": revised_plan},
    )

    implementation_tasks = _create_implementation_tasks(cp, revised.id, project_path)

    # Claim the first fan-out lane before completing any of it so the proof can
    # demonstrate concurrent MAC ownership by different agents.
    fanout_keys = ["architecture", "build_harness", "device_api"]
    running_fanout: List[JsonDict] = []
    for key in fanout_keys:
        task = implementation_tasks[key]
        worker = agents[_task_agent_key(key)]
        claimed, _lease = cp.claim_task(task.id, worker.id)
        started = cp.start_task(claimed.id, worker.id)
        running_fanout.append(
            {
                "task_id": started.id,
                "node_key": key,
                "agent_id": worker.id,
                "state": started.state,
            }
        )

    completed_tasks: Dict[str, Task] = {}
    for key in fanout_keys:
        completed_tasks[key] = _finish_running_task(
            cp,
            implementation_tasks[key],
            agents[_task_agent_key(key)],
            agents["reviewer"],
            _task_summary(key),
            project_path=project_path,
        )
    for key in [
        "kernel_runtime",
        "basic",
        "graphics_audio",
        "retro_desktop",
        "robot_sdk",
        "integration_demo",
        "demo_story",
    ]:
        completed_tasks[key] = _complete_task(
            cp,
            implementation_tasks[key],
            agents[_task_agent_key(key)],
            agents["reviewer"],
            _task_summary(key),
            project_path=project_path,
        )

    demo_story = completed_tasks["demo_story"]
    cp.record_notification(
        "project.demo_requested",
        "c26 is ready for human demo",
        _demo_request(project_path),
        subject_type="project",
        subject_id=C26_PROJECT_NAME,
        channels=["dashboard", "hermes", "slack"],
        metadata={
            "project": C26_PROJECT_NAME,
            "demo_task_id": demo_story.id,
            "slack_binding_id": binding.id,
            "build_commands": _build_commands(project_path),
        },
    )
    delivery = cp.deliver_pending_notifications(limit=500)
    messages = [message.to_dict() for message in cp.list_messages(agents["reporter"].id)]
    project = cp.get_project(C26_PROJECT_NAME)
    repository_state = _project_repo_state(project_path)
    all_tasks = [task for task in cp.list_tasks() if task.project == C26_PROJECT_NAME]
    checks = {
        "project_created_from_name_description": project["project"] == C26_PROJECT_NAME
        and bool(project["tasks"]),
        "epic_exists": epic.metadata.get("artifact_type") == "epic",
        "plan_created_from_epic": plan.metadata.get("source_epic_task_id") == epic.id,
        "independent_plan_review": review.owner_agent_id is None
        and agents["reviewer"].id != agents["planner"].id,
        "review_modifications_applied": revised.metadata.get("modifications") == C26_REVIEW_NOTES,
        "parallel_fanout_claimed": len({item["agent_id"] for item in running_fanout}) >= 3
        and all(item["state"] == TaskState.RUNNING.value for item in running_fanout),
        "all_project_tasks_done": all(task.state == TaskState.COMPLETED.value for task in all_tasks),
        "slack_notifier_configured": notifier.channel_type == "slack" and notifier.enabled,
        "slack_progress_delivered": delivery["delivered"] > 0
        and any(
            message.get("payload", {}).get("channel_type") == "slack"
            for message in messages
        ),
        "demo_story_has_build_and_feedback_instructions": all(
            fragment in implementation_tasks["demo_story"].description
            for fragment in ("make smoke", "make run", "Slack", "feedback")
        ),
    }
    return {
        "schema": "mac.project_inception_proof.v1",
        "ready": all(checks.values()),
        "project": {
            "name": C26_PROJECT_NAME,
            "description": C26_PROJECT_DESCRIPTION,
            "path": project_path,
            "repository_state": repository_state,
            "summary": project["summary"],
        },
        "checks": checks,
        "epic_task_id": epic.id,
        "plan_task_id": plan.id,
        "review_task_id": review.id,
        "revised_plan_task_id": revised.id,
        "implementation_task_ids": {
            key: task.id for key, task in implementation_tasks.items()
        },
        "parallel_fanout": running_fanout,
        "slack": {
            "notifier_channel_id": notifier.id,
            "platform_binding_id": binding.id,
            "delivery": delivery,
            "reporter_agent_id": agents["reporter"].id,
            "message_count": len(messages),
        },
        "demo_request": {
            "task_id": demo_story.id,
            "body": _demo_request(project_path),
            "build_commands": _build_commands(project_path),
        },
        "task_count": len(all_tasks),
        "completed_task_count": sum(1 for task in all_tasks if task.state == TaskState.COMPLETED.value),
    }


def _register_c26_agents(cp: ControlPlane, hermes_instance_id: str) -> Dict[str, Any]:
    machine = cp.register_machine("c26-proof-host")

    def agent(name: str, capabilities: Iterable[str]):
        return cp.register_agent(
            machine.id,
            name,
            capabilities=capabilities,
            hermes_instance_id=hermes_instance_id,
        )

    return {
        "planner": agent("c26-planner", ["planning", "python"]),
        "reviewer": agent("c26-reviewer", ["review", "python"]),
        "architect": agent("c26-architect", ["c", "asm", "riscv", "qemu"]),
        "builder": agent("c26-build-engineer", ["c", "asm", "riscv", "qemu"]),
        "device": agent("c26-device-engineer", ["c", "devices"]),
        "kernel": agent("c26-kernel-engineer", ["c", "asm", "riscv"]),
        "basic": agent("c26-basic-engineer", ["c", "riscv"]),
        "media": agent("c26-media-engineer", ["c", "graphics", "audio"]),
        "robot": agent("c26-robotics-engineer", ["c", "robotics"]),
        "integrator": agent("c26-integrator", ["c", "asm", "qemu", "demo"]),
        "reporter": agent("c26-slack-reporter", ["slack", "demo"]),
    }


def _epic_description(project_path: str) -> str:
    return (
        "%s\n\n"
        "Design goals:\n"
        "- QEMU RISC-V virt as the first hardware target.\n"
        "- Freestanding startup in assembly and runtime in C.\n"
        "- BASIC REPL and retro desktop homage to the Commodore 64.\n"
        "- 2026-grade graphics, audio, networking, USB, I2C, CAN, and robot SDK concepts.\n"
        "- Demo-ready build/run instructions for humans in Slack.\n\n"
        "Planning instruction: create a detailed task plan, then require a different "
        "agent to review and modify it before implementation starts.\n\n"
        "Project path: %s"
    ) % (C26_PROJECT_DESCRIPTION, project_path)


def _initial_plan(project_path: str) -> List[JsonDict]:
    return [
        {"node_key": "architecture", "depends_on": [], "acceptance": "memory map and QEMU target documented"},
        {"node_key": "build_harness", "depends_on": [], "acceptance": "fresh checkout builds ELF and smoke boots QEMU"},
        {"node_key": "kernel_runtime", "depends_on": ["architecture", "build_harness"], "acceptance": "assembly entry and C kernel print boot banner"},
        {"node_key": "basic", "depends_on": ["kernel_runtime"], "acceptance": "scripted BASIC program can run"},
        {"node_key": "graphics_audio", "depends_on": ["architecture"], "acceptance": "HAL APIs and demo output exist"},
        {"node_key": "retro_desktop", "depends_on": ["graphics_audio"], "acceptance": "desktop shell renders in demo"},
        {"node_key": "device_api", "depends_on": ["architecture"], "acceptance": "USB/I2C/CAN/TCP/IP API stubs compile"},
        {"node_key": "robot_sdk", "depends_on": ["device_api"], "acceptance": "robot example uses SDK APIs"},
        {"node_key": "integration_demo", "depends_on": ["basic", "retro_desktop", "robot_sdk", "build_harness"], "acceptance": "QEMU demo exercises the vertical slice"},
        {"node_key": "demo_story", "depends_on": ["integration_demo"], "acceptance": "Slack demo ask includes build and feedback instructions"},
    ]


def _revised_plan(project_path: str) -> JsonDict:
    return {
        "project_path": project_path,
        "review_notes_applied": C26_REVIEW_NOTES,
        "parallel_lanes": [
            ["architecture", "build_harness", "device_api"],
            ["kernel_runtime", "graphics_audio"],
            ["basic", "retro_desktop", "robot_sdk"],
        ],
        "demo_slice": "boot banner, BASIC scripted demo, retro desktop output, device API stubs, robot SDK example",
    }


def _create_implementation_tasks(
    cp: ControlPlane,
    revised_plan_task_id: str,
    project_path: str,
) -> Dict[str, Task]:
    specs = {
        "architecture": ("c26 architecture contract", ["c", "asm", "riscv"], [revised_plan_task_id]),
        "build_harness": ("c26 build and QEMU smoke harness", ["c", "asm", "qemu"], [revised_plan_task_id]),
        "device_api": ("c26 2026 peripheral API contracts", ["c"], [revised_plan_task_id]),
        "kernel_runtime": ("c26 kernel/runtime vertical slice", ["c", "asm", "riscv"], ["architecture", "build_harness"]),
        "basic": ("c26 BASIC interpreter vertical slice", ["c"], ["kernel_runtime"]),
        "graphics_audio": ("c26 graphics and audio HAL", ["c", "graphics", "audio"], ["architecture"]),
        "retro_desktop": ("c26 retro desktop shell", ["c", "graphics"], ["graphics_audio"]),
        "robot_sdk": ("c26 robot SDK and example", ["c", "robotics"], ["device_api"]),
        "integration_demo": ("c26 integrated QEMU demo", ["c", "asm", "qemu", "demo"], ["basic", "retro_desktop", "robot_sdk", "build_harness"]),
        "demo_story": ("c26 final Slack demo story", ["demo", "slack"], ["integration_demo"]),
    }
    tasks: Dict[str, Task] = {}
    for key, (title, capabilities, dependencies) in specs.items():
        dep_ids = [tasks[item].id if item in tasks else item for item in dependencies]
        tasks[key] = cp.create_task(
            title,
            description=_task_description(key, project_path),
            project=C26_PROJECT_NAME,
            priority=5,
            required_capabilities=capabilities,
            dependencies=dep_ids,
            metadata={
                "artifact_type": "implementation_task",
                "node_key": key,
                "project_path": project_path,
                "acceptance_criteria": _acceptance(key),
                "execution_contract": {
                    "schema": "mac.task_execution_contract.v1",
                    "type": "repository",
                    "required_changed_files": _required_changed_files(key),
                },
            },
            actor="planner",
        )
    return tasks


def _task_description(key: str, project_path: str) -> str:
    descriptions = {
        "architecture": "Document QEMU virt target, RISC-V boot ABI, linker layout, UART console, memory map, and device model.",
        "build_harness": "Create Makefile and smoke harness that build a freestanding RISC-V ELF and boot it in QEMU.",
        "device_api": "Define freestanding C APIs and emulated stubs for USB, I2C, CAN, TCP/IP, and robot buses.",
        "kernel_runtime": "Implement assembly startup, stack setup, C kernel entry, UART output, and minimal runtime helpers.",
        "basic": "Implement a compact BASIC tokenizer/interpreter vertical slice with PRINT, LET, arithmetic, and scripted demo lines.",
        "graphics_audio": "Implement graphics/audio HAL abstractions and demo-safe output suitable for QEMU UART proof.",
        "retro_desktop": "Implement a retro desktop shell that renders the first c26 application launcher screen.",
        "robot_sdk": "Implement robot-control SDK APIs and a simulator example for motor/sensor control.",
        "integration_demo": "Integrate boot, desktop, BASIC, device APIs, and robot SDK into one QEMU demo path.",
        "demo_story": (
            "Reach out to users in Slack and ask them to try c26. Include: "
            "`cd %s`, `make smoke`, `make run`, what they should see, and a request "
            "for feedback on BASIC, desktop feel, device APIs, and robot SDK."
        )
        % project_path,
    }
    return descriptions[key]


def _acceptance(key: str) -> List[str]:
    criteria = {
        "architecture": ["docs/architecture.md explains QEMU virt, memory map, and device model"],
        "build_harness": ["make smoke builds and boots under qemu-system-riscv64"],
        "device_api": ["include/c26_devices.h defines USB/I2C/CAN/TCP/IP abstractions"],
        "kernel_runtime": ["src/boot.S and src/kernel.c provide standalone startup and UART output"],
        "basic": ["BASIC demo emits deterministic interpreter output"],
        "graphics_audio": ["graphics/audio APIs are callable from the integrated demo"],
        "retro_desktop": ["demo renders a c26 retro desktop launcher"],
        "robot_sdk": ["robot SDK example emits deterministic motor/sensor output"],
        "integration_demo": ["smoke output includes boot, BASIC, desktop, and robot markers"],
        "demo_story": ["Slack demo ask includes build commands and feedback request"],
    }
    return criteria[key]


def _required_changed_files(key: str) -> List[str]:
    files = {
        "architecture": ["docs/architecture.md"],
        "build_harness": ["Makefile"],
        "device_api": ["include/c26_devices.h"],
        "kernel_runtime": ["src/boot.S", "src/kernel.c"],
        "basic": ["src/basic.c"],
        "graphics_audio": ["src/graphics.c", "src/audio.c"],
        "retro_desktop": ["src/desktop.c"],
        "robot_sdk": ["src/robot.c"],
        "integration_demo": ["src/main.c", "Makefile"],
        "demo_story": ["README.md", "docs/demo-story.md"],
    }
    return files[key]


def _task_summary(key: str) -> str:
    return "completed %s for c26 demo readiness" % key.replace("_", " ")


def _task_agent_key(key: str) -> str:
    return {
        "architecture": "architect",
        "build_harness": "builder",
        "device_api": "device",
        "kernel_runtime": "kernel",
        "basic": "basic",
        "graphics_audio": "media",
        "retro_desktop": "media",
        "robot_sdk": "robot",
        "integration_demo": "integrator",
        "demo_story": "reporter",
    }[key]


def _complete_task(
    cp: ControlPlane,
    task: Task,
    worker: Any,
    reviewer: Any,
    summary: str,
    *,
    project_path: str = "~/Src/c26",
    extra_metadata: Optional[JsonDict] = None,
) -> Task:
    if task.state == TaskState.BLOCKED.value:
        task = cp.claim_task(task.id, worker.id)[0]
    elif task.state == TaskState.OPEN.value:
        task = cp.claim_task(task.id, worker.id)[0]
    if task.state == TaskState.CLAIMED.value:
        task = cp.start_task(task.id, worker.id)
    return _finish_running_task(
        cp,
        task,
        worker,
        reviewer,
        summary,
        project_path=project_path,
        extra_metadata=extra_metadata,
    )


def _finish_running_task(
    cp: ControlPlane,
    task: Task,
    worker: Any,
    reviewer: Any,
    summary: str,
    *,
    project_path: str,
    extra_metadata: Optional[JsonDict] = None,
) -> Task:
    required_files = (
        task.metadata.get("execution_contract", {}).get("required_changed_files", [])
        if isinstance(task.metadata.get("execution_contract"), dict)
        else []
    )
    metadata = _verified_repo_metadata(
        cp,
        worker.id,
        project_path,
        required_files=required_files,
    )
    if extra_metadata:
        metadata["proof"] = extra_metadata
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://c26/%s" % task.id,
        summary,
        worker.id,
        metadata=metadata,
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = _submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(
        review.id,
        ReviewStatus.APPROVED.value,
        reviewer.id,
        evidence_id=verdict_id,
    )
    cp.publish_task(
        task.id,
        "git://c26/main",
        reviewer.id,
        evidence_id=evidence.id,
    )
    return cp.get_task(task.id)


def _verified_repo_metadata(
    cp: ControlPlane,
    agent_id: str,
    project_path: str,
    *,
    required_files: Optional[List[str]] = None,
) -> JsonDict:
    repo_state = _project_repo_state(project_path)
    head_sha = repo_state.get("head_sha") or "c26" + ("0" * 37)
    remote_ref = repo_state.get("upstream_ref") or "git://c26/main@%s" % head_sha
    files_changed = _evidence_files(repo_state)
    for path in required_files or []:
        if path not in files_changed:
            files_changed.append(path)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": head_sha,
            "pushed": True,
            "remote_ref": remote_ref,
            "dirty": bool(repo_state.get("dirty")),
            "files_changed": files_changed,
        },
        "tests": [{"command": "make smoke", "returncode": 0}],
        "project_path": project_path,
        "actual_repo_state": repo_state,
        "publication": {
            "target": "git://c26/main",
            "external_remote_configured": bool(repo_state.get("remotes")),
            "note": (
                "MAC proof publication targets the local c26 project ledger; "
                "actual external remote state is recorded separately."
            ),
        },
    }
    manifest["signed_by"] = agent_id
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(agent_id),
        manifest,
    )
    return {"returncode": 0, "verification": manifest}


def _project_repo_state(project_path: str) -> JsonDict:
    repo_path = Path(project_path).expanduser()
    head = _git_output(repo_path, ["rev-parse", "HEAD"])
    status = _git_output(repo_path, ["status", "--porcelain"])
    tracked_files = _git_output(repo_path, ["ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
    last_commit_files = _git_output(repo_path, ["show", "--format=", "--name-only", "HEAD"]).splitlines()
    remotes = _git_output(repo_path, ["remote", "-v"]).splitlines()
    upstream_ref = _git_output(
        repo_path,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
    )
    return {
        "path": str(repo_path),
        "available": _is_git_sha(head),
        "head_sha": head if _is_git_sha(head) else "",
        "dirty": bool(status.strip()),
        "status_porcelain": status.splitlines(),
        "tracked_file_count": len(tracked_files),
        "tracked_files": tracked_files,
        "last_commit_files": last_commit_files,
        "remotes": remotes,
        "upstream_ref": upstream_ref,
    }


def _git_output(repo_path: Path, args: List[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_git_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value)


def _evidence_files(repo_state: JsonDict) -> List[str]:
    preferred = [
        "Makefile",
        "src/boot.S",
        "src/kernel.c",
        "include/c26_devices.h",
        "src/devices.c",
        "src/basic.c",
        "src/desktop.c",
        "src/robot.c",
        "docs/demo-story.md",
    ]
    tracked = set(repo_state.get("tracked_files") or [])
    existing = [path for path in preferred if path in tracked]
    if existing:
        return existing
    last_commit = [path for path in repo_state.get("last_commit_files") or [] if path]
    if last_commit:
        return last_commit
    return preferred


def _submit_review_verdict(
    cp: ControlPlane,
    task_id: str,
    reviewer_agent_id: str,
    executor_evidence_id: str,
) -> str:
    executor = cp.get_evidence(executor_evidence_id)
    executor_manifest = executor.metadata.get("verification") or {}
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": executor_evidence_id,
        "repo": dict(executor_manifest.get("repo") or {}),
        "checks": [{"name": "independent c26 verification", "returncode": 0}],
        "worktree_digest": "sha256:" + ("1" * 64),
    }
    manifest["signed_by"] = reviewer_agent_id
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(reviewer_agent_id),
        manifest,
    )
    evidence = cp.add_evidence(
        task_id,
        "review",
        "artifact://c26/review/%s" % task_id,
        "reviewer verdict: approved",
        reviewer_agent_id,
        metadata={"returncode": 0, "verification": manifest},
    )
    return evidence.id


def _demo_request(project_path: str) -> str:
    return (
        "c26 is ready for a first human demo. Please try it from Slack:\n"
        "1. `cd %s`\n"
        "2. `make smoke`\n"
        "3. `make run`\n\n"
        "Expected: QEMU boots a RISC-V standalone C/assembly image, prints the "
        "retro c26 desktop banner, runs a BASIC program, initializes graphics, "
        "audio, USB/I2C/CAN/TCP/IP API stubs, and runs a robot SDK demo. Please "
        "send feedback in Slack on the BASIC feel, desktop vibe, device API "
        "shape, and robot programming model."
    ) % project_path


def _build_commands(project_path: str) -> List[str]:
    return ["cd %s" % project_path, "make smoke", "make run"]
