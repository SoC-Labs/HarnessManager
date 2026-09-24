# Lease requests, force release and leaving the queue (frozen 2026-09-24)

david's requirements:
- Ask the Harness Manager session that holds a board's hub lease to give the board up.
- The holder can release it.
- With no answer within **2 minutes**, the requester may **force-release** it, after an "are you sure" confirmation.
- The previous holder's session is told who took the board.
- A requester can always **leave the queue**.

**Hard rule for every lane:** nothing in this work runs against the real hub. `fpgahub board lease revoke` kicks a real person. Tests use fakes only.

## How it works: the hub is the only meeting point

Two Harness Manager sessions never talk directly; they may be on different machines. Everything goes through what fpgahub already provides, reached with `ssh HUB 'sg fpga -c "fpgahub …"'` as `harness_manager_mps3/hub.py` already does:

| Need | Mechanism (fpgahub 0.3.0, source in `~/SoCLabs/fpgahub`) |
|---|---|
| Ask for the board | **Join the lease queue.** An acquire that queues is the request. `lease show` lists the queue (`position`, `holder`, `user`), so the holder's session sees who is waiting. |
| Say why, and reply "keep it for N minutes" | **Note files on the hub:** `/tmp/harness-manager-lease/<target>/`, a group-`fpga` directory (mode 2770). Request notes are `req-<id>.json` and answers `ans-<id>.json`. Writes are atomic (write a temp file, then `mv`), and each note is at most 4 KiB. `id` and `target` are `[A-Za-z0-9_.-]` only, and every value is shell-quoted. Notes older than 1 h are pruned by whoever lists. |
| Release | The holder's normal `lease release`. fpgahub then promotes the head of the queue. |
| Force-release | `fpgahub board lease revoke <board> --reason "<text>" --yes` (admin: the unix socket through `sg fpga`). fpgahub promotes the next FCFS waiter and writes `lease.admin_revoked {by, reason, prior_holder}` to the audit log. `<board>` is the physical board that owns the target (`mps3_01` for `mps3_01_pl`): take it from fpgahub, or from `hub.board` in boards.toml. |
| Tell the victim | Its heartbeat fails (lost), or `lease show` names another holder. It then reads `fpgahub target lease-history <target> --json`, finds the `admin_revoked` entry, and reports `{by, reason, at}`. |
| Leave the queue | `fpgahub lease cancel <target> --holder <principal>`, plus deleting our request note. |
| Who am I | fpgahub records a lease's holder as the caller's principal (`name@host`) and ignores `--holder`. Learn this client's principal once (`fpgahub whoami`, or from the first acquire) and use it for "is this lease or queue entry mine". **This also fixes today's bug:** Harness Manager saved the holder it asked for (`david-hm`), the hub showed `david@mapstone-dev`, so `mine` was false. |

## The rules

| Rule | Detail |
|---|---|
| **Timer** | A request has `created_at` and `deadline_at = created_at + 120 s`, both UTC ISO 8601 and written in the note. Both sides count down from the note, not from their own clocks. |
| **Holder's answers** | `release` releases now. `keep` takes `minutes` (5, 15, 30 or 60) and an optional `message`. |
| **Force available** | Only when all of these hold: the deadline has passed; there is **no answer**, or a `keep` whose minutes have run out; and the requester is **at the head of the queue** (a revoke promotes the head, and anyone else would get the board). Otherwise force is refused with the reason: 409 REFUSED, or 422 with the time left. |
| **Force needs `confirm: true`** | The UI shows "Are you sure? This kicks `<holder>` off mps3-01 now; anything they are running on the board is interrupted." |
| **Revoke reason** | `"force-released by <principal> via Harness Manager: no answer to a request made at <created_at>"`. |
| **Leaving** | Leaving the queue withdraws the request. Closing the board in the app while queued also leaves the queue (L1 already cancels an acquire on close). |
| **Polling** | A session watches its incoming requests every **10 s** while it holds a lease and the board is open. A requester watches its request and the answers every **10 s**. Each poll is one ssh call. |

## Interfaces (frozen: build against these; changes go through the lead)

