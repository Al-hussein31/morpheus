/* Morpheus.app's main program: starts the Python listener as a child and waits.
 *
 * macOS grants microphone, accessibility and screen recording to the app that
 * started a process, so the listener must run as this app's child (not exec'd
 * in its place) for the permissions to belong to "Morpheus".
 */
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child;

static void forward(int sig) { if (child > 0) kill(child, sig); }

int main(void) {
    char *argv[] = {PYTHON, "-u", DIR "/voice.py", "--menubar", NULL};
    if (chdir(DIR) != 0) { perror("chdir"); return 1; }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    signal(SIGUSR1, forward);
    if (posix_spawn(&child, PYTHON, NULL, NULL, argv, environ) != 0) { perror("spawn"); return 1; }
    int status;
    while (waitpid(child, &status, 0) < 0) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
