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
| Tell the victim | Its heartbeat fails (lost), or `lease show` names another holder. **As built (CCR-A1):** fpgahub v0.3.0's lease-history leaves out `admin_revoked` (the event has no `board=`), and its `lease.revoked` entry drops the reason. So `lease_revoke` writes a `rev-<id>.json` note before revoking, and `lease_history()` merges those notes in as `lease.admin_revoked {ts, by, actor, reason, prior_holder, board}`. `hub.taken_from_history(events, holder)` returns `{by, reason, at}`. Without a note, `by` is the next holder and `reason` is `""`. An fpgahub fix (emit with `board=`, keep the reason) is a follow-up. |
| Leave the queue | `fpgahub lease cancel <target> --holder <principal>`, plus deleting our request note. **Pass the PRINCIPAL** (CCR-A3): over the unix socket the caller is an admin, whose `--holder` is taken literally, so the stored requested holder cancels nothing. L1's cancel-on-abort had the same bug. |
| Who am I | fpgahub records a lease's holder as the caller's principal (`name@host`) and ignores `--holder`. Learn this client's principal once (`fpgahub whoami`, or from the first acquire) and use it for "is this lease or queue entry mine". **This also fixes today's bug:** Harness Manager saved the holder it asked for (`david-hm`), the hub showed `david@mapstone-dev`, so `mine` was false. |

## The rules

| Rule | Detail |
|---|---|
| **Timer** | A request has `created_at` and `deadline_at = created_at + 120 s`, both UTC ISO 8601 and written in the note. Both sides count down from the note, not from their own clocks. |
| **Holder's answers** | `release` releases now. `keep` takes `minutes` (5, 15, 30 or 60) and an optional `message`. |
| **Force available** | Only when all of these hold: the deadline has passed; there is **no answer**, or a `keep` whose minutes have run out; and the requester is **at the head of the queue** (a revoke promotes the head, and anyone else would get the board). Otherwise force is refused with the reason: 409 REFUSED, or 422 with the time left. |
| **Force needs `confirm: true`** | The UI shows "Are you sure? This kicks `<holder>` off mps3-01 now; anything they are running on the board is interrupted." When no Harness Manager session is known to hold the lease (it may be a script), the board's name must be typed too: `confirm_board` (D12). |
| **Revoke reason** | `"force-released by <principal> via Harness Manager: no answer to a request made at <created_at>"`. |
| **Leaving** | Leaving the queue withdraws the request. Closing the board in the app while queued also leaves the queue (L1 already cancels an acquire on close). |
| **Same person, two sessions** (CCR-A2) | fpgahub keys leases on `user@hubhost`, so a second session of the same person would be handed the lease back instead of queueing. `request()` refuses when `lease_status().holder == principal()`: "you already hold this board (another session)". |
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
lease:  {target, board, holder, user, expires_at, mine, here,
         holder_kind: "hm" | "unknown", holder_kind_reason} | null      # D12
         # mine: by principal (another session of the same principal too);
         # here: THIS process holds the token (REVIEW-W5; background reads go by it)
         # board: the physical board (mps3_01) or null (LEASE-BOARD; target is leased)
queue:  [{position, holder, user, mine}]
request: {id, message, created_at, deadline_at, position,
          answer: {answer, minutes, message, at} | null,
          force_available: bool, force_reason: str,
          reasked: bool, reasked_at: str | null,
          tapped_at: str | null} | null   # my outgoing request; tapped_at: PANEL-1
incoming: [{id, by, user, host, message, created_at, deadline_at,
            answer, tapped_at}]           # requests for my lease; answer: D5
taken:  {by, reason, at} | null        # the last time my lease was force-released
notes_supported: bool                  # messages and keep answers work (False over REST)
notes_reason: str                      # why not; "" when supported
can_revoke: bool                       # this client may force-release (REST: admin token only)
revoke_reason: str                     # why not; "" when it may
stale:  {confirmed_at, source, misses, error}   # LEASE-FRESH, additive: ABSENT on a fresh
        # read; present when the hub did not answer and this is the state last known
        # (source: acquire | release | heartbeat | show). 3 failed reads in a row, a state
        # over 5 min old, a passed expiry, or no known state: the read's error instead.
