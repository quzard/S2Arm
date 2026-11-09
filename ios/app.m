// iOS App 壳：UIKit 占主线程（s2a_front_run 覆盖 host_main.c 的弱缺省）。窗口与根视图在这里建；Metal 层与触屏 / 手柄由帧率能耗线、服务接入线挂到 s2a_ios_root_view()。
#include <dlfcn.h>
#import <UIKit/UIKit.h>
#import "controller.h"
#include "input_config.h"
#include "root_ready.h"
#import "vpad.h"
#import "save_transfer.h"
#import "app_settings.h"
#import "notice.h"
#include "s2a_session.h"
#include "s2a_dguard.h"
#include "s2a_init_fail.h"   // 初启失败页的标题 / 按钮文案（与 launchers/shared/experience.v1.json init_fail_page 逐字一致）
void s2a_loadpage_attach(UIView *root); void s2a_loadpage_front(void);   // loadpage.m：首次加载页 / 简短启动画面
#import <Metal/Metal.h>   // NSProcessPerformanceProfileSustained 定义在 Metal 的 MTLDeviceCertification.h（心跳打持续执行档用）
static _Atomic double g_brightness = -1;   // 屏幕亮度：主线程建场景 / 心跳时更新，渲染侧（hle_nvn.c 每 300 帧那行）只读缓存，不在别的线程碰 UIKit
void s2a_brightness_hold_log(void);   // 亮度保持（S2A_BRIGHTNESS）：心跳行带出整局实际亮度最小 / 最大值
#include "native.h"
#include "s2a_pad.h"
#include "s2a_boot.h"
#include "s2a_launch_ctx.h"
#include "s2a_docs.h"   // s2a_container_docs：游戏库按款核资源包的进程级 Documents
// 游戏库界面（shell 的 S2ArmShellHost，C 边界同 Sources/S2ArmShellBridge/include/s2a_shell.h）：make ios S2A_SHELLUI=1 才链进来，
//   缺省壳不带、为空指针（多款容器包配了不带库界面的壳 = 报红提示，不起游戏）。这里本地声明、不 include 那个头：缺省出壳不读 frontend-ios（壳输入闭包）
// 弱链接[ios]：s2a_shell_library_vc
// 弱链接[ios]：s2a_shell_set_play_handler
void *s2a_shell_library_vc(const char *catalogue_json_path) __attribute__((weak_import));
void s2a_shell_set_play_handler(void (*fn)(const char *game_id, void *ctx), void *ctx) __attribute__((weak_import));
// 弱链接[ios]：s2a_shell_refresh
void s2a_shell_refresh(void) __attribute__((weak_import));
void s2a_boot_rebase(const char *why);   // boot.c：起局计时起点改为此刻（多款容器点 Play；不进 native.h——它在抽取阶段键里，改它冷全库）
const char *s2a_host_pre_ui_checks(void);   // host_main.c：起 UIKit 前读游戏相关值的检查（多款容器在选游戏之后调）
#include <stdatomic.h>
#include <pthread.h>
// UIKit lifecycle events publish on the main thread, independently of the
// background-only GPU/guest pause policy. now_ns is also the NVN clock.
static _Atomic int display_focused;
static _Atomic uint64_t display_focus_since;
int dm_focus_now(void) { return atomic_load(&display_focused); }
uint64_t dm_focus_in_since(void) { return atomic_load(&display_focus_since); }
static void display_focus_publish(BOOL active) {
    if (active && !atomic_load(&display_focused)) atomic_store(&display_focus_since, now_ns());
    atomic_store(&display_focused, active);
}
static _Atomic int g_front_running;
static void s2a_ios_suspend(int on, const char *why);
// 重启完成（runtime/src/session.c s2a_session_on_restart_done，启动时登记一次）：不管设置页 / 游戏 ExecuteProgram / 测试开关谁发起，
//   新局起来后都在这里放「重启首帧屏障」（s2a_session_guest_start_prepare 立的客体冻结 + GPU hold）。屏障防的是重建期间 App 进了后台、
//   新局在后台提交 GPU：所以只在 UIKit 确认前台活跃时放；不活跃就留给「回前台 / 获得焦点」那次 s2a_ios_suspend(0) 放
static void restart_done(unsigned generation) {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (UIApplication.sharedApplication.applicationState == UIApplicationStateActive) s2a_ios_suspend(0, "重启完成");
        else LOG("iOS 壳：会话 %u 重启完成，App 不在前台活跃，屏障留到回前台放", generation);
    });
}
// 设置页「立刻重启」的请求回调：只回设置页结果；屏障由 restart_done 放（控制线程先调它，两者都派主线程，先放屏障后回设置页）
static void settings_restart_result(int result, const char *detail, void *context) {
    void (^completion)(NSString *) = CFBridgingRelease(context);
    NSString *error = result ? (detail ? [NSString stringWithUTF8String:detail] : @"游戏未能重新启动，请查看诊断日志。") : nil;
    dispatch_async(dispatch_get_main_queue(), ^{ completion(error); });
}
static void settings_attach_runtime(void) {
    S2ASettingsRuntime *runtime = [S2ASettingsRuntime new];
    runtime.manageSaves = ^(UIViewController *presenter) { s2a_ios_save_present(presenter); };
    runtime.multiGameContainer = s2a_launch_ctx_container() != 0;   // 设置页「换一款游戏」说明（一进程一款，换游戏要划掉重开）
    runtime.canRestart = ^BOOL { return s2a_host_session_state() == S2A_SESSION_RUNNING; };
    runtime.restart = ^(void (^completion)(NSString *)) {
        void *context = (void *)CFBridgingRetain([completion copy]);
        int result = s2a_host_restart(settings_restart_result, context);
        if (!result) { s2a_controller_set_foreground(0); return; }
        void (^rejected)(NSString *) = CFBridgingRelease(context);
        NSString *error = result == -3 ? @"游戏正在重启，请稍候。" : result == -2 ? @"当前构建不支持完整重启。" : @"当前没有可重启的游戏会话。";
        rejected(error);
    };
    s2a_settings_set_runtime(runtime);
    // 测试开关 S2A_TEST_RESTART_AFTER=<秒>（11-04 阶段 0 验收「进程内重启」，真机局无人点设置页）：到点代按设置页「立刻重启」，走同一个 runtime.restart；
    //   进程内只触发一次（静态标记跨进程内重启保留，不会重启后再触发）。结果写 host.log，判据见 Windows出包线状态文件
    { static int armed; const char *e = getenv("S2A_TEST_RESTART_AFTER"); double sec = e ? atof(e) : 0;
      if (sec > 0 && !armed) { armed = 1; LOG("设置页：测试开关 S2A_TEST_RESTART_AFTER=%s，%.0f 秒后代按「立刻重启」", e, sec);
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(sec * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            if (!runtime.canRestart()) { LOG("【红】测试重启：当前不能重启（会话不在运行）"); return; }
            LOG("测试重启：代按「立刻重启」");
            runtime.restart(^(NSString *err) { if (err) LOG("【红】测试重启失败：%s", err.UTF8String); else LOG("测试重启：完成，新会话已起"); }); }); } }
}
static void *deferred_host_thread(void *arg) {
    s2a_boot_entry entry = (s2a_boot_entry)arg;
    @autoreleasepool { entry(); }
    return NULL;
}
void s2a_host_shutdown(const char *why);
#include <mach/mach.h>
uint64_t s2a_ap_frames(void);   // ap_host.c：上屏帧数
void s2a_guest_run_mark(int running);   // vawin.c：「本局在跑」标记（前台写、后台 / 将终止删；下一局见标记还在 = 上局在前台死掉，强制开客体内存文件后备）
static unsigned long long s2a_footprint_mb(void) { task_vm_info_data_t v; mach_msg_type_number_t n = TASK_VM_INFO_COUNT;
    return task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&v, &n) == KERN_SUCCESS ? v.phys_footprint >> 20 : 0; }
