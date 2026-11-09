// 解包单测：用系统 tar 以执行器同样的参数（pax、中文长路径、子目录、空文件）打包，s2a_pack_apply 解到临时 Documents，逐字节核对；
// 再核：.incoming 里的 tar 解完即删、.applied = manifest 的 index_sha256、unpack.log 一行无「返回 -」；坏包（路径含 ..）判失败且日志带「返回 -3」。
#include "../src/native.h"
#include "s2a_launch_ctx.h"
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/resource.h>
#include <signal.h>
#include <fcntl.h>
#include <unistd.h>
#if defined(__linux__)
#define TAR_OPTIONS "--format pax --no-xattrs --blocking-factor=1"
#define TAR_RENAME "--transform='s,^a.txt,x/../../逃逸.txt,'"
#else
#define TAR_OPTIONS "--format pax --no-xattrs --no-mac-metadata"
#define TAR_RENAME "-s ',^a.txt,x/../../逃逸.txt,'"
#endif
int s2a_pack_apply(const char *docs); int s2a_untar(const char *tar, const char *root, unsigned *nf, uint64_t *nb);
int s2a_import_docs_pick(const s2a_launch_ctx *c, int game_data_env, const char *proc_docs, char *out, size_t n); int s2a_import_state_game(const char *proc_docs, const char *gid);
int s2a_import_state_game_quick(const char *proc_docs, const char *gid);
int s2a_import_state(const char *docs); void s2a_pack_help_detail(int st, int again, char *d, size_t n); extern void (*s2a_pack_apply_progress)(int done, int total);
// 与 runtime/src/pack_apply.c 同（闸门下一步与指纹记录）
enum { S2A_GATE_READY, S2A_GATE_IMPORT, S2A_GATE_SETTLE, S2A_GATE_WAIT, S2A_GATE_STALLED, S2A_GATE_NONE, S2A_GATE_BROKEN };
struct s2a_gate_watch { int n, moved; uint64_t fp; time_t changed; };
int s2a_import_transferring(const char *root, char *why, size_t n); int s2a_import_decide(const char *docs, struct s2a_gate_watch *w, time_t now, char *why, size_t n);
static int g_pd = -1, g_pt = -1, g_pcalls; static void prog(int d, int t) { g_pd = d; g_pt = t; g_pcalls++; }
uint64_t now_ns(void) { return 0; }
static int bad;
#define CHECK(c, ...) do { if (!(c)) { printf("✗ " __VA_ARGS__); printf("\n"); bad++; } } while (0)
static int sh(const char *c) { return system(c); }
// 造一个 512 字节的 ustar 头（name / size / 类型 / 校验和），给手写的异常 pax 头用
static void tar_hdr(uint8_t h[512], const char *name, unsigned size, char type) {
    memset(h, 0, 512); snprintf((char *)h, 100, "%s", name); memcpy(h + 100, "0000644", 8); memcpy(h + 108, "0000000", 8); memcpy(h + 116, "0000000", 8);
    snprintf((char *)h + 124, 12, "%011o", size); memcpy(h + 136, "00000000000", 12); h[156] = (uint8_t)type; memcpy(h + 257, "ustar", 6); memcpy(h + 263, "00", 2);
    unsigned sum = 0; for (int i = 0; i < 512; i++) sum += (i >= 148 && i < 156) ? ' ' : h[i]; snprintf((char *)h + 148, 8, "%06o", sum); h[155] = ' ';
}
// 在子进程里跑 s2a_untar：返回「返回 -N」或「崩溃（信号 N）」，崩了不拖死整个单测
static void untar_child(const char *tar, const char *root, char *out, size_t n) {
    pid_t pid = fork(); if (pid == 0) { unsigned nf = 0; uint64_t nb = 0; int r = s2a_untar(tar, root, &nf, &nb); _exit(r < 0 ? -r : 0); }
    int st = 0; waitpid(pid, &st, 0);
    if (WIFSIGNALED(st)) snprintf(out, n, "崩溃（信号 %d）", WTERMSIG(st)); else snprintf(out, n, "返回 -%d", WEXITSTATUS(st));
}
static int same(const char *a, const char *b) { char c[4096]; snprintf(c, sizeof c, "cmp -s '%s' '%s'", a, b); return sh(c) == 0; }
// Original synthetic tar headers; no game or user data. Lengths and expected rc are hand-written.
static void tar_entry(FILE *f, const char *name, char type, const char *data, size_t n) {
    uint8_t h[512], zero[512] = {0}; tar_hdr(h, name, (unsigned)n, type);
    CHECK(fwrite(h, 1, sizeof h, f) == sizeof h, "合成 tar 头写失败");
    CHECK(fwrite(data, 1, n, f) == n, "合成 tar 内容写失败");
    size_t pad = (512 - n % 512) % 512;
    CHECK(fwrite(zero, 1, pad, f) == pad, "合成 tar 补齐失败");
}
static void boundary_cases(const char *T) {
    const char *records[] = {"7 path\n12 path=abc\n", "12 path=abc\n"};
    char path[1024], root[1024]; snprintf(root, sizeof root, "%s/pax", T); CHECK(!mkdir(root, 0755), "建 pax 目录失败");
    for (unsigned i = 0; i < 2; i++) {
        snprintf(path, sizeof path, "%s/pax-%u.tar", T, i); FILE *f = fopen(path, "wb");
        CHECK(f != NULL, "造 pax 文件失败"); if (!f) continue;
        tar_entry(f, "PaxHeader", 'x', records[i], strlen(records[i]));
        tar_entry(f, "original", '0', "ok", 2); char zero[1024] = {0}; fwrite(zero, 1, sizeof zero, f); fclose(f);
        fflush(NULL); pid_t pid = fork(); CHECK(pid >= 0, "pax fork 失败");
        if (!pid) { unsigned nf = 0; uint64_t nb = 0; int rc = s2a_untar(path, root, &nf, &nb);
            _exit(i == 0 ? (rc == -2 && nf == 0 && nb == 0 ? 0 : 1) : (rc == 0 && nf == 1 && nb == 2 ? 0 : 1)); }
        if (pid > 0) { int status = 0; CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
            "pax case %u: 期望 %s 且不崩，wait status=%d", i, i == 0 ? "-2/0文件/0字节" : "0/1文件/2字节", status); }
    }
    snprintf(path, sizeof path, "%s/abc", root); FILE *f = fopen(path, "rb"); char value[3] = {0};
    CHECK(f && fread(value, 1, 2, f) == 2 && !strcmp(value, "ok"), "合法 pax path 内容不符"); if (f) fclose(f);
}

