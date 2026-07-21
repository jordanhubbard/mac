from __future__ import annotations

from mac.dispatch import RemoteDispatch
from mac.http_client import HubClient


def test_remote_directive_lifecycle_uses_public_http_contract() -> None:
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body, token))
        if method == "GET" and url.endswith("/directives"):
            return []
        return {"id": "ok"}

    dispatch = RemoteDispatch(HubClient("https://hub.example", token="test", transport=transport))
    dispatch.propose_directive({"schema": "mac.directive.v1"}, actor="operator")
    dispatch.list_directives()
    dispatch.check_directive("build.bazel-first", version=1, actor="operator")
    dispatch.approve_directive(
        "build.bazel-first",
        version=1,
        directive_digest="a" * 64,
        check_id="check_1",
        actor="operator",
    )
    dispatch.activate_directive(
        "build.bazel-first",
        version=1,
        directive_digest="a" * 64,
        actor="operator",
    )

    assert [(method, url.removeprefix("https://hub.example")) for method, url, _body, _token in calls] == [
        ("POST", "/directives"),
        ("GET", "/directives"),
        ("POST", "/directives/build.bazel-first/check"),
        ("POST", "/directives/build.bazel-first/approve"),
        ("POST", "/directives/build.bazel-first/activate"),
    ]
    assert all(token == "test" for _method, _url, _body, token in calls)