### Hub client: `harness_manager_mps3/hub.py`, lane LR-A
```python
@dataclass(frozen=True)
class QueueEntry:
    position: int
    holder: str        # principal, "david@mapstone-dev"
    user: str
@dataclass(frozen=True)
class LeaseStatus:     # replaces or extends today's LeaseView
    held: bool
    holder: str
    user: str
    expires_at: str
    queue: tuple[QueueEntry, ...]
@dataclass(frozen=True)
class RequestNote:
    id: str
    by: str
    user: str
    host: str
    message: str
    created_at: str
    deadline_at: str
@dataclass(frozen=True)
class AnswerNote:
    id: str
    answer: str        # "release" | "keep"
    minutes: int
    message: str
    at: str
class HubClient:       # additions
    def principal(self) -> str: ...
    def lease_status(self) -> LeaseStatus: ...
    def board_id(self) -> str: ...               # the physical board that owns the target
    def lease_revoke(self, reason: str) -> dict: ...  # {"revoked": [...], "by": "..."}
    def lease_history(self, limit: int = 50) -> list[dict]: ...
    def put_request(self, note: RequestNote) -> None: ...
    def list_requests(self) -> list[RequestNote]: ...
    def delete_request(self, request_id: str) -> None: ...
    def put_answer(self, note: AnswerNote) -> None: ...
    def get_answer(self, request_id: str) -> AnswerNote | None: ...
```

### Lease service: `harness_manager/services/lease.py`, lane LR-B
```python
class LeaseService:    # additions; the existing acquire/release/view/track stay
    def request(self, board_id, hub, *, message="", ttl_s=7200, progress=None,
                cancel=None) -> dict: ...  # queue + note; blocks until held, answered-keep, or cancelled
    def respond(self, board_id, hub, request_id, answer, *, minutes=0, message="") -> dict: ...
    def force(self, board_id, hub, *, confirm: bool) -> dict: ...  # revoke, then our queued acquire is promoted
    def leave(self, board_id, hub) -> dict: ...  # {"left": bool}
    def view(self, hub) -> dict: ...  # extended, see the API below
```

## API: daemon routes in `daemon/hub_api.py`, lane LR-C

`GET /boards/{bid}/lease` adds these keys; the old ones are unchanged:
```
lease:  {target, holder, user, expires_at, mine} | null
queue:  [{position, holder, user, mine}]
request: {id, message, created_at, deadline_at, position,
          answer: {answer, minutes, message, at} | null,
          force_available: bool, force_reason: str} | null   # my outgoing request
incoming: [{id, by, user, host, message, created_at, deadline_at}]  # requests for my lease
taken:  {by, reason, at} | null        # the last time my lease was force-released
```

| Method and path | Body | Returns |
|---|---|---|
| `POST /boards/{bid}/lease/request` | `{message?, ttl_s?}` | 202 job `lease_request`. Phases: `queued`, `notified`, `answered`, `force-available`, `held`. The result is `{lease}`, or `{answered: {...}}` when kept. |
| `POST /boards/{bid}/lease/respond` | `{id, answer: "release"\|"keep", minutes?, message?}` | 200 `{ok}` |
| `POST /boards/{bid}/lease/force` | `{confirm: true}` | 202 job `lease_force`, whose result is `{lease}`. Before any revoke: 409 REFUSED (not available: not at the head, answered, not yours) or 422 USAGE (`confirm` missing). |
| `DELETE /boards/{bid}/lease/queue` | none | 200 `{left: bool}` (leave the queue and withdraw the request) |

**Events** (append to CONTRACTS):

| Topic | Payload | Who gets it |
|---|---|---|
| `lease.wanted` | `{id, by, user, host, message, deadline_at}` | the holder's session |
| `lease.answered` | `{id, answer, minutes, message}` | the requester's session |
| `lease.force_available` | `{id}` | the requester's session, at the deadline |
| `lease.taken` | `{by, reason, at}` | the holder's session, after a forced release |
| `lease.left` | `{}` | whoever left the queue |

## CLI: `cli/cmd_hub.py`, lane LR-C

| Command | What it does |
|---|---|
| `harness-manager lease request TARGET [--message M] [--ttl S]` | Waits; shows the answer and the countdown. |
| `harness-manager lease requests TARGET` | Lists incoming requests. |
| `harness-manager lease respond TARGET ID --release \| --keep MINUTES [--message M]` | Answers a request. |
| `harness-manager lease force TARGET [--yes]` | Prompts "Are you sure…" unless `--yes`; refuses if not available. |
| `harness-manager lease leave TARGET` | Leaves the queue. |
| `lease show` | Also prints the queue and the requests. |

## UI, lane LR-D
- **Lease chip:** "Queue for it" becomes **"Request board"**, which opens a small form with an optional message.
- **While waiting:** show the queue position, "the holder has been asked", a 2:00 countdown, and **"Leave queue"**.
- **At the deadline, with no answer and at the head:** **"Force release…"** opens a confirm modal (red, names the holder, and says what happens). A "keep" answer shows its message and the new time.
- **Holder side:** a prominent prompt when `lease.wanted` arrives: "`<by>` wants mps3-01: *message*. Release now / Keep for 5 / 15 / 30 / 60 min (+ message)", with the countdown. It must be visible from any section.
- **Victim side:** a persistent banner: "mps3-01 was force-released by `<by>` at `<time>`: `<reason>`". It stays until dismissed and is also written to Activity.
