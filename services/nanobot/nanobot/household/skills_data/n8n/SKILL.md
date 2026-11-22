---
name: n8n
description: "Interact with n8n workflow automation: list, get, create, update, activate, deactivate, and delete workflows, plus execute workflows and inspect executions. Invoke with JSON: {\"skill\":\"n8n\",\"action\":\"...\"} — actions: list_workflows | get_workflow(id) | create_workflow(name, nodes, connections) | update_workflow(id, data) | delete_workflow(id) | activate_workflow(id) | deactivate_workflow(id) | execute_workflow(id, data) | list_executions(workflow_id). Use when a message is about an automation workflow."
metadata: {"nanobot":{"translatable":true,"requires":{"env":["NANOBOT_N8N_API_KEY"]}}}
---

# n8n

n8n is a workflow automation platform. Use this skill to manage and trigger workflows via the n8n REST API.

## Required config

The n8n integration reads connection details from `~/.nanobot/config.json` under the `n8n` key:

- `apiKey` — n8n API key (or set `NANOBOT_N8N_API_KEY` env var with `${...}` syntax)
- `baseURL` — n8n server base URL (default `http://localhost:5678`)
- `workflowsEndpoint` — workflows API path (default `/api/v1/workflows/`)

## Actions

### List all workflows
```json
{"skill": "n8n", "action": "list_workflows"}
```

### Get a specific workflow
```json
{"skill": "n8n", "action": "get_workflow", "id": "ABC123"}
```

### Create a workflow
```json
{"skill": "n8n", "action": "create_workflow", "name": "My Workflow", "nodes": [], "connections": {}}
```

### Update a workflow
```json
{"skill": "n8n", "action": "update_workflow", "id": "ABC123", "data": {"name": "Renamed"}}
```

### Delete a workflow
```json
{"skill": "n8n", "action": "delete_workflow", "id": "ABC123"}
```

### Activate a workflow
```json
{"skill": "n8n", "action": "activate_workflow", "id": "ABC123"}
```

### Deactivate a workflow
```json
{"skill": "n8n", "action": "deactivate_workflow", "id": "ABC123"}
```

### Execute a workflow (trigger)
```json
{"skill": "n8n", "action": "execute_workflow", "id": "ABC123", "data": {"key": "value"}}
```

### List recent executions
```json
{"skill": "n8n", "action": "list_executions", "workflow_id": "ABC123"}
```

## Rules
- Always use the workflow **id** returned by `list_workflows` for subsequent actions.
- When creating workflows, `nodes` and `connections` follow the n8n workflow schema.
- `execute_workflow` sends a POST to the workflow's webhook or test URL — the workflow must have a trigger node configured.