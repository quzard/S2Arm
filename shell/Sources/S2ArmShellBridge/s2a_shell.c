// Play 回调的登记与转发（见 include/s2a_shell.h）。只在主线程用，不加锁。
#include "s2a_shell.h"
#include <stdio.h>

static s2a_shell_play_fn g_play;
static void *g_play_ctx;

void s2a_shell_set_play_handler(s2a_shell_play_fn fn, void *ctx) {
    g_play = fn;
    g_play_ctx = fn ? ctx : NULL;
}

int s2a_shell_play(const char *game_id) {
    const char *id = game_id ? game_id : "";
    if (!g_play) {
        fprintf(stderr, "S2Arm 游戏库：Play %s（没登记处理函数，只记日志）\n", id);
        return 0;
    }
    g_play(id, g_play_ctx);
    return 1;
}

// 虚拟手柄布局：宿主（ios/vpad_shell.m）有强定义时用它；本包单独编（swift test）时用这里的缺省
__attribute__((weak)) char *s2a_shell_vpad_layout_json(const char *game_id, double safe_w, double safe_h) { (void)game_id; (void)safe_w; (void)safe_h; return NULL; }
__attribute__((weak)) int s2a_shell_vpad_save(const char *game_id, const char *centers_json, double safe_w, double safe_h) { (void)game_id; (void)centers_json; (void)safe_w; (void)safe_h; return 0; }
__attribute__((weak)) int s2a_shell_vpad_select(const char *game_id, const char *layout_id) { (void)game_id; (void)layout_id; return 0; }
__attribute__((weak)) int s2a_shell_vpad_set_hold_lock(const char *game_id, int enabled) { (void)game_id; (void)enabled; return 0; }

static s2a_shell_refresh_fn g_refresh;
static void *g_refresh_ctx;
void s2a_shell_set_refresh_handler(s2a_shell_refresh_fn fn, void *ctx) { g_refresh = fn; g_refresh_ctx = fn ? ctx : NULL; }
void s2a_shell_refresh(void) { if (g_refresh) g_refresh(g_refresh_ctx); }

// 本款资源包：宿主（ios/app.m）有强定义时用它；本包单独编时按就绪、点了只记日志
__attribute__((weak)) int s2a_shell_game_data_ready(const char *game_id) { (void)game_id; return 1; }
__attribute__((weak)) void s2a_shell_library_appeared(void) {}
__attribute__((weak)) void s2a_shell_pick_game_data(const char *game_id) { fprintf(stderr, "S2Arm 游戏库：要导入 %s 的游戏数据（宿主没接说明页，只记日志）\n", game_id ? game_id : ""); }