int main(void) {
    char T[] = "/tmp/s2a_t_pack_XXXXXX"; if (!mkdtemp(T)) return 1;
    boundary_cases(T);
    char c[8192];
    snprintf(c, sizeof c, "cd %s && mkdir -p src/romfs/很长的中文目录名用来超过一百个字节的老式头部限制很长的中文目录名用来超过一百个字节/子 docs/资源包/.incoming && "
        "head -c 70000 /dev/urandom > 'src/romfs/很长的中文目录名用来超过一百个字节的老式头部限制很长的中文目录名用来超过一百个字节/子/文件.bin' && : > src/空.txt && printf abc > src/a.txt && "
        "cd src && COPYFILE_DISABLE=1 tar " TAR_OPTIONS " -cf ../docs/资源包/.incoming/1_000.tar romfs a.txt && "
        "COPYFILE_DISABLE=1 tar " TAR_OPTIONS " -cf ../docs/资源包/.incoming/1_001.tar 空.txt && cd .. && "
        "printf '{\"version\":2,\"index_sha256\":\"deadbeef01\"}' > docs/资源包/manifest.json && "
        "printf 'a.txt\\t3\\tx\\n空.txt\\t0\\tx\\nromfs/很长的中文目录名用来超过一百个字节的老式头部限制很长的中文目录名用来超过一百个字节/子/文件.bin\\t70000\\tx\\n' > docs/资源包/index.tsv && "
        "mkdir -p docs/资源包/old && head -c 3000 /dev/zero > docs/资源包/old/part000 && printf p > docs/资源包/.pushed && mkdir -p docs/资源包/shaders/direct/b-0a7dae3c1e7b docs/资源包/.hid/空", T);
    CHECK(sh(c) == 0, "造包失败");
    char docs[512]; snprintf(docs, sizeof docs, "%s/docs", T);
    CHECK(s2a_pack_apply(docs) == 0, "解包返回非 0");
    snprintf(c, sizeof c, "%s/src/romfs/很长的中文目录名用来超过一百个字节的老式头部限制很长的中文目录名用来超过一百个字节/子/文件.bin", T); char d[8192];
    snprintf(d, sizeof d, "%s/docs/资源包/romfs/很长的中文目录名用来超过一百个字节的老式头部限制很长的中文目录名用来超过一百个字节/子/文件.bin", T);
    CHECK(same(c, d), "长中文路径文件逐字节不符");
    snprintf(c, sizeof c, "%s/src/a.txt", T); snprintf(d, sizeof d, "%s/docs/资源包/a.txt", T); CHECK(same(c, d), "a.txt 不符");
    struct stat st; snprintf(d, sizeof d, "%s/docs/资源包/空.txt", T); CHECK(!stat(d, &st) && st.st_size == 0, "空文件没解出来");
    snprintf(d, sizeof d, "%s/docs/资源包/.incoming/1_000.tar", T); CHECK(stat(d, &st) != 0, "解完的 tar 没删");
    snprintf(c, sizeof c, "grep -qx deadbeef01 %s/docs/资源包/.applied", T); CHECK(sh(c) == 0, ".applied 不对");
    snprintf(c, sizeof c, "grep -q '解开 2 个包 3 个文件' %s/docs/unpack.log && ! grep -q '返回 -' %s/docs/unpack.log", T, T); CHECK(sh(c) == 0, "unpack.log 行不对");
    snprintf(c, sizeof c, "test $(grep -c '进度：解开 1_00' %s/docs/unpack.log) = 2 && grep -q '清掉不在 index.tsv 里的旧文件 1 个' %s/docs/unpack.log", T, T); CHECK(sh(c) == 0, "进度行（每个 tar 一行）或清旧文件行不对");
    snprintf(d, sizeof d, "%s/docs/资源包/old/part000", T); CHECK(stat(d, &st) != 0, "不在 index.tsv 里的旧分卷没删");
    snprintf(d, sizeof d, "%s/docs/资源包/.pushed", T); CHECK(stat(d, &st) == 0, "执行器记账文件 .pushed 被误删");
    // 删空了的目录一并清掉（10-25 马车8 旧直连库 b-<链>/ 空目录残留）；有文件的目录、以 . 开头的目录（.incoming / .hid）留着
    snprintf(d, sizeof d, "%s/docs/资源包/old", T); CHECK(stat(d, &st) != 0, "旧分卷删光后的空目录 old/ 没删");
    snprintf(d, sizeof d, "%s/docs/资源包/shaders", T); CHECK(stat(d, &st) != 0, "空目录链 shaders/direct/b-…/ 没删干净");
    snprintf(d, sizeof d, "%s/docs/资源包/romfs/很长的中文目录名用来超过一百个字节的老式头部限制很长的中文目录名用来超过一百个字节/子", T); CHECK(stat(d, &st) == 0, "有文件的目录被误删");
    snprintf(d, sizeof d, "%s/docs/资源包/.incoming", T); CHECK(stat(d, &st) == 0, ".incoming 被误删");
    snprintf(d, sizeof d, "%s/docs/资源包/.hid/空", T); CHECK(stat(d, &st) == 0, "以 . 开头目录下的空目录被误删");
    // 坏包：路径含 ..（tar 默认会去掉 ..，用 -s 改名造出来）
    snprintf(c, sizeof c, "cd %s/src && COPYFILE_DISABLE=1 tar " TAR_OPTIONS " " TAR_RENAME " -cf ../docs/资源包/.incoming/2_000.tar a.txt 2>/dev/null", T); sh(c);
    CHECK(s2a_pack_apply(docs) != 0, "含 .. 的包应判失败");
    snprintf(c, sizeof c, "tail -1 %s/docs/unpack.log | grep -q '返回 -3'", T); CHECK(sh(c) == 0, "失败行应含「返回 -3」");
    snprintf(d, sizeof d, "%s/docs/逃逸.txt", T); CHECK(stat(d, &st) != 0, "路径逃逸写出了资源包目录");
    // 写失败要带现场（10-20 测试设备2 巫师3 连续「返回 -4」看不出哪个条目）：资源包下已有同名普通文件 aoc，包里要建 aoc/ 目录 →
    // 期望「返回 -4」，且同一行带「现场：建目录 <…>/aoc：errno 20 Not a directory」（ENOTDIR = 20）
    snprintf(c, sizeof c, "cd %s && rm -f docs/资源包/.incoming/2_000.tar && printf x > docs/资源包/aoc && mkdir -p src2/aoc && printf y > src2/aoc/d.bin && "
        "cd src2 && COPYFILE_DISABLE=1 tar " TAR_OPTIONS " -cf ../docs/资源包/.incoming/3_000.tar aoc/d.bin", T);
    CHECK(sh(c) == 0, "造冲突包失败");
    CHECK(s2a_pack_apply(docs) != 0, "同名文件挡路应判失败");
    snprintf(c, sizeof c, "tail -1 %s/docs/unpack.log | grep -q '返回 -4.*现场：建目录 .*/资源包/aoc：errno 20 Not a directory'", T); CHECK(sh(c) == 0, "失败行应含「返回 -4」与建目录现场（errno 20）");
    // 已应用索引：解包成功时 index.tsv 整份存成 index.applied.tsv（执行器据它算补推）
    snprintf(c, sizeof c, "cmp -s %s/docs/资源包/index.tsv %s/docs/资源包/index.applied.tsv", T, T); CHECK(sh(c) == 0, "解包成功后 index.applied.tsv 应与 index.tsv 相同");
    // 截短包（推送中途被杀留下的）：① 截在条目边界——条目完整、只少了归档尾两块全零（旧代码当成解完）；② 截在条目中间。
    //   期望：判失败「返回 -4」并注明「截短包：已删」，tar 已删、排在它后面的待解包与 .pushed 也删（执行器只按 index.applied.tsv 补推）；.applied / index.applied.tsv 不动（机上状态没变）
    for (int k = 0; k < 2; k++) {
        snprintf(c, sizeof c, "cd %s && rm -f docs/资源包/.incoming/*.tar docs/资源包/aoc && cp docs/资源包/index.applied.tsv ia.bak && cd src && "
            "COPYFILE_DISABLE=1 tar " TAR_OPTIONS " -cf ../t.tar a.txt romfs && cd .. && "
            "n=$(wc -c < t.tar) && head -c $(( %s )) t.tar > docs/资源包/.incoming/4_000.tar && cp t.tar docs/资源包/.incoming/4_001.tar && printf 'a.txt\\th\\n' > docs/资源包/.pushed && "
            "printf '{\"version\":2,\"index_sha256\":\"deadbeef02\"}' > docs/资源包/manifest.json", T, k == 0 ? "n - 1024" : "n / 2");
        CHECK(sh(c) == 0, "造截短包失败（%d）", k);
        CHECK(s2a_pack_apply(docs) != 0, "截短包应判失败（%s）", k == 0 ? "截在条目边界" : "截在条目中间");
        snprintf(c, sizeof c, "tail -1 %s/docs/unpack.log | grep -q '截短包：已删.*返回 -4'", T); CHECK(sh(c) == 0, "失败行应注明截短包已删、返回 -4（%d）", k);
        snprintf(d, sizeof d, "%s/docs/资源包/.incoming/4_000.tar", T); CHECK(stat(d, &st) != 0, "截短包应已删（%d）", k);
        snprintf(d, sizeof d, "%s/docs/资源包/.incoming/4_001.tar", T); CHECK(stat(d, &st) != 0, "截短包后面的待解包也应删（%d）", k);
        snprintf(d, sizeof d, "%s/docs/资源包/.pushed", T); CHECK(stat(d, &st) != 0, "推送记录 .pushed 应删（%d）", k);
        snprintf(c, sizeof c, "grep -qx deadbeef01 %s/docs/资源包/.applied && cmp -s %s/ia.bak %s/docs/资源包/index.applied.tsv", T, T, T); CHECK(sh(c) == 0, ".applied / index.applied.tsv 不应变（%d）", k);
    }
    // 核对机上实况（10-25 测试设备2：推了 4 批、解开 0 个包却记成已应用）：index.tsv 里有、没有任何推送包送来的文件 →
    //   期望判失败「返回 -8」、行里写明缺几个和第一个的路径；.applied / index.applied.tsv 不动（执行器下次按旧的已应用索引补推）；
    //   核对在清旧文件之前：核不过不删任何文件（旧包的文件都还在）
    snprintf(c, sizeof c, "cd %s && rm -f docs/资源包/.incoming/*.tar && cp docs/资源包/index.applied.tsv ia.bak && "
        "printf 'a.txt\\t3\\tx\\nshaders/direct/keys.tsv\\t504067\\tx\\n' > docs/资源包/index.tsv && "
        "printf '{\"version\":2,\"index_sha256\":\"deadbeef03\"}' > docs/资源包/manifest.json", T);
    CHECK(sh(c) == 0, "造缺文件夹具失败");
    CHECK(s2a_pack_apply(docs) != 0, "索引里的文件机上不在，应判失败");
    snprintf(c, sizeof c, "tail -1 %s/docs/unpack.log | grep -q '1 个文件机上缺或大小不符（例：shaders/direct/keys.tsv（不在））.*返回 -8'", T); CHECK(sh(c) == 0, "失败行应写明缺 1 个、例 keys.tsv、返回 -8");
    snprintf(c, sizeof c, "grep -qx deadbeef01 %s/docs/资源包/.applied && cmp -s %s/ia.bak %s/docs/资源包/index.applied.tsv", T, T, T); CHECK(sh(c) == 0, "缺文件时 .applied / index.applied.tsv 不应变");
    snprintf(c, sizeof c, "cd %s && printf 'a.txt\\t4\\tx\\n' > docs/资源包/index.tsv", T); sh(c);
    snprintf(d, sizeof d, "%s/docs/资源包/空.txt", T); CHECK(stat(d, &st) == 0, "核对没过不应清旧文件（空.txt 被删了）");
    CHECK(s2a_pack_apply(docs) != 0, "大小不符应判失败");
    snprintf(c, sizeof c, "tail -1 %s/docs/unpack.log | grep -q '例：a.txt（大小不符）.*返回 -8'", T); CHECK(sh(c) == 0, "失败行应写明 a.txt 大小不符");
    // pax 扩展头记录缺空格（11-01 审计 P2-17）：头数据 "4abc\0"（长度 5），记录「4abc」长度 4 合法、没有空格；旧代码 strchr 到 \0 得 NULL、+1 后解引用崩。
    //   期望按语义手写：判头坏「返回 -2」、不崩；unpack 级的现场串里写明是 pax 记录格式异常（这里直接调 s2a_untar，只核返回）
    { char tp[2100], r[64], root2[2100]; snprintf(tp, sizeof tp, "%s/badpax.tar", T); snprintf(root2, sizeof root2, "%s/badpax_root", T); mkdir(root2, 0755);
      uint8_t h[512], z[1024] = {0}; int fd = open(tp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
      tar_hdr(h, "PaxHeader/x", 5, 'x'); write(fd, h, 512); memset(h, 0, 512); memcpy(h, "4abc", 5); write(fd, h, 512); write(fd, z, sizeof z); close(fd);
      untar_child(tp, root2, r, sizeof r);
      CHECK(!strcmp(r, "返回 -2"), "pax 记录缺空格：期望 返回 -2 → %s", r); }
    // .applied 不先截断（11-01 审计 P2-16）：旧 .applied = oldmark；这次 manifest 的 index_sha256 是 64 字符，子进程把单文件上限设成 32 字节（SIGXFSZ 忽略），
    //   index.applied.tsv（10 字节）写得下、.applied（64 字节）写不下。期望按语义手写：解包判失败（退出码 1）、.applied 仍是 oldmark、不留 .applied.tmp。
    //   旧代码 fopen("w") 先把 .applied 截成 0 字节再写失败 → 旧标记丢了
    { char D[2100], c2[4096], ap[2200], got[128] = ""; struct stat s2; snprintf(D, sizeof D, "%s/atom", T);
      snprintf(c2, sizeof c2, "mkdir -p '%s/资源包' && cd '%s/资源包' && printf abc > a.txt && printf 'a.txt\\t3\\tx\\n' > index.tsv && printf oldmark > .applied && "
          "printf '{\"version\":2,\"index_sha256\":\"%%064d\"}' 7 > manifest.json", D, D);
      CHECK(sh(c2) == 0, "造 .applied 夹具失败");
      pid_t pid = fork(); if (pid == 0) { signal(SIGXFSZ, SIG_IGN); struct rlimit rl = {32, 32}; setrlimit(RLIMIT_FSIZE, &rl); _exit(s2a_pack_apply(D)); }
      int st = 0; waitpid(pid, &st, 0); int ec = WIFEXITED(st) ? WEXITSTATUS(st) : 100 + WTERMSIG(st);
      snprintf(ap, sizeof ap, "%s/资源包/.applied", D); FILE *f = fopen(ap, "r"); if (f) { size_t k = fread(got, 1, sizeof got - 1, f); got[k] = 0; fclose(f); }
      snprintf(ap, sizeof ap, "%s/资源包/.applied.tmp", D);
      CHECK(ec == 1 && !strcmp(got, "oldmark") && stat(ap, &s2) != 0, ".applied 写失败：期望 退出 1 / oldmark / 无 .applied.tmp → 退出 %d / %s / %s", ec, got, stat(ap, &s2) ? "无 .applied.tmp" : "留了 .applied.tmp"); }
    // 玩家导入闸门（用户体验线 10-23）：状态 0 就绪 / 1 待导入 / 2 没有资源；先改名再判。期望按规则手写：
    //   ① 空 Documents → 2；② 恰好一个「游戏名资源包」→ 改名成「资源包」，里面有 .incoming/*.tar → 1；③ 解开后（有 manifest.json、没有 tar）→ 0，进度回调最后一次 = (总批数, 总批数)；
    //   ③b 混进一个「._1_000.tar」（访达从非 APFS 盘拷来的附属文件）：不算包、不解，导入照样成功
    //   ④ 两个「…资源包」且没有「资源包」→ 2、两个都不改名；⑤ 只有「资源包」没有 manifest、没有 tar → 2
    { char D[2100], c2[4096]; struct stat s2;
      snprintf(D, sizeof D, "%s/imp1", T); mkdir(D, 0755); CHECK(s2a_import_state(D) == 2, "① 空 Documents 应判没有资源");
      snprintf(c2, sizeof c2, "cd %s && mkdir -p src2 '%s/SUPER MARIO ODYSSEY资源包/.incoming' && echo hi > src2/a.txt && cd src2 && COPYFILE_DISABLE=1 tar " TAR_OPTIONS " -cf '%s/SUPER MARIO ODYSSEY资源包/.incoming/1_000.tar' a.txt && cd .. && cp '%s/SUPER MARIO ODYSSEY资源包/.incoming/1_000.tar' '%s/SUPER MARIO ODYSSEY资源包/.incoming/1_001.tar' && printf 'a.txt\\t3\\tx\\n' > '%s/SUPER MARIO ODYSSEY资源包/index.tsv' && printf '{\"version\":2,\"index_sha256\":\"cafe01\"}' > '%s/SUPER MARIO ODYSSEY资源包/manifest.json'", T, D, D, D, D, D, D);
      CHECK(sh(c2) == 0, "造导入夹具失败");
      snprintf(c2, sizeof c2, "printf 'not a tar' > '%s/SUPER MARIO ODYSSEY资源包/.incoming/._1_000.tar'", D); CHECK(sh(c2) == 0, "造 AppleDouble 附属文件失败");
      CHECK(s2a_import_state(D) == 1, "② 有待导入的 tar 应判待导入");
      snprintf(c2, sizeof c2, "%s/资源包/.incoming/1_000.tar", D); CHECK(stat(c2, &s2) == 0, "② 「游戏名资源包」应已改名成「资源包」");
      s2a_pack_apply_progress = prog; int r = s2a_pack_apply(D); s2a_pack_apply_progress = NULL;
      snprintf(c2, sizeof c2, "%s/资源包/.incoming/._1_000.tar", D); CHECK(stat(c2, &s2) == 0, "③b 「._」附属文件不是包，不解、不删");
      CHECK(r == 0 && g_pd == 2 && g_pt == 2 && g_pcalls == 3, "③ 解包应成功、进度回调 3 次且最后 = 2/2（rc %d，最后 %d/%d，%d 次）", r, g_pd, g_pt, g_pcalls);
      CHECK(s2a_import_state(D) == 0, "③ 解开后应判就绪");
      snprintf(D, sizeof D, "%s/imp2", T); snprintf(c2, sizeof c2, "mkdir -p '%s/甲资源包' '%s/乙资源包'", D, D); sh(c2);
      CHECK(s2a_import_state(D) == 2, "④ 两个「…资源包」应判没有资源");
      snprintf(c2, sizeof c2, "%s/甲资源包", D); CHECK(stat(c2, &s2) == 0, "④ 不应改名");
      snprintf(D, sizeof D, "%s/imp3", T); snprintf(c2, sizeof c2, "mkdir -p '%s/资源包'", D); sh(c2);
      CHECK(s2a_import_state(D) == 2, "⑤ 空的「资源包」应判没有资源");
      // ⑥ 10-26 测试设备2：App 先建了空的「资源包」，再拷进来带 manifest.json 的「smg2资源包」→ 改用后者、判就绪；空的挪到「资源包.空.<时刻>」、不删
      snprintf(c2, sizeof c2, "mkdir -p '%s/smg2资源包/ctl' && echo '{}' > '%s/smg2资源包/manifest.json' && echo n > '%s/smg2资源包/ctl/control.nacp' && printf 'ctl/control.nacp\\t2\\tx\\n' > '%s/smg2资源包/index.tsv'", D, D, D, D); sh(c2);   // pack.py 与 manifest.json 同写 index.tsv
      CHECK(s2a_import_state(D) == 0, "⑥ 空「资源包」+ 可用的「smg2资源包」应改用后者、判就绪");
      snprintf(c2, sizeof c2, "%s/资源包/ctl/control.nacp", D); CHECK(stat(c2, &s2) == 0, "⑥ 「smg2资源包」应已换成「资源包」");
      snprintf(c2, sizeof c2, "ls -d '%s'/资源包.空.* > /dev/null 2>&1", D); CHECK(sh(c2) == 0, "⑥ 空的「资源包」应挪开保留，不删");
      // ⑦ 「资源包」已有内容（manifest.json 在）时，另有「…资源包」也不动它
      snprintf(c2, sizeof c2, "mkdir -p '%s/新资源包' && echo '{}' > '%s/新资源包/manifest.json'", D, D); sh(c2);
      CHECK(s2a_import_state(D) == 0, "⑦ 已有可用「资源包」应判就绪");
      snprintf(c2, sizeof c2, "%s/新资源包", D); CHECK(stat(c2, &s2) == 0, "⑦ 已有可用「资源包」时另一份不应被挪");
      // ⑧ 10-26 测试设备2：manifest.json 在、但 index.tsv 记的文件缺（拷到一半）→ 3 不完整；补齐 → 0。期望按 index.tsv 手写：ctl/control.nacp 2 字节在、romfs/x.bin 5 字节缺
      snprintf(c2, sizeof c2, "printf 'ctl/control.nacp\\t2\\tx\\nromfs/x.bin\\t5\\tx\\n' > '%s/资源包/index.tsv'", D); sh(c2);
      CHECK(s2a_import_state(D) == 3, "⑧ index.tsv 里有文件缺应判不完整");
      snprintf(c2, sizeof c2, "mkdir -p '%s/资源包/romfs' && printf 1234 > '%s/资源包/romfs/x.bin'", D, D); sh(c2);
      CHECK(s2a_import_state(D) == 3, "⑧ 大小不对（4 ≠ 5）也应判不完整");
      snprintf(c2, sizeof c2, "printf 12345 > '%s/资源包/romfs/x.bin'", D); sh(c2);
      CHECK(s2a_import_state(D) == 0, "⑧ 补齐后应判就绪");
      // ⑨ 审计 P2-2：说明页正文。按过「重新检查」还不行，开头说「刚刚查过」；首次不说（文字手写）
      char hd[1024]; s2a_pack_help_detail(2, 1, hd, sizeof hd); CHECK(!strncmp(hd, "刚刚查过，还是没有完整的资源包。", strlen("刚刚查过，还是没有完整的资源包。")), "⑨ 重新检查后仍缺：开头说刚刚查过");
      s2a_pack_help_detail(2, 0, hd, sizeof hd); CHECK(!strstr(hd, "刚刚查过") && strstr(hd, "外壳"), "⑨ 首次显示：不说刚刚查过");
      s2a_pack_help_detail(3, 1, hd, sizeof hd); CHECK(strstr(hd, "刚刚查过") && strstr(hd, "没拷全"), "⑨ 不完整重新检查后：也说刚刚查过");
      // ⑩ 审计 P2-7：index.tsv 由 pack/pack.py 与 manifest.json 同写，出包保证在。manifest.json 在而 index.tsv 不在 / 打不开（自环软链 ELOOP，root 下也打不开）/ 读不了（目录，fopen 成功、读 EISDIR）
      //   → 期望都判 3 不完整；旧代码打不开当「老包不核」判 0 就绪。11-02 维护者：说明页不点名 index.tsv 这类内部词（缺哪个只进日志）
      snprintf(D, sizeof D, "%s/imp4", T); snprintf(c2, sizeof c2, "mkdir -p '%s/资源包/ctl' && echo '{}' > '%s/资源包/manifest.json' && echo n > '%s/资源包/ctl/control.nacp'", D, D, D); sh(c2);
      const char *how[3] = {"不在", "打不开", "读不了"};
      for (int k = 0; k < 3; k++) {
          if (k == 1) { snprintf(c2, sizeof c2, "cd '%s/资源包' && ln -s index.tsv index.tsv", D); sh(c2); }
          if (k == 2) { snprintf(c2, sizeof c2, "cd '%s/资源包' && rm index.tsv && mkdir index.tsv", D); sh(c2); }
          int got = s2a_import_state(D); s2a_pack_help_detail(got, 0, hd, sizeof hd);
          CHECK(got == 3 && !strstr(hd, "index.tsv") && strstr(hd, "装到 iPhone"), "⑩ index.tsv %s：期望 3 不完整、说明页不点名 index.tsv、给出「装到 iPhone」 → %d「%s」", how[k], got, hd);
      }
      // ⑪ 解包核对读 index.tsv 打不开（自环软链）→ 期望判失败、unpack.log 写明「index.tsv 打不开」（旧代码按缺 1 个文件报、例子为空）
      snprintf(D, sizeof D, "%s/imp5", T); snprintf(c2, sizeof c2, "mkdir -p '%s/资源包' && printf '{\"version\":2,\"index_sha256\":\"cafe05\"}' > '%s/资源包/manifest.json' && cd '%s/资源包' && ln -s index.tsv index.tsv", D, D, D); sh(c2);
      CHECK(s2a_pack_apply(D) != 0, "⑪ index.tsv 打不开时解包应判失败");
      snprintf(c2, sizeof c2, "tail -1 '%s/unpack.log' | grep -q 'index.tsv 打不开'", D); CHECK(sh(c2) == 0, "⑪ 失败行应写明 index.tsv 打不开"); }
    // ⑫ 11-02 维护者 测试设备2 暗黑3：推送中途点开 App。推送契约（tools/push_pack.sh）：tar 先到、index.tsv 与 manifest.json 最后到。期望按契约手写：
    //   清单没到 / 清单还是已导入那份 / 有包没写完 → 判「还在传」，闸门只等不解（旧闸门见待导入就解 → 下面 ⑫a 复现出 维护者 那页的「index.tsv 打不开 / 返回 -8」）；
    //   清单到齐、包都写完、指纹稳住 ≥ 3 秒才导入；推送没完且 180 秒没动 → 停住；没有 / 不完整的资源包首轮直接给说明页，1 分钟内在变（访达正拷）→ 等，拷齐 → 就绪
    { char D[2100], R[2200], c2[4096], why[600]; struct s2a_gate_watch w = {0};
      snprintf(D, sizeof D, "%s/imp6", T); snprintf(R, sizeof R, "%s/资源包", D);
      snprintf(c2, sizeof c2, "mkdir -p '%s/.incoming' && cd %s/src && COPYFILE_DISABLE=1 tar " TAR_OPTIONS " -cf '%s/.incoming/1_000.tar' a.txt && printf aa11 > '%s/.applied'", R, T, R, R);
      CHECK(sh(c2) == 0, "⑫ 造推送中夹具失败");
      CHECK(s2a_import_state(D) == 1, "⑫ 有 tar 应判待导入");
      CHECK(s2a_import_transferring(R, why, sizeof why) == 1 && strstr(why, "index.tsv"), "⑫ 清单没到：应判还在传、原因点名 index.tsv →「%s」", why);
      CHECK(s2a_import_decide(D, &w, 1000, why, sizeof why) == S2A_GATE_WAIT && s2a_import_decide(D, &w, 1010, why, sizeof why) == S2A_GATE_WAIT, "⑫ 清单没到：闸门应一直等、不导入");
      // ⑫a 旧闸门（待导入就解）在这一刻解包：失败、点名 index.tsv 打不开——维护者 照片上那页的由来
      snprintf(c2, sizeof c2, "rm -rf '%s/imp6old' && cp -R '%s' '%s/imp6old'", T, D, T); sh(c2); snprintf(c2, sizeof c2, "%s/imp6old", T);
      CHECK(s2a_pack_apply(c2) != 0, "⑫a 推送中途解包应失败（旧行为复现）");
      snprintf(c2, sizeof c2, "tail -1 '%s/imp6old/unpack.log' | grep -q 'index.tsv 打不开.*返回 -8'", T); CHECK(sh(c2) == 0, "⑫a 旧行为失败行应是 index.tsv 打不开、返回 -8");
      snprintf(c2, sizeof c2, "printf 'a.txt\\t3\\tx\\n' > '%s/index.tsv'", R); sh(c2);
      CHECK(s2a_import_transferring(R, why, sizeof why) == 1 && strstr(why, "manifest.json"), "⑫ 只到了 index.tsv：应判还在传、点名 manifest.json →「%s」", why);
      snprintf(c2, sizeof c2, "printf '{\"version\":2,\"index_sha256\":\"aa11\"}' > '%s/manifest.json'", R); sh(c2);
      CHECK(s2a_import_transferring(R, why, sizeof why) == 1 && strstr(why, "上次已导入"), "⑫ 清单 = .applied（覆盖装、新清单没到）：应判还在传 →「%s」", why);
      snprintf(c2, sizeof c2, "printf '{\"version\":2,\"index_sha256\":\"bb22\"}' > '%s/manifest.json'", R); sh(c2);
      CHECK(s2a_import_transferring(R, why, sizeof why) == 0, "⑫ 新清单到齐、包写完：不应判还在传 →「%s」", why);
      w = (struct s2a_gate_watch){0};
      int a0 = s2a_import_decide(D, &w, 2000, why, sizeof why), a1 = s2a_import_decide(D, &w, 2001, why, sizeof why), a2 = s2a_import_decide(D, &w, 2003, why, sizeof why);
      CHECK(a0 == S2A_GATE_SETTLE && a1 == S2A_GATE_SETTLE && a2 == S2A_GATE_IMPORT, "⑫ 推送已完：首轮与 1 秒时等稳住、3 秒时导入 → %d %d %d", a0, a1, a2);
      // 有包没写完（推到一半：完整包去掉末尾 1024 字节归档尾）→ 还在传；指纹 180 秒没动 → 停住；包又长了 → 回到等
      snprintf(c2, sizeof c2, "cd '%s/.incoming' && n=$(wc -c < 1_000.tar) && head -c $((n - 1024)) 1_000.tar > 1_001.tar", R); CHECK(sh(c2) == 0, "⑫ 造半截包失败");
      CHECK(s2a_import_transferring(R, why, sizeof why) == 1 && strstr(why, "1_001.tar"), "⑫ 半截包：应判还在传、点名 1_001.tar →「%s」", why);
      w = (struct s2a_gate_watch){0};
      a0 = s2a_import_decide(D, &w, 3000, why, sizeof why); a1 = s2a_import_decide(D, &w, 3179, why, sizeof why); a2 = s2a_import_decide(D, &w, 3180, why, sizeof why);
      CHECK(a0 == S2A_GATE_WAIT && a1 == S2A_GATE_WAIT && a2 == S2A_GATE_STALLED, "⑫ 半截包不动：0 / 179 秒等、180 秒停住 → %d %d %d", a0, a1, a2);
      snprintf(c2, sizeof c2, "head -c 512 /dev/urandom >> '%s/.incoming/1_001.tar'", R); sh(c2);
      CHECK(s2a_import_decide(D, &w, 3200, why, sizeof why) == S2A_GATE_WAIT, "⑫ 半截包又长了：应回到等");
      snprintf(c2, sizeof c2, "%s/.incoming/1_000.tar", R); struct stat s3; CHECK(stat(c2, &s3) == 0, "⑫ 等待期间不应解 / 删任何包");
      // 访达拷整个文件夹：首轮就是不完整 → 直接说明页（不假装在传）；拷进来一个文件（指纹变）→ 等；60 秒不动 → 说明页；拷齐 → 就绪
      snprintf(D, sizeof D, "%s/imp7", T); snprintf(R, sizeof R, "%s/资源包", D);
      snprintf(c2, sizeof c2, "mkdir -p '%s/romfs' && echo '{}' > '%s/manifest.json' && printf 'a.bin\\t2\\tx\\nromfs/b.bin\\t5\\tx\\n' > '%s/index.tsv' && printf 12 > '%s/a.bin'", R, R, R, R); sh(c2);
      w = (struct s2a_gate_watch){0};
      CHECK(s2a_import_decide(D, &w, 100, why, sizeof why) == S2A_GATE_BROKEN, "⑫ 不完整、首轮：应直接说明页");
      snprintf(c2, sizeof c2, "printf 12 > '%s/romfs/b.bin'", R); sh(c2);
      CHECK(s2a_import_decide(D, &w, 103, why, sizeof why) == S2A_GATE_WAIT, "⑫ 不完整、文件在变：应等");
      CHECK(s2a_import_decide(D, &w, 162, why, sizeof why) == S2A_GATE_WAIT && s2a_import_decide(D, &w, 163, why, sizeof why) == S2A_GATE_BROKEN, "⑫ 不完整、59 秒仍等、60 秒不动回说明页");
      snprintf(c2, sizeof c2, "printf 12345 > '%s/romfs/b.bin'", R); sh(c2);
      CHECK(s2a_import_decide(D, &w, 170, why, sizeof why) == S2A_GATE_READY, "⑫ 拷齐：应就绪");
      snprintf(D, sizeof D, "%s/imp8", T); mkdir(D, 0755); w = (struct s2a_gate_watch){0};
      CHECK(s2a_import_decide(D, &w, 1, why, sizeof why) == S2A_GATE_NONE, "⑫ 没有资源包、首轮：应直接说明页"); }
    snprintf(c, sizeof c, "rm -rf %s", T); sh(c);
    // 资源包闸门按启动上下文定查哪儿（11-07 Duo：多款容器选了游戏后 export_env 写了 S2A_GAME_DATA，原先「设了就不查」→ 资源包不在也照起，服务层 DirectoryNotFound）
    {   s2a_launch_ctx c; char out[1024];
        memset(&c, 0, sizeof c); c.source = S2A_LCTX_CONTAINER; snprintf(c.docs, sizeof c.docs, "%s/home/Documents/games/0100FD70134FA000", T);
        CHECK(s2a_import_docs_pick(&c, 1, "/proc/Documents", out, sizeof out) == 1 && !strcmp(out, c.docs), "多款容器（S2A_GAME_DATA 已由选游戏写进环境）：要查、查本款文档根（得 %s）", out);
        memset(&c, 0, sizeof c); c.source = S2A_LCTX_COMPAT;
        CHECK(s2a_import_docs_pick(&c, 0, "/proc/Documents", out, sizeof out) == 1 && !strcmp(out, "/proc/Documents"), "单款兼容来源、没设 S2A_GAME_DATA：查进程级 Documents（同原先）");
        CHECK(s2a_import_docs_pick(&c, 1, "/proc/Documents", out, sizeof out) == 0, "单款、开发指定了 S2A_GAME_DATA：不查（同原先）");
        c.source = S2A_LCTX_FILE;
        CHECK(s2a_import_docs_pick(&c, 1, "/proc/Documents", out, sizeof out) == 0, "单款文件来源（构造期写了 S2A_GAME_DATA）：不查（同原先）");
        CHECK(s2a_import_docs_pick(NULL, 0, "/proc/Documents", out, sizeof out) == 1 && !strcmp(out, "/proc/Documents"), "上下文不可用、没设 S2A_GAME_DATA：查进程级 Documents");
        // 游戏库按款判：Documents/games/<游戏号>/资源包 不在 → 没有资源；放好完整资源包 → 就绪；游戏号带 / 或以 . 开头 → 按没有资源（不进路径）
        char h[1024], pd[1100], cmd[4096]; snprintf(h, sizeof h, "%s/home", T); snprintf(pd, sizeof pd, "%s/Documents", h);
        CHECK(s2a_import_state_game(pd, "0100FD70134FA000") == 2, "本款资源包不在 → S2A_IMPORT_NONE(2)");
        snprintf(cmd, sizeof cmd, "mkdir -p %1$s && cd %1$s && mkdir -p Documents/games/0100FD70134FA000/资源包/exefs && printf x > Documents/games/0100FD70134FA000/资源包/exefs/main.npdm && "
            "printf 'exefs/main.npdm\t1\tx\n' > Documents/games/0100FD70134FA000/资源包/index.tsv && printf '{}' > Documents/games/0100FD70134FA000/资源包/manifest.json", h);
        CHECK(sh(cmd) == 0, "造本款资源包失败");
        CHECK(s2a_import_state_game(pd, "0100FD70134FA000") == 0, "本款资源包完整 → S2A_IMPORT_READY(0)");
        CHECK(s2a_import_state_game(pd, "0100C2500FC20000") == 2, "另一款没放 → 不受这款影响");
        CHECK(s2a_import_state_game(NULL, "0100FD70134FA000") == 2 && s2a_import_state_game(pd, "../x") == 2 && s2a_import_state_game(pd, ".hid") == 2 && s2a_import_state_game(pd, "") == 2, "不合规游戏号 → 按没有资源");
        snprintf(cmd, sizeof cmd, "rm %s/Documents/games/0100FD70134FA000/资源包/exefs/main.npdm", h); sh(cmd);
        CHECK(s2a_import_state_game(pd, "0100FD70134FA000") == 3, "缺 main.npdm（Duo 那种没导完）→ S2A_IMPORT_BROKEN(3)");
        // 库界面快判：.applied 等于清单的 index_sha256 就不逐文件核（主线程上 10 万文件要几秒）；不等 / 没有 .applied 退回完整核对；非法游戏号同完整判据
        { char qc[4096], qr[2300]; snprintf(qr, sizeof qr, "%s/games/0100FD70134FA000/资源包", pd);
          snprintf(qc, sizeof qc, "printf '{\"version\":2,\"index_sha256\":\"abc123\"}' > '%s/manifest.json' && printf 'abc123' > '%s/.applied'", qr, qr); CHECK(sh(qc) == 0, "造快判夹具失败");
          CHECK(s2a_import_state_game_quick(pd, "0100FD70134FA000") == 0, "快判：.applied 等于清单 → 就绪(0)，不逐文件核");
          CHECK(s2a_import_state_game(pd, "0100FD70134FA000") == 3, "完整判据不受影响：照旧查出缺文件(3)");
          snprintf(qc, sizeof qc, "printf 'other' > '%s/.applied'", qr); sh(qc);
          CHECK(s2a_import_state_game_quick(pd, "0100FD70134FA000") == 3, "快判：.applied 不等于清单 → 退回完整核对(3)");
          snprintf(qc, sizeof qc, "rm '%s/.applied'", qr); sh(qc);
          CHECK(s2a_import_state_game_quick(pd, "0100FD70134FA000") == 3, "快判：没有 .applied → 退回完整核对(3)");
          CHECK(s2a_import_state_game_quick(pd, "../x") == 2, "快判：非法游戏号 → 没有资源(2)"); }
    }
    printf("%s t_pack_apply：%d 处不符\n", bad ? "✗" : "✓", bad); return bad != 0;
}
