#!/usr/bin/env python3
"""Render one assistant instance per member as Kubernetes objects.

    ./home-stack k8s render [member ...]     print the YAML
    ./home-stack k8s apply  [member ...]     render and kubectl apply
    ./home-stack k8s status                  what is running, and is it ready

Everything here is derived from `deploy/manifest.yml` and
`config/home-stack.yml` -- the same two files the compose path reads. That is
deliberate and it is the whole design constraint: `runtime:` makes nanobot
deployable two ways, and two ways means the one nobody uses drifts. There is no
k8s YAML in this repository to keep in step; there is a renderer.

Three decisions worth stating, because each is a failure this codebase has
already had in some other form:

**StatefulSet, one per member, not one with five replicas.** Members are people,
not interchangeable ordinals. And a StatefulSet terminates its pod before
creating the replacement, where a Deployment's rolling update overlaps them --
two pods on one member's `.nanobot` corrupts consolidated memory silently,
because `USER.md` is written by the consolidation pass and not only read.

**Identity comes from one member id.** Three of the five compose instances
shipped answering as the wrong member, from five near-identical blocks with the
identity written out in each. Five near-identical YAML documents is the same
shape with better syntax highlighting, so every one of the instance name,
`NANOBOT_INSTANCE`, `HOMECORE_USER_ID`, `FILE_SHARE_FOLDER`, the volume, the
secret and the labels is computed from `member` and nothing else.

**Per-member secrets stay per-member.** `nanobot-house` holds no member's
credentials -- there is a check asserting it -- and that boundary has to survive
the move. Shared keys go in one Secret; a member's three go in theirs, and
nothing else mounts it. `PROXY_SHARED_SECRET` is in neither: it is the master
every member's `HOMECORE_PROXY_TOKEN` is derived from, the container never
reads it, and a pod that held it could mint any other member's portal token.

**What this does not do yet, so nobody discovers it on a live household:**

* There is no `runtime:` switch. `deploy.py` still starts a compose container
  for every member in `services.nanobot.members`, so a member running as a pod
  is running twice -- two heartbeats, two morning messages, and two stores that
  each consolidate memory and then diverge. Stop the compose instance by hand.
* Member profiles are not seeded. Compose's `init-dirs` copies the admin page's
  generated `<member>.md` into `workspace/USER.md` on first boot; nothing here
  does, so a pod on a fresh volume knows nothing about the person, and the seed
  is once-only so it does not self-heal.
* The prompt files are shipped unpatched. The compose path rewrites
  `config.json` from `assistant.models` and the assistant's name across
  `config/*.md` before it ships them; `config_files()` reads the checkout.
* `HOST_IP` is the node the pod landed on, which is the hub only while the
  cluster is one machine. See the note in `env_for`.
* Nothing pushes the image to `services.registry`, and no node is configured to
  trust a plain-HTTP one, so turning the registry on renders a reference that
  cannot be pulled.
* PVCs are not host paths, so `./home-stack backup` does not capture
  them. It knows: the run warns, the archive records the gap and
  `--verify` repeats it. Recording a hole is not filling it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import deploy as D  # noqa: E402

NAMESPACE = "home-stack"
SHARED_SECRET = "nanobot-shared"
CONFIG_MAP = "nanobot-files"

# The ports the image binds, from `entrypoint.sh` and the right-hand side of
# the compose file's `ports:`. Every pod gets its own address, so these are the
# same for every member; only what the Service *publishes* moves with the
# member, and it moves the way every other consumer already expects -- the base
# is the first member's port, not the port before it.
API_PORT, WEBSOCKET_PORT, GATEWAY_PORT = 8900, 8765, 18790


def _cfg() -> dict:
    cfg = D.load_yaml(D.CONFIG)
    D.apply_service_renames(cfg)
    D.add_new_services(cfg)
    D.apply_config_defaults(cfg)
    # `derive` calls `add_container_addresses` itself -- calling it again here
    # was a no-op that read as a step.
    return D.derive(cfg, D.load_secrets(D.SECRETS))


def _spec(manifest: dict) -> dict:
    return manifest["services"]["nanobot"]


def _unit(spec: dict) -> dict:
    for unit in spec["units"]:
        if unit.get("member_profiles_gate"):
            return unit
    raise D.DeployError("no per-member nanobot unit in the manifest")


def image_ref(cfg: dict) -> str:
    """Where a node pulls the assistant image from.

    A node can only run a pod whose image it can pull. The image is built, not
    published, so on more than one machine this has to be the registry -- and
    if the registry is off, say so rather than rendering something that will
    sit in ImagePullBackOff on every node but the one that built it.
    """
    reg = ((cfg.get("services") or {}).get("registry") or {})
    if not reg.get("enabled"):
        return "alfred-nanobot:latest"
    host = (cfg.get("hosts") or {}).get(reg.get("host", "hub"), {})
    return f"{host.get('address', '127.0.0.1')}:{reg.get('port', 5005)}/alfred-nanobot:latest"


def _alias_targets(unit: dict) -> dict[str, str]:
    """`{FILE_SHARE_PASSWORD: SHARE_SMB_PASSWORD}`.

    A manifest `env:` value of `$OTHER_KEY` means "this secret, under the name
    the container actually reads". The value is a credential however it is
    spelled, so it belongs in a Secret and never in the ConfigMap.
    """
    return {name: str(value)[1:]
            for name, value in (unit.get("env") or {}).items()
            if str(value).startswith("$")}


def env_for(member: str, cfg: dict, unit: dict, spec: dict,
            secrets: dict) -> tuple[dict, dict, dict, dict]:
    """The plain values, the host-facing ones, the shared keys and this member's.

    Split four ways because they land in three different places -- a ConfigMap,
    one Secret everybody mounts, one Secret only this member mounts -- and the
    host-facing ones have to be `env:` entries rather than either, because
    `$(VAR)` expansion works in `env` and `envFrom` expands nothing.

    Built by `collect_env` -- the same function the compose path calls -- rather
    than by re-walking `unit['env']`. A hand-rolled subset of it dropped three
    things silently: `$ALIAS` entries (so `FILE_SHARE_PASSWORD` reached no pod
    under any name), the derived `HOMECORE_PROXY_TOKEN` (read from a key
    `install.sh` deliberately deletes, so every pod got an empty one and every
    call to the portal 401'd without saying so), and `TZ`.
    """
    env = D.collect_env(spec, unit, secrets, cfg)

    declared = spec.get("secrets", {}) or {}
    per_member_tpl = list(declared.get("per_member", []))
    everyones = set(D.expand_per_member(per_member_tpl, D.member_ids(cfg)))
    mine_keys = set(D.expand_per_member(per_member_tpl, [member]))
    shared_names = set(declared.get("required", [])) | set(declared.get("optional", []))
    aliases = _alias_targets(unit)
    # Host paths the deployer hands compose for a bind mount. They name nothing
    # inside a pod, and the compose file does not pass them to the container
    # either.
    state_vars = {e["env"] for e in D.interpolate(unit.get("state", []), cfg)
                  if e.get("env")}
    # A credential can also arrive by interpolation rather than by name --
    # `OLLAMA_CLOUD_API_KEY: "{derived.ollama_cloud_api_key}"` resolves to the
    # household's ollama.com key. Matching on the value catches those, so a key
    # cannot land in a ConfigMap just because of how it was spelled.
    secret_values = {v for v in secrets.values() if v and len(str(v)) >= 8}

    plain, hostward, shared, mine = {}, {}, {}, {}
    for key, value in env.items():
        value = str(value)
        if key in state_vars:
            continue
        if key in everyones and key not in mine_keys:
            continue                      # another member's credential
        if key in mine_keys:
            # `NANOBOT_API_SECRET_USER_1` in the file is `NANOBOT_API_SECRET`
            # in the container: the instance knows which member it is from its
            # own identity, not from the variable name.
            mine[key.rsplit("_USER_", 1)[0]] = value
            continue
        if key == "PROXY_SHARED_SECRET":
            # The master every member's `HOMECORE_PROXY_TOKEN` is derived from.
            # The container never reads it -- the compose file's per-service
            # `environment:` list leaves it out -- and a pod that held it could
            # mint any other member's portal token.
            continue
        if key in shared_names or key in aliases or value in secret_values:
            shared[key] = value
            continue
        # `host.docker.internal` is Docker's name for the machine the container
        # runs on, and it does not exist in a Kubernetes pod. Its equivalent is
        # the node's own address, which only the cluster knows, so it arrives
        # through the downward API.
        #
        # This is right while the cluster is one node. It is not right on two:
        # `status.hostIP` is whichever node the scheduler picked, and Ollama,
        # the tasks API and ntfy run on `hosts.hub`. Give the roles real
        # addresses in `config/home-stack.yml` before adding a second node --
        # then nothing is loopback, no value carries this name, and this branch
        # never fires.
        if D.CONTAINER_GATEWAY in value:
            hostward[key] = value.replace(D.CONTAINER_GATEWAY, "$(HOST_IP)")
        else:
            plain[key] = value

    # Every declared key is set, empty if the household has not got one.
    # `collect_env` omits an absent optional key, because compose re-supplies
    # it as `${VAR:-}`; there is no such fallback in a pod, and the container
    # distinguishes unset from empty -- it exits naming the first thing its
    # config.json references that is unset.
    for key in mine_keys:
        mine.setdefault(key.rsplit("_USER_", 1)[0], "")
    for key in shared_names | set(aliases):
        if key != "PROXY_SHARED_SECRET":
            shared.setdefault(key, "")

    # Identity, all of it from `member`. Not read from anywhere that could
    # disagree with itself.
    plain.update({
        "NANOBOT_INSTANCE": member,
        "HOMECORE_USER_ID": member,
        "FILE_SHARE_FOLDER": member,
    })
    return plain, hostward, shared, mine


def _warn_empty(member: str, mine: dict) -> None:
    """Say which of this member's credentials the household has not got.

    Every declared key is still set, empty if absent: `${VAR:-}` in compose
    *sets the variable to the empty string* rather than leaving it unset, and
    the container tells the two apart -- it checks that everything its
    config.json references exists and exits naming the first that does not.
    """
    missing = sorted(k for k, v in mine.items() if not v)
    if not missing:
        return
    # stderr: `render` writes YAML to stdout and a person redirects it to a
    # file. A warning in that stream is a manifest kubectl will not parse.
    print(f"    warn {member}: no {', '.join(missing)} -- rendered without "
          f"them, as the compose path also runs without them. "
          f"`--generate-secrets` mints what it can; the Paperless token comes "
          f"from the admin page.", file=sys.stderr)


def _probe(unit: dict) -> dict:
    """The health check the manifest already declares, as a probe.

    Asserted on the payload, not the status: an `httpGet` probe cannot see a
    body at all, so a redirect or a degraded answer on the right port reads as
    healthy -- which is the failure `verify()` was written for and which
    CLAUDE.md names outright. Read from `verify:` so changing the health path
    or the expected answer moves both runtimes at once.
    """
    check = (unit.get("verify") or [{}])[0]
    path = urlsplit(str(check.get("http", ""))).path or "/health"
    want = next(iter((check.get("expect_json") or {}).values()), "ok")
    return {"exec": {"command": [
        "sh", "-c",
        f"curl -sf http://127.0.0.1:{API_PORT}{path} | grep -q '\"{want}\"'"]}}


def render(member: str, cfg: dict, unit: dict, spec: dict,
           secrets: dict) -> list[dict]:
    """Every object one member needs, in apply order."""
    svc = ((cfg.get("services") or {}).get("nanobot") or {})
    # The base is the first member's port, the way the config's own comment
    # ("five members means 8761..8765"), the compose file (`8901:8900` for
    # user1) and home-core's `NANOBOT_BASE_PORT + id - 1` all read it. `+ 1`
    # handed every member the next member's number.
    index = D.member_ids(cfg).index(member)
    published = {
        "api": int(svc.get("api_port_base", 8901)) + index,
        "websocket": int(svc.get("websocket_port_base", 8761)) + index,
        "gateway": int(svc.get("gateway_port_base", 18791)) + index,
    }
    served = {"api": API_PORT, "websocket": WEBSOCKET_PORT,
              "gateway": GATEWAY_PORT}
    if len(set(published.values())) != len(published):
        # These three were renamed once, because `api_port_base` used to mean
        # the websocket and the portal spent three minutes a deploy polling a
        # port that answers Not Found. A config written before that rename has
        # no `websocket_port_base`, so the default collides with whatever the
        # old spelling still holds -- and a Service cannot publish one number
        # twice. Say so rather than rendering an object the API server refuses.
        raise D.DeployError(
            f"services.nanobot's port bases collide: {published}. This config "
            f"predates their rename -- `api_port_base` used to mean the "
            f"websocket. Set websocket_port_base / api_port_base / "
            f"gateway_port_base as config/home-stack.example.yml has them.")
    name = f"nanobot-{member}"
    labels = {"app.kubernetes.io/name": "nanobot",
              "app.kubernetes.io/instance": name,
              "home-stack/member": member}

    plain, hostward, _shared, mine = env_for(member, cfg, unit, spec, secrets)
    _warn_empty(member, mine)
    # `envFrom` is read once, at container start, and `kubectl apply` does not
    # restart a pod because a ConfigMap it references changed. Without this the
    # environment a person edited on the admin page would never reach a running
    # container while the apply reported success.
    config_hash = hashlib.sha256(
        json.dumps([plain, hostward, mine], sort_keys=True).encode()
    ).hexdigest()[:32]

    probe = _probe(unit)

    objects: list[dict] = [
        # This member's own credentials, and nobody else mounts it. The shared
        # assistant is asserted elsewhere to hold none of these.
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(labels)},
         "stringData": dict(sorted(mine.items()))},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(labels)},
         "data": dict(sorted(plain.items()))},
        # RWO: one writer. The whole point.
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(labels)},
         "spec": {"accessModes": ["ReadWriteOnce"],
                  "resources": {"requests": {"storage": "5Gi"}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(labels)},
         # Headless: one pod, reached by its own address. `targetPort` names the
         # container port rather than repeating a number -- without it a Service
         # port defaults to targeting itself, so every one of these pointed at a
         # port nothing serves.
         "spec": {"selector": dict(labels), "clusterIP": "None",
                  "ports": [{"name": port, "port": published[port],
                             "targetPort": port}
                            for port in ("api", "websocket", "gateway")]}},
        {"apiVersion": "apps/v1", "kind": "StatefulSet",
         "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(labels)},
         "spec": {
             "serviceName": name,
             "replicas": 1,
             "selector": {"matchLabels": dict(labels)},
             "template": {
                 "metadata": {"labels": dict(labels),
                              "annotations": {"home-stack/config-hash": config_hash}},
                 "spec": {
                     # Nothing here talks to the API server.
                     "automountServiceAccountToken": False,
                     # The image runs as nanobot (1000) and checks it can write
                     # its state directory before it starts -- it exits with an
                     # explanation rather than failing later and obscurely.
                     # fsGroup makes the volume group-writable by that gid,
                     # which is what the compose path's init container does
                     # with a chown.
                     "securityContext": {"runAsUser": 1000, "runAsGroup": 1000,
                                         "fsGroup": 1000},
                     "containers": [{
                         "name": "nanobot",
                         "image": image_ref(cfg),
                         # Never `Always` with a locally-built tag: the node
                         # would try the registry for an image it already has
                         # and fail closed on a network it does not need.
                         "imagePullPolicy": "IfNotPresent",
                         # The image ships bubblewrap and chromium and the
                         # assistant shells out to both, which is why the two
                         # compose files grant this. A pod without it looks
                         # healthy -- /health is served by the process itself --
                         # and fails only when a tool is used.
                         "securityContext": {
                             "capabilities": {"add": ["SYS_ADMIN"]},
                             "seccompProfile": {"type": "Unconfined"}},
                         # The image is ENTRYPOINT ["entrypoint.sh"] plus
                         # CMD ["status"], and the two runtimes disagree about
                         # what that means. Compose's `entrypoint:` key *clears*
                         # the image CMD, so the compose path runs
                         # `entrypoint.sh` and gets the server. Kubernetes'
                         # `command:` does not clear `args`, and an empty
                         # `args: []` is dropped on the way in -- so the pod ran
                         # `entrypoint.sh status`, printed the provider list and
                         # exited 0. That reads as CrashLoopBackOff with no
                         # error anywhere in the log, because there is no error.
                         #
                         # So say what the no-argument branch of entrypoint.sh
                         # does, rather than relying on an absence Kubernetes
                         # cannot express.
                         "command": ["entrypoint.sh"],
                         "args": ["gateway", "--api-port", str(API_PORT)],
                         "env": [
                             {"name": "HOST_IP",
                              "valueFrom": {"fieldRef": {
                                  "fieldPath": "status.hostIP"}}},
                         ] + [{"name": k, "value": v}
                              for k, v in sorted(hostward.items())],
                         "envFrom": [
                             {"configMapRef": {"name": name}},
                             {"secretRef": {"name": SHARED_SECRET}},
                             {"secretRef": {"name": name}},
                         ],
                         "ports": [{"name": port, "containerPort": served[port]}
                                   for port in ("api", "websocket", "gateway")],
                         # What compose caps this at. Requests as well as
                         # limits, because a pod with no request is a pod the
                         # scheduler cannot place on the machine with room for
                         # it -- which is half the reason for doing any of this.
                         "resources": {
                             "requests": {"cpu": "100m", "memory": "512Mi"},
                             "limits": {"cpu": "1", "memory": "1Gi"}},
                         "volumeMounts": [
                             {"name": "state", "mountPath": "/home/nanobot/.nanobot"},
                             # Where the image already keeps config.json and
                             # where the entrypoint looks for the prompt files.
                             {"name": "files", "mountPath": "/etc/nanobot",
                              "readOnly": True},
                             # Executable, and on PATH: the assistant shells out
                             # to this to send a notification.
                             {"name": "files", "mountPath": "/usr/local/bin/ntfy-send",
                              "subPath": "ntfy-send", "readOnly": True},
                         ],
                         # The deploy verify allows 180s for a cold start --
                         # MCP connections and the model client. A liveness
                         # probe that kills a slow start is a restart loop that
                         # looks like a crash, so startupProbe carries the wait
                         # and liveness only watches a *running* instance.
                         "startupProbe": dict(deepcopy(probe), periodSeconds=10,
                                              failureThreshold=30),
                         "readinessProbe": dict(deepcopy(probe), periodSeconds=15,
                                                failureThreshold=3),
                         "livenessProbe": dict(deepcopy(probe), periodSeconds=30,
                                               failureThreshold=4),
                     }],
                     "volumes": [{
                         "name": "files",
                         "configMap": {"name": CONFIG_MAP, "defaultMode": 0o555},
                     }, {
                         # The claim, not a hostPath. An earlier draft rendered
                         # both and mounted the hostPath, which left the PVC
                         # Pending forever -- an object nothing uses, which is
                         # the decorative-declaration shape this deployer has
                         # been bitten by twice.
                         #
                         # It also has to be the claim for the thing this is
                         # for: a hostPath pins the pod to the node holding
                         # that directory, so the scheduler could never move it,
                         # and moving it is the point.
                         #
                         # Existing state does not come across by itself. That
                         # is a migration, and the backup tool is how: back up,
                         # verify it restores, then restore into the volume.
                         "name": "state",
                         "persistentVolumeClaim": {"claimName": name},
                     }],
                 },
             },
         }},
    ]
    return objects




def config_files() -> dict:
    """The assistant's own prompt files, as a ConfigMap.

    The compose path bind-mounts `services/nanobot/config` and the container
    seeds its workspace from what it finds. Without them it starts, warns that
    SOUL.md, AGENTS.md, TOOLS.md, HEARTBEAT.md and MORNING.md are missing, and
    exits -- which is what the first render did, because it carried the
    environment and not the files.

    Only the top level: `instances/` holds overlays for the shared house
    instance, and a member instance falls back to the base plus its own
    `config.<member>.json`, which is a top-level file.

    Not the staged copy: the compose path patches `config.json` from
    `assistant.models` and rewrites the assistant's name across `config/*.md`
    before it ships them, and none of that happens here yet -- a pod runs the
    checked-in defaults. That is the largest remaining gap between the two
    runtimes.

    About 54 KB. The cap is 256 KB rather than the 1 MB a ConfigMap holds,
    because `kubectl apply` writes the whole object into a
    `last-applied-configuration` annotation and annotations are capped at
    262144 bytes -- so the real ceiling for this code path is the smaller one,
    and the error when it is hit names annotations rather than these files.
    """
    src = ROOT / "services" / "nanobot" / "config"
    data = {f.name: f.read_text(encoding="utf-8")
            for f in sorted(src.iterdir()) if f.is_file()}
    total = sum(len(v.encode()) for v in data.values())
    if total > 200_000:
        raise D.DeployError(
            f"{src} is {total/1000:.0f} KB and `kubectl apply` caps an "
            f"object's annotations at 256 KB. Bake it into the image instead.")
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CONFIG_MAP, "namespace": NAMESPACE},
            "data": data}


def namespace() -> dict:
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE}}


def shared_secret(shared: dict) -> dict:
    """The keys every member's pod mounts.

    Built from what `env_for` decided is shared, so the two cannot disagree --
    notably `PROXY_SHARED_SECRET`, which every member's `HOMECORE_PROXY_TOKEN`
    is derived from and which no member's container may hold.
    """
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SHARED_SECRET, "namespace": NAMESPACE},
            "stringData": dict(sorted(shared.items()))}


def objects_for(cfg: dict, unit: dict, spec: dict, secrets: dict,
                members: list[str]) -> list[dict]:
    """Every object the cluster needs, in apply order.

    One function so that what `render` prints and what `apply` sends cannot
    drift: the module's whole guarantee is that they are the same stream.
    """
    shared = env_for(members[0], cfg, unit, spec, secrets)[2] if members else {}
    objects = [namespace(), config_files(), shared_secret(shared)]
    for member in members:
        objects += render(member, cfg, unit, spec, secrets)
    return objects


def _yaml(objects: list[dict]) -> str:
    import yaml as _y
    return "\n---\n".join(_y.safe_dump(o, sort_keys=False) for o in objects)


def _members(cfg: dict, argv: list[str]) -> list[str]:
    known = D.member_ids(cfg)
    asked = [a for a in argv if not a.startswith("-")]
    unknown = [a for a in asked if a not in known]
    if unknown:
        raise D.DeployError(
            f"not a member of this household: {', '.join(unknown)}. "
            f"`services.nanobot.members` lists {', '.join(known)}.")
    return asked or known


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.split("\n\n")[1].strip())
        return 0
    verb, rest = argv[0], argv[1:]
    try:
        if verb == "status":
            return subprocess.run(
                ["kubectl", "-n", NAMESPACE, "get", "statefulset,pod,pvc",
                 "-o", "wide"]).returncode
        if verb not in ("render", "apply"):
            D.out.fail(f"unknown: {verb}. render | apply | status")
            return 2

        # `out.warn` prints to stdout, and `render` writes the YAML a person
        # redirects to a file -- a permissive mode on the secrets file was
        # enough to put a warning line above the first `apiVersion:` and make
        # the file unparseable. Everything that builds the objects says what it
        # has to say on stderr; only the document stream reaches stdout.
        with contextlib.redirect_stdout(sys.stderr):
            cfg = _cfg()
            secrets = D.load_secrets(D.SECRETS)
            spec = _spec(D.load_yaml(D.MANIFEST))
            unit = _unit(spec)
            objects = objects_for(cfg, unit, spec, secrets, _members(cfg, rest))

        if verb == "render":
            print(_yaml(objects))
            return 0

        D.out.step(f"applying {len(objects)} object(s)")
        secrets = [o for o in objects if o["kind"] == "Secret"]
        others = [o for o in objects if o["kind"] != "Secret"]

        # Apply Secrets server-side so kubectl does not store their values in
        # the last-applied-configuration annotation.
        if secrets:
            D.out.step(f"applying {len(secrets)} Secret object(s) server-side")
            result = subprocess.run(
                ["kubectl", "apply", "--server-side", "--field-manager=home-stack", "-f", "-"],
                input=_yaml(secrets), text=True, capture_output=True)
            print(result.stdout.rstrip())
            if result.returncode != 0:
                D.out.fail(result.stderr.strip())
                return 1

        if others:
            result = subprocess.run(["kubectl", "apply", "-f", "-"],
                                    input=_yaml(others), text=True,
                                    capture_output=True)
            print(result.stdout.rstrip())
            if result.returncode != 0:
                D.out.fail(result.stderr.strip())
                return 1
        # Not "applied, therefore working": `kubectl apply` returning 0
        # means the API server accepted the objects, and the plan's own
        # history records three fixes that never reached a pod stuck in
        # CrashLoopBackOff. Wait for the rollout, and let that fail.
        D.out.step("waiting for the rollout")
        for obj in objects:
            if obj["kind"] != "StatefulSet":
                continue
            name = obj["metadata"]["name"]
            rollout = subprocess.run(
                ["kubectl", "-n", NAMESPACE, "rollout", "status",
                 f"statefulset/{name}", "--timeout=300s"],
                capture_output=True, text=True)
            if rollout.returncode != 0:
                D.out.fail(f"{name}: {rollout.stderr.strip() or rollout.stdout.strip()}")
                return 1
            D.out.ok(f"{name} ready")
        D.out.ok("applied, and every instance answered its probe.")
        return 0
    except D.DeployError as exc:
        D.out.fail(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
