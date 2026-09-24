"""Keep pytest (and so `make check`) out of tests/spikes/.

The spikes are design experiments, run by hand as modules
(``PYTHONPATH=src:. python -m tests.spikes.<name>``). Several start a private sshd, a real
Vivado hw_server or a real install, so none of them is hermetic. Their file names already
miss ``python_files``; this makes the exclusion explicit, so a future ``test_*.py`` here
is not collected either.
"""

collect_ignore_glob = ["*"]
