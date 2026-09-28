# Board 2 (mps3_02_pl): Harness Manager's own board, for HIL-AUTO's nightly run
# (docs/HIL_AUTO.md "Board 2: the nightly run") and HIL_LINUX.md's Card-less mode.
#
#   source ~/SoCLabs/harness-manager/tools/hil/env_b2.sh
#
# Source it; never run it. It only sets variables: nothing is sent to the board or the hub.
# Board 2 has NO user microSD and no JTAG: use --plan linux-nocard, never a reset.

# The board, as boards.toml [boards.lab2] matches it; only the hub reaches it.
export B=192.168.11.101
export H=mapstone-dev.ecs.soton.ac.uk
# Its fpgahub target on the hub, and its MCC console there (ONE reader; never share it).
export T=mps3_02_pl
export MCC_TTY=/dev/mps3_02_pl/tty_00

# This checkout's Harness Manager (main's code), not an installed release's launcher.
export HM_ROOT=${HM_ROOT:-$HOME/SoCLabs/harness-manager}
export PATH=$HM_ROOT/.venv/bin:$PATH

# Evidence. EV: the manual runbook's files (HIL_LINUX.md steps write $EV/<file>).
# RUN: tonight's HIL-AUTO folder (a new one each night; the runner never overwrites one).
export EV=$HM_ROOT/docs/evidence/2026-09-hil-linux-b2
export RUN=$HM_ROOT/docs/evidence/2026-09-hil-auto/$(date +%m%d)-b2

# RC2's overlays (static 0x44EE76D5): board 2 runs the same static as board 1.
export HARNESS_MANAGER_MPS3_OVERLAY_DIRS=$HOME/SoCLabs/mps3-nanosoc-platform-lx/fpga/dfx/build_mint3_rc2_linux/overlay_mbv
# The ILAs are Vivado 2026.1: Harness Manager's own hw_server must be 2026.1 too.
export HARNESS_MANAGER_HW_SERVER=/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/hw_server

# HIL_LINUX.md F3: board 2's OWN config-SD bake (on the hub). Never board 1's
# (286ae54d..., rescue 192.168.10.101): board 2 could not be reached from rescue.
export BAKE=/home/david/mps3_02_pack/sd_tree/MB/HBI0309C/Nanosoc/nanosoc.bit
export BAKE_SHA=f206f788f7497b650b6f0408ebb2fbdb795edb749784a3ec42e6caaaa3df5058
export BAKE_STAGE0=0x6FAE6A0B

mkdir -p "$EV"
[ -d "$HARNESS_MANAGER_MPS3_OVERLAY_DIRS" ] ||
    echo "env_b2: no overlays at $HARNESS_MANAGER_MPS3_OVERLAY_DIRS (HIL_LINUX.md 0.1 says where the hub's copy is)" >&2
echo "env_b2: board 2 = $B ($T, MCC $MCC_TTY); tonight's evidence: $RUN"