```

- `reasked` is true, and `reasked_at` is when, once the request was re-sent to a new holder
  (D9); `deadline_at` is then the new deadline. Only the waiting process knows this: another
  process (the CLI's `lease show`) reports false.
- The four capability keys come from the hub client: `notes_supported`/`notes_reason` and
  `can_revoke()` on T8's REST client; an SSH client has both, so they are true and `""`.
  Without a hub they are false with the reason `this board is not behind a hub`. They are
  known before any request, so the UI can hide Keep and say why force will not be offered.

| Method and path | Body | Returns |
|---|---|---|
| `POST /boards/{bid}/lease/request` | `{message?, ttl_s?}` | 202 job `lease_request`. Phases: `queued`, `notified`, `answered`, `force-available`, `held`. The result is `{lease}`, or `{answered: {...}}` when kept. |
| `POST /boards/{bid}/lease/respond` | `{id, answer: "release"\|"keep", minutes?, message?}` | 200 `{ok}` |
| `POST /boards/{bid}/lease/force` | `{confirm: true, confirm_board?}` | 202 job `lease_force`, whose result is `{lease}`. Before any revoke: 409 REFUSED (not available: not at the head, answered, not yours) or 422 USAGE (`confirm` missing). D12: when `lease.holder_kind` is not `"hm"`, `confirm_board` must be the board's name: 400 USAGE without it, 409 REFUSED with another name. |
| `DELETE /boards/{bid}/lease/queue` | none | 200 `{left: bool}` (leave the queue and withdraw the request) |

**Events** (append to CONTRACTS):

| Topic | Payload | Who gets it |
|---|---|---|
| `lease.wanted` | `{id, by, user, host, message, deadline_at}` | the holder's session |
| `lease.answered` | `{id, answer, minutes, message}` | the requester's session |
| `lease.force_available` | `{id}` | the requester's session, at the deadline |
| `lease.taken` | `{by, reason, at}` | the holder's session, after a forced release |
| `lease.left` | `{}` | whoever left the queue |
| `lease.tapped` | `{id, by, at}` | every session watching the board, when someone at the board taps the front panel's banner for the open request (CCR PANEL-1, `LeaseService.notify_holder`). A notice only: nothing is released, answered, forced or left; `tapped_at` in `GET /lease` records it |

## CLI: `cli/cmd_hub.py`, lane LR-C

| Command | What it does |
|---|---|
| `harness-manager lease request TARGET [--message M] [--ttl S]` | Waits; shows the answer and the countdown. |
| `harness-manager lease requests TARGET` | Lists incoming requests. |
| `harness-manager lease respond TARGET ID --release \| --keep MINUTES [--message M]` | Answers a request. |
| `harness-manager lease force TARGET [--yes] [--confirm-board NAME]` | Prompts "Are you sure…" unless `--yes`; refuses if not available. D12: when the holder may be a script, it asks for the board's name to be typed instead, and without a terminal needs `--confirm-board NAME` (`--yes` is not enough). |
| `harness-manager lease leave TARGET` | Leaves the queue. |
| `lease show` | Also prints the queue and the requests. |

## UI, lane LR-D
- **Lease chip:** "Queue for it" becomes **"Request board"**, which opens a small form with an optional message.
- **While waiting:** show the queue position, "the holder has been asked", a 2:00 countdown, and **"Leave queue"**.
- **At the deadline, with no answer and at the head:** **"Force release…"** opens a confirm modal (red, names the holder, and says what happens). A "keep" answer shows its message and the new time.
- **Holder side:** a prominent prompt when `lease.wanted` arrives: "`<by>` wants mps3-01: *message*. Release now / Keep for 5 / 15 / 30 / 60 min (+ message)", with the countdown. It must be visible from any section.
- **Victim side:** a persistent banner: "mps3-01 was force-released by `<by>` at `<time>`: `<reason>`". It stays until dismissed and is also written to Activity.

## Decisions after the lanes built it (lead, 2026-09-24 afternoon)

These amend the frozen spec. Lanes align to them before merging.

| # | Question (raised by) | Decision |
|---|---|---|
| D1 | A keep answer ended the request job, but we are still queued: who holds the queue entry, and who takes the lease when we are promoted? (LR-D, LR-C) | **The request job keeps running after a `keep` answer.** Its phase becomes `answered`, and `lease.answered` is emitted, but `request()` keeps polling the acquire. It ends only when the lease is **held**, we **leave**, or a **force** succeeds. `lease request` on the CLI prints the answer and keeps waiting (Ctrl-C leaves). After the keep minutes run out, `force_available` can become true again. |
| D2 | Force while our own request job is running (LR-D, LR-C) | **`lease_force` runs beside the requester's own `lease_request` job** (LR-C's second JobManager). The revoke promotes us, so the request job ends with `{lease}`, and so does the force job. Any OTHER job on the board still makes force answer 409 HELD. |
| D3 | Status codes (LR-D) | **API.md's table wins.** `confirm` missing is **400 USAGE**; "time left" is **422 UNAVAILABLE** with `error.data.time_left_s` and `deadline_at`; not the head, already answered, or not ours is **409 REFUSED** with the reason. |
| D4 | The confirm dialog needs the board that is revoked (LR-D) | `GET /lease` adds **`board`**, the physical board from `hub.board_id()` (`mps3_01`). The UI shows the N1 name and says which board is revoked when it differs from the target. |
| D5 | The holder could not see its earlier answers after a reload (LR-D) | **`incoming[].answer: {answer, minutes, message, at} \| null`**, read from the answer notes. |
| D6 | The 10 s `GET /lease` cache could hide `force_available` (LR-D) | The service **drops its cached view on every lease state change** and **at a request's `deadline_at`** (and at keep expiry). |
| D7 | Outcome of a cancelled request job (LR-D) | **The job succeeds with `{left: true}`**, so leaving is not a failure. |
| D8 | Timestamp formats (LR-D) | Every lease-request time is **ISO 8601 UTC with `+00:00`**: `created_at`, `deadline_at`, `answer.at`, `taken.at`. fpgahub's `…Z` forms are parsed on Python 3.10 too (3.10's `fromisoformat` rejects `Z`). |
| D9 | The holder changes while we wait: the new holder was never asked (LR-B-4) | **The note is re-sent to the new holder with a fresh 120 s deadline**, and `force()` refuses ("was not asked") until that deadline passes. Otherwise a stale deadline would let us kick someone who just got the board. Limit: a separate process with no memory of the wait cannot see the change. |
| D10 | A requester's poll costs two ssh calls, not one (LR-B) | **Accepted.** One acquire plus one answer-note read per 10 s. The holder's poll stays at one call. |
| D11 | The victim's "taken" banner has no way to be closed (LR-B) | **`DELETE /boards/{bid}/lease/taken`** → 200 `{dismissed: bool}`, calling `LeaseService.dismiss_taken(hub)`. `GET /lease` then returns `taken: null` until the next forced release. |
| D12 | Scripts (the B1 runner, soaks, proof scripts) hold leases through pyverify and never answer a request, so force-release would kick them after 2 minutes (lead decision W5, 2026-09-24) | **When no Harness Manager session is known to hold the lease, warn and require the board's name typed.** `GET /lease` adds `lease.holder_kind`: `"hm"` or `"unknown"`, and `lease.holder_kind_reason`. The one signal a script cannot give is **an answer to our current request**: `respond()` writes it only from the session holding the lease's token, and D9 re-sends the request (no answer yet) to a new holder. So `"hm"` means this session holds it, or the holder answered our request (a keep that ran out). Everything else is `"unknown"`, including our own principal held elsewhere. Principal-level evidence (the holder once wrote a request note) is not enough: scripts lease under their owner's `name@host`, and fpgahub keeps neither `--holder` nor a client kind. There is no hub-side presence note. `force` with `"unknown"` needs `confirm_board`: the board's N1 name (`mps3-01`), the hub's board id (`mps3_01`), the address or the target, any case. Missing: 400 USAGE with `error.data.{holder_kind, holder_kind_reason, confirm_board}`; another name: 409 REFUSED; a name given for an `"hm"` holder must be right too. The route checks the view before the job and `force()` checks the hub again. **CLI:** a terminal asks "Type mps3-01 to force-release"; without a terminal, `--confirm-board NAME` (`--yes` is not enough). **UI:** the confirm says "No Harness Manager session is known to hold mps3-01; it may be a script (a soak or runner). Type mps3-01 to force-release." with a text field; Force stays disabled until the name matches. **Limits:** an HM user away from the app also gets the typed confirm; a separate process that missed a holder change (D9's limit) may count a previous holder's answer. A cheap follow-up that would let silent HM holders count as `"hm"`: the holder's 10 s poll writes a `seen-<id>` receipt (a frozen-interface change). |
