#include <stdio.h>

static void process(void)
{
    int *data;
    data = NULL;
    printf("%d\n", *data);
}

int main(void)
{
    process();
    return 0;
}