// 客体内存分区账（S2A_MEM_SPLIT=1，缺省关；11-05 时之笛读档进场景被系统杀）：
//   堆区当前段、别名区（MapPhysicalMemory）各扫一次常驻 / 压缩，窗口其余（代码、栈、线程局部、共享 / 传输内存等）= 窗口合计 − 两者，窗口外 ≈ 我方。
//   kinds：再带我方细分（图形层 nvn_be_mem_kinds：驻留集里的私有贴图 / 私有缓冲（上传暂存块为主）/ 游戏池零拷贝缓冲虚拟长度与其中常驻页 / 其它共享缓冲），
//   以及实占的内核分项（task_vm_info：匿名 internal、已压缩、图形账 graphics）。注意 mincore「常驻」≠ 计进实占：M5 Pro 实测把没写过的 1 GB
//   零拷贝缓冲放进驻留集，全机 wired +1024 MB、mincore 常驻 1024 MB，phys_footprint 不变（报告 41）；所以「窗口外 ≈ 实占 − 窗口合计」在池被钉住时偏小，看内核分项
static int mem_split_on(void) { static int on = -1; if (on < 0) { const char *e = getenv("S2A_MEM_SPLIT"); on = e && *e && strcmp(e, "0"); } return on; }
static void mem_split_line(const char *tag, unsigned long long fp, uint64_t wr, uint64_t wo, int kinds) {
    uint64_t hl, hn, al, an, ph, hr, ho, ar, ao; s2a_mem_guest_split(&hl, &hn, &al, &an, &ph); s2a_range_resident(hl, hn, &hr, &ho); s2a_range_resident(al, an, &ar, &ao);
    uint64_t rest_r = wr > hr + ar ? wr - hr - ar : 0, rest_o = wo > ho + ao ? wo - ho - ao : 0;
    char k[640] = "";
    void nvn_be_mem_kinds(uint64_t *, uint64_t *, uint64_t *, uint64_t *, uint64_t *, uint64_t *) __attribute__((weak_import));   // 直连图形层（通用模式没有这个符号）
    if (kinds && nvn_be_mem_kinds) { uint64_t pt, pb, pl, pc, so, no; nvn_be_mem_kinds(&pt, &pb, &pl, &pc, &so, &no);
        snprintf(k, sizeof k, "｜我方细分（驻留集 %llu 个对象）：私有贴图 %llu MB、私有缓冲（上传暂存块为主）%llu MB、其它共享缓冲 %llu MB、游戏池零拷贝缓冲 %llu MB（其中常驻 / 压缩 %llu MB，含被驻留集钉住的未写页）",
                 (unsigned long long)no, (unsigned long long)(pt >> 20), (unsigned long long)(pb >> 20), (unsigned long long)(so >> 20), (unsigned long long)(pl >> 20), (unsigned long long)(pc >> 20)); }
    if (kinds) { task_vm_info_data_t v; mach_msg_type_number_t n = TASK_VM_INFO_COUNT; size_t o = strlen(k);
        if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&v, &n) == KERN_SUCCESS && o < sizeof k)
            snprintf(k + o, sizeof k - o, "｜内核分项：匿名 %llu MB、已压缩 %llu MB、图形账 %llu MB（其中压缩 %llu MB）", (unsigned long long)(v.internal >> 20), (unsigned long long)(v.compressed >> 20),
                     (unsigned long long)(v.ledger_tag_graphics_footprint >> 20), (unsigned long long)(v.ledger_tag_graphics_footprint_compressed >> 20)); }
    LOG("[%s] 帧 %llu 实占 %llu MB｜堆 已设 %llu MB：常驻 %llu MB、压缩 %llu MB｜物理映射区 已映射 %llu MB：常驻 %llu MB、压缩 %llu MB｜窗口其余 常驻 %llu MB、压缩 %llu MB｜窗口外（我方）≈ 实占 − 窗口合计 = %lld MB%s",
        tag, (unsigned long long)s2a_ap_frames(), fp, (unsigned long long)(hn >> 20), (unsigned long long)(hr >> 20), (unsigned long long)(ho >> 20), (unsigned long long)(ph >> 20), (unsigned long long)(ar >> 20), (unsigned long long)(ao >> 20),
        (unsigned long long)(rest_r >> 20), (unsigned long long)(rest_o >> 20), (long long)fp - (long long)((wr + wo) >> 20), k);
}
// 跳变采样（11-05 第二十二版读档局：约 4 秒内实占涨 3.5 GB 到 8192 MB 被杀，10 秒一次的心跳看不到构成）：后台线程每 100 ms 取一次实占，
//   比上次打印涨 ≥ 256 MB 就当场打一行分区账 + 我方细分（降回 256 MB 以下时只把基线跟下来，不打）。日志是逐行 write 进 host.log，
//   进程被系统 SIGKILL 时已 write 的行在内核页缓存里不丢，所以被杀前最后一跳的那行在。打一行的扫描（整窗 mincore + 驻留集分类）约毫秒级，只在跳变时做
enum { MEM_STEP_MB = 256, MEM_STEP_MS = 100 };
static void *mem_split_thread(void *arg) {
    (void)arg; pthread_setname_np("s2a-memstep");
    unsigned long long last = s2a_footprint_mb();
    for (;;) {
        struct timespec ts = {0, MEM_STEP_MS * 1000000L}; nanosleep(&ts, NULL);
        unsigned long long fp = s2a_footprint_mb(); if (!fp) continue;
        if (fp + MEM_STEP_MB <= last) { last = fp; continue; }
        if (fp < last + MEM_STEP_MB) continue;
        uint64_t wr, wo; s2a_window_resident(&wr, &wo);
        char tag[64]; snprintf(tag, sizeof tag, "内存跳变 +%llu MB", fp - last);
        mem_split_line(tag, fp, wr, wo, 1); last = fp;
    }
    return NULL;
}
static void mem_split_start(void) {
    static atomic_int started; if (atomic_exchange(&started, 1)) return;
    pthread_t th; pthread_attr_t a; pthread_attr_init(&a); pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
    if (pthread_create(&th, &a, mem_split_thread, NULL)) LOG("【红】 内存跳变采样线程起不来，只有 10 秒一次的心跳分区账");
    else LOG("内存跳变采样：每 %d ms 取一次实占，比上次打印涨 ≥ %d MB 就打一行「[内存跳变 …]」（S2A_MEM_SPLIT）", MEM_STEP_MS, MEM_STEP_MB);
    pthread_attr_destroy(&a);
}
static UIView *g_root;
static _Atomic int g_render_failed;
static _Atomic int g_gpu_stopped;   // GPU 挂死安全停页在屏上（s2a_ios_gpu_stopped 置位）：停帧看门的提示不盖掉它
void s2a_guest_freeze(int on);                   // thread.c
void s2a_host_focus_set(int focused);            // os.c（接口 v2.15）
void s2a_host_sleep_set(int sleeping);           // os.c（接口 v2.15）
void nxgal_gpu_hold(int on, const char *why);    // gal_core.m
static _Atomic int g_bg;              // 进过后台、还没恢复
static _Atomic uint64_t g_fg_note;   // 最近一次「将回前台」通知的时刻（恢复时间线起点）
void s2a_resume_timeline(uint64_t t_note, uint64_t t_ds, uint64_t t_gpu, uint64_t t_thr, uint64_t t_done);   // ap_host.c
static void s2a_ios_suspend(int on, const char *why) {
    int transition = s2a_session_background_begin(on);
    if (transition == S2A_SESSION_LIFECYCLE_DEFERRED) {
        s2a_controller_set_foreground(0);
        if (on) atomic_store(&g_bg, 1);
        else if (!atomic_load(&g_render_failed) && s2a_host_session_state() == S2A_SESSION_RUNNING) {
            uint64_t t_ds, t_gpu, t_thr, t_done;
            if (s2a_session_guest_start_complete(&t_ds, &t_gpu, &t_thr, &t_done)) {
                s2a_controller_set_foreground(1);
                if (atomic_exchange(&g_bg, 0)) {
                    uint64_t t0 = atomic_load(&g_fg_note); if (!t0 || t0 > t_ds) t0 = t_ds;
                    s2a_resume_timeline(t0, t_ds, t_gpu, t_thr, t_done);
                }
                LOG("iOS 壳：重启期间进入后台后回前台 → 新会话已恢复");
            }
        }
        return;
    }
    if (transition == S2A_SESSION_LIFECYCLE_TERMINAL) {
        s2a_controller_set_foreground(0); return;
    }
    if (transition == S2A_SESSION_LIFECYCLE_BUSY) {
        // UIKit lifecycle notifications are main-thread serialized; retry only
        // if a future caller reaches here from a concurrent lifecycle source.
        s2a_controller_set_foreground(0);
        dispatch_async(dispatch_get_main_queue(), ^{
            UIApplicationState state = UIApplication.sharedApplication.applicationState;
            if ((on && state == UIApplicationStateBackground) || (!on && state != UIApplicationStateBackground))
                s2a_ios_suspend(on, why);
        });
        return;
    }
    // A failed render startup is terminal for this session. Foregrounding must
    // not restart the guest behind the error page.
    if (atomic_load(&g_render_failed)) { s2a_controller_set_foreground(0); s2a_guest_freeze(1); s2a_session_background_end(); return; }
    s2a_controller_set_foreground(!on);
    // 焦点报给服务层（接口 v2.15，缺口扫描族 1）：服务层把它转成游戏的焦点消息（失焦 / 回到前台）；进后台的消息在回前台解冻后游戏才读到
    s2a_host_focus_set(!on); s2a_host_sleep_set(on);   // iOS 后台同时算睡眠（psc 休眠 / 唤醒请求）
    // 直连模式进出后台（双轨 §12.2，s2a_direct_suspend 在直连图形层 graphics/nvn/host_adapt.m）：运行时取符号，没链直连层的构建取不到就不调；通用模式下它是空操作
    uint64_t t_ds = now_ns();
    { static void (*ds)(int); static int got; if (!got) { ds = (void (*)(int))dlsym(RTLD_DEFAULT, "s2a_direct_suspend"); got = 1; } if (ds) ds(on); }
    // 游戏时钟（#495）：进后台最先冻、回前台最后放——解冻瞬间先跑的线程看到的是停住的时钟，不会看到后台那段时间
    if (on) { guest_clock_freeze_for(1, 1); s2a_guest_freeze(1); nxgal_gpu_hold(1, why); atomic_store(&g_bg, 1); LOG("iOS 壳：%s → 游戏时钟与线程冻结、GPU 停止提交", why); }
    else {
        uint64_t t1 = now_ns(); nxgal_gpu_hold(0, why); uint64_t t2 = now_ns(); s2a_guest_freeze(0); guest_clock_freeze_for(1, 0); uint64_t t3 = now_ns();
        s2a_clock_catchup();   // 游戏日历（#863）：后台那段（含锁屏睡眠）补进 Ryujinx 稳态时钟；计数器仍从冻结那一刻接着走（#495）
        if (atomic_exchange(&g_bg, 0)) {   // 从后台回来的第一次（回前台 / 获得焦点两条通知都会到这里）：打恢复时间线（ap_host.c）
            uint64_t t0 = atomic_load(&g_fg_note); if (!t0 || t0 > t_ds) t0 = t_ds;
            s2a_resume_timeline(t0, t_ds, t1, t2, t3);
        }
        LOG("iOS 壳：%s → 恢复", why);
    }
    s2a_session_background_end();
}
// 提示 / 出错页（notice.m 画；11-02 维护者：满屏、一句人话 + 下一步、和加载页同一套样子、技术细节进诊断包不上屏）。窗口还没建时只记下，建窗口时显示
static NSString *g_msg;   // 正在屏上（或待显示）的提示，s2a_ios_clear_message 按它认
static NSArray *g_note;   // 待显示 / 正显示的提示页参数：@[符号, 标题, 正文, 进度, 进度小字, 是否给「导出诊断包」]
static UIView *g_note_view;
static UIView *g_help; static NSArray *g_help_args; static void (*g_help_recheck)(void);   // 缺资源包说明页（见 s2a_ios_show_pack_help）
void s2a_diag_export(void);
static void note_build(void) {
    [g_note_view removeFromSuperview]; g_note_view = nil;
    if (!g_root || !g_note) return;
    NSMutableArray *bs = [NSMutableArray array];
    if (g_note.count > 6) { NSString *text = g_note[6];   // 「复制诊断」（初启失败页）：诊断文本放进剪贴板，玩家直接粘贴发给我们
        [bs addObject:s2a_notice_button(@S2A_INIT_FAIL_COPY, YES, ^(UIButton *b) { UIPasteboard.generalPasteboard.string = text; [b setTitle:@S2A_INIT_FAIL_COPIED forState:UIControlStateNormal]; LOG("初启失败页：诊断已复制到剪贴板"); })]; }
    if ([g_note[5] boolValue]) [bs addObject:s2a_notice_button(@"导出诊断包", NO, ^(UIButton *b) { (void)b; s2a_diag_export(); })];
    g_note_view = s2a_notice_page(g_root.bounds, g_note[0], g_note[1], g_note[2], [g_note[3] floatValue], [g_note[4] length] ? g_note[4] : nil, bs);
    [g_root addSubview:g_note_view];
}
// 可从任意线程调（导入资源的后台线程用它刷进度）：主线程上直接改，别的线程派回主线程。done / total：total > 0 时画进度条；diag：给「导出诊断包」按钮
void s2a_ios_show_notice(const char *symbol, const char *title, const char *body, int done, int total, int diag) {
    NSString *sy = @(symbol), *t = @(title), *b = @(body ?: ""), *key = [NSString stringWithFormat:@"%@\n\n%@", t, b];
    NSArray *n = @[sy, t, b, @(total > 0 ? (float)done / total : -1.0f), total > 0 ? [NSString stringWithFormat:@"第 %d / %d 批", done < total ? done + 1 : total, total] : @"", @(diag != 0)];
    void (^show)(void) = ^{ g_msg = key; g_note = n; [g_help removeFromSuperview]; g_help = nil; g_help_args = nil; note_build(); };
    if (NSThread.isMainThread) show(); else dispatch_async(dispatch_get_main_queue(), show);
}
// 老入口（停帧看门 ap_host.c 经 dlsym 调）：第一段当标题、其余当正文
void s2a_ios_show_message(const char *m) {
    if (atomic_load(&g_gpu_stopped)) return;   // GPU 安全停页在屏上：不盖掉
    const char *nl = strchr(m, '\n'); char t[512]; size_t k = nl ? (size_t)(nl - m) : strlen(m); if (k >= sizeof t) k = sizeof t - 1;
    memcpy(t, m, k); t[k] = 0; const char *b = nl ? nl : ""; while (*b == '\n') b++;
    s2a_ios_show_notice("info.circle.fill", t, b, 0, 0, 0);
}
// 收掉 s2a_ios_show_message 给的那句话（任意线程）：只在屏上还是同一句时收（停帧看门帧又走了时调，审计 P1-4；别的提示不动）
void s2a_ios_clear_message(const char *m) {
    if (atomic_load(&g_gpu_stopped)) return;
    NSString *s = @(m);
    void (^clear)(void) = ^{
        NSString *cur = [g_msg stringByReplacingOccurrencesOfString:@"\n\n" withString:@""], *want = [s stringByReplacingOccurrencesOfString:@"\n" withString:@""];
        if (![cur isEqualToString:want]) return;
        g_msg = nil; g_note = nil; [g_note_view removeFromSuperview]; g_note_view = nil;
    };
    if (NSThread.isMainThread) clear(); else dispatch_async(dispatch_get_main_queue(), clear);
}
// 缺资源包 / 资源包不全的说明页（pack_apply.c 闸门与游戏 Break 时调，任意线程）：不起游戏、不退出。示意（Mac → 线 → iPhone）+ 三步 + 「我已放好，重新检查」。
//   10-26 维护者：「壳上做好说明别闪退，打开发现缺资源包就告诉用户把启动器导出的资源包导到手机上指定位置」。窗口还没建时记下，建窗口时显示
// 「重新检查」按下后一直禁用到宿主核完：就绪收起整页，没就绪重画整页（新按钮可点、正文开头说「刚刚查过，还是没有…」，pack_apply.c s2a_pack_help_detail）。
//   不用定时恢复：原先 0.6 秒后就恢复可点，宿主还没核完、页面原样，玩家以为按钮没生效（审计 P2-2）
static void help_build(NSString *title, NSString *detail, NSString *app) {
    [g_help removeFromSuperview]; g_help = nil;
    if (!g_root) return;
    UIButton *btn = s2a_notice_button(@"我已放好，重新检查", YES, ^(UIButton *b) { b.enabled = NO; [b setTitle:@"正在检查…" forState:UIControlStateDisabled];
        void (*f)(void) = g_help_recheck; if (f) f(); });
    g_help = s2a_notice_help_page(g_root.bounds, title, detail, app, btn); [g_root addSubview:g_help];
    // 测试开关（真机验收没法点屏）：S2A_PACK_RECHECK_AUTO=<秒> 每隔这么久代按一次「重新检查」，说明页收起即停
    { static int armed; const char *e = getenv("S2A_PACK_RECHECK_AUTO"); double sec = e ? atof(e) : 0;
      if (sec > 0 && !armed) { armed = 1; LOG("说明页：测试开关 S2A_PACK_RECHECK_AUTO=%s，每 %.0f 秒代按「重新检查」", e, sec);
          [NSTimer scheduledTimerWithTimeInterval:sec repeats:YES block:^(NSTimer *tm) {
              if (!g_help || !g_help_recheck) { [tm invalidate]; armed = 0; return; }
              LOG("说明页：代按「重新检查」（测试开关）"); g_help_recheck(); }]; } }
}
void s2a_ios_show_pack_help(const char *title, const char *detail, const char *app, void (*recheck)(void)) {
    NSString *t = [NSString stringWithUTF8String:title], *d = [NSString stringWithUTF8String:detail], *a = [NSString stringWithUTF8String:app];
    void (^show)(void) = ^{ g_help_recheck = recheck; g_help_args = @[t, d, a]; [g_note_view removeFromSuperview]; g_note_view = nil; g_note = nil; g_msg = nil; help_build(t, d, a); };
    if (NSThread.isMainThread) show(); else dispatch_async(dispatch_get_main_queue(), show);
}
void s2a_ios_hide_pack_help(void) {
    dispatch_async(dispatch_get_main_queue(), ^{ g_help_args = nil; g_help_recheck = NULL; [g_help removeFromSuperview]; g_help = nil; });
}
// 收掉提示页（任意线程）：资源导入就绪、当场接着起游戏时调（pack_apply.c gate_thread）。导入进度 / 「还在传」那几页是提示页不是说明页，
//   只收说明页的话提示页留在出图层上面盖住游戏（提示页 zPosition 2000，出图层在最底层）
void s2a_ios_hide_notice(void) {
    dispatch_async(dispatch_get_main_queue(), ^{ g_msg = nil; g_note = nil; [g_note_view removeFromSuperview]; g_note_view = nil; });
}
// Called on the render thread only for attach failure, before GPU emulation.
// Leave UIKit and diagnostic export alive instead of throwing across NativeAOT.
// 直连安全网提示页（host_main.c 起游戏主线程前、本款直连已连续失败满 3 次时调，宿主线程）：同一套提示页样子（notice.m），一句话 + 「导出诊断包」「仍然启动」。
//   诊断信息（失败记录）进 host.log、随诊断包带走，不上屏（11-02 维护者：内部词不上屏）。阻塞到玩家点「仍然启动」才返回；宿主在主线程上时也一样等（审计 P2-16），见 s2a_ios_wait_ui_signal
@interface S2ADguardTarget : NSObject { @public dispatch_semaphore_t go; UIView *page; BOOL pending; } @end
@implementation S2ADguardTarget
@end
static S2ADguardTarget *g_dguard_target;
// 提示页（主线程，g_root 已建）
static void dguard_build(S2ADguardTarget *t) {
    __weak S2ADguardTarget *w = t;
    UIButton *diag = s2a_notice_button(@S2A_DGUARD_DIAG, NO, ^(UIButton *b) { (void)b; s2a_diag_export(); });
    UIButton *go = s2a_notice_button(@S2A_DGUARD_GO, YES, ^(UIButton *b) { (void)b; S2ADguardTarget *x = w; if (!x) return; [x->page removeFromSuperview]; x->page = nil; dispatch_semaphore_signal(x->go); });
    UIView *page = s2a_notice_page(g_root.bounds, @"exclamationmark.triangle.fill", @S2A_DGUARD_TITLE, @S2A_DGUARD_MSG, -1, nil, @[diag, go]);
    t->page = page; [g_root addSubview:page]; [g_root bringSubviewToFront:page];
    LOG("直连安全网：提示页已显示");
}
// 建窗口时调：窗口建好前就要提示的，现在补画
static void dguard_show_pending(void) {
    S2ADguardTarget *t = g_dguard_target;
    if (t && t->pending && g_root) { t->pending = NO; dguard_build(t); }
}
void s2a_ios_dguard_prompt(const char *diag) {
    S2ADguardTarget *t = [S2ADguardTarget new]; t->go = dispatch_semaphore_create(0); g_dguard_target = t;
    LOG("直连安全网：诊断信息（随诊断包带走，不上屏）\n%s", diag);
    // 窗口还没建：记下，建好窗口时补画（dguard_show_pending），调用方照旧等玩家选；不放行（审计 P2-5：原先直接照常起局，玩家不知道在强行再试直连）
    void (^show)(void) = ^{ if (g_root) dguard_build(t); else { t->pending = YES; LOG("直连安全网：窗口还没建，建好窗口再画提示页"); } };
    // 主线程上当场画（不派队列：调用方若正跑在主队列的块里，派出去的块要等它返回才轮到）；等待同一个接口，主线程上转运行循环
    if (NSThread.isMainThread) show(); else dispatch_async(dispatch_get_main_queue(), show);
    s2a_ios_wait_ui_signal(t->go);
}
void s2a_ios_render_init_failed(void) {
    LOG("【红】iOS 渲染初始化失败：请求暂停游戏并显示错误，保留日志与诊断入口");
    // Serialize the terminal state with UIKit foreground notifications, so a
    // foreground callback cannot undo the failure's guest freeze.
    dispatch_async(dispatch_get_main_queue(), ^{
        atomic_store(&g_render_failed, 1);
        s2a_controller_set_foreground(0); s2a_guest_freeze(1);
        s2a_ios_show_notice("exclamationmark.triangle.fill", "游戏画面没能启动", "请从后台划掉本 App，再重新打开。还是不行的话，点下面「导出诊断包」发给我们。", 0, 0, 1);
    });
    { int s2a_dump_threads(const char *why); s2a_dump_threads("iOS 出图层挂接失败"); }
    s2a_logcon_flush();
}
// GPU 挂死安全停页（11-07 Switch2 线 84；graphics/nvn/host_adapt.m s2a_gpu_safe_stop 经 dlsym 调，Metal 完成回调线程）：GPU 已在 nvn_mtl.m 那道门停交，这里冻游戏线程、
//   盖一张 UIKit 原生错误页（不经 Metal：上屏本身也是 GPU 提交），停在这页。复用渲染失败的终局标记：回前台不恢复游戏、不碰 GPU 闸门 / 后台排空（s2a_ios_suspend 早返），
//   之后停帧看门的提示（s2a_ios_show_message / clear_message）不盖掉它。玩家从后台划掉再开 = 新进程，唯一干净的重置
void s2a_ios_gpu_stopped(const char *why) {
    (void)why;   // 原文已由 nvn_mtl.m mtl_gpu_halt 打进 host.log（第一条错误原文、帧号、提交序号），不上屏
    atomic_store(&g_gpu_stopped, 1);
    LOG("【红】iOS 壳：GPU 挂死安全停 → 冻结游戏线程、显示错误页（UIKit），不再恢复本局");
    dispatch_async(dispatch_get_main_queue(), ^{
        atomic_store(&g_render_failed, 1);
        s2a_controller_set_foreground(0); s2a_guest_freeze(1);
        NSString *t = @"画面出错，已停止\nGraphics error — stopped", *b = @"请从后台划掉本 App，再重新打开游戏。\nPlease reopen the game.";
        g_msg = [NSString stringWithFormat:@"%@\n\n%@", t, b]; g_note = @[@"exclamationmark.triangle.fill", t, b, @(-1.0f), @"", @YES];
        [g_help removeFromSuperview]; g_help = nil; g_help_args = nil; note_build();
        if (g_note_view) [g_root bringSubviewToFront:g_note_view];
        LOG("GPU 挂死安全停页：%s", g_note_view ? "已显示" : "窗口还没建，建好窗口再画");
    });
    s2a_logcon_flush();
}
// 初启失败页（host_main.c failed_session，宿主线程；审计 #1534 P1-4）：初启失败 = 本进程终局，不在本进程重试。
//   原先直接 _exit，玩家只看到 App 自己消失；现在停在提示页：玩家话术一句 +「复制诊断」（剪贴板）+「导出诊断包」，请玩家从后台划掉再打开（新进程是唯一干净的重置）。
//   body 由 s2a_init_fail_screen 按错误码给（只含玩家话术），这里只负责显示；内部摘要只在 diag 里（11-02 维护者：内部词不上屏）。
//   复用渲染失败的终局标记：回前台不恢复游戏、不再碰服务层焦点 / GPU 闸门（s2a_ios_suspend）
void s2a_ios_init_failed(const char *body, const char *diag) {
    NSString *t = @S2A_INIT_FAIL_TITLE, *b = @(body ?: S2A_INIT_FAIL_MSG_GENERIC);
    NSString *d = @(diag ?: ""), *key = [NSString stringWithFormat:@"%@\n\n%@", t, b];
    dispatch_async(dispatch_get_main_queue(), ^{
        atomic_store(&g_render_failed, 1);
        s2a_controller_set_foreground(0); s2a_guest_freeze(1);
        g_msg = key; g_note = @[@"exclamationmark.triangle.fill", t, b, @(-1.0f), @"", @YES, d];
        [g_help removeFromSuperview]; g_help = nil; g_help_args = nil; note_build();
        if (g_note_view) [g_root bringSubviewToFront:g_note_view];
        LOG("初启失败页：%s", g_note_view ? "已显示" : "窗口还没建，建好窗口再画");
    });
}
UIView *s2a_ios_root_view(void) { return g_root; }
// 撑不住就问玩家（runtime/src/qprompt_host.c 调，宿主线程；判定 runtime/src/qprompt.c）：问过就记在本 App 设置（一款一个 App = 按游戏记住），
//   设置页「60 帧」「宽屏」开关读写的是同一个设置（S2AFps60 / S2AWidescreen），玩家随时改回。帧率选「切换」当场热切（s2a_fps_set_enabled(0)，游戏速度不变）；
//   宽屏选「切换」调满屏线 s2a_widen_force_native(1)（runtime/src/widen.c：本局钉成 1920×1080、热切到原生 16:9）：返回 1 = 已热切；
//   -1 = 游戏自己出宽图的款（第二层值槽装载时填死，裁中间会切掉贴边界面）→ 提示重启后生效（下次起局读 S2AWidescreen 关宽屏）
static NSString *qp_key(int kind) { return kind == 1 ? @"S2AFps60Asked" : @"S2AWidescreenAsked"; }
int s2a_ios_qp_asked(int kind) { return [NSUserDefaults.standardUserDefaults boolForKey:qp_key(kind)]; }
// 本款原生 60 帧（runtime/src/qprompt_host.c 按运行时实际状态判定后调）：记进本 App 设置，设置页 60 帧那行据此显示「本款原生 60 帧」、开关置灰（app_settings.m）
void s2a_ios_fps_native60(int on) { [NSUserDefaults.standardUserDefaults setBool:on != 0 forKey:@"S2AFpsNative60"]; }
void s2a_fps_set_enabled(int on);
int s2a_fps_hot_revert_ok(void);   // runtime/src/fps.c：1 = 运行中关 60 帧能完全回原帧率；0 = 游戏自己的限帧变量要重启才回（帧率表 w 行没写关值）
// 「切换到原生帧率」重启后生效（原生线 11-02：w 行写回的「原值」是初始化前的快照，八方2 / 巫师3 写 0 = 不限帧）：S2AFps60 已写 NO，新局帧率表起局就关。
//   能进程内重启就问要不要立刻重启（同设置页「立刻重启」s2a_host_restart）；重启失败或当前不能重启，提示从后台划掉再打开
static void qp_fps_restart_ask(void) {
    UIAlertController *r = [UIAlertController alertControllerWithTitle:@"重启游戏后生效"
        message:@"这款游戏的帧率上限由游戏自己记着，运行中改不回去，要重启游戏才会回到原生帧率。是否立刻重启？未保存的进度会丢失。" preferredStyle:UIAlertControllerStyleAlert];
    [r addAction:[UIAlertAction actionWithTitle:@"稍后自行重启" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) { (void)x; LOG("撑不住就问玩家：原生帧率重启后生效，玩家选稍后"); }]];
    [r addAction:[UIAlertAction actionWithTitle:@"立刻重启" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { (void)x;
        void (^done)(NSString *) = ^(NSString *error) {
            if (!error) { LOG("撑不住就问玩家：原生帧率，已进程内重启"); return; }
            LOG("撑不住就问玩家：原生帧率进程内重启没成：%s", error.UTF8String);
            UIAlertController *e = [UIAlertController alertControllerWithTitle:@"没能自动重启" message:[NSString stringWithFormat:@"%@\n请从后台划掉本 App 再打开，原生帧率就会生效。", error] preferredStyle:UIAlertControllerStyleAlert];
            [e addAction:[UIAlertAction actionWithTitle:@"知道了" style:UIAlertActionStyleCancel handler:nil]];
            UIViewController *p = g_root.window.rootViewController; while (p.presentedViewController) p = p.presentedViewController; [p presentViewController:e animated:YES completion:nil]; };
        void *context = (void *)CFBridgingRetain([done copy]);
        int result = s2a_host_session_state() == S2A_SESSION_RUNNING ? s2a_host_restart(settings_restart_result, context) : -1;
        if (!result) { s2a_controller_set_foreground(0); return; }
        void (^rejected)(NSString *) = CFBridgingRelease(context);
        rejected(result == -3 ? @"游戏正在重启，请稍候。" : result == -2 ? @"当前构建不支持完整重启。" : @"当前没有可重启的游戏会话。"); }]];
    UIViewController *p = g_root.window.rootViewController; while (p.presentedViewController) p = p.presentedViewController; [p presentViewController:r animated:YES completion:nil];
}
void s2a_dprompt_notice(NSString *title, NSString *body, double delay, double seconds);   // dprompt.m
// 60 帧降档 / 恢复（runtime/src/fps.c s2a_fps_cap_tick，审计 P1-80）：降档时设置里的 60 帧开关仍显示玩家选的「开」，画面已回到原生帧率——横幅说清楚，原因去掉自动恢复
void s2a_ios_fps_capped(int reason) {
    NSString *body = reason == 1 ? @"手机温度过高（危急），已暂时回到原生帧率，降温后自动恢复。" : reason == 2 ? @"低电量模式开着，已暂时回到原生帧率，关掉低电量模式后自动恢复。" : @"60 帧已恢复。";
    dispatch_async(dispatch_get_main_queue(), ^{ s2a_dprompt_notice(@"60 帧", body, 0, 6); });
}
// 档位梯子要下次启动才生效的换档（runtime/src/qladder_host.c：宽屏第二层按起局档位装表的款切主机 / 掌机）：一行横幅，不打断游戏
void s2a_ios_ladder_note(const char *msg) {
    NSString *body = @(msg ?: "");
    dispatch_async(dispatch_get_main_queue(), ^{ s2a_dprompt_notice(@"画面档位", body, 0, 6); });
}
// 满屏自检自动退回（runtime/src/qprompt_host.c widen_auto_revert，11-06 满屏泛化）：记进本 App 设置（宽屏关 = 下次起局 16:9，设置页可改回），横幅一行告诉玩家
//   persist（11-06 起）：两次不同启动都触发才记住（runtime/src/widen_remember.c）；只一次的不写设置，下次启动重新试满屏
void s2a_ios_widen_auto(int hot, int why, int persist) {
    if (persist) { NSUserDefaults *d = NSUserDefaults.standardUserDefaults; [d setBool:NO forKey:@"S2AWidescreen"]; [d setBool:YES forKey:qp_key(2)]; }
    NSString *body = persist ? (hot ? @"检测到满屏画面异常，已切回原生画面比例，之后都用原生比例。可在设置里重新打开满屏。" : @"检测到满屏画面异常，之后启动都会用原生画面比例。可在设置里重新打开满屏。")
                             : (hot ? @"检测到满屏画面异常，本次已切回原生画面比例，下次启动会再试满屏。" : @"检测到满屏画面异常，下次启动会再试满屏。");
    (void)why; dispatch_async(dispatch_get_main_queue(), ^{ s2a_dprompt_notice(@"满屏", body, 0, 6); });
}
void s2a_ios_qprompt(int kind, int secs) {   // 文案 维护者 11-02 定：「过去 XX 秒检测到自适应满屏/60帧不稳定，是否切换到原生画面比例/帧率。」
    dispatch_async(dispatch_get_main_queue(), ^{
        UIViewController *top = g_root.window.rootViewController; while (top.presentedViewController) top = top.presentedViewController;
        if (!top) { LOG("撑不住就问玩家：没有可弹窗的界面，这次不问"); return; }
        NSString *t = kind == 1 ? [NSString stringWithFormat:@"过去 %d 秒检测到 60 帧不稳定，是否切换到原生帧率？", secs]
                                : [NSString stringWithFormat:@"过去 %d 秒检测到自适应满屏不稳定，是否切换到原生画面比例？", secs];
        UIAlertController *a = [UIAlertController alertControllerWithTitle:t message:@"选择会记住，之后可在设置里随时改。" preferredStyle:UIAlertControllerStyleAlert];
        [a addAction:[UIAlertAction actionWithTitle:@"切换" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { (void)x;
            NSUserDefaults *d = NSUserDefaults.standardUserDefaults; [d setBool:YES forKey:qp_key(kind)];
            if (kind == 1) { [d setBool:NO forKey:@"S2AFps60"];
                if (s2a_fps_hot_revert_ok()) { s2a_fps_set_enabled(0); LOG("撑不住就问玩家：玩家选还原原生帧率，60 帧已热切关"); return; }
                LOG("撑不住就问玩家：玩家选还原原生帧率，这款要重启后生效（帧率表有没写关值的 w 行）"); qp_fps_restart_ask(); return; }
            [d setBool:NO forKey:@"S2AWidescreen"];
            if (s2a_widen_force_native(1) > 0) { LOG("撑不住就问玩家：玩家选原生画面比例，已运行中退回 16:9"); return; }
            LOG("撑不住就问玩家：玩家选原生画面比例，这款要重启后生效");
            UIAlertController *r = [UIAlertController alertControllerWithTitle:@"重启游戏后生效" message:@"这款游戏的宽画面由游戏自己画，需要从后台划掉本 App 再打开，才会切到 16:9。" preferredStyle:UIAlertControllerStyleAlert];
            [r addAction:[UIAlertAction actionWithTitle:@"知道了" style:UIAlertActionStyleCancel handler:nil]];
            UIViewController *p = g_root.window.rootViewController; while (p.presentedViewController) p = p.presentedViewController; [p presentViewController:r animated:YES completion:nil]; }]];
        [a addAction:[UIAlertAction actionWithTitle:@"保持" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) { (void)x;
            [NSUserDefaults.standardUserDefaults setBool:YES forKey:qp_key(kind)]; LOG("撑不住就问玩家：玩家选保持（%s）", kind == 1 ? "60 帧" : "宽屏"); }]];
        [top presentViewController:a animated:YES completion:nil];
        LOG("撑不住就问玩家：已弹「%s」", t.UTF8String);
    });
}
// 资源导入期间不让系统自动锁屏（锁屏后 App 被挂起、解包停住）；任意线程可调
void s2a_ios_keep_awake(int on) { dispatch_async(dispatch_get_main_queue(), ^{ UIApplication.sharedApplication.idleTimerDisabled = on ? YES : NO; }); }
// 宽屏用的屏幕宽（游戏 1080 高对应的宽）：横屏 = 1080 × 长边 ÷ 短边，取偶（iOS 壳锁横屏，进程内不变）
// 屏幕比例取 App 根视图实际所在的那块屏（视图控制器每次布局记下长边 ÷ 短边）：10-27 Duo 上 UIScreen.mainScreen.nativeBounds 一直报外屏 2034×1398（1.455），
//   内屏起局（视图 951×669 点，1.422）也按外屏比例出图、内屏上下留黑。视图还没布局时退回主屏原生尺寸。读的一方（runtime/src/widen.c）首次读后缓存，进程内不变
static _Atomic double g_view_ratio;
//   宽屏装载（模块装载后 0.15 秒、游戏线程）比场景连上早：非主线程第一次问时等场景记下比例，至多 5 秒；等不到 / 主线程上问才退回主屏原生尺寸（记红）
static double screen_ratio(void) {
    double r = g_view_ratio;
    if (!(r > 0) && !NSThread.isMainThread) for (int i = 0; i < 500 && !((r = g_view_ratio) > 0); i++) usleep(10000);
    if (r > 0) return r;
    CGSize z = UIScreen.mainScreen.nativeBounds.size; double a = fmax(z.width, z.height), b = fmin(z.width, z.height);
    return b > 0 ? a / b : 16.0 / 9.0;
}
int s2a_ios_screen_known(void) { return g_view_ratio > 0; }   // 窗口场景已连上、比例取自实际所在的屏（runtime/src/widen.c 到这时才缓存屏幕高）
int s2a_ios_screen_w(void) { double r = screen_ratio(); return ((int)(1080.0 * r)) & ~1; }
int s2a_ios_screen_h(void) { double r = screen_ratio(); return r * 9 < 16 ? ((int)(1920.0 / r)) & ~1 : 1080; }   // 比 16:9 窄的屏（Duo 内屏 951×669 点 → 1350）
// 游戏画面根控制器：只许横屏（Info.plist 同声明）；画面铺满全屏，四边系统边缘手势延后一拍（贴边控件一拖不退出游戏）、底部横条自动隐藏、状态栏隐藏（照旧库 SwGameVC）
@interface S2AGameVC : UIViewController
@end
@implementation S2AGameVC
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskLandscape; }
- (UIRectEdge)preferredScreenEdgesDeferringSystemGestures { return UIRectEdgeAll; }
- (BOOL)prefersHomeIndicatorAutoHidden { return YES; }
- (BOOL)prefersStatusBarHidden { return YES; }
- (void)viewDidLayoutSubviews {   // 屏幕尺寸变了（10-27 Duo 合上切外屏、展开回内屏）：记下当前屏的比例；直连出图层按新尺寸重摆（graphics/nvn/host_adapt.m；通用模式没有这个符号）
    [super viewDidLayoutSubviews];
    CGSize z = self.view.bounds.size; double a = fmax(z.width, z.height), b = fmin(z.width, z.height); if (b > 0) g_view_ratio = a / b;
    void s2a_direct_relayout(void) __attribute__((weak_import));
    if (s2a_direct_relayout) s2a_direct_relayout();
}
- (void)viewSafeAreaInsetsDidChange {   // 方向与视图尺寸记进日志（真机验横屏用）
    [super viewSafeAreaInsetsDidChange]; UIEdgeInsets i = self.view.safeAreaInsets; CGSize z = self.view.bounds.size; CGSize n = UIScreen.mainScreen.nativeBounds.size;
    UIInterfaceOrientation o = self.view.window.windowScene.interfaceOrientation;
    LOG("iOS 壳：朝向 %s（%ld），视图 %.0fx%.0f 点，屏幕 %.0fx%.0f 像素，屏幕宽（1080 高对应）%d；安全区 上 %.1f 左 %.1f 下 %.1f 右 %.1f",
        UIInterfaceOrientationIsLandscape(o) ? "横屏" : UIInterfaceOrientationIsPortrait(o) ? "竖屏" : "未知", (long)o, z.width, z.height, fmax(n.width, n.height), fmin(n.width, n.height),
        s2a_ios_screen_w(), i.top, i.left, i.bottom, i.right);
    // 横屏判据看落定后的朝向：每次变化后等 1 秒，那时仍不是横屏才报红。LiveContainer 里 UIKit 读的是 LC 本体的 Info.plist（竖屏排第一、没声明全屏），
    //   场景先以竖屏起、本类只许横屏随即转过来（10-30 测试设备1：视图 440x956 → 956x440，画面最终横屏满屏），转的那一下不是缺陷；原生安装起手就是横屏
    static unsigned gen; unsigned g = ++gen; __weak UIViewController *wself = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        UIViewController *s = wself; if (!s || g != gen) return;   // 1 秒内又变过：以最后一次为准
        UIInterfaceOrientation now = s.view.window.windowScene.interfaceOrientation;
        if (!UIInterfaceOrientationIsLandscape(now) && now != UIInterfaceOrientationUnknown) LOG("【红】iOS 壳：画面不是横屏（落定 1 秒后朝向 %ld）", (long)now);
    });
}
@end
@interface S2AAppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
- (void)startMonitoring;
@end
@implementation S2AAppDelegate
- (BOOL)application:(UIApplication *)app didFinishLaunchingWithOptions:(NSDictionary *)opts {
    display_focus_publish(app.applicationState == UIApplicationStateActive);
    if (!s2a_host_boot_deferred()) [self startMonitoring];
    return YES;
}
- (void)startMonitoring {
    display_focus_publish(UIApplication.sharedApplication.applicationState == UIApplicationStateActive);
    // iOS 27 场景生命周期（27.1 SDK 对链接新 SDK 的 App 强制 UIScene；没有场景代理 UIKit 会在
    // _UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption 启动即崩）：窗口 / 根视图 / 虚拟手柄挪到
    // S2ASceneDelegate 的 scene:willConnectToSession 里建（见下）。这里只留 App 级的电量 / 前后台 / 心跳 / 热。
    // 测试层契约第 4 节：每分钟一行「[电] 电量 <N>%，<充电中|已充满|未充电>」（拔电时电量闸 / 局中停局用）
    { void s2a_diag_boot(void); s2a_diag_boot(); }   // 导出诊断包（diag.m）：订阅 MetricKit、设置页开关回前台时检查
    UIDevice.currentDevice.batteryMonitoringEnabled = YES;
    void (^battery)(NSTimer *) = ^(NSTimer *t) { (void)t; UIDevice *d = UIDevice.currentDevice; UIDeviceBatteryState st = d.batteryState;
        LOG("[电] 电量 %d%%，%s", (int)lroundf(d.batteryLevel * 100), st == UIDeviceBatteryStateCharging ? "充电中" : st == UIDeviceBatteryStateFull ? "已充满" : "未充电"); };
    battery(nil); [NSTimer scheduledTimerWithTimeInterval:60 repeats:YES block:battery];
    // 前后台（10-20 测试设备1 塞尔达切桌面后 GPU 拒收后台提交、渲染器按出错退出）：iOS 只禁后台提交 GPU，进后台 = 游戏线程在系统调用出入口冻结 + GPU 闸门关
    // （不放新提交、已提交的做完才返回；queue:nil 在发通知的主线程上同步跑完，满足「进后台回调返回前停提交」）；回前台 = 放开，游戏接着跑，不重开
    // 只失去焦点（系统弹框 / 控制中心 / 来电横幅，仍在前台）不冻结：10-21 测试设备2 塞尔达起局 3 秒系统浮层失焦，旧做法冻到收局 54 秒没出首帧；
    // 同一浮层下随后起的暗黑3 从没「回前台」过、一直出帧——失焦期间画面照常可提交，冻结只该跟后台走
    // 用通知不用委托方法：场景式 / 旧式生命周期都发这几条
    s2a_session_on_restart_done(restart_done);   // 任何发起方的进程内重启完成都由壳放重启首帧屏障（见 restart_done）
    NSNotificationCenter *nc = NSNotificationCenter.defaultCenter;
    [nc addObserverForName:UIApplicationWillResignActiveNotification object:nil queue:nil usingBlock:^(NSNotification *n) { (void)n; display_focus_publish(NO); LOG("iOS 壳：失去焦点（仍在前台，游戏照跑）"); }];
    [nc addObserverForName:UIApplicationDidEnterBackgroundNotification object:nil queue:nil usingBlock:^(NSNotification *n) { (void)n; display_focus_publish(NO); s2a_dguard_background(1, now_ns()); { void s2a_diag_session_fg(int); s2a_diag_session_fg(0); } s2a_ios_suspend(1, "进后台"); s2a_guest_run_mark(0); }];   // 直连安全网：后台被杀 / 玩家划掉不算崩
    // 直连安全网：前台收到内存告警算这局的失败证据（之后被系统按内存杀掉进不了崩溃处理）；玩满 60 秒照样清零，进后台撤回
    [nc addObserverForName:UIApplicationDidReceiveMemoryWarningNotification object:nil queue:nil usingBlock:^(NSNotification *n) { (void)n;
        if (UIApplication.sharedApplication.applicationState != UIApplicationStateBackground) s2a_dguard_evidence("内存告警", 0); }];
    [nc addObserverForName:UIApplicationWillEnterForegroundNotification object:nil queue:nil usingBlock:^(NSNotification *n) { (void)n;
        display_focus_publish(NO);
        atomic_store(&g_fg_note, now_ns()); s2a_dguard_background(0, now_ns());
        // 回前台时可能停在失焦（浮层还在），等不到「获得焦点」：主线程下一拍状态已离开后台就放开
        dispatch_async(dispatch_get_main_queue(), ^{ if (UIApplication.sharedApplication.applicationState != UIApplicationStateBackground) s2a_ios_suspend(0, "回前台"); }); }];
    [nc addObserverForName:UIApplicationDidBecomeActiveNotification object:nil queue:nil usingBlock:^(NSNotification *n) { (void)n; display_focus_publish(UIApplication.sharedApplication.applicationState == UIApplicationStateActive); { void s2a_diag_session_fg(int); s2a_diag_session_fg(1); } s2a_ios_suspend(0, "获得焦点"); s2a_guest_run_mark(1); }];
    // 心跳：每 10 秒一行，被系统直接杀（SIGKILL 不进崩溃处理）时也能看到死前最后的帧号 / 热状态 / 内存（10-20 维护者 塞尔达闪退，日志 12 秒后无一行）
    // 低区余量（10-21 巫师3 玩约 2 分钟低区耗尽、Metal 申请失败崩溃）：每行带上，局中能看到往下掉的过程
    [NSTimer scheduledTimerWithTimeInterval:10 repeats:YES block:^(NSTimer *t) { (void)t;
        g_brightness = UIScreen.mainScreen.brightness;   // 自动亮度 / 手动调过也跟上（主线程）
        s2a_brightness_hold_log();
        // 前后台状态只能在主线程读；其余（整窗 mincore 扫描、分区账、能耗、输入延迟）放后台串行队列：11-05 时之笛客体内存文件后备后，
        //   整窗 64 GB 的 mincore 一次要数秒，在主线程上跑让系统卡顿检测连报 2–6 秒（MetricKit 卡顿诊断栈：startMonitoring 心跳块 → s2a_window_resident / mem_split_line）
        int app_state = MIN((int)UIApplication.sharedApplication.applicationState, 2);
        static dispatch_queue_t hb_q; static dispatch_once_t hb_once; dispatch_once(&hb_once, ^{ hb_q = dispatch_queue_create("s2a.heartbeat", DISPATCH_QUEUE_SERIAL); });
        dispatch_async(hb_q, ^{
        uint64_t lt, ll, fs, fh, wr = 0, wo = 0; int fn; s2a_low_holes(&lt, &ll); s2a_fd_usage(&fn, &fs, &fh);
        // 整窗 mincore（64 GB）只在内存分区诊断（S2A_MEM_SPLIT=1）开时扫（11-06 合入运维 / 性能线 A：Duo 热状态 2 下一次扫到心跳间隔超 30 秒，
        //   每 10 秒在后台吃秒级 CPU、分段持有进程地址映射锁，执行器还据此误判「出图停」）；缺省只报实占（phys_footprint，系统杀进程按它判），窗口常驻记「未扫」
        int scan = mem_split_on(); if (scan) s2a_window_resident(&wr, &wo);
        char wins[160]; if (scan) snprintf(wins, sizeof wins, "，客体窗口常驻 %llu MB、已压缩换出 %llu MB", (unsigned long long)(wr >> 20), (unsigned long long)(wo >> 20)); else snprintf(wins, sizeof wins, "，客体窗口常驻 未扫（S2A_MEM_SPLIT=1 才扫）");
        LOG("[心跳] 帧 %llu，热状态 %ld（0 正常 1 一般 2 严重 3 危急），进程实占 %llu MB，%s，持续执行档 %d（1 = 系统按稳态功率限制本进程，要权限 sustained-execution），低电量模式 %d，低区空洞合计 %llu MB、最大一块 %llu MB，文件句柄 %d / 软上限 %llu%s", (unsigned long long)s2a_ap_frames(), (long)NSProcessInfo.processInfo.thermalState,
            s2a_footprint_mb(), (const char *[]){"前台", "前台失焦", "后台"}[app_state],
            (int)[NSProcessInfo.processInfo hasPerformanceProfile:NSProcessPerformanceProfileSustained], (int)NSProcessInfo.processInfo.lowPowerModeEnabled, (unsigned long long)(lt >> 20), (unsigned long long)(ll >> 20), fn, (unsigned long long)fs, wins);
        // 本款本机实测峰值（vawin.c s2a_guest_mem_note：进程实占 + 文件后备里已落块的客体字节）：下一局起局按它判文件后备开不开（11-07 卡顿线）
        { void s2a_guest_mem_note(uint64_t footprint); s2a_guest_mem_note((uint64_t)s2a_footprint_mb() << 20); }
        int s2a_disk_written(uint64_t *bytes);   // energy.c（只此一处用，不进 native.h：那是出包抽取阶段的键）
        // 写盘量（11-05 能耗线，客体内存文件后备的代价）：本进程累计与近 10 秒，内存紧张时内核把文件脏页写回闪存的量在这里看
        { static uint64_t last; static int have; uint64_t w;
          if (!s2a_disk_written(&w)) { LOG("[写盘] 进程累计 %llu MB，近 10 秒 %llu MB（客体内存文件后备 %s）", (unsigned long long)(w >> 20), (unsigned long long)(have && w >= last ? (w - last) >> 20 : 0), s2a_guest_fd() >= 0 ? "开" : "关"); last = w; have = 1; } }
        // 客体内存分区账（S2A_MEM_SPLIT=1，缺省关）：心跳每 10 秒一行，另起 100 ms 采样线程抓跳变（mem_split_*）
        if (mem_split_on()) { mem_split_line("内存分区", s2a_footprint_mb(), wr, wo, 0); mem_split_start(); }
        s2a_energy_log();
        s2a_pad_latency latency; s2a_pad_get_latency(&latency, 1);
        if (latency.samples) LOG("[输入延迟] %llu 次变化：快照到HID提交 平均 %.3f ms / 最大 %.3f ms；读取到提交 平均 %.3f ms / 最大 %.3f ms（不含蓝牙和画面显示）",
            (unsigned long long)latency.samples, latency.host_total_us / (1000.0 * latency.samples), latency.host_max_us / 1000.0,
            latency.read_total_us / (1000.0 * latency.samples), latency.read_max_us / 1000.0);
        });
    }];   // 能耗与输入延迟同节拍，不逐帧写日志。
    [NSNotificationCenter.defaultCenter addObserverForName:NSProcessInfoThermalStateDidChangeNotification object:nil queue:nil usingBlock:^(NSNotification *n) { (void)n;
        LOG("[热] 热状态变为 %ld（0 正常 1 一般 2 严重 3 危急），帧 %llu", (long)NSProcessInfo.processInfo.thermalState, (unsigned long long)s2a_ap_frames()); }];
}
- (UIInterfaceOrientationMask)application:(UIApplication *)app supportedInterfaceOrientationsForWindow:(UIWindow *)w { return UIInterfaceOrientationMaskLandscape; }
- (void)applicationWillTerminate:(UIApplication *)app {
    s2a_guest_run_mark(0);   // 正常终止不算「前台死掉」
    if (s2a_host_boot_pending()) return;
    s2a_controller_set_foreground(0); s2a_host_shutdown("iOS 终止回调");
}
@end
// iOS 27 场景生命周期代理：窗口在这里建并绑 windowScene。Info.plist 的 UIApplicationSceneManifest 指向本类。
// 前后台仍走 app delegate 里注册的 NSNotification（场景式 / 旧式都发这几条），不另挪场景回调。
@interface S2ASceneDelegate : UIResponder <UIWindowSceneDelegate>
@property (strong, nonatomic) UIWindow *window;
@property BOOL managingSaves;
@end
// 亮度保持（10-30 维护者）：只在起局设一次留不住——测试设备开着自动亮度，0.5 设下去随后被系统拉回 0.20–0.33（10-30 暗黑3 测试设备2 一局），
//   那局整机瓦数就不是 0.5 口径。设了 S2A_BRIGHTNESS 时每 2 秒、以及亮度变化通知时重设回目标；整局实际亮度的最小 / 最大值在每次检查时
//   记下，由心跳行与收局行带出（tools/quality_check.py 读「亮度保持」行：偏离设定超过 0.05 判局无效）
static double g_bright_target = -1, g_bright_min = 2, g_bright_max = -1; static unsigned g_bright_resets;
static void brightness_check(UIScreen *scr) {
    double now = scr.brightness;
    if (now < g_bright_min) g_bright_min = now;
    if (now > g_bright_max) g_bright_max = now;
    if (fabs(now - g_bright_target) > 0.005) { scr.brightness = g_bright_target; g_bright_resets++; }
    g_brightness = scr.brightness;
}
static void brightness_hold(UIScreen *scr, double b) {
    g_bright_target = b;
    [NSTimer scheduledTimerWithTimeInterval:2 repeats:YES block:^(NSTimer *t) { (void)t; brightness_check(scr); }];
    [NSNotificationCenter.defaultCenter addObserverForName:UIScreenBrightnessDidChangeNotification object:scr queue:NSOperationQueue.mainQueue
                                                usingBlock:^(NSNotification *n) { (void)n; brightness_check(scr); }];
}
// 收局 / 心跳都打：「亮度保持：设定 0.50，实际最小 0.50 最大 0.50，重设 N 次」（没设 S2A_BRIGHTNESS 不打）
void s2a_brightness_hold_log(void) {
    if (g_bright_target < 0) return;
    LOG("亮度保持：设定 %.2f，实际最小 %.2f 最大 %.2f，重设 %u 次", g_bright_target, g_bright_min, g_bright_max, g_bright_resets);
}
// 主屏长按快捷操作（Info.plist UIApplicationShortcutItems，定义在 ios/shortcuts.json，runtime/build.mk 写入）：
//   维护者 11-02「App 不是可以在主界面摁住弹出快捷设置么」——10-27 runtime/home-actions 分支做过、没合入 main；这里只接 main 现成的三条流程
//   冷启动（App 没开时选）：置开关，走原有打开流程；热启动（游戏开着时选）：就地执行
void s2a_diag_export(void);
void s2a_diag_open_menu(void);
static void home_shortcut(NSString *type, BOOL cold, UIViewController *presenter) {
    LOG("主屏快捷操作：%s（%s）", type.UTF8String, cold ? "冷启动" : "运行中");
    if ([type isEqualToString:@"dev.s2arm.save"]) {
        // 冷启动：同「设置 → 管理存档」置的开关（save_transfer.m ManageKey），sceneDidBecomeActive 先进存档管理页
        if (cold) [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"S2ASaveManageOnOpen"];
        else if (presenter) s2a_ios_save_present(presenter);
    } else if ([type isEqualToString:@"dev.s2arm.diag"]) {
        // 冷启动：同设置页「下次打开时导出诊断包」开关（diag.m kOpenToggle，打开时导出并复位）
        if (cold) [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"S2ADiagExportOnOpen"];
        else s2a_diag_export();
    } else if ([type isEqualToString:@"dev.s2arm.settings"]) {
        // 冷启动：游戏界面建好后 2 秒开设置页（diag.m 读 S2A_OPEN_SETTINGS 的同一条路）；运行中直接开（四指长按同一入口）
        if (cold) setenv("S2A_OPEN_SETTINGS", "2", 1);
        else s2a_diag_open_menu();
    }
}
// 多款容器（launchctx.c ③，11-07 P2 单款启动门）：场景先挂游戏库；Play → s2a_launch_ctx_select → 起局前检查 → 换游戏界面 → 现行起宿主流程。一进程一款
static _Atomic int g_game_picked;   // 本进程已选过游戏（选上之后才放行 sceneDidBecomeActive 起宿主）
static void top_alert(UIWindow *w, NSString *title, NSString *body) {
    UIViewController *top = w.rootViewController; while (top.presentedViewController) top = top.presentedViewController;
    UIAlertController *a = [UIAlertController alertControllerWithTitle:title message:body preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"好" style:UIAlertActionStyleDefault handler:nil]];
    [top presentViewController:a animated:YES completion:nil];
}
@interface S2ASceneDelegate ()
- (void)enterGame;
@end
static void library_play(const char *gid, void *ctx) {   // 主线程（s2a_shell.h 约定）
    S2ASceneDelegate *sd = (__bridge S2ASceneDelegate *)ctx;
    if (atomic_load(&g_game_picked)) {
        LOG("游戏库：同一进程第二次 Play %s，拒绝（一进程一款）", gid);
        top_alert(sd.window, @"请关掉 App 重新打开", @"同一次打开只能玩一款游戏。请从后台划掉 App 后重新打开，再选这款。"); return;
    }
    const char *why = s2a_launch_ctx_select(gid);
    if (why) { top_alert(sd.window, @"这款游戏打不开", @(why)); return; }
    atomic_store(&g_game_picked, 1);
    s2a_boot_rebase("点 Play（多款容器选了游戏、启动上下文已冻结）");   // 起局计时从这里算，库界面停留不计
    LOG("游戏库：选了 %s，%s", gid, s2a_host_pre_ui_checks());
    [sd enterGame];
}
// 游戏库按款核资源包（11-07 Duo「游戏没能启动：系统功能没能准备好」：库里对资源包不在机上的款仍给 Play，服务层读 资源包/exefs/main.npdm 抛 DirectoryNotFound）。
//   库界面（S2ArmShellHost）经 s2a_shell.h 的这两个口问 / 报；这里是强定义，盖过 S2ArmShellBridge 里的弱缺省。判据同起局前的导入闸门（pack_apply.c s2a_import_state，
//   按本款文档根 Documents/games/<游戏号>）：就绪或已拷到待解开（Play 后闸门解开）= 可开始；不在 / 不完整 = 库里「需要游戏数据」、点了给下面的说明
static __weak UIWindow *g_lib_window;
int s2a_import_state_game_quick(const char *proc_docs, const char *gid);   // pack_apply.c
static int game_pack_state(const char *gid) { char d[1024]; s2a_container_docs(d, sizeof d); return s2a_import_state_game_quick(d, gid); }
void s2a_shell_library_appeared(void) { static int once; if (!once++) BOOT_MARK("游戏库界面可操作"); }
int s2a_shell_game_data_ready(const char *gid) {
    int st = game_pack_state(gid);
    return st == 0 || st == 1;   // S2A_IMPORT_READY / S2A_IMPORT_PENDING
}
static void library_pack_help(NSString *gid, BOOL again) {
    int st = game_pack_state(gid.UTF8String);
    if (st == 0 || st == 1) { LOG("游戏库：%s 资源包已就绪，刷新库界面", gid.UTF8String); if (s2a_shell_refresh) s2a_shell_refresh(); return; }
    BOOL broken = st == 3;   // S2A_IMPORT_BROKEN
    LOG("游戏库：%s 资源包%s，给导入说明%s", gid.UTF8String, broken ? "不完整" : "不在机上", again ? "（重新检查后仍不行）" : "");
    NSString *body = broken
        ? @"这款游戏的资源只拷了一部分，多半是拷到一半断了。请在 Mac 上的 S2Arm 里对这款游戏再点一次「装到 iPhone」，它会把缺的补上。补好后点「重新检查」。"
        : [NSString stringWithFormat:@"这款游戏的资源还不在手机上。请在 Mac 上的 S2Arm 里对这款游戏点「装到 iPhone」，资源会自动拷进来；"
                                     "也可以用访达把它的「…资源包」文件夹拷进本 App「文件」里的 games/%@ 文件夹。放好后点「重新检查」。", gid];
    if (again) body = [@"刚刚查过，还是没有完整的资源包。\n\n" stringByAppendingString:body];
    UIWindow *w = g_lib_window; UIViewController *top = w.rootViewController; while (top.presentedViewController) top = top.presentedViewController;
    if (!top) { LOG("【红】游戏库：没有窗口，导入说明画不出来"); return; }
    UIAlertController *a = [UIAlertController alertControllerWithTitle:broken ? @"游戏资源不完整" : @"还缺游戏资源" message:body preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"稍后" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) { (void)x; if (s2a_shell_refresh) s2a_shell_refresh(); }]];
    [a addAction:[UIAlertAction actionWithTitle:@"重新检查" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { (void)x; library_pack_help(gid, YES); }]];
    [top presentViewController:a animated:YES completion:nil];
}
void s2a_shell_pick_game_data(const char *gid) {   // 主线程（s2a_shell.h 约定）
    if (!gid || !*gid) return;
    library_pack_help(@(gid), NO);
}
@implementation S2ASceneDelegate
// 多款容器：根控制器先是游戏库；壳没链库界面（缺省 make ios）时给提示页，不起游戏
- (void)showLibrary {
    if (!s2a_shell_library_vc || !s2a_shell_set_play_handler) {
        UIViewController *vc = [S2AGameVC new]; vc.view.backgroundColor = UIColor.blackColor; g_root = vc.view; self.window.rootViewController = vc;
        LOG("【红】多款容器：这个壳没带游戏库界面（出壳要 make ios S2A_SHELLUI=1），不起游戏");
        s2a_ios_show_notice("exclamationmark.triangle.fill", "这个版本缺游戏库界面", "这个 App 装了多款游戏，但程序本体不带选择游戏的界面。请用新版启动器重新安装。", 0, 0, 1);
        return;
    }
    NSString *cat = [@(s2a_launch_ctx_bundle_dir()) stringByAppendingPathComponent:@"s2a_catalogue.json"];
    s2a_shell_set_play_handler(library_play, (__bridge void *)self);
    g_lib_window = self.window;
    self.window.rootViewController = (__bridge_transfer UIViewController *)s2a_shell_library_vc(cat.fileSystemRepresentation);
    LOG("游戏库：已挂库界面（清单 %s）%s", cat.UTF8String, s2a_process_prewarmed() ? "，预热进程只显示库、不起宿主" : "");
}
// Play 选上之后：换成游戏界面（同单款 willConnect 建的那套：加载页、提示页、安全网提示），再走同一条起宿主路（存档管理 → startHost）
- (void)enterGame {
    UIViewController *vc = [S2AGameVC new]; vc.view.backgroundColor = UIColor.blackColor; g_root = vc.view; g_note_view = nil;
    self.window.rootViewController = vc;
    { const char *e = getenv("S2A_PACK_APPLY_ONLY"); if (!g_msg && !(e && *e == '1')) s2a_loadpage_attach(vc.view); }
    note_build();
    if (g_help_args) help_build(g_help_args[0], g_help_args[1], g_help_args[2]);
    dguard_show_pending();
    [self sceneDidBecomeActive:self.window.windowScene];
}
- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    (void)session;
    if (options.shortcutItem) home_shortcut(options.shortcutItem.type, YES, nil);
    UIWindowScene *ws = (UIWindowScene *)scene;
    // 屏幕亮度（S2A_BRIGHTNESS=0–1，缺省不动；帧率能耗线 10-26）：整机瓦数含屏幕，和小菠萝「亮度 0.5」读数同口径对比时用。
    //   放在建场景这里：sceneDidBecomeActive 非预热路径会早返（10-26 R41 三局都没设上）
    { const char *e = getenv("S2A_BRIGHTNESS"); double b = e && *e ? atof(e) : -1;
      if (b >= 0 && b <= 1) { ws.screen.brightness = b; LOG("屏幕亮度设为 %.2f（S2A_BRIGHTNESS）", b); brightness_hold(ws.screen, b); } }
    g_brightness = ws.screen.brightness;
    self.window = [[UIWindow alloc] initWithWindowScene:ws];
    { CGSize z = self.window.bounds.size; double a = fmax(z.width, z.height), b = fmin(z.width, z.height); if (b > 0) g_view_ratio = a / b; }   // 当前屏的比例（宽屏装载在等它，见 screen_ratio）
    if (s2a_launch_ctx_container() && !atomic_load(&g_game_picked)) {   // 多款容器：先出游戏库，不建游戏界面、不挂加载页
        [self showLibrary];
        { const char *e = getenv("S2A_NO_KEY_WINDOW"); if (e && !strcmp(e, "0")) [self.window makeKeyAndVisible]; else self.window.hidden = NO; }
        LOG("iOS 壳：窗口已建 %.0fx%.0f（场景，多款容器游戏库）", self.window.bounds.size.width, self.window.bounds.size.height);
        return;
    }
    UIViewController *vc = [S2AGameVC new]; vc.view.backgroundColor = UIColor.blackColor; g_root = vc.view;
    self.window.rootViewController = vc;
    // 首次加载页 / 简短启动画面（loadpage.m）：点开立即出现；自检不过的提示、只解包模式不进游戏，不挂
    { const char *e = getenv("S2A_PACK_APPLY_ONLY"); if (!g_msg && !(e && *e == '1')) s2a_loadpage_attach(vc.view); }
    // 窗口只显示、不成为 key 窗口（缺省；S2A_NO_KEY_WINDOW=0 回到 makeKeyAndVisible）。10-26 测试设备2 全量系统日志：游戏模式开局弹游戏浮层，浮层收起后键盘事件转发目标回到
    //   我们 key 窗口所在的场景，UIKit 重建键盘代理、拉起听写（AFDictationConnection）查网络，包名禁网即弹「无线数据」框。触摸按落点分发、手柄走 GameController，都不靠 key 窗口
    //   10-26 测试设备1 同包对位：不成为 key 的两局浮层照出、App 进程听写 / 网络检测 0 行；对照局 3 行
    { const char *e = getenv("S2A_NO_KEY_WINDOW"); if (e && !strcmp(e, "0")) [self.window makeKeyAndVisible]; else { self.window.hidden = NO; LOG("iOS 壳：窗口只显示、不成为 key 窗口"); } }
    note_build();
    if (g_help_args) help_build(g_help_args[0], g_help_args[1], g_help_args[2]);
    dguard_show_pending();
    LOG("iOS 壳：窗口已建 %.0fx%.0f（场景）", self.window.bounds.size.width, self.window.bounds.size.height);
    if (!s2a_host_boot_pending()) [self prepareGameView];
}
- (void)prepareGameView {
    UIViewController *vc = self.window.rootViewController;
    settings_attach_runtime();
    if (!g_msg) { s2a_vpad_attach(vc.view); void s2a_dprompt_attach(UIView *root); s2a_dprompt_attach(vc.view); }   // 触屏虚拟手柄（vpad.m）；游戏内提示条（dprompt.m）
    // 四指长按 1.5 秒打开设置；提示页（缺资源 / 导入失败）保留诊断按钮。
    { void s2a_diag_attach(UIViewController *vc); s2a_diag_attach(vc); }
    if (!g_msg) { void s2a_hud_attach(UIView *root); s2a_hud_attach(vc.view); }   // 开发浮层（hud.m，S2A_HUD=1 才开）
    s2a_loadpage_front();   // 加载页压回手柄 / 浮层上面（首帧前不露出手柄）
    if (g_help) [g_root bringSubviewToFront:g_help];   // 老路径说明页先于手柄建，压回最上
    s2a_ios_root_view_ready();   // RenderLoop can now attach below the virtual controls.
}
- (void)windowScene:(UIWindowScene *)windowScene performActionForShortcutItem:(UIApplicationShortcutItem *)item completionHandler:(void (^)(BOOL))done {
    (void)windowScene; UIViewController *top = self.window.rootViewController; while (top.presentedViewController) top = top.presentedViewController;
    home_shortcut(item.type, NO, top); done(YES);
}
- (void)sceneDidBecomeActive:(UIScene *)scene {
    (void)scene;
    if (!s2a_host_boot_pending()) return;
    if (s2a_launch_ctx_container() && !atomic_load(&g_game_picked)) return;   // 多款容器：等游戏库 Play（预热进程同样只显示库）
    if (self.managingSaves) return;
    if (s2a_ios_save_management_requested()) {
        self.managingSaves = YES;
        __weak S2ASceneDelegate *weakSelf = self;
        self.window.rootViewController = s2a_ios_save_manager(^{
            S2ASceneDelegate *owner = weakSelf;
            if (!owner) return;
            UIViewController *game = [S2AGameVC new]; game.view.backgroundColor = UIColor.blackColor;
            g_root = game.view; g_note_view = nil; owner.window.rootViewController = game; dguard_show_pending();
            s2a_loadpage_attach(game.view);
            owner.managingSaves = NO; [owner startHost];
        });
        return;
    }
    [self startHost];
}
- (void)startHost {
    s2a_boot_entry entry = s2a_host_boot_take();
    if (!entry) return;   // 先领取，再启动；后续激活只走现有恢复路径
    int inputError = s2a_ios_input_config_load();
    if (inputError) { LOG("【红】iOS 输入配置损坏，拒绝启动（%d: %s）", inputError, s2a_input_config_error_message(inputError)); exit(2); }
    s2a_log_session_start();
    BOOT_MARK("预热 → 场景激活");
    { uint64_t base, size; s2a_window_get(&base, &size);
      const char *ao = getenv("S2A_PACK_APPLY_ONLY");
      if (ao && *ao == '1') LOG("预热激活：解包专用局，没占客体窗口（vawin.c）");
      else LOG("预热激活：沿用构造函数窗口 %#llx +%#llx（%s）", (unsigned long long)base, (unsigned long long)size, s2a_window_ok() ? "已占整窗" : "占窗失败，交由自检提示"); }
    [(S2AAppDelegate *)UIApplication.sharedApplication.delegate startMonitoring];
    [self prepareGameView];
    { void s2a_iml_probe_check(void); s2a_iml_probe_check(); }   // D：首启 IML 复核（iml_probe.m）：核实 IML 权限是否保留，丢了打日志并写 diag/iml_probe.json
    LOG("预热启动：前台已激活，启动宿主");
    // C# 启动含大块栈上结构；不能用 iOS pthread 默认的 512 KB 栈（HID SharedMemory.Create 会越界）。
    pthread_t th; pthread_attr_t at; int rc = pthread_attr_init(&at);
    if (rc) die("预热宿主线程属性初始化失败（%d）", rc);
    rc = pthread_attr_setstacksize(&at, 8 << 20);
    if (rc) die("预热宿主线程 8 MB 栈设置失败（%d）", rc);
    rc = pthread_create(&th, &at, deferred_host_thread, (void *)entry); pthread_attr_destroy(&at);
    if (rc) die("预热宿主线程起不来（pthread_create=%d）", rc);
    pthread_detach(th);
}
@end
void s2a_front_run(void (*start_host)(void)) {
    // 预热路径已在主线程运行 UIKit；宿主工作线程的提示页 / 导入 / 正常收尾不能再次进入 UIApplicationMain。
    if (atomic_exchange(&g_front_running, 1)) return;
    if (start_host && s2a_host_boot_defer(start_host, 1) < 0) die("宿主启动续体冲突");
    @autoreleasepool { char *argv[] = {"S2Arm", NULL}; UIApplicationMain(1, argv, nil, NSStringFromClass([S2AAppDelegate class])); }
}

// 直连图形层（graphics/nvn hle_nvn.c）读设备热状态 / 低电量模式的强定义：那边只有弱定义（热状态 -1、低电量 0，给 Mac 与单测用），
//   iOS 壳此前没给强定义，热状态帧率帽和帧率台阶里按热状态 / 低电量的判断在 iPhone 上都拿到默认值、等于没生效（10-26 帧率能耗线移植热状态帧率帽时查出）。
//   NSProcessInfo 的这两个属性可在任意线程读
double dm_brightness_now(void) { return g_brightness; }
int dm_thermal_now(void) { return (int)NSProcessInfo.processInfo.thermalState; }
int dm_low_power_now(void) { return NSProcessInfo.processInfo.lowPowerModeEnabled ? 1 : 0; }
