// 资源包收推送（测试层契约 tests/真机契约.md 第 1、2 节 S2A_PACK_APPLY_ONLY）：App 起来只做——
//   解开 Documents/资源包/.incoming/*.tar（按名字序，解完即删）→ 写 资源包/.applied = manifest.json 的 index_sha256 → 往 Documents/unpack.log 追加一行 → 退出。
//   失败行含「返回 -<码>」（执行器按这个判红）。每解完一个 tar 记一行「进度：…」；全部解完后删掉资源包下不在 index.tsv 里的旧文件（旧版分卷等）；
//   再逐条核 index.tsv 的文件在、大小对（-8），都对才写 .applied / index.applied.tsv——「已应用」= 机上真有。
//   iOS 上在后台线程跑（host_main.c），UIKit 照常起来——近 10 GB 在启动路径上同步跑会被启动看门狗杀（测试设备1 10-19 首局实测）。tar 是执行器用 `tar --format pax` 打的：只认普通文件、目录、pax 扩展头（path / size），
//   路径相对资源包根；绝对路径、含 .. 的路径、其他条目类型一律判失败（不猜）。
#include "native.h"
#include "s2a_docs.h"   // iOS 进程级容器 Documents（s2a_container_docs）
#include "s2a_launch_ctx.h"   // 资源包闸门按启动上下文定文档根（多款容器 = 本款 Documents/games/<游戏号>）
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <time.h>
#include <string.h>
#if defined(__APPLE__)
#include <TargetConditionals.h>
#endif

// 写失败现场（10-20 测试设备2 巫师3 收推送连续「返回 -4」，看不出是哪个条目）：失败的那一步、路径与 errno，解包失败时写进 unpack.log
static char g_untar_err[2400];
static int g_untar_trunc;   // 本次解包碰到截短（读到的少于条目 / 包尾结束块）
static void untar_err(const char *step, const char *path, int e) { snprintf(g_untar_err, sizeof g_untar_err, "%s %s：errno %d %s", step, path, e, strerror(e)); }
static int mkdirs(char *p) {   // 建 p 的各级父目录（p 是文件路径）；已存在但不是目录（同名文件挡路）也算失败
    for (char *s = p + 1; *s; s++) if (*s == '/') { *s = 0; int r = mkdir(p, 0755), e = errno; struct stat st;
        if (r && e == EEXIST && (stat(p, &st) || !S_ISDIR(st.st_mode))) { r = -1; e = ENOTDIR; }
        if (r && e != EEXIST) { untar_err("建目录", p, e); *s = '/'; return -1; } *s = '/'; }
    return 0;
}
static uint64_t oct(const char *s, int n) { uint64_t v = 0; for (int i = 0; i < n && s[i]; i++) if (s[i] >= '0' && s[i] <= '7') v = v * 8 + (uint64_t)(s[i] - '0'); return v; }
static int safe(const char *p) { return *p && *p != '/' && !strstr(p, "../") && strcmp(p, "..") && !(strlen(p) >= 3 && !strcmp(p + strlen(p) - 3, "/..")); }

// 解一个 tar 到 root；返回 0 成功，负数 = 失败码（-1 读不了 -2 头坏 -3 路径不安全 -4 写失败 -5 条目类型不支持）；*nf / *nb 累计文件数与字节
int s2a_untar(const char *tar, const char *root, unsigned *nf, uint64_t *nb) {
    g_untar_trunc = 0;
    int fd = open(tar, O_RDONLY); if (fd < 0) return -1;
    uint8_t h[512], buf[1 << 16]; char pax_path[4096] = ""; uint64_t pax_size = (uint64_t)-1; int rc = 0;
    for (;;) {
        // 没读到归档尾（全零块）就到文件末尾 / 只读到半个头 = 包被截短（执行器用 tar 打的包必有归档尾；截在条目边界时旧代码当成解完、把缺文件的状态记成已应用）
        ssize_t r = read(fd, h, 512); if (r >= 0 && r != 512) { untar_err("包被截短（没有归档尾）", tar, EIO); g_untar_trunc = 1; rc = -4; break; } if (r < 0) { rc = -2; break; }
        int zero = 1; for (int i = 0; i < 512 && zero; i++) zero = !h[i]; if (zero) break;   // 归档尾（两块全零）
        unsigned sum = 0; for (int i = 0; i < 512; i++) sum += (i >= 148 && i < 156) ? ' ' : h[i];
        if (sum != oct((char *)h + 148, 8)) { rc = -2; break; }
        char type = (char)h[156]; uint64_t size = oct((char *)h + 124, 12);
        char name[4096];
        if (h[345]) snprintf(name, sizeof name, "%.155s/%.100s", (char *)h + 345, (char *)h); else snprintf(name, sizeof name, "%.100s", (char *)h);
        if (pax_size != (uint64_t)-1) size = pax_size;
        if (pax_path[0]) strlcpy(name, pax_path, sizeof name);
        uint64_t pad = (512 - size % 512) % 512;
        if (type == 'x' || type == 'g') {   // pax 扩展头：「长度 键=值\n」记录；只取 path / size，作用于下一个条目（g 全局头忽略）
            if (size > sizeof buf - 1 || read(fd, buf, size) != (ssize_t)size || lseek(fd, (off_t)pad, SEEK_CUR) < 0) { rc = -2; break; }
            buf[size] = 0;   // strtoul 不越过本头数据
            pax_path[0] = 0; pax_size = (uint64_t)-1;
            if (type == 'g') continue;
            for (uint64_t o = 0; o < size;) {
                char *rec = (char *)buf + o; unsigned long len = strtoul(rec, NULL, 10); if (!len || o + len > size) { rc = -2; break; }
                // 记录格式「长度 键=值\n」：空格与换行都得在本条记录内（11-01 审计 P2-17：缺空格时 strchr 得 NULL、+1 后解引用崩；按整条长度找「=」会越过记录尾），不符判头坏
                char *sp = memchr(rec, ' ', len), *end = rec + len - 1;
                if (!sp || *end != '\n') { untar_err("pax 扩展头记录格式异常（缺空格或换行）", tar, EINVAL); rc = -2; break; }
                char *kv = sp + 1, *eq = memchr(kv, '=', (size_t)(end - kv));
                if (!eq) { untar_err("pax 扩展头记录格式异常（缺等号）", tar, EINVAL); rc = -2; break; }
                if (eq && eq - kv == 4 && !memcmp(kv, "path", 4) && (size_t)(end - eq - 1) < sizeof pax_path) { memcpy(pax_path, eq + 1, (size_t)(end - eq - 1)); pax_path[end - eq - 1] = 0; }
                if (eq && eq - kv == 4 && !memcmp(kv, "size", 4)) pax_size = strtoull(eq + 1, NULL, 10);
                o += len;
            }
            if (rc) break;
            continue;
        }
        pax_path[0] = 0; pax_size = (uint64_t)-1;
        if (!strncmp(name, "./", 2)) memmove(name, name + 2, strlen(name + 2) + 1);
        if (type == '5') { if (!*name) continue; if (!safe(name)) { rc = -3; break; } char p[8192]; snprintf(p, sizeof p, "%s/%s/x", root, name); if (mkdirs(p)) { rc = -4; break; } continue; }
        if (type != '0' && type != 0) { LOG("【红】解包：%s 条目类型 %c 不支持", name, type); rc = -5; break; }
        if (!safe(name)) { LOG("【红】解包：路径不安全 %s", name); rc = -3; break; }
        char p[8192], tmp[8300]; snprintf(p, sizeof p, "%s/%s", root, name); snprintf(tmp, sizeof tmp, "%s.part", p);
        if (mkdirs(p)) { rc = -4; break; }
        int of = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644); if (of < 0) { untar_err("建临时文件", tmp, errno); rc = -4; break; }
        for (uint64_t left = size; left;) {
            size_t k = left > sizeof buf ? sizeof buf : (size_t)left;
            ssize_t rr = read(fd, buf, k); if (rr != (ssize_t)k) { untar_err(rr < 0 ? "读包" : "包被截短（读到的少于条目大小）", name, rr < 0 ? errno : EIO); if (rr >= 0) g_untar_trunc = 1; rc = -4; break; }
            if (write(of, buf, k) != (ssize_t)k) { untar_err("写文件", tmp, errno); rc = -4; break; }
            left -= k;
        }
        if (close(of) && !rc) { untar_err("关文件", tmp, errno); rc = -4; }
        if (!rc && rename(tmp, p)) { untar_err("改名到", p, errno); rc = -4; }
        if (rc) { unlink(tmp); break; }
        if (pad && lseek(fd, (off_t)pad, SEEK_CUR) < 0) { rc = -2; break; }
        (*nf)++; *nb += size;
    }
    close(fd); return rc;
}

