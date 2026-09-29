---
status: accepted
---
# The MCP server retrieves and stores state; the host agent's model writes every answer

The coach ships as an MCP server plus Playbooks used from Claude (or any MCP host). The
server returns Knowledge items, Citations and Founder state; it never calls an LLM at
query time. This keeps the server cheap, model-agnostic and testable, and lets each
Founder's own assistant subscription do the reasoning. A future web app runs its own
agent loop against the same tools.

## Consequences

Answer quality depends on the host model following the Playbook's answer contract
(cite everything, state Gaps), so Playbooks and the eval set must check that contract.
