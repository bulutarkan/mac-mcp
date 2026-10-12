// Mac MCP Server: the app macOS holds Mac MCP's permissions for.
//
// macOS records Accessibility, Screen Recording and Automation consent for the
// responsible process of whatever asks. Started by launchd, this app is its own
// responsible process, and so is every process it spawns; it runs the server
// (the venv's python -m uvicorn ...) as a child, so the prompts and System
// Settings say "Mac MCP" instead of "Python". It must never exec: after exec the
// responsible process would run Python's code and macOS would see Python again.
//
//   mac-mcp-server COMMAND [ARGS...]               run the server as a child
//   mac-mcp-server --request accessibility|screen-recording
//       ask macOS for a permission. Opened as an app (open -a), macOS shows its
//       prompt; asked from inside a background process it stays silent.
//   mac-mcp-server --check accessibility|screen-recording
//       print granted or denied. A process sees a new grant only after it
//       starts, so the server asks a fresh one.
//
// Keep this file unchanged. With an ad-hoc signature macOS ties the grant to the
// exact binary, so any change means the owner has to allow everything again.
#include <ApplicationServices/ApplicationServices.h>
#include <CoreGraphics/CoreGraphics.h>
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static bool accessibility(bool prompt) {
    const void *keys[] = {kAXTrustedCheckOptionPrompt};
    const void *values[] = {prompt ? kCFBooleanTrue : kCFBooleanFalse};
    CFDictionaryRef options = CFDictionaryCreate(NULL, keys, values, 1, &kCFTypeDictionaryKeyCallBacks,
                                                 &kCFTypeDictionaryValueCallBacks);
    bool trusted = AXIsProcessTrustedWithOptions(options);
    CFRelease(options);
    return trusted;
}

static int permission(const char *mode, const char *name) {
    bool request = strcmp(mode, "--request") == 0;
    bool granted;
    if (strcmp(name, "accessibility") == 0) {
        granted = accessibility(request);
    } else if (strcmp(name, "screen-recording") == 0) {
        granted = request ? CGRequestScreenCaptureAccess() : CGPreflightScreenCaptureAccess();
    } else {
        fprintf(stderr, "mac-mcp-server: unknown permission %s\n", name);
        return 64;
    }
    // Stay a moment so the system prompt is attached to a running app.
    if (request && !granted) sleep(2);
    printf("%s\n", granted ? "granted" : "denied");
    return granted ? 0 : 1;
}

int main(int argc, char *argv[]) {
    if (argc == 3 && (strcmp(argv[1], "--request") == 0 || strcmp(argv[1], "--check") == 0)) {
        return permission(argv[1], argv[2]);
    }
    if (argc < 2) {
        fprintf(stderr, "usage: %s command [arguments...]\n", argv[0]);
        return 64;
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    pid_t pid = 0;
    int error = posix_spawn(&pid, argv[1], NULL, NULL, &argv[1], environ);
    if (error != 0) {
        errno = error;
        perror("mac-mcp-server: cannot start the server");
        return 127;
    }
    child = pid;
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
