// Live-test target: globals, nested frames, a struct, a byte buffer,
// and an optional crash path for core-dump testing.
#include <cstdint>
#include <cstdio>
#include <cstring>

struct Header {
    uint32_t length;
    uint32_t magic;
};

int g_connection_count = 17;          // 0x11 once output-radix is 16
char g_hostname[32] = "prod-db-01";
static int g_retries = 3;
uint64_t g_table[4] = {0x1111111111111111ULL, 0x2222222222222222ULL,
                       0x3333333333333333ULL, 0x4444444444444444ULL};

int parse_header(const char *raw, int limit, Header *out) {
    out->length = (uint32_t)strlen(raw);
    out->magic = 0xDEADBEEF;
    int scratch[6];
    int total = 0;
    for (int i = 0; i < 6; i++) {
        scratch[i] = i * i;
        total += scratch[i];
    }
    if ((int)out->length > limit) {
        return -1;
    }
    return total;
}

int handle_packet(const char *payload, int limit) {
    Header h;
    printf("handling %zu bytes\n", strlen(payload));
    fflush(stdout);
    return parse_header(payload, limit, &h);
}

int main(int argc, char **argv) {
    printf("target starting\n");
    fflush(stdout);
    int total = handle_packet("hello world", 4096);
    printf("ok total=%d globals=%d %s retries=%d\n", total, g_connection_count,
           g_hostname, g_retries);
    fflush(stdout);

    if (argc > 1 && strcmp(argv[1], "crash") == 0) {
        Header *bad = nullptr;
        bad->length = 1;  // deliberate segfault
    }
    return 0;
}
