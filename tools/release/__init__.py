"""The lead-run release tool (lane OTA-R): signed releases of Harness Manager and of harness
bundles, without CI.

Run it from the repository root with the dev venv (``make release``, or
``.venv/bin/python -m tools.release --help``). Every command is a DRY RUN into
``dist/release/`` unless ``--publish`` is given. docs/RELEASING.md is the runbook;
docs/KEYS.md is the key ceremony (pending david's U2 decision).

Modules:

- ``common``: errors and exit codes, hashing, git, deterministic zips, the tree layout;
- ``signer``: the key source (the ``minisign`` CLI, or HM's own signer for throwaway keys);
- ``channel_doc``: build, merge, promote and withdraw a ``channel.json`` for one
  (catalog, channel), validated with the app's own parser before it is signed;
- ``app``: an app release (wheel, hashed lock, the pyverify ``dep``);
- ``harness``: the harness front-end (H13): ingest a mint's bundle dir, refuse what
  must never ship, emit catalogue entries;
- ``assemble``: the platform's artifacts (a mint's prod dir, the SD templates, the
  overlays, the RM kit) laid out as that bundle dir (RELEASE-PIPE; the R1 stand-in);
- ``publish_check``: may a built harness release go to OWNER/REPO (the publish script);
- ``smoke``: verify a release tree with HM's own channel client, downloader and
  bundle checks; optionally install the app from it into a throwaway venv;
- ``publish``: the ``gh`` plan (run only with ``--publish``) and the hub-mirror layout;
- ``cli``: the command line.
"""
