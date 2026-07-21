from __future__ import annotations

from mac.cli import build_parser


def test_directive_cli_parses_version_bound_activation() -> None:
    args = build_parser().parse_args(
        [
            "directive",
            "activate",
            "build.bazel-first",
            "--version",
            "3",
            "--digest",
            "a" * 64,
            "--actor",
            "operator",
        ]
    )
    assert args.directive == "build.bazel-first"
    assert args.version == 3
    assert args.digest == "a" * 64


def test_directive_cli_requires_file_backed_document() -> None:
    args = build_parser().parse_args(
        ["directive", "propose", "--document-file", "directive.yaml"]
    )
    assert args.document_file == "directive.yaml"


def test_directive_cli_parses_conditional_binding_value_file() -> None:
    args = build_parser().parse_args(
        [
            "directive",
            "binding",
            "set",
            "repository",
            "repo_mac",
            "build.primary_target",
            "--value-file",
            "-",
        ]
    )
    assert args.target_type == "repository"
    assert args.key == "build.primary_target"
    assert args.value_file == "-"
