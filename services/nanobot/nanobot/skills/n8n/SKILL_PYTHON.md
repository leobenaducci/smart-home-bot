```python
import json, os, requests

BASE = os.environ.get("NANOBOT_N8N_BASE_URL", "http://localhost:5678").rstrip("/")
API_KEY = os.environ.get("NANOBOT_N8N_API_KEY", "")
WF_EP = os.environ.get("NANOBOT_N8N_WORKFLOWS_ENDPOINT", "/api/v1/workflows/").strip("/")
HEADERS = {"X-N8N-API-KEY": API_KEY, "Content-Type": "application/json"} if API_KEY else {"Content-Type": "application/json"}

def list_workflows():
    return requests.get(f"{BASE}/{WF_EP}", headers=HEADERS, timeout=15).json()

def get_workflow(id):
    return requests.get(f"{BASE}/{WF_EP}/{id}", headers=HEADERS, timeout=15).json()

def create_workflow(name="Untitled", nodes=None, connections=None):
    payload = {"name": name, "nodes": nodes or [], "connections": connections or {}}
    return requests.post(f"{BASE}/{WF_EP}", headers=HEADERS, json=payload, timeout=15).json()

def update_workflow(id, data):
    return requests.patch(f"{BASE}/{WF_EP}/{id}", headers=HEADERS, json=data, timeout=15).json()

def delete_workflow(id):
    r = requests.delete(f"{BASE}/{WF_EP}/{id}", headers=HEADERS, timeout=15)
    return {"status": r.status_code, "deleted": r.ok}

def activate_workflow(id):
    return requests.patch(f"{BASE}/{WF_EP}/{id}", headers=HEADERS, json={"active": True}, timeout=15).json()

def deactivate_workflow(id):
    return requests.patch(f"{BASE}/{WF_EP}/{id}", headers=HEADERS, json={"active": False}, timeout=15).json()

def execute_workflow(id, data=None):
    return requests.post(f"{BASE}/webhook/{id}", headers=HEADERS, json=data or {}, timeout=30).json()

def list_executions(workflow_id=None):
    params = {"workflowId": workflow_id} if workflow_id else {}
    return requests.get(f"{BASE}/api/v1/executions", headers=HEADERS, params=params, timeout=15).json()
```

Call the function matching the `action` field. Print the result with `print(json.dumps(result, indent=2))`.