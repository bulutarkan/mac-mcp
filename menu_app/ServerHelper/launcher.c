// Mac MCP Server: the process macOS holds Mac MCP's permissions for.
//
// macOS records Accessibility, Screen Recording and Automation consent for the
// responsible process of whatever asks. Started by launchd, this app is its own
// responsible process, and so is every process it spawns; it runs the server
// (the venv's python -m uvicorn ...) as a child, so the prompts and System
// Settings say "Mac MCP" instead of "Python". It must never exec: after exec the
// responsible process would run Python's code and macOS would see Python again.
//
// Keep this file unchanged. With an ad-hoc signature macOS ties the grant to the
// exact binary, so any change means the owner has to allow everything again.
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char *argv[]) {
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
