// S2Arm iOS 游戏库界面的 C 边界（app.m / 宿主 C 侧只包含这一个头文件）。
//   库界面：s2a_shell_library_vc 返回一个 UIHostingController（SwiftUI 游戏库），由 app.m 挂到自己的 Scene / 根窗口，
//     不另起 UIApplicationMain、不接管生命周期。
//   Play：界面上点 Play（可运行的那款）→ s2a_shell_play(gameId) → 登记的处理函数；没登记时只记一行日志。宿主侧 ios/app.m（多款容器）登记，处理函数里选游戏、起宿主。
// 线程：两样都只在主线程调；处理函数也在主线程被回调。
#ifndef S2A_SHELL_H
#define S2A_SHELL_H

#ifdef __cplusplus
extern "C" {
#endif

// Play 处理函数：game_id 只在回调期间有效（要留就拷一份）；ctx 原样回传
typedef void (*s2a_shell_play_fn)(const char *game_id, void *ctx);

// 登记 / 换掉 Play 处理函数；传 NULL 取消登记（回到只记日志）
void s2a_shell_set_play_handler(s2a_shell_play_fn fn, void *ctx);

// 界面 Play 的出口（S2ArmShellHost 调它）：有处理函数就转给它并返回 1；没有只记日志并返回 0
int s2a_shell_play(const char *game_id);

// 库界面控制器（实现在 S2ArmShellHost，Swift @_cdecl；只在 iOS 有）：
//   catalogue_json_path = 游戏目录清单 JSON 的绝对路径（格式见 shell/UPSTREAM.md「catalogue」）；
//   返回 +1 的 UIViewController（ARC 下 (__bridge_transfer UIViewController *)s2a_shell_library_vc(p)），失败不返回 NULL：
//   清单读不了时界面照出、列表为空并记日志。
void *s2a_shell_library_vc(const char *catalogue_json_path);

// 本款资源包（11-07 Duo「游戏没能启动」：库里对资源包不在机上的款仍给 Play，起局后服务层读不到 main.npdm）：
//   s2a_shell_game_data_ready：1 = 就绪或已拷到、待解开（Play 后起局前的导入闸门会解开）；0 = 不在 / 不完整（库里显示「需要游戏数据」、不给 Play）。
//     实现在宿主 ios/app.m（按 Documents/games/<游戏号> 跑 pack_apply.c 同一判据）；本包 s2a_shell.c 里弱定义缺省 1（Swift 单测不链宿主）
//   s2a_shell_pick_game_data：库里点「需要游戏数据」那款 → 宿主给导入说明（含「重新检查」）；弱定义缺省只记日志
int s2a_shell_game_data_ready(const char *game_id);
// 库界面第一次出现（启动打点「游戏库界面可操作」）；弱定义缺省什么都不做
void s2a_shell_library_appeared(void);
void s2a_shell_pick_game_data(const char *game_id);
// 宿主让库界面重判各款资源包（说明里「重新检查」后调）。库界面建好时登记；没登记时 s2a_shell_refresh 什么都不做
typedef void (*s2a_shell_refresh_fn)(void *ctx);
void s2a_shell_set_refresh_handler(s2a_shell_refresh_fn fn, void *ctx);
void s2a_shell_refresh(void);

// 虚拟手柄布局（11-07 维护者：游戏库按住游戏 / 点设置 → 虚拟手柄布局 → 二级页拖动；按游戏存，与游戏内拖动编辑同一份数据）。
//   实现在宿主 ios/vpad_shell.m；本包 s2a_shell.c 里是弱定义缺省（读返回 NULL、写返回 0），Swift 单测不链宿主也能编。
//   坐标：safe_w × safe_h 是编辑页里按键可放的区域（点），原点左上；按键字段同布局 JSON（x / y 为该区域宽高比例、width / height /
//   cornerRadius 为区域短边比例，见 ios/layouts/layout.schema.json）。
// 要编辑的布局：{"selected":"default|mine|touch|other","hasMine":bool,"holdLock":bool,"buttons":[{id,x,y,width,height,cornerRadius}…]}，
//   有本款「我的布局」就是它，否则按这块区域现算的默认布局；malloc 出的 UTF-8，调用方 free。游戏号不合规返回 NULL
char *s2a_shell_vpad_layout_json(const char *game_id, double safe_w, double safe_h);
// 存本款「我的布局」并选中：centers_json = {"A":[x,y],…}（区域坐标的点，只含挪过的键也行），返回 1 成功 / 0 失败
int s2a_shell_vpad_save(const char *game_id, const char *centers_json, double safe_w, double safe_h);
// 选方案：layout_id = "default" / "mine"，返回 1 成功 / 0 失败（「恢复默认」= 选 default，「我的布局」文件保留）
int s2a_shell_vpad_select(const char *game_id, const char *layout_id);
// ZL / ZR 按住锁定开关（按游戏，与游戏内设置同一份数据），返回 1 成功 / 0 失败
int s2a_shell_vpad_set_hold_lock(const char *game_id, int enabled);

#ifdef __cplusplus
}
#endif

#endif
