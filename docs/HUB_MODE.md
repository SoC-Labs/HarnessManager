# Hub mode: Harness Manager with an fpgahub token, no SSH

**Status (2026-09-24, team T8):** the REST transport, the event stream and the `via = "hub"` planner are built and tested against a fake hub that is checked against fpgahub v0.3.0's own app. None of it has run against the real hub yet. Wiring it into the pack and the daemon needs the CCRs T8-1 to T8-5; until they land, `boards.toml` with `url` is refused as an unknown key.

Today Harness Manager reaches the lab hub (fpgahub on mapstone-dev) over SSH: `ssh HUB 'sg fpga -c "fpgahub …"'` for leases and shares, `ssh -N -L` for the board's ports. Hub mode replaces the first half with fpgahub's own REST API and its event stream, so a user with an **fpgahub token** and no SSH account can lease the board, see the queue and read the hub's shares. The second half, the board's own TCP ports, needs a route through the hub, which the lab hub does not offer today (see [The data plane](#the-data-plane)).

Code: `src/harness_manager/transports/hub_rest.py` (the client), `hub_events.py` (the SSE stream), `hub_reach.py` (the data plane).

## Named hubs (lane SET-HUBS, 2026-09-25)

A hub can be a named setting instead of a table repeated in every board. **No hub at all is still the default**: a board on your desk or your network needs none, and nothing below runs, reads or probes until a hub is configured.

```toml
# settings.toml (beside boards.toml; `harness-manager hub add` writes it)
[hubs.lab]
host = "mapstone-dev.ecs.soton.ac.uk"   # SSH: your lab account ("local" on the hub itself)
group = "fpga"                           # the fpgahub socket's group ("" = no sg)
jump = ""                                # an SSH jump host on the way to the hub (ssh -J)
holder = ""                              # the lease holder ("" = harness-manager-<user>@<host>)
lease_ttl = "1h"                         # also request_ttl (2h) and queue_timeout (1h)
stage_dir = ".cache/harness-manager/hub-sd"  # the hub SD door's staging dir (see below)

[hubs.remote]
url = "https://mapstone-dev.ecs.soton.ac.uk:7246"   # REST: a token (7245 is mTLS)
token = "store"                          # store (default) | file:PATH | env:VAR | fpgahub-login
ca_file = "~/lab-hub-ca.pem"
```

```toml
# boards.toml
[boards.lab]
match = ["192.168.10.101"]
via = "hub"
hub = { use = "lab", target = "mps3_01_pl" }
```

- **Which keys go where.** The board keeps `use`, `target`, `board`, `shares`, `baud` and `start_shares`. `shares` is for the FPGA UART lanes (`tty_01..03`) only: see [The MCC behind a hub](#the-mcc-behind-a-hub-never-a-share-on-tty_00). Every other key belongs to the hub. A board table that has `use` and a hub key (`host`, `url`, `token_file`, …) is refused, naming the key.
- **Machine hubs.** The admin's `/etc/harness-manager/policy.toml` may define `[hubs.<name>]`. Every key the admin wrote is locked (`hub add --update` and `hub remove` exit 15), but each user sets their own token with `harness-manager hub token NAME --stdin`. A `token` in the policy file is dropped, because every user can read that file.
- **A named REST hub's token, first match wins:** `$FPGAHUB_TOKEN` (only when `$FPGAHUB_ADDR` is unset or names this hub), then your `hubs.<name>.token` (the secret store by default: the OS keyring, else a 0600 file; `file:PATH` is read strictly, so a file others can read is **refused**, not warned about), then the fpgahub login store for this hub. A token stored in a keyring this process cannot reach is an error (exit 7), never quietly replaced by the login store. An inline table keeps T8's order above (`token_file` first).
- **`jump`** reaches the lease and share commands (`ssh -J`), the share forwards and the board's tunnel.
- **`holder`, `lease_ttl`, `request_ttl`, `queue_timeout`** are what `lease acquire`, `lease request` and `POST /boards/{bid}/lease` ask for when the caller names none.

### The `hub` verb

```
harness-manager hub list
harness-manager hub add lab --ssh mapstone-dev.ecs.soton.ac.uk [--jump HOST] [--lease-ttl 30m]
harness-manager hub add remote --url https://HUB:7246 --ca-file ~/lab-hub-ca.pem --token-stdin
harness-manager hub token NAME --stdin | --ref file:PATH | --clear
harness-manager hub test NAME [--target mps3_01_pl]
harness-manager hub targets NAME [--add TARGET [--board KEY]]
harness-manager hub adopt BOARD [--as NAME]
harness-manager hub remove NAME [--force]
```

**`hub test`** proves, in order, and stops at the first failure with the reason and the next step: **config → reach → auth → group → targets → target**. It never takes, joins or releases a lease, and never starts a share.
- Over REST it makes three reads: `GET /health`, `/whoami`, `/groups`.
- Over SSH it makes **one** round trip with pyverify's own quoting, plus `ConnectTimeout=10`: `echo HM-TEST:login; id -Gn; echo HM-TEST:ids; sg fpga -c 'fpgahub board list --json'`. The markers show how far it got:
  - a dead host or DNS failure stops at **reach**;
  - `Permission denied (publickey)` or a host-key failure stops at **auth**;
  - a login without the `fpga` group stops at **group**, with the admin's `usermod -aG fpga` command;
  - no fpgahub on the host stops at **targets**.
- The **target** step checks that the targets of the boards using the hub (or `--target`) are on offer.

**`hub targets NAME --add TARGET`** writes a board for a target the hub offers. The board gets `hub.use`, `via = "hub"`, `match` (the target's `board_ip`) and `name` (its description). These come from `GET /targets/{t}` over REST, or `fpgahub target show T` over SSH, which v0.3.0 prints as JSON.

**`hub adopt BOARD`** is "Make this a hub": the board's inline `hub` table becomes `[hubs.<name>]` plus `hub.use`.
- The default name is the host's first label.
- A `token_file` becomes `token = "file:PATH"`.
- `via = "ssh:<the same host>"` becomes `via = "hub"`, which reaches an SSH hub through the same tunnel.
- Comments are kept, and `boards.toml` is copied to `boards.toml.bak-<date>` first.
- A second board with the same inline table reuses the hub. A name taken by a different definition is refused.
- Run again, it does nothing.

## Setup for a user with a token

1. **Get a token with the `write` role.** Two ways:
   - ask the hub admin to run `fpgahub token create <name> --role write --owner <you>`;
   - or sign in to the hub's web UI and mint your own with `POST /api/v1/tokens/self`.

   A `read` token can see the lease but cannot take it. Force-release needs an `admin` token (see [Limits](#limits)).
2. **Get the hub's CA certificate** from the hub admin. The web listener's certificate is `/etc/fpgahub/tls/cert.pem`, signed by `/etc/fpgahub/tls/ca.pem`. Save the CA file as `~/lab-hub-ca.pem`.
3. **Store the token.** Choose one:
   - Write it to a file only you can read:
     ```
     umask 077; printf '%s\n' 'YOUR-TOKEN' > ~/.config/harness-manager/hub.token
     ```
   - Or log in with the fpgahub CLI:
     ```
     fpgahub login --addr mapstone-dev.ecs.soton.ac.uk:7246 --token YOUR-TOKEN
     ```
     Harness Manager then reads `~/.config/fpgahub/config.toml`, but only for the hub that file names.
4. **Add the hub to `boards.toml`** (in `~/.config/harness-manager/`):
   ```toml
   [boards.lab]
   match = ["192.168.10.101"]
   hub = { url = "https://mapstone-dev.ecs.soton.ac.uk:7246", target = "mps3_01_pl",
           token_file = "~/.config/harness-manager/hub.token", ca_file = "~/lab-hub-ca.pem" }
   ```
5. **Check it:** `harness-manager lease show 192.168.10.101`. It prints the holder and the queue, or "not leased". A 401 means the hub did not accept the token, and the message says which file it came from.

**Use port 7246, not 7245.** Port 7246 is fpgahub's web listener (`listen_web = "0.0.0.0:7246"`, `web_tls = true` in the lab config). Port 7245 is the mTLS peer listener. It demands a client certificate during the TLS handshake, so a token alone never gets in. A workstation that does have a hub-signed peer certificate can use 7245 with `cert_file` and `key_file`.

### The `hub` keys

| Key | Meaning |
|---|---|
| `url` | fpgahub's API: `https://HOST:PORT`, optionally ending in `/api/v1`. Plain `http` is refused except to loopback, because the token would cross the network readable. A URL with credentials, a query or another path is refused. |
| `target` | the fpgahub target name (`mps3_01_pl`); the same key as in SSH mode |
| `token_file` | one line holding the token; chmod 600 (a readable file is logged as a warning, never its content) |
| `ca_file` | the CA that signed the hub's certificate |
| `cert_file`, `key_file` | an mTLS client pair (both or neither) |
| `insecure` | skip hostname verification only; the certificate chain is still checked |
| `events` | stream the hub's events while the board is open (default true) |
| `direct` | the data plane: `auto` (default), `never` (always the SSH tunnel), `always` (trust the route) |
| `board` | the physical board, when you want to skip asking the hub (`mps3_01`) |
| `timeout_s` | per-request timeout (default 30) |
| `host` | with `url`: the SSH host for the data-plane fallback. Without `url`: SSH mode, exactly as before |

**Which transport is used:** with `url`, REST; with `host` alone, SSH; with both, REST, and `host` is then used only for the SSH tunnel fallback.

**Where the token comes from, first match wins:**
1. `token_file`;
2. `$FPGAHUB_TOKEN`, used only when `$FPGAHUB_ADDR` is unset or names this hub;
3. the fpgahub login store, used only when its `addr` names this hub.

The token is sent only in the `Authorization` header. It never appears in a URL, a log line, an error message or a `repr`; the tests check all four.

## What is different from SSH mode

| | SSH mode | Hub mode (REST) |
|---|---|---|
| Who the hub thinks you are | the unix account, `admin` on the socket | the token's owner, `owner@mapstone-dev`, with the token's role |
| Holder name | fpgahub ignores `--holder` | the same: `Lease.holder` is the principal, e.g. `alice@mapstone-dev` |
| Waiting in the queue | re-run `lease acquire` every 20 s | the hub's own long poll, `GET /lease/wait`, in 5 s slices (a cancel takes effect within a slice); the queue place is re-asserted every 20 s |
| Leaving the queue | `fpgahub lease cancel` | `DELETE /targets/{t}/queue`; on a multi-target board it follows fpgahub's 409 `board_required` to `DELETE /boards/{b}/queue` |
| Force-release | always allowed (the socket is admin) | **admin tokens only**; a write token gets REFUSED and `can_revoke()` says so in advance |
| Request notes (message, "keep for N min") | files in `/tmp/harness-manager-lease/` on the hub | **degraded mode**: no messages (see below) |
| Lease changes | polled every 10 s | pushed by the hub, arriving in well under a second (`hub.event`) |
| Who force-released me, and why | see the gap below | from the event stream |

### The lease-history gap (it affects SSH mode too)

Measured on fpgahub v0.3.0's own app (`tests/fakes/t8_fpgahub_v030_golden.json`): after an admin revoke, `GET /targets/{t}/lease/history` and `/boards/{b}/lease/history` return a `lease.revoked` record **without `by` or `reason`**. The cause is that `LeaseEventRecord` has no such fields. They **never** return `lease.admin_revoked`, because that record has no `board` key and both routes filter on it.

`fpgahub target lease-history --json` calls the same route, so the victim path in `docs/LEASE_REQUESTS.md` ("find the admin_revoked entry, report {by, reason, at}") finds nothing in SSH mode either.

Where `by` and `reason` do exist:
- **The event stream.** `lease.admin_revoked {by, reason, prior_holder, members, chassis}` and `lease.revoked {board, holder, reason}`. The REST client keeps what the stream saw and merges it into `lease_history()`.
- **Over SSH:** the hub's audit file `/var/log/fpgahub/lease_events.jsonl` (mode 0644), which keeps every field.

A second limit: a revoke with nobody waiting reads as `expired` at the next heartbeat, the same answer as a TTL expiry. Only the stream, or the `lease.revoked` history record, tells the two apart.

### Request notes over REST: degraded mode

REST has no file store, so a request is **its queue entry alone**:

| Call | What it does over REST |
|---|---|
| `put_request` | Keeps the note locally; the message never reaches the holder. |
| `list_requests` | Returns your own notes, plus every other waiter as a request with an empty message. The id is `q-<holder>`. `created_at` is the hub's `lease.queued` time when the stream saw it, otherwise when this client first saw the waiter. `deadline_at` is 120 s later. |
| `put_answer` | Raises `UnavailableError`: a "keep" answer cannot reach the requester. "Release" still works, because it is a lease release. |
| `get_answer` | Always None. |

`client.notes_supported` is False and `client.notes_reason` gives the reason, so the UI can hide the message field and the Keep buttons.

The requester's 2-minute timer runs from its own `created_at`. The holder's copy runs from the hub's queue time or from when it first saw the request, so the two can differ by one poll.

**Proposal to fpgahub (lifts the degraded mode):** lease requests as a hub feature. The hub then stores the message and enforces the 2-minute rule itself.

| Route | Who | Does |
|---|---|---|
| `POST /boards/{b}/lease/requests` `{message}` | write; the caller must be queued | stores `{id, by, message, created_at, deadline_at}` next to the queue entry, 4 KiB max; emits `lease.requested` |
| `GET /boards/{b}/lease/requests` | read | lists them, answers included |
| `POST /boards/{b}/lease/requests/{id}/answer` `{answer: release\|keep, minutes, message}` | the current holder | `release` releases (and promotes); `keep` records the answer; emits `lease.request_answered` |
| `DELETE /boards/{b}/lease/requests/{id}` | the requester | withdraws it; leaving the queue also drops it |
| `POST /boards/{b}/lease/force` `{request_id}` | **write** | revokes only when the request is at the head, its deadline has passed, and it has no answer or its keep has run out. Otherwise 409 with the reason. Audited as `lease.admin_revoked` with `by` = the requester. |

The last route is the important one: a developer with a write token could then take the board the way `LEASE_REQUESTS.md` describes, without an admin token, and the hub would enforce the timer. The client would detect the feature by the route being present in `/openapi.json`, and set `notes_supported`.

## The data plane

The session needs the board's TCP ports:

| Port | Use |
|---|---|
| 6900 | control |
| 6910 | push |
| 6921 | JTAG remote_bitbang |
| 6930-6932 | consoles |
| 2542 | XVC |

UDP 69 (TFTP) and 6899 (identify) also exist. The board, 192.168.10.101, sits on a point-to-point /24 whose only other host is the hub's `mps3_01_pl` NIC (192.168.10.1).

### Options, best first

1. **The SSH tunnel.** This is today's method and still the only working one. It needs an SSH account on the hub. `hub.host` next to `hub.url` keeps it as the fallback. The ethernet gate never applies to it: the forwarded connections start on the hub itself (the OUTPUT chain), not in FORWARD.
2. **Routing through the hub (`via = "hub"`).** An external user reaches 192.168.10.x only when all of these hold:
   1. **A route on the client.** On campus: `ip route add 192.168.10.0/24 via <mapstone-dev's campus IP>`. Off campus, a VPN into the campus network first: 192.168.10.x is private and never crosses the internet.
   2. **The hub forwards.** fpgahub writes the board NIC's networkd file with `IPForward=no` (`network.py render_networkd`). The admin must enable forwarding on both NICs, and let the host firewall pass it.
   3. **The board can answer.** The shell has no gateway on its /24, so the hub must masquerade: `nft add rule ip nat postrouting oifname "mps3_01_pl" masquerade`, or the equivalent firewalld rule. The gate checks the source address in FORWARD, before the masquerade rewrites it, so the two work together.
   4. **The ethernet gate admits you.** Set `[boards.mps3_01.targets.pl.access] gate_ethernet = true`, then run `fpgahub nftables apply`. With the gate on, the forward chain drops everything on the board NIC except the current lease holder's IP: the address the hub saw on the **acquire** request. So:
      - the lease must be acquired over REST from the machine that will use the board (an acquire over SSH or the socket records no IP);
      - everyone behind one NAT address shares the gate, as fpgahub's own `LEASE_GATES.md` warns.

      With the gate off there is no chain at all, and anyone with a route could reach the board.
3. **TTY shares directly.** fpgahub's shares listen on `0.0.0.0:<share_port_base + i>` (12000 up) on the hub. A REST-only client (CCR T8-1) connects to `HUB:<port>` directly when the hub firewall lets it. The share port has **no authentication**, and the first client to connect holds the write slot. Opening those ports to campus lets anyone on campus type into a console, so a firewall scoped to lease holders (or the gate idea extended to shares) must come first. This is for the FPGA UART lanes only: the MCC's `tty_00` is never shared (above).
4. **A VPN or WireGuard peer per user, ending on the hub.** It solves the off-campus route and the NAT caveat together: each user gets a unique tunnel IP for the gate. The Linux-harness WireGuard scaffold is the board-side half of this idea, not this.
5. **An authenticated tunnel in fpgahub (proposal):** `GET /api/v1/targets/{t}/tunnel?port=6900` upgraded to a WebSocket and gated on the lease. It needs no routing and no firewall change, and works through NAT. It is the cleanest option for external users; `transports/__init__.py` has always anticipated it.

**What the lab has today.** fpgahub's own record of the live config (`tests/fixtures/live_shape_config.toml` in v0.3.0) has:
- `gate_ethernet = false` and `share_tty = false` for `mps3_01_pl`;
- `listen_web = "0.0.0.0:7246"` with TLS.

Together with `IPForward=no`, that means options 2 and 3 do not work today, and only the SSH tunnel does. **None of this can be proven without looking at the real hub** (checklist below).

### What `via = "hub"` does (CCR T8-5)

`via = "hub"` in boards.toml (or `--via hub`) makes the pack plan the route before it connects. `hub_reach.plan_reach` checks, in order:
1. this machine has a specific route to the board (Linux `/proc/net/route`; the default route does not count);
2. the hub reports `access.gate_ethernet = true` for the target (`GET /targets/{t}`);
3. this credential holds the lease (`GET /whoami` and `GET /targets/{t}/lease`);
4. the board answers a ping through that route. A ping, because it never opens one of the board's single-client TCP ports.

When all four pass, the session talks to `192.168.10.101:6900…` directly: a `DirectReach`, shown by `GET /boards/{bid}/tunnel` as `mode: direct` with the plan. When any check fails, it opens the SSH tunnel to `hub.host`, and the plan's reason names the first missing piece. With no `hub.host` it fails with that reason.

`direct = "always"` skips the checks, for a user who knows the path works. `direct = "never"` always tunnels. On macOS and Windows the route check reads nothing today, so `auto` falls back to the tunnel; use `always` there if the route exists.

## The MCC behind a hub: never a share on `tty_00`

Harness Manager never starts or uses an fpgahub share on the MCC console `tty_00`, on any board
(the Linux lead and the lead, 2026-09-26). The paced REBOOT works only with exactly one reader on
`tty_00`; a share is a reader (fpgahub's broker holds the tty), it cannot be stopped on its own
(`share stop` stops every share, and Harness Manager never runs it), and while one exists the
platform's own tools refuse to REBOOT (pyverify's writer, rc 3/4).

So every MCC operation of a hub board runs ON the hub, through the hub's SSH runner
(`ssh HUB 'sg fpga -c …'`; the console is `root:fpga`):

| Operation | What runs on the hub |
|---|---|
| `mcc TARGET reboot`, **Power > Board reboot** | pyverify's paced REBOOT writer (`bootrate.HUB_MCC_REBOOT_PY`): refuses a second reader and a missing `Cmd>`, types REBOOT at 100 ms a character, captures the boot log (the result names the `.bit` the MCC loaded) |
| the REBOOT after a hub SD write (`harness install --door hub`) | pyverify's `sd field --already-written`: the journal witness of that sha, the one-reader scan, then the same paced REBOOT |
| `mcc TARGET temp` / `osc`, `mcc TARGET cmd "CFG R …"` | Harness Manager's hub-side reader: the same one-reader rule and `Cmd>` check; reads only (no `CFG W`, no `DEBUG`/`EXIT` left open) |

- **Python on the hub.** pyverify's code runs under a Python 3.10+ found on the hub
  (`python3.11` first, then 3.12, 3.13, 3.10, then fpgahub's `/opt/fpgahub/bin/python3.11`); the
  lab hub's system `python3` is 3.6 and is never used for it. With none, a REBOOT refuses and says
  so. The reads are Harness Manager's own script, proven under Python 3.6.
- **Straight after an SD write** the MCC answers a bare CR with only `\r\n` for a few seconds.
  The REBOOT is tried again, up to 3 times 5 s apart, only while that is the answer. The SD write
  is never retried.
- **Not during a card job.** Every REBOOT and every Linux harness restart first asks the reset
  guard (lane SLOT-TIMING) and refuses while the user microSD is being written or read back.
- `share start TARGET mcc` (or its `/dev/…/tty_00` path) is refused before the hub is asked. An old
  `shares = { mcc = … }` entry in boards.toml is ignored: it only names the MCC's path.
- A REST-only hub (a token, no SSH login) cannot reach `tty_00`: the board has no controller
  there, and reboots need the Debug USB or an SSH account on the hub.

## The hub SD door's staging directory (`hubs.<name>.stage_dir`)

The hub SD door (lane HUB-SD) writes a harness base to the config SD through fpgahub on an SSH hub. It uploads the `.bit` over ssh into a staging directory on the hub, checks its sha256 there, and asks fpgahubd to program that path with `--method sd`. **fpgahubd reads the file itself**, so the directory must be readable by the daemon, not only by you.

- **Default:** `.cache/harness-manager/hub-sd`, relative to your home on the hub (`~/.cache/harness-manager/hub-sd/<sha256>.bit`). Unset, nothing changes.
- **Set it** with `harness-manager config set hubs.lab.stage_dir /srv/fpga/hm-stage` (an absolute path, or one relative to your hub home; no spaces, `~` or `..`). A REST hub ignores it: its `.bit` goes to the hub's bitstream repository.
- **When the default cannot work:** fpgahub's own unit file (`systemd/fpgahubd.service`, v0.3.0) sets `ProtectHome=yes` and `PrivateTmp=yes`. With those, fpgahubd sees an empty `/home` and its own `/tmp`, so `bitstream not found` names your staged file. The stage dir must then be **outside `/home` and `/tmp`**: a group-`fpga` directory the hub's admin creates once, for example:
  ```bash
  sudo install -d -m 2770 -g fpga /srv/fpga/hm-stage     # or /var/lib/harness-manager-stage
  ```
  `ProtectSystem=strict` only makes paths read-only for the daemon, and it only needs to read.
- **Which case the lab hub is in:** `docs/HIL_LINUX.md` §F1 (the unit's `ProtectHome`) and §F4 (the read probe) answer it.
- **Unchanged:** the one `program --method sd --force` request is never retried mid-write. The stage dir only changes where the file waits.

## Events

While a REST board is open, the daemon (CCR T8-2) holds one `GET /api/v1/events` stream per board. The token rides in the header, never the URL. The `types` filter asks for the lease and share events only. Two topics are published:
- `hub.event {type, ts, target, board, data}`: each event about this target or its board;
- `hub.stream {state: connecting|up|down|refused|closed, detail, url, reconnects}`: the stream's own state.

How the stream behaves:
- **Reconnect.** After a drop it reconnects with a 1, 2, 5, 10, 30 s back-off.
- **Resync.** After every (re)connect it drops the lease service's cached view. fpgahub keeps no replay buffer, so anything that happened during a gap is recovered by re-reading the lease.
- **Refused.** A 401/403 puts the stream in `refused` and it retries only every 30 s, so a revoked token does not hammer the hub.
- **Settles a lost lease at once (CCR T8-4).** `LeaseService.on_hub_event` heartbeats as soon as a `lease.revoked`, `lease.admin_revoked` or `lease.expired` event names our lease. The `lost`/`expired` state then arrives in about a second, instead of at the next heartbeat, which can be 20 minutes away.

fpgahub sends nothing while idle, so the stream has no read timeout; TCP keep-alive detects a dead hub.

## Limits

- **Force-release needs an admin token.** A write token cannot force; the UI should grey the button out with `can_revoke()`'s reason. The hub feature proposed above would lift this.
- **No request messages or "keep" answers over REST.** This is the degraded mode above.
- **History has no `by`/`reason` without the event stream.** It needs the stream to have been open at the time of the revoke.
- **The data plane still needs SSH on the lab hub today.** See the options above.
- **Timestamps are the hub's.** `expires_at` is fpgahub's ISO 8601 string, e.g. `2026-09-24T11:55:53.433256Z`.
- **The contract is pinned to v0.3.0.** `tests/unit/test_t8_contract.py` replays a transcript of the real v0.3.0 app against the fake. After a hub upgrade, regenerate the transcript with `tests/fakes/t8_record_fpgahub_golden.py` in a scratch venv; its docstring has the commands.

## Checking against the real hub (with david; read-only first)

Nothing below changes hub state until step 6. Steps 1-5 are safe while someone holds the board.

1. `curl -s https://mapstone-dev.ecs.soton.ac.uk:7246/api/v1/health` (anonymous). It should return `{"status":"ok","version":"0.3.0",…}`. This proves the web listener is reachable from srv03335 and which CA signs it (add `--cacert`).
2. With a **read** token: `GET /api/v1/whoami` (the role and `holder`), `GET /api/v1/groups` (is `mps3_01` one target or several?), and `GET /api/v1/targets/mps3_01_pl`:
   - `access.gate_ethernet` and `access.share_tty`;
   - `network.host_ip`.
3. `GET /api/v1/targets/mps3_01_pl/lease` and `/lease/history?limit=5`. Confirm the lease shape, and that a real revoke record has no `by`.
4. `curl -N` on `/api/v1/events?types=lease.heartbeat` for a minute while someone's session heartbeats. This proves SSE reaches srv03335, through any proxy, unbuffered.
5. On the hub, as david, read only:
   - `sysctl net.ipv4.ip_forward net.ipv4.conf.mps3_01_pl.forwarding`
   - `sudo nft list ruleset | grep -A3 fpgahub`
   - `ls -l /var/log/fpgahub/lease_events.jsonl`
   - `ss -ltnp | grep -E ':(7245|7246|120[0-9][0-9])'`

   These answer the data-plane questions, and whether the audit file is readable for the SSH-mode victim path.
6. **Only in a window david opens,** with a **write** token of his own and the board free: `harness-manager lease acquire`, `lease show`, `lease release` through REST. Never a revoke while anyone else holds the board.
