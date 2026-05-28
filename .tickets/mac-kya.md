---
id: mac-kya
status: closed
deps: []
links: []
created: 2026-05-24T10:13:26Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-kya
---
# Expose Hermes web search bridge

Hermes runtime proof declares Firecrawl-backed web_search as a required direct-session capability, and fleet deployment configures the hub Firecrawl-compatible service, but mac-hermes does not expose search, scrape, or crawl operations. Add Hermes adapter and CLI commands for Firecrawl-compatible web search/extract/crawl using MAC/Hermes environment defaults, wire them into runtime/proof contracts and docs, and cover them with tests so Hermes agents have an actual web-search affordance rather than only a health check.

## Close Reason

Added mac-hermes web-search/web-scrape/web-crawl bridge commands and adapter methods, wired them into runtime/proof contracts, and covered them with tests.
