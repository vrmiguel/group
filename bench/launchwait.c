// launchwait: launch a macOS app through `open` and measure the time until
// its main window is on screen.
//
// The clock starts right before `open` is spawned and stops when the app's
// main process owns an on-screen, normal-layer window at least
// --min-width x --min-height points large. The size threshold exists so that
// splash screens (DBeaver, pgAdmin 4) are not mistaken for the main window.
//
// Reading window owners and bounds through CGWindowListCopyWindowInfo does not
// require the Screen Recording permission (only window titles would).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <limits.h>
#include <spawn.h>
#include <time.h>
#include <unistd.h>
#include <sys/wait.h>
#include <libproc.h>
#include <CoreGraphics/CoreGraphics.h>

extern char **environ;

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

static void sleep_ms(int ms) {
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

// finds a running process whose executable lives in <app_path>/Contents/MacOS/
static pid_t find_app_pid(const char *app_path) {
    char needle[PATH_MAX + 32];
    snprintf(needle, sizeof(needle), "%s/Contents/MacOS/", app_path);

    int cap = 4096;
    pid_t *pids = malloc((size_t)cap * sizeof(pid_t));
    // proc_listallpids returns the number of pids, not a byte count
    int n = proc_listallpids(pids, cap * (int)sizeof(pid_t));
    pid_t found = 0;
    for (int i = 0; i < n && !found; i++) {
        char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
        if (pids[i] <= 0) continue;
        if (proc_pidpath(pids[i], path, sizeof(path)) <= 0) continue;
        if (strncasecmp(path, needle, strlen(needle)) == 0) found = pids[i];
    }
    free(pids);
    return found;
}

static int dict_int(CFDictionaryRef d, CFStringRef key, int *out) {
    CFNumberRef num = CFDictionaryGetValue(d, key);
    return num && CFNumberGetValue(num, kCFNumberIntType, out);
}

// returns 1 if `pid` owns an on-screen, layer-0 window of at least min_w x min_h
// points; with `list` set, prints every on-screen window of that pid instead
static int has_main_window(pid_t pid, int min_w, int min_h, int list,
                           char *sig, size_t sig_len) {
    CFArrayRef windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID);
    if (!windows) return 0;

    int found = 0;
    if (sig) sig[0] = 0;
    for (CFIndex i = 0; i < CFArrayGetCount(windows); i++) {
        CFDictionaryRef w = CFArrayGetValueAtIndex(windows, i);
        int owner = 0, layer = 0;
        CGRect bounds = CGRectZero;

        if (!dict_int(w, kCGWindowOwnerPID, &owner) || owner != pid) continue;
        dict_int(w, kCGWindowLayer, &layer);
        CFDictionaryRef bd = CFDictionaryGetValue(w, kCGWindowBounds);
        if (bd) CGRectMakeWithDictionaryRepresentation(bd, &bounds);

        if (sig) {
            char item[96];
            snprintf(item, sizeof(item), "[L%d %.0fx%.0f] ", layer,
                bounds.size.width, bounds.size.height);
            strlcat(sig, item, sig_len);
        }
        if (list) {
            printf("layer %d  %.0fx%.0f at (%.0f,%.0f)\n", layer,
                bounds.size.width, bounds.size.height,
                bounds.origin.x, bounds.origin.y);
            continue;
        }
        if (layer == 0 &&
            bounds.size.width >= min_w && bounds.size.height >= min_h) {
            found = 1;
            if (!sig) break;
        }
    }
    CFRelease(windows);
    return found;
}

static void usage(const char *argv0) {
    fprintf(stderr,
        "usage:\n"
        "  %s </path/to/App.app> [--min-width W] [--min-height H] [--timeout S] [--trace]\n"
        "      launches the app and prints \"<pid> <startup_ms>\"; --trace logs\n"
        "      every change in the app's windows to stderr while waiting\n"
        "  %s --list <pid>\n"
        "      prints the on-screen windows owned by <pid> (to calibrate sizes)\n"
        "\n"
        "defaults: --min-width 800 --min-height 500 --timeout 120\n",
        argv0, argv0);
}

int main(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "--list") == 0) {
        has_main_window((pid_t)atoi(argv[2]), 0, 0, 1, NULL, 0);
        return 0;
    }
    if (argc < 2 || argv[1][0] == '-') { usage(argv[0]); return 2; }

    const char *app_arg = argv[1];
    int min_w = 800, min_h = 500, timeout_s = 120, trace = 0;
    for (int i = 2; i < argc; i++) {
        if (i + 1 < argc && strcmp(argv[i], "--min-width") == 0) min_w = atoi(argv[++i]);
        else if (i + 1 < argc && strcmp(argv[i], "--min-height") == 0) min_h = atoi(argv[++i]);
        else if (i + 1 < argc && strcmp(argv[i], "--timeout") == 0) timeout_s = atoi(argv[++i]);
        else if (strcmp(argv[i], "--trace") == 0) trace = 1;
        else { usage(argv[0]); return 2; }
    }

    char app_path[PATH_MAX];
    if (!realpath(app_arg, app_path)) {
        fprintf(stderr, "error: %s not found\n", app_arg);
        return 1;
    }
    // strip a trailing slash so the Contents/MacOS match works
    size_t len = strlen(app_path);
    if (len > 1 && app_path[len - 1] == '/') app_path[len - 1] = 0;

    if (find_app_pid(app_path)) {
        fprintf(stderr, "error: %s is already running; quit it first\n", app_path);
        return 1;
    }

    char *open_argv[] = { "open", "-a", app_path, NULL };
    pid_t open_pid;
    double t0 = now_ms();
    if (posix_spawnp(&open_pid, "open", NULL, NULL, open_argv, environ) != 0) {
        perror("posix_spawnp open");
        return 1;
    }

    double deadline = t0 + timeout_s * 1e3;
    pid_t app_pid = 0;
    char sig[2048] = {0}, last_sig[2048] = {0};
    while (now_ms() < deadline) {
        if (!app_pid) app_pid = find_app_pid(app_path);
        int ready = app_pid && has_main_window(app_pid, min_w, min_h, 0,
            trace ? sig : NULL, sizeof(sig));
        if (trace && app_pid && strcmp(sig, last_sig) != 0) {
            fprintf(stderr, "%8.0f ms  %s\n", now_ms() - t0, sig[0] ? sig : "(no windows)");
            strlcpy(last_sig, sig, sizeof(last_sig));
        }
        if (ready) {
            double elapsed = now_ms() - t0;
            waitpid(open_pid, NULL, 0);
            printf("%d %.0f\n", app_pid, elapsed);
            return 0;
        }
        sleep_ms(5);
    }

    waitpid(open_pid, NULL, 0);
    fprintf(stderr, "error: no main window within %d s (pid %d)\n",
        timeout_s, app_pid);
    return 1;
}
