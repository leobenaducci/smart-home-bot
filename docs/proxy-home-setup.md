# Home-side setup — make chat live

The VPS proxy is deployed and serving the login page over TLS. What remains is
on the **hub**: a reverse tunnel, so the proxy has a house to forward to.

Everything the tunnel needs, this stack already has. `cloud.vps` says where the
VPS is, which account to reach it as and which port to bind there; the key is
the one on the admin page's Access screen; and the far end is the portal, which
is `services.home-core.port` on this machine.

`PROXY_SHARED_SECRET` is the value already in `secrets/smart-home-bot.env` —
the same one the portal holds. There is no separate VPS credential.

---

## 1. Reverse tunnel

A **systemd user unit**, not a system one, and plain `ssh` rather than
`autossh`. Both for the same reason: neither needs root, and this stack's whole
posture is that a household should not have to become an administrator of its
own machine to run it. `loginctl show-user $USER -p Linger` must say `yes` for
a user unit to start at boot; it usually does, and `loginctl enable-linger` is
the one command here that wants sudo.

What autossh adds over plain ssh is noticing a connection that has died but is
still open. `ServerAliveInterval=30` with `ServerAliveCountMax=3` already does
that — three missed probes and ssh exits — and `Restart=always` brings it back.

`ExitOnForwardFailure=yes` matters more than it looks. Without it, an ssh that
cannot bind the remote port stays connected forwarding nothing, and the VPS
reports a healthy proxy with no house behind it.

```ini
# ~/.config/systemd/user/home-stack-tunnel.service
[Unit]
Description=Reverse tunnel: the VPS proxy's upstream is this house
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N \
  -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=accept-new \
  -o IdentitiesOnly=yes -i <paths.config>/ssh/id_ed25519 \
  -R 127.0.0.1:<cloud.vps.tunnel_port>:127.0.0.1:<services.home-core.port> \
  <cloud.vps.user>@<cloud.vps.host>
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now home-stack-tunnel.service
```

**The far end is HTTPS.** The portal serves TLS and nothing else, so the proxy
dials `https://127.0.0.1:<tunnel_port>` with `VERIFY_UPSTREAM_TLS: "0"`. A
tunnel carries bytes rather than a protocol: what arrives on that port is
whatever the portal speaks. And the certificate on the far end is the house's
own, for the house's own name, reached at 127.0.0.1 — nothing a verify could
check would mean anything. The tunnel is the identity.

## 2. Checking it

```bash
curl -s https://<cloud.vps.domain>/healthz/upstream
# {"status":"ok","upstream":"https://127.0.0.1:21100"}
```

`no-upstream` there means the proxy is fine and the tunnel is not: the VPS is
answering and has nothing to answer *with*. That is the one line worth putting
in front of somebody who says "the house is down from outside", because it
tells them which end to go to.

## 3. The portal needs nothing

`PROXY_SHARED_SECRET` reaches home-core through the manifest like every other
credential, so there is no step here and no `.env` to edit by hand. That
instruction survived from before the deployer owned the environment; following
it now writes a file the next deploy replaces.

---

## 4. End to end

Open `https://<cloud.vps.domain>` from a phone that is *not* on the house
network — mobile data, not wifi. Log in with a household account and you should
land in the assistant's chat.

From wifi it may not prove anything: a resolver on the LAN can answer that name
with an address inside the house, which is a reasonable thing to set up and
means the request never leaves the network. If it works on wifi and not on
mobile data, the split horizon is hiding a broken tunnel.

## What replaces what

This describes the tunnel only. Everything on the VPS itself -- the proxy, its
state, the vhost -- is `./home-stack deploy cloud-proxy`, which needs
`cloud.vps.managed` on and an account there with a shell and the docker socket.
See `cloud.vps` in `config/home-stack.example.yml`.
