```python
import subprocess, urllib.parse, json, os

BASE = os.environ.get("PAPERLESS_URL", "http://paperless.home:8000")
TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "")
AUTH = f"Authorization: Token {TOKEN}"

def api(path, method="GET", data=None):
    cmd = ["curl", "-s", "-X", method, "-H", AUTH, f"{BASE}/api/{path}"]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:
        # Never hand back an empty string. A blank result reads as "no such
        # document" and the caller goes off writing its own curl instead —
        # which is how the API token ended up on a command line.
        body = r.stdout.strip()
        if not body:
            return {"error": f"paperless returned nothing for {path}",
                    "stderr": r.stderr.strip()[:200]}
        return body

def search_documents(query="", page_size=25, **kw):
    # Searches the Paperless API itself (BASE, :8000). This used to query
    # GPT_BASE (:8001, paperless-gpt), whose /api/documents is not the search
    # endpoint — it answered with nothing at all, so every search looked like
    # "no results" and the agent fell back to improvising curl by hand: seven
    # round trips, 26-120s, one outright timeout, and the token echoed into the
    # shell log. The trailing slash matters; without it Paperless redirects and
    # `curl -s` follows nothing.
    q = urllib.parse.quote(str(query))
    return api(f"documents/?query={q}&page_size={page_size}&truncate_content=true")

def list_documents(page=1, page_size=25, **kw):
    return api(f"documents/?page={page}&page_size={page_size}")

def get_document(id, **kw):
    return api(f"documents/{id}/")

def get_document_metadata(id, **kw):
    return api(f"documents/{id}/metadata/")

def download_document(id, filename=None, **kw):
    # Into the workspace, not /tmp. The chat serves files from the workspace
    # media dir; a PDF written anywhere else is reachable by nothing, and the
    # reply that links to it is a link to a 404.
    import os, re
    media_dir = os.path.expanduser("~/.nanobot/workspace/media")
    os.makedirs(media_dir, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", str(filename or "")).strip("_").lower()
    name = f"doc_{id}_{stem}.pdf" if stem else f"doc_{id}.pdf"
    path = f"{media_dir}/{name}"
    subprocess.run(["curl", "-s", "-H", AUTH, f"{BASE}/api/documents/{id}/download/", "-o", path], timeout=30)
    rel = f"media/{name}"
    return {
        "file_path": path,
        "workspace_rel": rel,
        # Handed over ready-made. Asked to splice a path into a sentence, the
        # model writes a filename the skill never created and drops the
        # download: prefix — observed as "[document](media/doc_41_poliza_chubb.pdf)"
        # for a file actually named doc_41.pdf.
        "download_link": f"[{name}](download:{rel})",
        "message": f"Here is the document:\n[{name}](download:{rel})",
        "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
    }

def download_thumb(id, **kw):
    import os
    media_dir = os.path.expanduser("~/.nanobot/workspace/media")
    os.makedirs(media_dir, exist_ok=True)
    path = f"{media_dir}/doc_{id}_thumb.jpg"
    subprocess.run(["curl", "-s", "-H", AUTH, f"{BASE}/api/documents/{id}/thumb/", "-o", path], timeout=30)
    return {
        "file_path": path,
        "workspace_rel": f"media/doc_{id}_thumb.jpg",
        "download_link": f"[imagen](download:media/doc_{id}_thumb.jpg)",
    }

def list_tags(**kw):
    return api("tags/")

def list_correspondents(**kw):
    return api("correspondents/")

def list_document_types(**kw):
    return api("document_types/")

def list_storage_paths(**kw):
    return api("storage_paths/")

def list_tasks(**kw):
    return api("tasks/")

def update_document(id, **kw):
    fields = {k: v for k, v in kw.items() if k in ("title", "correspondent", "document_type", "tags")}
    return api(f"documents/{id}/", method="PATCH", data=fields)

def delete_document(id, **kw):
    return api(f"documents/{id}/", method="DELETE")

def trash_document(id, **kw):
    return api(f"documents/{id}/trash/", method="POST")
```
