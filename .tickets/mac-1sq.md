---
id: mac-1sq
status: closed
deps: []
links: []
created: 2026-05-25T04:27:16Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-1sq
---
# Prove c26 project inception on Rocky fleet

Run the user-requested end-to-end MAC workflow proof for c26 on the Rocky/Natasha/Bullwinkle fleet: create a first-class project from just name and description, create the planning epic, run independent plan review, dispatch implementation tasks to agents through MAC APIs, verify Slack progress reporting, and produce demo-ready evidence. Improve MAC APIs where required by this workflow.

## Notes

2026-05-25: Completed c26 Rocky fleet E2E proof. MAC pushed b630326 to GitHub and redeployed Rocky/Natasha/Bullwinkle from HTTPS GitHub source with startup warnings=0 and identity continuity ok. Created final docs task task_86f8018b438447e1b3b2ff94ef1db8b2 targeting Bullwinkle with Natasha review and repository_auto_publish=true. Bullwinkle produced pushed task branch commit 4ab0058; Natasha signed approved review verdict ev_a68028bca6d247b88c52a721ec690e12 with make smoke returncode=0; MAC published pub_39da54c79e9e4df4b7d27753c97b9eb1 to git://main. Local/Rocky/Natasha/Bullwinkle c26 checkouts fast-forwarded to 4ab0058. Local make smoke passed. During proof, fixed MAC worker auto-publish and review verdict remote_ref normalization; full suite passed with PATH=.venv/bin:/usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/local/bin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/bin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/appleinternal/bin:/opt/pkg/env/active/bin:/opt/pmk/env/global/bin:/opt/X11/bin:/Library/Apple/usr/bin:/Library/TeX/texbin:/Applications/VMware Fusion.app/Contents/Public:/opt/homebrew/bin:/Users/jordanh/.codex/tmp/arg0/codex-arg0ZiX9t3:/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex-path:/Users/jordanh/Library/pnpm:/opt/homebrew/sbin:/Applications/Docker.app/Contents/Resources/bin:/Users/jordanh/.cargo/bin:/Users/jordanh/.local/bin:/Users/jordanh/Bin:. .venv/bin/python -m pytest (418 passed).

## Close Reason

c26 Rocky fleet E2E proof completed and verified