#include <stdarg.h>
#include <ftw.h>
static void log_line(const char *logp, const char *fmt, ...) {
    time_t now = time(NULL); char ts[32]; strftime(ts, sizeof ts, "%m-%d %H:%M:%S", localtime(&now));
    char b[1024]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof b, fmt, ap); va_end(ap);
    FILE *f = fopen(logp, "a"); if (f) { fprintf(f, "%s %s\n", ts, b); fclose(f); }
}
// 清旧文件：遍历资源包根，删掉不在 index.tsv 里的普通文件（index.tsv / manifest.json / 以 . 开头的执行器与 App 记账文件、.incoming 目录除外）
static char **g_keep; static size_t g_nkeep, g_rootlen; static unsigned *g_nd; static uint64_t *g_db;
static int keep_cmp(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }
static int prune_one(const char *p, const struct stat *st, int flag, struct FTW *f) {
    (void)f; if (flag != FTW_F) return 0;
    const char *rel = p + g_rootlen + 1;
    if (!strcmp(rel, "index.tsv") || !strcmp(rel, "index.applied.tsv") || !strcmp(rel, "manifest.json") || rel[0] == '.' || !strncmp(rel, ".incoming/", 10)) return 0;   // index.applied.tsv：核对不过时它得留着（执行器据它补推）
    if (bsearch(&rel, g_keep, g_nkeep, sizeof *g_keep, keep_cmp)) return 0;
    if (!unlink(p)) { (*g_nd)++; *g_db += (uint64_t)st->st_size; }
    return 0;
}
// 删空了的目录也清掉（10-25 测试设备2 马车8：旧直连库的 shaders/direct/b-<链>/ 文件删光、目录还在，开局「包里有 b-… 但没有 shader_map.tsv」报红误导排查）。
//   后序遍历（子目录先于父目录），只删空目录（rmdir 非空即失败、不管）；根、以 . 开头的目录（.incoming 等记账目录）及其下不动
static int prune_dir(const char *p, const struct stat *st, int flag, struct FTW *f) {
    (void)st; if (flag != FTW_DP || f->level == 0) return 0;
    const char *rel = p + g_rootlen + 1;
    if (rel[0] == '.' || strstr(rel, "/.")) return 0;
    rmdir(p);
    return 0;
}
static int prune(const char *root, unsigned *nd, uint64_t *db) {
    char ip[2100]; snprintf(ip, sizeof ip, "%s/index.tsv", root);
    FILE *f = fopen(ip, "r"); if (!f) return -7;
    size_t cap = 1024; g_keep = malloc(cap * sizeof *g_keep); g_nkeep = 0; char line[4600];
    while (fgets(line, sizeof line, f)) { char *t = strchr(line, '\t'); if (!t) continue; *t = 0; if (g_nkeep == cap) g_keep = realloc(g_keep, (cap *= 2) * sizeof *g_keep); g_keep[g_nkeep++] = strdup(line); }
    fclose(f);
    qsort(g_keep, g_nkeep, sizeof *g_keep, keep_cmp);
    g_rootlen = strlen(root); g_nd = nd; g_db = db;
    nftw(root, prune_one, 32, FTW_PHYS);
    nftw(root, prune_dir, 32, FTW_PHYS | FTW_DEPTH);
    for (size_t i = 0; i < g_nkeep; i++) free(g_keep[i]); free(g_keep);
    return 0;
}

