#include <stdio.h>

static void process(void)
{
    int *data;
    data = NULL;
    if (data != NULL)
    {
        printf("%d\n", *data);
    }
    else
    {
        puts("data is NULL");
    }
}

int main(void)
{
    process();
    return 0;
}
