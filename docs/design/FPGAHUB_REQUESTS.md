# fpgahub requests for the hub SD door (FH-a to FH-d)

**Lane HUB-SD, 2026-09-26.** These are requests to fpgahub (david's repo), from david's decision U9: Harness Manager gets a hub SD door now (`harness_manager_mps3/hub_sd.py`), and fpgahub gets four improvements as requests. The door works on fpgahub v0.3.0 as it is. Each request removes a workaround the door carries today.

**Sources:**
- the design: `docs/design/HARNESS_DISTRIBUTION.md` §6 (b), §11.1;
- fpgahub v0.3.0 (22aa362): `program.py`, `program_plugins/sd_install.py`, `api/v1.py`, `cli.py`, `ipc.py`;
- the platform's memory note "sd_install timeout trap" (2026-07-18, corrected 2026-09-08).

## What the door does today, and why

The door writes `MB/HBI0309C/Nanosoc/nanosoc.bit` through one `fpgahub target program TARGET BIT --method sd --force`. It then proves the write finished and REBOOTs the board over the hub's MCC share. Four facts of v0.3.0 shape it:

1. **The program request is synchronous.** The CLI's client gives up after 30 s (`ipc.DaemonClient(timeout=30.0)`). The 12 MB write takes about 68 s. So the CLI always prints `POST /targets/T/program: timed out`, while the daemon keeps writing. A client that reads this as a failure and retries starts a second write. A reset mid-write then darkens the board (2026-07-18).
2. **The `sd_install` plugin writes one file.** It cannot also change `nanosoc.txt`, so the config SD cannot be A/B by pointer through the hub.
3. **Nothing reports what is on the card.** `last_fingerprint` is the last image *programmed through fpgahub* under the current lease. It is not the sha of the file on the SD.
4. **"Available" is configuration, not reachability.** `target program T --list` says `Available: yes` while the SD is unreachable. For example, `reconcile-macs --apply` pinned `effective_hub_path` to the Ethernet dongle's branch (2026-09-09).

## The requests

### FH-a: an async job for `--method sd`

**Ask.** `POST /targets/{t}/program` with `method=sd` answers at once, with `{job_id}`. A new route, `GET /jobs/{job_id}`, then gives `{state: queued|writing|done|failed, sha256, dur_s, message}`. The CLI gains `--wait` / `--no-wait`.

**What it simplifies in HM:**
- **Completion.** `SshSdBackend.completion` stops reading `journalctl -u fpgahubd --since @EPOCH` and parsing the `program dispatched: … sha256=<12hex>` log line. That parse depends on a log format, and on the automation account being allowed to read the journal. `RestSdBackend` stops subscribing to `/events` before the request (the hub keeps no replay buffer) and stops falling back to `last_fingerprint`.
- **The timeout class goes away.** `parse_program_reply`'s `timeout` state becomes "queued", and the door stops needing `EXPECTED_TIMEOUT` in its progress text.
- **Crash recovery.** HM's in-flight marker (`update/hub_sd/<hub>_<target>.json`) would record the job id. `pending()` could then ask the hub about that exact job, instead of looking for a record of the sha after a time.

### FH-b: write a `.bit` and patch `nanosoc.txt` in one locked action

**Ask.** One `sd_install` action, under the board's action lock, that:
1. writes the bitstream to a chosen `dest` (for example `MB/HBI0309C/Nanosoc/nanosocb.bit`);
2. reads it back;
3. only then rewrites `F0FILE` in `nanosoc.txt` to name it.

fpgahub's manifest `sd_install` already allows files plus patches (MANIFESTS.md §4.2). The program plugin would need the same shape, or a dedicated action.

**What it simplifies in HM:**
- **The hub door gets U8's A/B by pointer.** Today `sd_ab.py` works only on a card mounted on this machine (`session.ab_storage`). The hub door must write `nanosoc.bit` in place.
- **Safer writes.** With FH-b, an interrupted 12 MB write through the hub could no longer touch the running image.
- **Rollback and auto-revert become a pointer flip.** Today the door's rollback, and U10's auto-revert of a dark board, is a second 68 s write of the previous `.bit`. With FH-b it would be a flip of `F0FILE`, which takes milliseconds.

### FH-c: report the sha of the current `dest`

**Ask.** `GET /targets/{t}/program` (and `--list`) reports `sd_dest: {path, sha256, size, mtime}` for each `sd` method. fpgahub hashes the file on the card, under the same mount the plugin uses. Whether an FH-b pointer names it would be a bonus.

**What it simplifies in HM:**
- **The backup.** Today the door keeps the *running release's* `nanosoc.bit` from the signed cache, because fpgahub cannot back up the SD. The planner therefore refuses the hub door when the board's running release is unrecorded on the channel. With FH-c, HM would know the card's actual sha and could:
  - match it against any cached or published `.bit`;
  - refuse when the card holds something HM has no copy of;
  - verify after the write, by reading the card, not a log line.
- **The in-flight marker.** It could settle by comparing the card's sha.
- **Confirming a flip.** It would also confirm the FH-b flip.

### FH-d: a reachability preflight for the `sd` method

**Ask.** `GET /targets/{t}/program/sd/preflight`, or `--list --probe`, runs the plugin's discover step without mounting. It resolves the USB-MSC device under `effective_hub_path`, reports `{reachable, device, size, reason}`, and says whether a mount of it is already held (EBUSY). The last case is the "human left the SD mounted on the hub, the MCC is starved" trap.

**What it simplifies in HM:**
- **The door's check becomes real.** Today `HubSdDoor.describe()` / `preflight()` can only check that an `sd` method is configured and available. The first real sign of an unreachable card is a failed program request. That comes after the upload, the backup and the lease interruption. With FH-d, the planner would block an unreachable card, and the Harness versions card would say why ("the hub cannot see the SD: effective_hub_path is pinned to 1-2.3.4.3.4") before anyone types the consent phrase.

## Until then

The door is safe on v0.3.0, because it relies only on what v0.3.0 already guarantees:
- **one** program request, never retried;
- completion only from the hub's own record naming our sha:
  - the journal line (SSH);
  - the `board.program_completed` event (REST);
  - `last_fingerprint` changing to ours;
- a sha mismatch or no record within 600 s refuses the REBOOT, and the in-flight marker stays until the record appears.

The four requests make it simpler and faster. None of them makes it correct.
