/* fwstream.c -- LCD-MIRROR spike: capture the EXACT {RS,byte} stream the real
 * harness renderer (firmware/clcd/clcd.c + hx8347_init.c, feat/linux-harness
 * 4956d88) pushes into clcd_0's CMD/DATA registers, plus the 40x15 grid it
 * believes is on the glass at quiescence. Board-free: mock_regs HAL.
 *
 * Output (dir argv[1]):
 *   <scenario>.stream  2 bytes per bus write: rs (0 cmd / 1 data), byte
 *   <scenario>.grid    clcd_preview-style lines "INVrr |<40 chars>|" / "   rr |...|"
 * Scenarios are cumulative on ONE panel: boot (reset+init+first paint), then
 * link_down (an incremental repaint), then banner (the DUT-OSD, red rows), then
 * regain (KVM hands the panel back: re-init + full repaint).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "platform_regs.h"
#include "net_proto.h"
#include "diag.h"
#include "../../coordinator/coordinator.h"
#include "../../coordinator/swap_fsm.h"
#include "mock_regs.h"
#include "../clcd.h"

mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }
static int      s_link_up;
static uint16_t s_anlpar;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *v) { if (v) *v = (reg == 0x05u) ? s_anlpar : 0u; return 0; }
static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

static FILE *s_out;
static unsigned long s_n, s_ctrl_writes;
static uint32_t s_last_ctrl;

/* An always-draining FIFO that records every accepted CMD/DATA write. CTRL
 * writes are recorded as pseudo-records (rs=2, byte=CTRL[7:0]) so the decoder
 * sees the panel RESET line exactly as the harness drives it (CTRL[2]=RESET_N). */
static int hook(void *c, int wr, uint32_t base, uint32_t off, uint32_t *v)
{
    (void)c; (void)base;
    if (!wr) { *v = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u; return 1; }
    if (off == CLCD_CMD || off == CLCD_DATA) {
        uint8_t rec[2] = { (uint8_t)(off == CLCD_DATA), (uint8_t)(*v & 0xFFu) };
        fwrite(rec, 1, 2, s_out); s_n++;
    } else if (off == CLCD_CTRL) {
        uint8_t rec[2] = { 2u, (uint8_t)(*v & 0xFFu) };
        fwrite(rec, 1, 2, s_out); s_ctrl_writes++; s_last_ctrl = *v;
    }
    return 1;
}

static void seed(void)
{
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id = 0x44EE76D5u;
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 0x01000001u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    g_shell_state.current_rm_id = 0x01000001u;
    s_icap_bytes = 1835072u;
    s_link_up = 1; s_anlpar = (1u << 8);
    s_swap_res.valid = 1; s_swap_res.ok = 1; s_swap_res.verified = 1; s_swap_res.rm_id = 0x01000001u;
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS, CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
}

static void open_out(const char *dir, const char *name)
{
    char p[512];
    if (s_out) fclose(s_out);
    snprintf(p, sizeof p, "%s/%s.stream", dir, name);
    s_out = fopen(p, "wb");
    if (!s_out) { perror(p); exit(2); }
    s_n = 0;
}

static void dump_grid(const char *dir, const char *name)
{
    char cells[CLCD_NCELLS]; uint8_t inv[CLCD_ROWS]; char p[512];
    clcd_test_shadow(cells, inv);
    snprintf(p, sizeof p, "%s/%s.grid", dir, name);
    FILE *f = fopen(p, "w");
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        fprintf(f, "%s%02u |", inv[r] ? "INV" : "   ", r);
        for (unsigned c = 0; c < CLCD_COLS; c++) {
            char ch = cells[r * CLCD_COLS + c];
            fputc((ch >= 0x20 && ch < 0x7F) ? ch : ' ', f);
        }
        fputs("|\n", f);
    }
    fclose(f);
}

/* Poll until the renderer is idle with nothing dirty for 2 s of mock time. */
/* Poll for at least min_polls (5 ms mock time each), then stop at the first
 * pass that pushed nothing with nothing dirty: nothing is in flight, so the
 * glass (the stream so far) equals the renderer's shadow grid at that instant. */
static void run_quiet(unsigned min_polls)
{
    for (unsigned i = 0; i < min_polls + 100000u; i++) {
        mock_time_advance_ms(5);
        clcd_poll();
        if (i >= min_polls && clcd_test_bytes_last_pass() == 0 && clcd_test_dirty_count() == 0)
            return;
    }
    fprintf(stderr, "never quiesced\n"); exit(3);
}

int main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : ".";
    mock_regs_reset();
    seed();
    mock_regs_set_hook(MPS3_CLCD_BASE, hook, 0);
    mock_time_set_ms(171907000u);
    clcd_set_board_name(MPS3_BOARD_NAME);

    open_out(dir, "boot");
    clcd_init();
    run_quiet(4000);
    fflush(s_out);
    dump_grid(dir, "boot");
    fprintf(stderr, "boot: %lu bus bytes, %lu CTRL writes, last CTRL 0x%02X, state %d\n",
            s_n, s_ctrl_writes, (unsigned)s_last_ctrl, clcd_test_state());

    open_out(dir, "link_down");
    s_link_up = 0; s_anlpar = 0;
    run_quiet(400);
    fflush(s_out);
    dump_grid(dir, "link_down");
    fprintf(stderr, "link_down: %lu bus bytes\n", s_n);

    open_out(dir, "banner");
    clcd_test_set_banner(1);
    run_quiet(400);
    fflush(s_out);
    dump_grid(dir, "banner");
    fprintf(stderr, "banner: %lu bus bytes\n", s_n);

    /* The KVM hands the panel to the DUT (the harness goes silent), then back:
     * clcd_regain() re-streams the init table and repaints every cell. The KVM's
     * own CLCD_RST pulse is not a clcd_0 CTRL write; the decoder adds it. */
    clcd_lose();
    for (int i = 0; i < 400; i++) { mock_time_advance_ms(5); clcd_poll(); }
    open_out(dir, "regain");
    clcd_regain();
    run_quiet(4000);
    fflush(s_out);
    dump_grid(dir, "regain");
    fprintf(stderr, "regain: %lu bus bytes\n", s_n);
    fclose(s_out);
    return 0;
}
