# MQTT conventions

The broker is shared by three services — `home-voice`, `home-cameras` and the
`mqtt` dashboard — which had drifted into five names for the broker, five
env-var schemes and two topic grammars. This is the agreed scheme, and it lives
here rather than in any one of them because it governs the whole stack. A
plugin that publishes follows it too.

## Broker

One machine, one name: **`mqtt.home:1883`**. `hub.home`, `192.168.1.10`,
`host.docker.internal` and `localhost` are all the same box — do not use them.

That is about not inventing a fifth name for one machine. It is not a reason to
write an address a container cannot use: `MQTT_BROKER` is supplied from
`{hosts.hub.from_container}` for a bridge-networked service and
`{hosts.hub.address}` for one on host networking, which are the same string
the moment the role has a real address. `--check-contract` refuses the other
combination — see "The address to use" in `docs/plugins.md`.

Configure with **`MQTT_BROKER`** (host only) and **`MQTT_PORT`**. In compose,
always the override form so the documented knob actually works:

```yaml
- MQTT_BROKER=${MQTT_BROKER:-mqtt.home}
- MQTT_PORT=${MQTT_PORT:-1883}
```

Anonymous, plaintext, no TLS. Enabling auth is a stack-wide change: the panel
firmware and the dashboard cannot currently supply credentials.

## Topic grammar

```
<prefix>/<domain>/<device-id>/<leaf>
```

- **prefix** — hyphenated, per application: `homecameras`, `home-voice`
- **domain** — plural noun: `cameras`, `motion`, `rooms`
- **device-id** — stable identity: a camera id, a room name, a MAC
- **leaf** — one of:

| Leaf | Direction | Retained | Meaning |
|---|---|---|---|
| `state` | service → world | **yes** | current state, survives reconnect |
| `set` | world → service | no | command |
| `event` | device → service, service → world | no | something happened |
| `ack` | service → device | no | receipt |
| `status` | service → world | **yes** | `{"online": bool}` liveness, backed by an MQTT will |

`#` is for debugging only. Subscribe with `+` at the level you mean:
`motion/#` also matches `motion/<cam>/objects`, which is how the camera agent
ended up firing twice per detection.

## QoS and retention

**QoS 1 everywhere.** State and status are retained; events and commands are not.

The broker runs `persistence false`, so retained state does **not** survive a
broker restart — treat `status` as the liveness signal, not the presence of a
retained `state`.

## Payloads

JSON objects. Identity key is **`mac`** (`device_mac` is accepted as an inbound
alias). Timestamps are **Unix epoch seconds**; the ESP32's `millis()` uptime is
not a timestamp and wraps after ~49 days.

Anything a service republishes carries **`"src"`** naming the publisher, so a
service subscribing to a topic it also publishes on cannot consume its own
messages.

## Live topics

```
homecameras/motion/{cam}                    ← cameras  → the assistant
homecameras/motion/{cam}/objects            ← cameras
homecameras/recording/{cam}                 ← cameras  → the assistant
homecameras/security/{cam}/state            ← cameras  → HA, the assistant   presence from *tracks*: person / vehicle / animal present, active, stationary
homecameras/security/{cam}/event            ← cameras  → HA                  one line per arrival or departure
homeassistant/binary_sensor/homecameras_{cam}_{person|any}/config   ← cameras (retained, re-announced)   HA MQTT discovery for the two occupancy sensors
home-voice/{room}/announce                  → panel    the announce skill
```

**Lights, switches and every other smart device are Home Assistant's**, and
they do not appear here. This stack used to run its own light service with a
`home-lights/` prefix and its own button topics; that went, and Home Assistant
speaks to the devices over whatever protocol each one uses. The assistant
reaches them through the Home Assistant MCP server, not over this broker.

## Still divergent

Deliberately not yet changed, because each needs a reflash or a coordinated
deploy:

- `homecameras` has no separator; the grammar above wants `home-cameras`.
- Button/light MACs disagree on colons (`AA:BB:...` vs `AABB...`).
- Cameras identify by IP *or* camera-id in the same topic position, and IPs put
  dots inside a topic level.
- HomeCameras uses `object_tags`/`tags` and `object_counts`/`counts` for the same
  data on two topics.

## Adding a publisher

1. Use `MQTT_BROKER`/`MQTT_PORT`, defaulting to `mqtt.home`/`1883`.
2. Follow the grammar above; `+` not `#`.
3. QoS 1. Retain state, never events.
4. Set a will on `<prefix>/status` and publish `{"online":true}` on connect.
5. Unique client id — a fixed one makes the broker evict the other session
   (rc=7 flapping) when two instances or a reconnect race overlap.