// 核对机上实况（10-25 测试设备2 马车8：执行器推了 4 批、App 解开 0 个包，照样把新 index.tsv 记成 index.applied.tsv / .applied，
//   之后两局执行器信这份「已应用」只推 3 个文件，机上没有直连库、退回通用）：index.tsv 每一行的文件都在、大小对得上，才算应用成功。
//   返回缺 / 大小不符的个数，*ex 记第一个（报红现场）；逐个 stat，九款最大索引约 39 万行、本机约 1 秒
static unsigned verify_present(const char *root, char *ex, size_t exn) {
    char ip[2100]; snprintf(ip, sizeof ip, "%s/index.tsv", root); FILE *f = fopen(ip, "r");
    if (!f) { snprintf(ex, exn, "index.tsv 打不开：%s", strerror(errno)); return 1; }   // 审计 P2-7：原先只记缺 1 个、例子为空
    char line[4600], p[6800]; unsigned miss = 0; struct stat st; ex[0] = 0;
    while (fgets(line, sizeof line, f)) {
        char *t = strchr(line, '\t'); if (!t) continue; *t = 0; long long want = atoll(t + 1);
        snprintf(p, sizeof p, "%s/%s", root, line);
        if (stat(p, &st) || !S_ISREG(st.st_mode) || st.st_size != want) { if (!miss++) snprintf(ex, exn, "%s（%s）", line, stat(p, &st) ? "不在" : "大小不符"); }
    }
    if (ferror(f)) { snprintf(ex, exn, "index.tsv 读失败：%s", strerror(errno)); miss++; }
    fclose(f); return miss;
}
// root/src 整份拷成 root/dst（先写 dst.tmp 再改名）；返回 0 成功
static int copy_atomic(const char *root, const char *src, const char *dst) {
    char a[2100], b[2100], t[2110]; snprintf(a, sizeof a, "%s/%s", root, src); snprintf(b, sizeof b, "%s/%s", root, dst); snprintf(t, sizeof t, "%s.tmp", b);
    FILE *in = fopen(a, "rb"); if (!in) return -1; FILE *out = fopen(t, "wb"); if (!out) { fclose(in); return -1; }
    char buf[65536]; size_t k; int bad = 0; while ((k = fread(buf, 1, sizeof buf, in)) > 0) if (fwrite(buf, 1, k, out) != k) { bad = 1; break; }
    bad |= ferror(in); fclose(in); bad |= fclose(out) != 0;
    if (bad || rename(t, b)) { unlink(t); return -1; } return 0;
}
// 写 root/name = s（先写 name.tmp 再改名；11-01 审计 P2-16：原先 fopen("w") 先截断再写，中途被杀 / 写失败旧标记就丢了）；返回 0 成功
static int write_atomic(const char *root, const char *name, const char *s) {
    char p[2100], t[2110]; snprintf(p, sizeof p, "%s/%s", root, name); snprintf(t, sizeof t, "%s.tmp", p);
    FILE *f = fopen(t, "w"); if (!f) return -1;
    int bad = fputs(s, f) < 0; bad |= fclose(f) != 0;
    if (bad || rename(t, p)) { unlink(t); return -1; } return 0;
}
static int name_cmp(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }
// 读 manifest.json 里 "index_sha256":"…"（执行器写的定长格式，只取这一个键）
static int manifest_sha(const char *path, char *out, size_t n) {
    FILE *f = fopen(path, "r"); if (!f) return -1; char b[4096]; size_t k = fread(b, 1, sizeof b - 1, f); fclose(f); b[k] = 0;
    char *p = strstr(b, "\"index_sha256\""); if (!p || !(p = strchr(p + 14, '"'))) return -1;
    char *e = strchr(++p, '"'); if (!e || (size_t)(e - p) >= n) return -1;
    memcpy(out, p, (size_t)(e - p)); out[e - p] = 0; return 0;
}
// 玩家导入（s2a_import_gate）用：每批解完回调一次（已解批数、总批数），失败原因留给提示页
void (*s2a_pack_apply_progress)(int done, int total);
static char g_apply_fail[2400];
const char *s2a_pack_apply_failure(void) { return g_apply_fail; }
// 返回进程退出码（0 成功）；docs = Documents 目录
int s2a_pack_apply(const char *docs) {
    char root[2048], inc[2100], logp[2100]; snprintf(root, sizeof root, "%s/资源包", docs); snprintf(inc, sizeof inc, "%s/.incoming", root); snprintf(logp, sizeof logp, "%s/unpack.log", docs);
    uint64_t t0 = now_ns(); unsigned ntar = 0, nf = 0; uint64_t nb = 0; int rc = 0; char what[2300] = "";
    DIR *d = opendir(inc); char *names[4096]; int nn = 0;
    if (d) { struct dirent *e; while ((e = readdir(d)) && nn < 4096) { size_t l = strlen(e->d_name); if (l > 4 && !strcmp(e->d_name + l - 4, ".tar") && strncmp(e->d_name, "._", 2)) names[nn++] = strdup(e->d_name); } closedir(d); }
    qsort(names, (size_t)nn, sizeof *names, name_cmp);
    if (s2a_pack_apply_progress) s2a_pack_apply_progress(0, nn);
    for (int i = 0; i < nn && !rc; i++) {
        char t[2400]; snprintf(t, sizeof t, "%s/%s", inc, names[i]);
        unsigned f0 = nf; uint64_t b0 = nb, t1 = now_ns();
        rc = s2a_untar(t, root, &nf, &nb);
        // 截短的推送包（推送中途被杀留下的）永远解不开：删掉它、它后面所有待解包和推送记录 .pushed，下次部署执行器只按 index.applied.tsv 补推（10-20 测试设备2 巫师3 同一个残包连红 4 局）。
        //   只删残包不够（10-21 测试出包线）：执行器见 .incoming 里还有包就把 .pushed 记的文件当「已推」，残包里的文件不补推；后面的包解完照写 index.applied.tsv = 全部目标，
        //   机上缺文件 / 留半截文件却记成已应用。待解包全删 = 机上状态退回 index.applied.tsv 那一刻，执行器补推的就是缺的全部
        if (rc) { int del = 0;
            if (g_untar_trunc && !unlink(t)) { del = 1; for (int j = i + 1; j < nn; j++) { char u[2400]; snprintf(u, sizeof u, "%s/%s", inc, names[j]); del += !unlink(u); }
                char pp[2100]; snprintf(pp, sizeof pp, "%s/.pushed", root); unlink(pp); }
            snprintf(what, sizeof what, "解 %s%s", names[i], del ? "（截短包：已删它与其后待解包、推送记录，下次部署按已应用索引补推）" : ""); }
        else { unlink(t); ntar++; if (s2a_pack_apply_progress) s2a_pack_apply_progress(i + 1, nn); log_line(logp, "进度：解开 %s（%u/%d）%u 个文件 %llu MB（%.1f 秒）", names[i], i + 1, nn, nf - f0, (unsigned long long)((nb - b0) >> 20), (now_ns() - t1) / 1e9); }
    }
    if (!rc) { char ex[2200]; unsigned miss = verify_present(root, ex, sizeof ex);
        if (miss) { rc = -8; snprintf(what, sizeof what, "核对：index.tsv 里 %u 个文件机上缺或大小不符（例：%s），不记已应用，执行器下次按旧的已应用索引补推", miss, ex); } }   // 核对在清旧文件之前：核不过不删任何文件
    if (!rc) { unsigned nd = 0; uint64_t db = 0; int r2 = prune(root, &nd, &db);   // 清旧文件：资源包下不在 index.tsv 里的（旧版分卷等）
        if (r2) { rc = r2; snprintf(what, sizeof what, "清旧文件（读 index.tsv）"); }
        else if (nd) log_line(logp, "进度：清掉不在 index.tsv 里的旧文件 %u 个 %llu MB", nd, (unsigned long long)(db >> 20)); }
    for (int i = 0; i < nn; i++) free(names[i]);
    char sha[200] = "";
    if (!rc) { char mp[2100]; snprintf(mp, sizeof mp, "%s/manifest.json", root);
        if (manifest_sha(mp, sha, sizeof sha)) { rc = -6; snprintf(what, sizeof what, "读 manifest.json 的 index_sha256"); }
        else if (copy_atomic(root, "index.tsv", "index.applied.tsv")) { rc = -4; snprintf(what, sizeof what, "存 index.applied.tsv"); }   // 「机上实际有什么」：执行器据它算补推
        else if (write_atomic(root, ".applied", sha)) { rc = -4; snprintf(what, sizeof what, "写 .applied"); } }
    time_t now = time(NULL); char ts[32]; strftime(ts, sizeof ts, "%m-%d %H:%M:%S", localtime(&now));
    FILE *lf = fopen(logp, "a");
    if (lf) {
        if (rc) snprintf(g_apply_fail, sizeof g_apply_fail, "%s（返回 %d）", what, rc);
        if (rc) fprintf(lf, "%s 解包失败：%s 返回 %d（已解 %u 个包 %u 个文件）%s%s\n", ts, what, rc, ntar, nf, g_untar_err[0] ? "；现场：" : "", g_untar_err);
        else fprintf(lf, "%s 解开 %u 个包 %u 个文件 %llu MB，.applied = %s（%.1f 秒）\n", ts, ntar, nf, (unsigned long long)(nb >> 20), sha, (now_ns() - t0) / 1e9);
        fclose(lf);
    }
    LOG("资源包收推送：%s", rc ? "失败（见 unpack.log）" : "完成");
    return rc ? 1 : 0;
}

