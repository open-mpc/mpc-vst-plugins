/* Test engine for the VST2 wrapper's five opt-in options (vst.json "defines", checked by tools/host_test.c under
 * WRAPOPTS_PROBE): ON_NO_UPDATE_PREFIX, LIT_UPDATE_ON_TEXT_POLL, ECHO_GUARD_POLLED_ONLY, OPTION_ANSWER_INDEX and
 * NO_ENGINE_CHANGED, all on in vst.json.
 * - "gain": a plain value, first, so that parameter 0 (which the wrapper holds for MPC's STOP toggle) is not one tested.
 * - "chan": options labelled "1".."16"; the engine answers with the option's index ("8" for the label "9"), as an engine
 *   that keeps indexes does (the wrapper sets it by index).
 * - "btn": a "poll": false list tile that never lights (no "btn_on"): a button drawn as a tile. Each press (a value of 1)
 *   counts in wrapopts_taps.
 * - "ph_1", "lt_1", "lt_2": polled list tiles, lit from wrapopts_lit (bits 0, 1, 2) through "<key>_on"; their text never
 *   changes. "ph_1" has the prefix ON_NO_UPDATE_PREFIX names.
 * - mpc_engine_changed() is defined and counts its calls in wrapopts_changed_calls: with NO_ENGINE_CHANGED the wrapper must
 *   never call it.
 * - mpc_engine() answers NULL on its first call and counts its calls in wrapopts_engine_calls: the wrapper must ask again on
 *   the next VSTPluginMain, as it did before the engine table was guarded. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../wrapper/engine.h"

volatile int wrapopts_taps, wrapopts_lit, wrapopts_changed_calls, wrapopts_engine_calls;

typedef struct { int chan; char gain[32]; } wo_t;

static void *create(const char *d) {
    (void)d;
    wo_t *s = calloc(1, sizeof *s);
    if (s) strcpy(s->gain, "0");
    return s;
}
static void destroy(void *i) { free(i); }
static void midi(void *i, const uint8_t *m, int n) { (void)i; (void)m; (void)n; }
static void set_param(void *i, const char *k, const char *v) {
    wo_t *s = i;
    if (!strcmp(k, "gain")) snprintf(s->gain, sizeof s->gain, "%s", v);
    else if (!strcmp(k, "chan")) {
        int x = atoi(v);   /* the wrapper sends an option's index (norm_to_str) */
        if (x >= 0 && x <= 15) s->chan = x;
    } else if (!strcmp(k, "btn") && atof(v) > 0.5) wrapopts_taps++;
}
static int get_param(void *i, const char *k, char *b, int n) {
    wo_t *s = i;
    if (!strcmp(k, "gain")) return snprintf(b, n, "%s", s->gain);
    if (!strcmp(k, "chan")) return snprintf(b, n, "%d", s->chan);   /* the index, not the label */
    if (!strcmp(k, "ph_1_on")) return snprintf(b, n, "%d", wrapopts_lit & 1 ? 1 : 0);
    if (!strcmp(k, "lt_1_on")) return snprintf(b, n, "%d", wrapopts_lit & 2 ? 1 : 0);
    if (!strcmp(k, "lt_2_on")) return snprintf(b, n, "%d", wrapopts_lit & 4 ? 1 : 0);
    if (!strcmp(k, "btn") || !strcmp(k, "ph_1") || !strcmp(k, "lt_1") || !strcmp(k, "lt_2")) return snprintf(b, n, "%s", k);
    return 0;
}
static void render(void *i, int16_t *o, int f) { (void)i; memset(o, 0, (size_t)f * 4); }

int mpc_engine_changed(void *inst, char *key, int len) {
    (void)inst; (void)key; (void)len;
    wrapopts_changed_calls++;
    return 0;
}

static const mpc_engine_t ENGINE = { create, destroy, midi, set_param, get_param, render, NULL };
const mpc_engine_t *mpc_engine(void) { return wrapopts_engine_calls++ ? &ENGINE : NULL; }
