#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <dlfcn.h>
#include <libproc.h>
#include <sys/resource.h>

typedef int (*resp_fn)(int);

typedef struct {
    pid_t pid;
    // in bytes, should match Activity Monitor's Memory column
    unsigned long long footprint;
    char name[256];
} Proc;

typedef enum {
    TARGET_AUTO,
    TARGET_PID,
    TARGET_APP_PATH,
    TARGET_BUNDLE,
    TARGET_NAME
} TargetMode;

typedef struct {
    TargetMode mode;
    const char *value;
    pid_t pid;
} Target;

static int contains_ci(const char *haystack, const char *needle) {
    return haystack && needle && strcasestr(haystack, needle) != NULL;
}

static int has_suffix_ci(const char *s, const char *suffix) {
    size_t slen, suffix_len;

    if (!s || !suffix) return 0;
    slen = strlen(s);
    suffix_len = strlen(suffix);
    if (suffix_len > slen) return 0;
    return strcasecmp(s + slen - suffix_len, suffix) == 0;
}

static int contains_path_separator(const char *s) {
    return s && strchr(s, '/') != NULL;
}

static const char *basename_of(const char *path) {
    const char *slash = path ? strrchr(path, '/') : NULL;
    return slash ? slash + 1 : path;
}

static void bundle_name_for_arg(const char *arg, char *out, size_t out_len) {
    const char *base = basename_of(arg);

    if (has_suffix_ci(base, ".app")) {
        snprintf(out, out_len, "%s", base);
    } else {
        snprintf(out, out_len, "%s.app", base);
    }
}

static void pid_path(pid_t p, char *path, size_t path_len) {
    path[0] = 0;
    proc_pidpath(p, path, (uint32_t)path_len);
}

static int pid_path_matches_bundle(pid_t p, const char *bundle_arg) {
    char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
    char bundle[512];
    char needle[640];

    pid_path(p, path, sizeof(path));
    bundle_name_for_arg(bundle_arg, bundle, sizeof(bundle));
    snprintf(needle, sizeof(needle), "/%s/Contents/MacOS/", bundle);

    return contains_ci(path, needle);
}

static int pid_path_matches_app_path(pid_t p, const char *app_path) {
    char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
    char needle[PROC_PIDPATHINFO_MAXSIZE + 32];
    size_t app_path_len;

    pid_path(p, path, sizeof(path));
    app_path_len = strlen(app_path);
    while (app_path_len > 1 && app_path[app_path_len - 1] == '/') {
        app_path_len--;
    }
    snprintf(needle, sizeof(needle), "%.*s/Contents/MacOS/",
        (int)app_path_len, app_path);

    return contains_ci(path, needle);
}

static int pid_name_matches(pid_t p, const char *name) {
    char procname[256] = {0};

    proc_name(p, procname, sizeof(procname));
    return strcmp(procname, name) == 0;
}

static int pid_auto_matches(pid_t p, const char *target, int pass) {
    if (contains_path_separator(target)) {
        return pass == 0 && pid_path_matches_app_path(p, target);
    }

    if (pass == 0) {
        return pid_path_matches_bundle(p, target);
    }

    return pid_name_matches(p, target);
}

static int by_footprint_desc(const void *a, const void *b) {
    unsigned long long fa = ((const Proc *)a)->footprint;
    unsigned long long fb = ((const Proc *)b)->footprint;
    return (fa < fb) - (fa > fb);
}

static int snapshot(pid_t **out) {
    int cap = 4096, bytes, count;
    pid_t *pids;
    for (;;) {
        pids = malloc((size_t)cap * sizeof(pid_t));
        bytes = proc_listallpids(pids, cap * (int)sizeof(pid_t));
        if (bytes <= 0) { free(pids); return -1; }
        count = bytes / (int)sizeof(pid_t);
        if (count < cap) {
            // non-full buffer is the only way to be sure proc_listallpids returned
            // all matching pids
            break;
        }
        free(pids); cap *= 2;
    }
    *out = pids;
    return count;
}

static unsigned long long footprint_of(pid_t p) {
    struct rusage_info_v2 ri;
    if (proc_pid_rusage(p, RUSAGE_INFO_V2, (rusage_info_t *)&ri) == 0)
        return ri.ri_phys_footprint;
    // 0 if we dont own this process
    return 0;
}