// ---- 玩家首次打开时从「文件」导入资源（用户体验线 10-23，原生层线审：后台线程解包、解完提示重新打开、不同进程内接着进游戏）----
// 状态：先改名（Documents/资源包 不在、且恰好一个名字以「资源包」结尾的目录 → 改名成「资源包」，同卷原子；多个不猜）→ 有 .incoming/*.tar = 待导入 → 有 manifest.json = 就绪 → 否则没有资源
enum { S2A_IMPORT_READY = 0, S2A_IMPORT_PENDING = 1, S2A_IMPORT_NONE = 2, S2A_IMPORT_BROKEN = 3 };
// 闸门下一步（s2a_import_decide）与它的节拍：稳住 3 秒才导入、1 分钟内变过算正在拷、推送没完且 3 分钟没动算停住
enum { S2A_GATE_READY, S2A_GATE_IMPORT, S2A_GATE_SETTLE, S2A_GATE_WAIT, S2A_GATE_STALLED, S2A_GATE_NONE, S2A_GATE_BROKEN };
enum { S2A_GATE_SETTLE_S = 3, S2A_GATE_MOVING_S = 60, S2A_GATE_STALL_S = 180 };
struct s2a_gate_watch { int n, moved; uint64_t fp; time_t changed; };
// 资源包完整性（10-26 测试设备2：装机拷到一半被 App 改名，留下有 manifest.json、缺 ctl/control.nacp 的「资源包」，原先只凭 manifest 判就绪，
//   游戏起来读不到数据 Break、App 退出像闪退）：按 index.tsv（路径<TAB>字节<TAB>sha256）逐个核在不在、大小对不对，返回不符个数，first 记第一个。
//   index.tsv 由 pack/pack.py 与 manifest.json 同写、出包保证在：不在 / 打不开 / 读不了都按不完整（审计 P2-7：原先打不开当「老包不核」判就绪），first 记 index.tsv 与原因
static int g_pack_bad; static char g_pack_first[512];
// 说明页正文（app.m s2a_ios_show_pack_help）。again = 玩家刚按过「重新检查」、还是不行：开头先说一句，不然整页原样重画，玩家以为按钮没生效（审计 P2-2）
void s2a_pack_help_detail(int st, int again, char *d, size_t n) {
    const char *pre = again ? "刚刚查过，还是没有完整的资源包。\n\n" : "";
    // 缺哪个文件（g_pack_first）只进日志（s2a_import_state 已打），不上屏（11-02 维护者：index.tsv 这类内部词玩家看不懂）
    if (st == S2A_IMPORT_BROKEN) snprintf(d, n, "%s资源包里有一部分文件没拷全，多半是拷到一半断了。请在 Mac 上的 S2Arm 里对这款游戏再点一次「装到 iPhone」，它会把缺的补上；也可以把「资源包」文件夹重新整个拷一次。拷好后这里会自动继续。", pre);
    else snprintf(d, n, "%s这个 App 只是游戏的「外壳」，游戏本身在 Mac 上 S2Arm 打包得到的「资源包」里，手机上还没找到它。照下面做，放好后这里会自动继续。", pre);
}
int s2a_pack_check(const char *root, char *first, size_t n) {
    char idx[2100]; snprintf(idx, sizeof idx, "%s/index.tsv", root); FILE *f = fopen(idx, "r");
    if (!f) { if (first && n) snprintf(first, n, "index.tsv（%s）", errno == ENOENT ? "不在" : strerror(errno)); return 1; }
    char line[4096]; int bad = 0; if (first && n) first[0] = 0;
    while (fgets(line, sizeof line, f)) {
        char *t1 = strchr(line, '\t'); if (!t1) continue; *t1 = 0; long long want = atoll(t1 + 1);
        char p[4200]; struct stat st; snprintf(p, sizeof p, "%s/%s", root, line);
        if (stat(p, &st) || st.st_size != want) { if (!bad++ && first && n) strlcpy(first, line, n); }
    }
    if (ferror(f)) { if (first && n) snprintf(first, n, "index.tsv（读失败：%s）", strerror(errno)); bad++; }
    fclose(f); return bad;
}
static int ends_with(const char *s, const char *t) { size_t a = strlen(s), b = strlen(t); return a >= b && !strcmp(s + a - b, t); }
// 资源包目录里有东西可用：manifest.json（已就绪）或 .incoming 里有待导入的包
static int pack_usable(const char *root) {
    struct stat st; char p[2100]; snprintf(p, sizeof p, "%s/manifest.json", root); if (!stat(p, &st)) return 1;
    snprintf(p, sizeof p, "%s/.incoming", root); DIR *d = opendir(p); int n = 0;
    if (d) { struct dirent *e; while ((e = readdir(d))) if (ends_with(e->d_name, ".tar") && strncmp(e->d_name, "._", 2)) n++; closedir(d); }
    return n > 0;
}
int s2a_import_state(const char *docs) {
    char root[2048]; struct stat st; snprintf(root, sizeof root, "%s/资源包", docs);
    int have = !stat(root, &st) && S_ISDIR(st.st_mode);
    // 「资源包」在但里面没有可用的东西（10-26 测试设备2：App 先建了空的「资源包」，之后拷进来的「smg2资源包」按「资源包不在才改名」永远认不到，
    //   游戏缺 ctl/control.nacp 自行退出、看着像闪退）：另有恰好一个可用的「…资源包」时，把空的挪到「资源包.空.<时刻>」（不删），换成那个
    if (have && !pack_usable(root)) {
        DIR *d = opendir(docs); char hit[1024] = ""; int n = 0;
        if (d) { struct dirent *e; while ((e = readdir(d))) { char p[2100]; snprintf(p, sizeof p, "%s/%s", docs, e->d_name);
            if (e->d_name[0] != '.' && strcmp(e->d_name, "资源包") && ends_with(e->d_name, "资源包") && !stat(p, &st) && S_ISDIR(st.st_mode) && pack_usable(p)) { n++; strlcpy(hit, e->d_name, sizeof hit); } } closedir(d); }
        if (n == 1) {
            char aside[2100]; snprintf(aside, sizeof aside, "%s/资源包.空.%ld", docs, (long)time(NULL));
            if (rename(root, aside)) LOG("资源导入：把没有内容的「资源包」挪开失败：%s", strerror(errno));
            else { LOG("资源导入：「资源包」里没有可用内容，挪到 %s，改用「%s」", strrchr(aside, '/') + 1, hit); have = 0; }
        }
    }
    if (!have) {
        DIR *d = opendir(docs); char hit[1024] = ""; int n = 0;
        if (d) { struct dirent *e; while ((e = readdir(d))) { char p[2100]; snprintf(p, sizeof p, "%s/%s", docs, e->d_name);
            if (e->d_name[0] != '.' && strcmp(e->d_name, "资源包") && ends_with(e->d_name, "资源包") && !stat(p, &st) && S_ISDIR(st.st_mode)) { n++; strlcpy(hit, e->d_name, sizeof hit); } } closedir(d); }
        if (n != 1) { if (n > 1) LOG("资源导入：Documents 下有 %d 个「…资源包」目录，不猜，按没有资源提示", n); return S2A_IMPORT_NONE; }
        char from[2100]; snprintf(from, sizeof from, "%s/%s", docs, hit);
        if (rename(from, root)) { LOG("资源导入：把「%s」改名成「资源包」失败：%s", hit, strerror(errno)); return S2A_IMPORT_NONE; }
        LOG("资源导入：「%s」改名成「资源包」", hit);
    }
    char inc[2100]; snprintf(inc, sizeof inc, "%s/.incoming", root); int ntar = 0;
    // 「._」开头 = 访达从非 APFS 盘拷来的 AppleDouble 附属文件，不是包（s2a_pack_apply 选包同样跳过）
    DIR *d = opendir(inc); if (d) { struct dirent *e; while ((e = readdir(d))) if (ends_with(e->d_name, ".tar") && strncmp(e->d_name, "._", 2)) ntar++; closedir(d); }
    if (ntar) return S2A_IMPORT_PENDING;
    char mf[2100]; snprintf(mf, sizeof mf, "%s/manifest.json", root);
    if (stat(mf, &st)) return S2A_IMPORT_NONE;
    g_pack_bad = s2a_pack_check(root, g_pack_first, sizeof g_pack_first);
    if (g_pack_bad) { LOG("资源导入：资源包不完整，缺 / 不对 %d 个文件（如 %s）", g_pack_bad, g_pack_first); return S2A_IMPORT_BROKEN; }
    return S2A_IMPORT_READY;
}
// 资源还在传（11-02 维护者 测试设备2 暗黑3：推 21.7 GB 中途点开 App，已到的几批被解开、核对时 index.tsv 还没到，报「返回 -8」要玩家删文件夹重拷）。
//   推送契约（tools/push_pack.sh）：各批 tar 先推进 .incoming/，index.tsv 与 manifest.json 最后推。所以有待导入的包、且下面任一条成立 = 这次推送还没完，不能开始解：
//   ① index.tsv / manifest.json 不在（首次推送，清单还没到）；② manifest.json 的 index_sha256 就是 .applied（机上清单还是上次解开的那份，这次的新清单还没到）；
//   ③ 有 tar 没写完（没有归档尾：不足两块、大小不是 512 的倍数、末尾 1024 字节不全零）——在写的包去解会被当截短包，连同后面的包和 .pushed 一起删。
//   返回 1 = 还在传，why 写一句原因（日志用）
static int tar_done(const char *p) {
    struct stat st; if (stat(p, &st) || st.st_size < 1024 || st.st_size % 512) return 0;
    int fd = open(p, O_RDONLY); if (fd < 0) return 0;
    uint8_t b[1024]; int ok = pread(fd, b, sizeof b, st.st_size - (off_t)sizeof b) == (ssize_t)sizeof b; close(fd);
    for (size_t i = 0; ok && i < sizeof b; i++) ok = !b[i];
    return ok;
}
int s2a_import_transferring(const char *root, char *why, size_t n) {
    char p[2100], sha[200], ap[200] = ""; struct stat st; why[0] = 0;
    snprintf(p, sizeof p, "%s/index.tsv", root); int idx = !stat(p, &st);
    snprintf(p, sizeof p, "%s/manifest.json", root);
    if (!idx || stat(p, &st)) { snprintf(why, n, "%s还没到", idx ? "manifest.json" : "index.tsv"); return 1; }
    if (manifest_sha(p, sha, sizeof sha)) { snprintf(why, n, "manifest.json 还没写完（读不出 index_sha256）"); return 1; }
    snprintf(p, sizeof p, "%s/.applied", root); FILE *f = fopen(p, "r");
    if (f) { size_t k = fread(ap, 1, sizeof ap - 1, f); ap[k] = 0; fclose(f); ap[strcspn(ap, "\r\n")] = 0; }
    if (!strcmp(ap, sha)) { snprintf(why, n, "清单还是上次已导入的那份（%.12s），这次的新清单还没到", sha); return 1; }
    snprintf(p, sizeof p, "%s/.incoming", root); DIR *d = opendir(p); int busy = 0;
    if (d) { struct dirent *e; while (!busy && (e = readdir(d))) { if (!ends_with(e->d_name, ".tar") || !strncmp(e->d_name, "._", 2)) continue;
        char t[2400]; snprintf(t, sizeof t, "%s/%s", p, e->d_name); if (!tar_done(t)) { busy = 1; snprintf(why, n, "%s 还没写完", e->d_name); } } closedir(d); }
    return busy;
}
// 资源包指纹：待导入时看 .incoming 的包与清单（老版本的几十万个文件不用每轮扫）；没有 / 不完整时看整个「资源包」目录的文件数与字节。指纹在变 = 有东西正在拷进来
static uint64_t g_fp_n, g_fp_b;
static int fp_one(const char *p, const struct stat *st, int flag, struct FTW *f) { (void)p; (void)f; if (flag == FTW_F) { g_fp_n++; g_fp_b += (uint64_t)st->st_size; } return 0; }
static uint64_t pack_fp(const char *root, int pending) {
    g_fp_n = g_fp_b = 0; char p[2100]; struct stat st;
    if (!pending) nftw(root, fp_one, 32, FTW_PHYS);
    else { const char *top[] = {".incoming", "index.tsv", "manifest.json", ".applied"};
        for (int i = 0; i < 4; i++) { snprintf(p, sizeof p, "%s/%s", root, top[i]); if (i) { if (!stat(p, &st)) fp_one(p, &st, FTW_F, NULL); } else nftw(p, fp_one, 8, FTW_PHYS); } }
    return g_fp_n * 0x9E3779B97F4A7C15ULL ^ g_fp_b;
}
// 闸门每隔几秒判一次下一步（iOS 闸门线程按它显示页面）。w 记上次的指纹、它最后变的时刻、是否真变过（首轮不算变）：
//   就绪 → 进游戏；待导入且推送已完、指纹稳住 ≥ 3 秒 → 导入（SETTLE = 还在等这 3 秒）；推送没完 → 等（指纹 3 分钟没动 → 停住了，请回 Mac 重装）；
//   没有 / 不完整、但指纹 1 分钟内变过（玩家正用访达拷）→ 等；否则说明页
int s2a_import_decide(const char *docs, struct s2a_gate_watch *w, time_t now, char *why, size_t n) {
    int st = s2a_import_state(docs); why[0] = 0;
    if (st == S2A_IMPORT_READY) return S2A_GATE_READY;
    char root[2048]; snprintf(root, sizeof root, "%s/资源包", docs);
    int busy = st == S2A_IMPORT_PENDING && s2a_import_transferring(root, why, n);
    uint64_t fp = pack_fp(root, st == S2A_IMPORT_PENDING);
    if (!w->n++) { w->fp = fp; w->changed = now; w->moved = 0; }
    else if (fp != w->fp) { w->fp = fp; w->changed = now; w->moved = 1; }
    long still = (long)(now - w->changed);
    if (st == S2A_IMPORT_PENDING) {
        if (busy) return still >= S2A_GATE_STALL_S ? S2A_GATE_STALLED : S2A_GATE_WAIT;
        if (w->n >= 2 && still >= S2A_GATE_SETTLE_S) return S2A_GATE_IMPORT;
        snprintf(why, n, "待导入的包 %ld 秒前还在变，等它稳住", still); return w->moved ? S2A_GATE_WAIT : S2A_GATE_SETTLE;
    }
    if (w->moved && still < S2A_GATE_MOVING_S) { snprintf(why, n, "「资源包」%ld 秒前还在变（正在拷进来）", still); return S2A_GATE_WAIT; }
    return st == S2A_IMPORT_BROKEN ? S2A_GATE_BROKEN : S2A_GATE_NONE;
}
// 资源包闸门查哪个文档根、查不查（11-07 Duo「游戏没能启动：系统功能没能准备好」定因）：原先只看「S2A_GAME_DATA 没设」才查、且一律查进程级 Documents——
//   多款容器选了游戏后 launchctx.c export_env 总会写 S2A_GAME_DATA → 不查，资源包不在也照起，服务层读 资源包/exefs/main.npdm 抛 DirectoryNotFound、归成 service_init。
//   现在：多款容器 → 查本款文档根（ctx->docs = Documents/games/<游戏号>，资源包在它下面）；单款兼容来源没设 S2A_GAME_DATA → 进程级 Documents（同原先）；
//   其余（单款文件来源 / 开发起局指定了 S2A_GAME_DATA）不查（同原先）。返回 1 = 要查，docs 写进 out；0 = 不查
int s2a_import_docs_pick(const s2a_launch_ctx *c, int game_data_env, const char *proc_docs, char *out, size_t n) {
    if (n) out[0] = 0;
    if (c && c->source == S2A_LCTX_CONTAINER) { snprintf(out, n, "%s", c->docs); return 1; }
    if (game_data_env) return 0;
    snprintf(out, n, "%s", proc_docs ? proc_docs : ""); return 1;
}
// 多款容器的游戏库（app.m s2a_shell_game_data_ready / 选游戏前）：本款资源包状态，按同一个判据（s2a_import_state）查 Documents/games/<游戏号>
//   proc_docs = 进程级 Documents（iOS 调用方取 s2a_container_docs），本款文档根 = <proc_docs>/games/<游戏号>（同 launchctx.c s2a_launch_ctx_resolve_game）
int s2a_import_state_game(const char *proc_docs, const char *gid) {
    char docs[1024]; if (!proc_docs || !*proc_docs || !gid || !*gid || strchr(gid, '/') || gid[0] == '.') return S2A_IMPORT_NONE;
    snprintf(docs, sizeof docs, "%s/games/%s", proc_docs, gid);
    return s2a_import_state(docs);
}
// 游戏库快判：解包成功时写的 .applied 等于当前清单 = 这份已核对过每个文件，直接算就绪。
//   库界面在主线程、每次回前台都判，逐文件 stat 一款 10 万文件的游戏要 2.5 秒以上（冷缓存更久，玩家「每次打开都要等」）；
//   完整核对仍在点 Play 后的导入闸门（s2a_import_state）做
int s2a_import_state_game_quick(const char *proc_docs, const char *gid) {
    if (proc_docs && *proc_docs && gid && *gid && !strchr(gid, '/') && gid[0] != '.') {
        char mf[2200], ap[2200], want[200], got[200] = ""; snprintf(mf, sizeof mf, "%s/games/%s/资源包/manifest.json", proc_docs, gid); snprintf(ap, sizeof ap, "%s/games/%s/资源包/.applied", proc_docs, gid);
        FILE *f = fopen(ap, "r");
        if (f) { size_t k = fread(got, 1, sizeof got - 1, f); fclose(f); got[k] = 0; got[strcspn(got, "\r\n")] = 0; }
        if (got[0] && !manifest_sha(mf, want, sizeof want) && !strcmp(got, want)) return S2A_IMPORT_READY;
    }
    return s2a_import_state_game(proc_docs, gid);
}
#if defined(__APPLE__) && TARGET_OS_IPHONE
#include <CoreFoundation/CoreFoundation.h>
#include <pthread.h>
#include <dispatch/dispatch.h>
#include <stdatomic.h>
void s2a_ios_show_notice(const char *symbol, const char *title, const char *body, int done, int total, int diag);   // app.m：整屏提示页（notice.m 画）
void s2a_ios_keep_awake(int on);   // app.m：导入期间关自动锁屏（原生层线审：锁屏 = 挂起 = 解包停住）
void s2a_front_run(void (*guest_main)(void));
// 提示页文案（11-02 维护者：一句人话说清发生了什么 + 玩家下一步；失败原因、文件名、返回码只进 host.log / unpack.log，经「导出诊断包」带走）。
//   「导入完」请重新打开（原生层线 10-23：不同进程内接着进游戏）
#define K_REINSTALL "请在 Mac 上的 S2Arm 里对这款游戏再点一次「装到 iPhone」"
static void note_settle(void) { s2a_ios_show_notice("magnifyingglass", "正在检查游戏资源", "马上就好。", 0, 0, 0); }
static void note_wait(void) { s2a_ios_show_notice("arrow.down.circle.fill", "游戏资源还没传完", "传完会自动开始导入，不用做任何操作。请让本 App 留在前台、不要锁屏。", 0, 0, 0); }
static void note_stalled(void) { s2a_ios_show_notice("pause.circle.fill", "游戏资源传到一半停住了", K_REINSTALL "，会接着传完；传完这里会自动继续。", 0, 0, 0); }
static void note_done(void) { s2a_ios_show_notice("checkmark.circle.fill", "游戏资源导入完成", "请从后台划掉本 App，再重新打开就能进入游戏。", 0, 0, 0); }
static void note_entering(void) { s2a_ios_show_notice("checkmark.circle.fill", "游戏资源导入完成", "正在进入游戏…", 0, 0, 0); }   // 有人等（预热路径）：导入完当场接着起游戏
static void note_ready_reopen(void) { s2a_ios_show_notice("checkmark.circle.fill", "游戏资源已经放好", "请从后台划掉本 App，再重新打开就能进入游戏。", 0, 0, 0); }
static void note_fail(void) { s2a_ios_show_notice("exclamationmark.triangle.fill", "游戏资源没导入成功", K_REINSTALL "，它会把缺的补上。还是不行的话，点下面「导出诊断包」发给我们。", 0, 0, 1); }
static char g_imp_docs[1024], g_imp_name[256];
// 缺资源包 / 资源包不全的说明页（app.m s2a_ios_show_pack_help：示意图 + 三步 + 「我已放好，重新检查」）。10-26 维护者：「壳上做好说明别闪退，
//   打开发现缺资源包就告诉用户把启动器导出的资源包导到手机上指定位置」
void s2a_ios_show_pack_help(const char *title, const char *detail, const char *app, void (*recheck)(void));
void s2a_ios_hide_pack_help(void); void s2a_ios_hide_notice(void);
// 闸门线程（11-02 根修「资源没传完就打开 App」）：每 3 秒按 s2a_import_decide 判一次——还在传就说「还没传完、传完自动继续」，传完自己开始导入；
//   不再一看到待导入的包就解（推送中途解 = 清单没到报「返回 -8」/ 在写的包被当截短包删掉）。说明页的「重新检查」= 立刻再判一次
static dispatch_semaphore_t g_poll_sem, g_ready_sem; static _Atomic int g_gate_waiting;
static void gate_recheck(void) { dispatch_semaphore_signal(g_poll_sem); }
static void imp_progress(int done, int total) { s2a_ios_show_notice("shippingbox.fill", "正在导入游戏资源", "第一次要几分钟，请让本 App 留在前台、不要锁屏。", done, total, 0); }
static void gate_show(int act, int again) {
    if (act == S2A_GATE_SETTLE) { note_settle(); return; }
    if (act == S2A_GATE_WAIT) { note_wait(); return; }
    if (act == S2A_GATE_STALLED) { note_stalled(); return; }
    int st = act == S2A_GATE_BROKEN ? S2A_IMPORT_BROKEN : S2A_IMPORT_NONE; char d[1024]; s2a_pack_help_detail(st, again, d, sizeof d);
    s2a_ios_show_pack_help(st == S2A_IMPORT_BROKEN ? "游戏资源不完整" : "还缺游戏资源", d, g_imp_name, gate_recheck);
}
static void *gate_thread(void *a) { (void)a;
    static const char *const act_name[] = {"就绪", "导入", "等包稳住", "资源还在传", "资源传输停住了", "没有游戏资源", "资源包不完整"};
    struct s2a_gate_watch w = {0}; int last = -1, poked = 0;
    for (;;) {
        char why[600]; int act = s2a_import_decide(g_imp_docs, &w, time(NULL), why, sizeof why);
        if (act == S2A_GATE_READY) {
            // 预热路径：宿主线程在 s2a_import_gate 里等 → 收起页面、接着起游戏；老路径（闸门在主线程、UIKit 接管后不返回）没人等 → 请玩家重新打开
            for (int i = 0; i < 20 && !atomic_load(&g_gate_waiting); i++) usleep(100000);
            s2a_ios_keep_awake(0); s2a_ios_hide_pack_help();
            if (atomic_load(&g_gate_waiting)) { s2a_ios_hide_notice(); LOG("资源导入：就绪，收起页面、接着起游戏"); dispatch_semaphore_signal(g_ready_sem); }
            else { LOG("资源导入：就绪，提示玩家重新打开"); note_ready_reopen(); }
            return NULL;
        }
        if (act == S2A_GATE_IMPORT) {
            LOG("资源导入：推送已完、包已稳住，后台解开");
            s2a_ios_keep_awake(1); imp_progress(0, 1); s2a_pack_apply_progress = imp_progress; int rc = s2a_pack_apply(g_imp_docs); s2a_pack_apply_progress = NULL;
            // 导入完回去再判一次：就绪 → 宿主线程在等（预热路径）就当场接着起游戏，没人等（老路径）才请玩家重新打开（11-02 用户体验线：首启导入完还要划掉重开是硬伤；
            //   10-23 定「重新打开」时闸门还在主线程、UIKit 接管后不返回，接不下去；10-26 预热路径能接了，这一支没跟上）。解包是流式的、文件都关了，不留占用
            //   页面也分两路：有人等就说「正在进入游戏」，「划掉重开」只留给没人等的老路径（11-02 用户体验线：成功路径先闪一句叫玩家划掉，紧接着游戏自己进去了）
            if (!rc) { LOG("资源导入：完成，再判一次就绪（有人等就接着起游戏）"); if (atomic_load(&g_gate_waiting)) note_entering(); else note_done(); w = (struct s2a_gate_watch){0}; last = -1; continue; }
            // 解到一半又来了新推送（执行器重推 / 又拷进一份）：回去等它传完再试，不报失败
            char root[2048], w2[600]; snprintf(root, sizeof root, "%s/资源包", g_imp_docs);
            if (s2a_import_state(g_imp_docs) == S2A_IMPORT_PENDING && s2a_import_transferring(root, w2, sizeof w2)) {
                LOG("资源导入：这次没解成（%s），但又有资源在传（%s）：等传完自动重试", s2a_pack_apply_failure(), w2); w = (struct s2a_gate_watch){0}; last = -1; continue; }
            s2a_ios_keep_awake(0); note_fail(); LOG("【红】资源导入：失败 %s", s2a_pack_apply_failure());
            return NULL;   // 不退出进程：UIKit 留着显示结果（玩家看到 _exit 会以为闪退）
        }
        if (act != last || poked) {
            if (act != last) LOG("资源导入：%s%s%s", act_name[act], why[0] ? "：" : "", why);
            gate_show(act, poked && act == last); s2a_ios_keep_awake(act == S2A_GATE_SETTLE || act == S2A_GATE_WAIT); last = act;
        }
        poked = !dispatch_semaphore_wait(g_poll_sem, dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC));
    }
}
// host_main.c 自检之后、占地址窗口之前调（只在没指定 S2A_GAME_DATA 时，即玩家正常打开）：就绪直接返回 0；否则起闸门线程显示页面、等它判就绪。
//   预热路径（UIKit 已在主线程，本函数在宿主线程）里 s2a_front_run 立即返回：等到就绪就返回 1 接着起游戏（当场进）；老路径里 s2a_front_run 不返回。
//   返回 1 = 本局资源是闸门当场解开的：起局前读过的随包补丁 / 能力清单读的是解开前的资源包，调用方要重读（审计 P1-106）
static int imp_docs(char *out, size_t n) {   // 本局资源包闸门的文档根；0 = 本局不查（见 s2a_import_docs_pick）
    char proc[1024]; s2a_container_docs(proc, sizeof proc);
    return s2a_import_docs_pick(s2a_launch_ctx_get(), getenv("S2A_GAME_DATA") != NULL, proc, out, n);
}
int s2a_import_gate_applies(void) { char d[1024]; return imp_docs(d, sizeof d); }
int s2a_import_gate(void) {
    if (!imp_docs(g_imp_docs, sizeof g_imp_docs)) return 0;
    CFStringRef dn = CFBundleGetValueForInfoDictionaryKey(CFBundleGetMainBundle(), CFSTR("CFBundleDisplayName"));
    if (!dn || !CFStringGetCString(dn, g_imp_name, sizeof g_imp_name, kCFStringEncodingUTF8)) strlcpy(g_imp_name, "S2Arm", sizeof g_imp_name);
    int st = s2a_import_state(g_imp_docs);
    if (st == S2A_IMPORT_READY) return 0;
    LOG("资源导入：资源没就绪（状态 %d），起闸门线程每 3 秒判一次（UIKit 先起来，免得被启动看门狗杀）", st);
    g_poll_sem = dispatch_semaphore_create(0); g_ready_sem = dispatch_semaphore_create(0);
    pthread_t th; if (pthread_create(&th, NULL, gate_thread, NULL)) die("资源导入线程起不来");
    s2a_front_run(NULL);
    atomic_store(&g_gate_waiting, 1); dispatch_semaphore_wait(g_ready_sem, DISPATCH_TIME_FOREVER); atomic_store(&g_gate_waiting, 0);
    return 1;
}
// main() 起 UIKit 前问：资源包没就绪（要显示说明页 / 导入进度）就走延后启动，闸门在宿主线程上等「重新检查」
int s2a_import_needs_ui(void) {
    char docs[1024]; if (!imp_docs(docs, sizeof docs)) return 0;
    return s2a_import_state(docs) != S2A_IMPORT_READY;
}
// 游戏中途因缺数据 Break（lifecycle.c 收局前问）：资源包这时核不过 = 是缺资源，不退出，弹同一页；游戏线程已坏、接不上，重新检查就绪后请玩家重新打开。
//   返回 1 = 已接管（调用方不退出，本线程停在这里）；资源包是好的返回 0（照常退出，是别的原因）
static void break_recheck(void) {
    int st = s2a_import_state(g_imp_docs);
    if (st == S2A_IMPORT_READY) { s2a_ios_hide_pack_help(); note_ready_reopen(); return; }
    char d[1024]; s2a_pack_help_detail(st, 1, d, sizeof d); s2a_ios_show_pack_help(st == S2A_IMPORT_BROKEN ? "游戏资源不完整" : "还缺游戏资源", d, g_imp_name, break_recheck);
}
int s2a_pack_help_on_break(void) {
    if (!imp_docs(g_imp_docs, sizeof g_imp_docs)) s2a_container_docs(g_imp_docs, sizeof g_imp_docs);   // 不查闸门的局（开发指定资源包）照旧核进程级 Documents
    if (!g_imp_name[0]) { CFStringRef dn = CFBundleGetValueForInfoDictionaryKey(CFBundleGetMainBundle(), CFSTR("CFBundleDisplayName"));
        if (!dn || !CFStringGetCString(dn, g_imp_name, sizeof g_imp_name, kCFStringEncodingUTF8)) strlcpy(g_imp_name, "S2Arm", sizeof g_imp_name); }
    int st = s2a_import_state(g_imp_docs); if (st == S2A_IMPORT_READY) return 0;
    LOG("【红】游戏 Break 时资源包%s：不退出，显示说明页", st == S2A_IMPORT_BROKEN ? "不完整" : "不在");
    char d[1024]; s2a_pack_help_detail(st, 0, d, sizeof d); s2a_ios_show_pack_help(st == S2A_IMPORT_BROKEN ? "游戏资源不完整" : "还缺游戏资源", d, g_imp_name, break_recheck);
    return 1;
}
#endif
