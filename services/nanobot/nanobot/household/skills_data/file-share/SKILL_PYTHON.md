```python
import json, os, shutil, subprocess
from urllib.parse import quote
import smbclient
import smbclient.path as smbpath

HOST = os.environ.get("FILE_SHARE_HOST", "compute.home")
SHARE = os.environ.get("FILE_SHARE_NAME", "share")
FOLDER = os.environ.get("FILE_SHARE_FOLDER", "")
ADMIN = os.environ.get("FILE_SHARE_ADMIN", "0") == "1"
ROOT = rf"\\{HOST}\{SHARE}"
MEDIA_DIR = os.path.expanduser("~/.nanobot/workspace/media")

smbclient.ClientConfig(
    username=os.environ.get("FILE_SHARE_USERNAME", "share"),
    password=os.environ.get("FILE_SHARE_PASSWORD", ""),
)

_KNOWN_FOLDERS = {"user1", "user2", "user3", "user4", "user5", "familia"}
# Where what we make for someone is filed, inside their own folder. HomeCore's
# chat lists exactly this as "My files".
OWN_SUBFOLDER = "alfred"


def _link(rel):
    """A chat link to a file on the share.

    Every save hands one back so the link is never written from memory. HomeCore
    serves `download:<ruta del compartido>` straight off the share, under the
    same access the Files page enforces — a path outside what the reader can
    reach still fails, but a path inside it no longer has to be copied into the
    workspace first.

    A name carrying brackets goes in the angle form, `[texto](<download:...>)`.
    Without it the reader's markdown ends the URL at the first ")", so
    "Factura (1).pdf" linked to ".../Factura (1" and died on click with a
    stray ".pdf)" printed after the button. Angle brackets only when needed:
    most names have none, and the plain form is what everything already reads.
    """
    name = rel.split("/")[-1]
    target = f"download:{rel}"
    if any(ch in rel for ch in "()[]<>"):
        return f"[{name}](<{target}>)"
    return f"[{name}]({target})"

# --- Share store (HomeCore) -------------------------------------------------
# Sharing a file with another family member is a *grant* recorded by HomeCore
# (backup_data/file_shares.db), not a copy: the file stays in the owner's
# folder and the recipient gets read-only access plus an ntfy notification.
# Same trusted-proxy auth as the `tasks` skill (per-user derived token).
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")
FILES_API = os.environ.get("FILES_API_URL") or os.environ.get(
    "TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/files/api")

FAMILY = {"user3": "user3", "user1": "user1", "user2": "user2", "user4": "user4"}
# Nicknames, in whatever language the household says them. Not prose: this
# resolves what a person actually typed, so both languages stay listed.
_ALIASES = {"kai": "user4", "kaito": "user4", "ka": "user4", "robin": "user3",
            "robita": "user3", "sam": "user2", "sammy": "user2",
            "mum": "user2", "mom": "user2", "mama": "user2", "mamá": "user2",
            "dad": "user1", "papa": "user1", "papá": "user1"}


def _uid(who):
    name = str(who).strip().lower()
    name = _ALIASES.get(name, name)
    return FAMILY.get(name, str(who).strip())


def _api(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the share store (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    cmd = ["curl", "-sk", "--max-time", "15", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{FILES_API}/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip()


def _resolve(path=""):
    """Share-relative path -> UNC path, enforcing per-user access."""
    if ADMIN and (path or "").strip() in ("/", "\\", "*"):
        return ROOT, ""
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise ValueError(f"invalid path: {path}")
    # Paths not starting with a known user folder are relative to own folder
    if not parts or parts[0] not in _KNOWN_FOLDERS:
        if not FOLDER:
            raise PermissionError("FILE_SHARE_FOLDER not set")
        parts = [FOLDER] + parts
    if not ADMIN and parts[0] not in (FOLDER, "familia"):
        raise PermissionError(f"access denied: only '{FOLDER}/' is allowed for this user")
    return ROOT + "\\" + "\\".join(parts), "/".join(parts)


def list_files(path="", **kw):
    unc, rel = _resolve(path)
    entries = []
    for e in smbclient.scandir(unc):
        try:
            st = e.stat()
            size, mtime = st.st_size, int(st.st_mtime)
        except Exception:
            size, mtime = 0, 0
        entries.append({"name": e.name, "is_dir": e.is_dir(), "size": size, "mtime": mtime})
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": rel, "entries": entries}


def download_file(remote_path, local_path=None, **kw):
    unc, rel = _resolve(remote_path)
    name = rel.split("/")[-1]
    if not local_path:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        local_path = os.path.join(MEDIA_DIR, name)
    local_path = os.path.expanduser(local_path)
    with smbclient.open_file(unc, mode="rb") as src, open(local_path, "wb") as dst:
        shutil.copyfileobj(src, dst, 256 * 1024)
    # The link points at the file on the share, not at this copy: the reader
    # can reach anything _resolve just let us reach, so there is nothing to
    # deliver — the copy is only here for us to work on locally.
    return {"ok": True, "remote_path": rel, "local_path": local_path,
            "download_link": _link(rel), "deliver": False}


def upload_file(local_path, remote_path=None, **kw):
    local_path = os.path.expanduser(local_path)
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"local file not found: {local_path}")
    unc, rel = _resolve(remote_path or f"{OWN_SUBFOLDER}/{os.path.basename(local_path)}")
    smbclient.makedirs("\\".join(unc.split("\\")[:-1]), exist_ok=True)
    with open(local_path, "rb") as src, smbclient.open_file(unc, mode="wb") as dst:
        shutil.copyfileobj(src, dst, 256 * 1024)
    return {"ok": True, "remote_path": rel, "size": os.path.getsize(local_path),
            "download_link": _link(rel), "deliver": False}


def save_text(content, remote_path, **kw):
    unc, rel = _resolve(remote_path)
    smbclient.makedirs("\\".join(unc.split("\\")[:-1]), exist_ok=True)
    with smbclient.open_file(unc, mode="w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "remote_path": rel, "size": len(content.encode("utf-8")),
            "download_link": _link(rel), "deliver": False}


def copy_file(src_path, dst_path, **kw):
    src_unc, src_rel = _resolve(src_path)
    dst_unc, dst_rel = _resolve(dst_path)
    if smbpath.isdir(dst_unc):
        dst_unc += "\\" + src_rel.split("/")[-1]
        dst_rel += "/" + src_rel.split("/")[-1]
    smbclient.makedirs("\\".join(dst_unc.split("\\")[:-1]), exist_ok=True)
    with smbclient.open_file(src_unc, mode="rb") as src, smbclient.open_file(dst_unc, mode="wb") as dst:
        shutil.copyfileobj(src, dst, 256 * 1024)
    return {"ok": True, "src": src_rel, "dst": dst_rel,
            "download_link": _link(dst_rel), "deliver": False}


def move_file(src_path, dst_path, **kw):
    result = copy_file(src_path, dst_path)
    src_unc, _ = _resolve(src_path)
    smbclient.remove(src_unc)
    result["moved"] = True
    return result


def make_folder(path, **kw):
    unc, rel = _resolve(path)
    smbclient.makedirs(unc, exist_ok=True)
    return {"ok": True, "path": rel}


def delete_file(path, **kw):
    unc, rel = _resolve(path)
    if len(rel.split("/")) < 2:
        raise PermissionError("cannot delete a top-level user folder")
    if smbpath.isdir(unc):
        smbclient.rmdir(unc)  # only empty folders
    else:
        smbclient.remove(unc)
    return {"ok": True, "deleted": rel}


def share_with(path, users, **kw):
    """Grant other family members read-only access to one of your files/folders.

    The file is NOT copied — it stays where it is and each recipient gets an
    ntfy push plus the item in their "Compartido conmigo" panel.
    """
    _, rel = _resolve(path)
    if isinstance(users, str):
        users = [u for u in users.replace(",", " ").split() if u]
    targets = [_uid(u) for u in (users or [])]
    if not targets:
        return {"error": "no recipients given"}
    result = _api("POST", "share", {"path": rel, "targets": targets})
    if isinstance(result, dict) and result.get("ok"):
        result["shared_path"] = rel
        result["shared_with"] = list(users)
    return result


def unshare(path, user=None, **kw):
    """Revoke a share you created (omit `user` to revoke it from everyone)."""
    _, rel = _resolve(path)
    body = {"path": rel}
    if user:
        body["target"] = _uid(user)
    return _api("POST", "unshare", body)


def list_my_shares(**kw):
    """Files/folders you are currently sharing, with their recipients."""
    return _api("GET", "sharing")


def list_shared_with_me(path="", **kw):
    """Items other people shared with you (pass a path to browse inside one)."""
    suffix = f"?path={quote(str(path))}" if path else ""
    return _api("GET", f"shared/list{suffix}")


def download_shared(path, local_path=None, **kw):
    """Download a file someone shared with you into the workspace media dir."""
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the share store (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    # Normalised the way _resolve normalises everything else: an unnormalised
    # 'user2/recetas//pastel.md' fetched fine and then produced a link with the
    # '//' still in it, which the chat refuses to render at all (empty path
    # segment) — so the file arrived as raw markdown text.
    rel = "/".join(p for p in str(path).replace("\\", "/").split("/") if p and p != ".")
    name = rel.split("/")[-1]
    if not local_path:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        local_path = os.path.join(MEDIA_DIR, name)
    local_path = os.path.expanduser(local_path)
    cmd = ["curl", "-sk", "--max-time", "60", "-o", local_path,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}",
           f"{FILES_API}/shared/download?path={quote(rel)}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if not os.path.isfile(local_path) or os.path.getsize(local_path) == 0:
        return {"error": r.stderr.strip() or "download failed"}
    # Same as download_file: the grant lets the reader open it where it lives.
    return {"ok": True, "remote_path": rel, "local_path": local_path,
            "download_link": _link(rel)}


mkdir = make_folder
delete = delete_file
share_file = share_with
```
