#define _GNU_SOURCE
#include <stdlib.h>
#include <sys/personality.h>
#include <unistd.h>

int main(int argc, char **argv, char **envp)
{
    char *args[] = {"/srv/app/run", NULL};

    (void)argc;
    (void)argv;
    (void)envp;
    if (personality(ADDR_NO_RANDOMIZE) < 0) {
        return 127;
    }
    if (setenv("LD_LIBRARY_PATH", "/srv/app:/usr/local/lib", 1) != 0) {
        return 127;
    }
    execve(args[0], args, environ);
    return 127;
}
