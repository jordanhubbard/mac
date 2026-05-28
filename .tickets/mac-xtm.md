---
id: mac-xtm
status: closed
deps: []
links: []
created: 2026-05-22T18:25:25Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-xtm
---
# Add fleet web search shared service

Agents currently lack reliable web search. Add a hub-hosted Firecrawl/web-search shared service to fleet deployment, propagate the endpoint to Hermes on all agents, and advertise a web-search capability/resource so MAC dispatch and operators can see which agents have web tooling.

## Acceptance Criteria

Fleet config and deploy support a hub-managed Firecrawl/web-search endpoint; hub deploy can start or validate the service; all agents receive a reachable FIRECRAWL_API_URL and Hermes web backend configuration; agents advertise web_search/firecrawl capability or resource; tests cover config rendering and deployment wiring; rocky, natasha, and bullwinkle verify web endpoint reachability after deploy.

## Notes

Implemented and deployed a hub-hosted Firecrawl-compatible web search gateway. Commit 3ede318 adds fleet firecrawl metadata, deploy-managed gateway install/validation, Hermes FIRECRAWL_API_URL/web backend configuration, Hermes firecrawl-py preinstall, and default web_search/web_extract/web_crawl/firecrawl worker capabilities. Verified Rocky/Natasha/Bullwinkle reach http://100.125.137.89:3002/health, import firecrawl in Hermes venv, and perform Firecrawl SDK search returning https://openai.com/. Hub registration shows all three fleet agents healthy with web capabilities.

## Close Reason

Implemented fleet Firecrawl-compatible web search gateway, deployed to Rocky/Natasha/Bullwinkle, and verified endpoint, Hermes SDK imports, Hermes web backend config, and agent capabilities.
