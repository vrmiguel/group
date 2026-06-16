#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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

int main(int argc, char **argv) {
    const char *app = (argc > 1) ? argv[1] : "pgpad";

    resp_fn responsible = (resp_fn)dlsym(
        RTLD_DEFAULT, "responsibility_get_pid_responsible_for_pid");

    pid_t *pids;
    int n = snapshot(&pids);
    if (n < 0) { fprintf(stderr, "proc_listallpids failed\n"); return 1; }

    // find app through name
    pid_t app_pid = 0;
    for (int i = 0; i < n; i++) {
        pid_t p = pids[i];
        if (p <= 0) continue;
        int rpid = responsible ? responsible(p) : p;
        if (rpid < 0) rpid = p;
        if (rpid != p) {
            continue;
        }
        char name[256] = {0};
        proc_name(p, name, sizeof(name));
        if (strcasestr(name, app)) { app_pid = p; break; }
    }
    if (app_pid == 0) {
        fprintf(stderr, "%s not running\n", app);
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
        if (rpid != app_pid) continue;
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