static void usage(const char *argv0) {
    fprintf(stderr,
        "usage:\n"
        "  %s                 # shorthand for: --bundle pgpad.app\n"
        "  %s <target>        # shorthand: bundle name first, then exact process name\n"
        "  %s --pid <pid>\n"
        "  %s --app </path/to/App.app>\n"
        "  %s --bundle <App.app|App>\n"
        "  %s --name <process-name>\n",
        argv0, argv0, argv0, argv0, argv0, argv0);
}

static int parse_pid(const char *s, pid_t *out) {
    char *end = NULL;
    long pid;

    if (!s || !*s) return 0;
    pid = strtol(s, &end, 10);
    if (*end != 0 || pid <= 0) return 0;
    *out = (pid_t)pid;
    return 1;
}

static int parse_target(int argc, char **argv, Target *target) {
    target->mode = TARGET_AUTO;
    target->value = "pgpad";
    target->pid = 0;

    if (argc == 1) return 1;

    if (argc == 2) {
        if (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0) {
            return 0;
        }
        target->mode = TARGET_AUTO;
        target->value = argv[1];
        return 1;
    }

    if (argc != 3) return 0;

    if (strcmp(argv[1], "--pid") == 0) {
        target->mode = TARGET_PID;
        if (!parse_pid(argv[2], &target->pid)) return 0;
        return 1;
    }
    if (strcmp(argv[1], "--app") == 0) {
        target->mode = TARGET_APP_PATH;
        target->value = argv[2];
        return 1;
    }
    if (strcmp(argv[1], "--bundle") == 0) {
        target->mode = TARGET_BUNDLE;
        target->value = argv[2];
        return 1;
    }
    if (strcmp(argv[1], "--name") == 0) {
        target->mode = TARGET_NAME;
        target->value = argv[2];
        return 1;
    }

    return 0;
}

static int pid_matches_target(pid_t p, const Target *target, int pass) {
    switch (target->mode) {
    case TARGET_PID:
        return p == target->pid;
    case TARGET_APP_PATH:
        return pid_path_matches_app_path(p, target->value);
    case TARGET_BUNDLE:
        return pid_path_matches_bundle(p, target->value);
    case TARGET_NAME:
        return pid_name_matches(p, target->value);
    case TARGET_AUTO:
        return pid_auto_matches(p, target->value, pass);
    }
    return 0;
}

int main(int argc, char **argv) {
    Target target;
    if (!parse_target(argc, argv, &target)) {
        usage(argv[0]);
        return (argc == 2 &&
            (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0)) ? 0 : 2;
    }

    resp_fn responsible = (resp_fn)dlsym(
        RTLD_DEFAULT, "responsibility_get_pid_responsible_for_pid");

    pid_t *pids;
    int n = snapshot(&pids);
    if (n < 0) { fprintf(stderr, "proc_listallpids failed\n"); return 1; }

    pid_t app_pid = 0;
    int passes = target.mode == TARGET_AUTO ? 2 : 1;
    for (int pass = 0; pass < passes && app_pid == 0; pass++) {

        for (int i = 0; i < n; i++) {
            pid_t p = pids[i];
            if (p <= 0) continue;
            if (pid_matches_target(p, &target, pass)) { app_pid = p; break; }
        }
    }
    if (app_pid == 0) {
        fprintf(stderr, "%s not running\n", target.value ? target.value : "target");
        free(pids);
        return 1;
    }

    // get related process to this app
    Proc *group = malloc((size_t)n * sizeof(Proc));
    int g = 0;
    for (int i = 0; i < n; i++) {
        pid_t p = pids[i];
        if (p <= 0) continue;
        int rpid = responsible ? responsible(p) : p;
        if (rpid < 0) rpid = p;
        if (p != app_pid && rpid != app_pid) continue;
        group[g].pid = p;
        group[g].footprint = footprint_of(p);
        group[g].name[0] = 0;
        proc_name(p, group[g].name, sizeof(group[g].name));
        g++;
    }
    free(pids);

    qsort(group, (size_t)g, sizeof(Proc), by_footprint_desc);

    unsigned long long total = 0;
    for (int i = 0; i < g; i++) {
        printf("%7d  %8.1f MB  %s\n",
               group[i].pid, group[i].footprint / 1e6, group[i].name);
        total += group[i].footprint;
    }
    printf("%7s  %8.1f MB  (total, %d processes)\n", "", total / 1e6, g);

    free(group);
    return 0;
}
