#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define MiB (1024 * 1024ULL)

struct verify_result {
    uint64_t offset_bytes;
    uint64_t length_bytes;
    uint32_t block_size_bytes;
    uint32_t logical_block_size;
    uint32_t alignment_bytes;
    uint64_t seed;
    uint64_t expected_fnv1a;
    uint64_t actual_fnv1a;
    uint64_t mismatch_offset;
    int match;
};

static uint64_t next_pattern_u64(uint64_t *state) {
    /*
     * 使用简单且可复现的 xorshift64* 作为 pattern 生成器。
     * 对测试工具来说，它比 rand() 更稳定，也更适合跨平台重复验证。
     */
    *state ^= *state >> 12;
    *state ^= *state << 25;
    *state ^= *state >> 27;
    return *state * 2685821657736338717ULL;
}

static void fill_pattern(unsigned char *buf, size_t len, uint64_t *state) {
    size_t i = 0;
    while (i < len) {
        uint64_t value = next_pattern_u64(state);
        size_t copy_len = len - i < sizeof(value) ? len - i : sizeof(value);
        memcpy(buf + i, &value, copy_len);
        i += copy_len;
    }
}

static uint64_t fnv1a_update(uint64_t hash, const unsigned char *buf, size_t len) {
    size_t i;
    for (i = 0; i < len; ++i) {
        hash ^= (uint64_t)buf[i];
        hash *= 1099511628211ULL;
    }
    return hash;
}

static int write_full(int fd, const unsigned char *buf, size_t len, off_t offset) {
    size_t total = 0;
    while (total < len) {
        ssize_t written = pwrite(fd, buf + total, len - total, offset + (off_t)total);
        if (written < 0) {
            return -1;
        }
        if (written == 0) {
            errno = EIO;
            return -1;
        }
        total += (size_t)written;
    }
    return 0;
}

static int read_full(int fd, unsigned char *buf, size_t len, off_t offset) {
    size_t total = 0;
    while (total < len) {
        ssize_t read_bytes = pread(fd, buf + total, len - total, offset + (off_t)total);
        if (read_bytes < 0) {
            return -1;
        }
        if (read_bytes == 0) {
            errno = EIO;
            return -1;
        }
        total += (size_t)read_bytes;
    }
    return 0;
}

static int parse_u64(const char *text, uint64_t *value) {
    char *end = NULL;
    unsigned long long parsed = strtoull(text, &end, 10);
    if (text[0] == '\0' || (end != NULL && *end != '\0')) {
        return -1;
    }
    *value = (uint64_t)parsed;
    return 0;
}

static uint32_t max_u32(uint32_t a, uint32_t b) {
    return a > b ? a : b;
}

static int run_verify(
    const char *device,
    uint64_t offset_mb,
    uint64_t length_mb,
    uint32_t block_size_kb,
    uint64_t seed,
    struct verify_result *result
) {
    int fd = -1;
    int logical_block_size = 0;
    uint32_t block_size_bytes = block_size_kb * 1024U;
    uint32_t alignment_bytes;
    uint64_t offset_bytes = offset_mb * MiB;
    uint64_t length_bytes = length_mb * MiB;
    uint64_t expected_hash = 1469598103934665603ULL;
    uint64_t actual_hash = 1469598103934665603ULL;
    uint64_t expected_state = seed == 0 ? 1 : seed;
    uint64_t verify_state = seed == 0 ? 1 : seed;
    unsigned char *write_buf = NULL;
    unsigned char *read_buf = NULL;
    uint64_t processed = 0;

    if (block_size_bytes == 0 || length_bytes == 0) {
        fprintf(stderr, "block size and length must be greater than 0\n");
        return 1;
    }

    fd = open(device, O_DIRECT | O_RDWR | O_SYNC);
    if (fd < 0) {
        fprintf(stderr, "failed to open %s with O_DIRECT: %s\n", device, strerror(errno));
        return 1;
    }

    if (ioctl(fd, BLKSSZGET, &logical_block_size) < 0) {
        fprintf(stderr, "failed to query logical block size: %s\n", strerror(errno));
        close(fd);
        return 1;
    }

    alignment_bytes = max_u32((uint32_t)logical_block_size, 4096U);
    alignment_bytes = max_u32(alignment_bytes, block_size_bytes);

    if (length_bytes % (uint64_t)block_size_bytes != 0) {
        fprintf(stderr, "length must be a multiple of block_size_bytes\n");
        close(fd);
        return 1;
    }

    if (offset_bytes % (uint64_t)logical_block_size != 0 ||
        length_bytes % (uint64_t)logical_block_size != 0 ||
        block_size_bytes % (uint32_t)logical_block_size != 0) {
        fprintf(stderr, "offset/length/block_size must align to logical block size %d\n", logical_block_size);
        close(fd);
        return 1;
    }

    if (posix_memalign((void **)&write_buf, alignment_bytes, block_size_bytes) != 0 ||
        posix_memalign((void **)&read_buf, alignment_bytes, block_size_bytes) != 0) {
        fprintf(stderr, "posix_memalign failed\n");
        close(fd);
        free(write_buf);
        free(read_buf);
        return 1;
    }

    while (processed < length_bytes) {
        fill_pattern(write_buf, block_size_bytes, &expected_state);
        if (write_full(fd, write_buf, block_size_bytes, (off_t)(offset_bytes + processed)) != 0) {
            fprintf(stderr, "pwrite failed at offset %" PRIu64 ": %s\n", offset_bytes + processed, strerror(errno));
            goto fail;
        }
        expected_hash = fnv1a_update(expected_hash, write_buf, block_size_bytes);
        processed += block_size_bytes;
    }

    if (fsync(fd) != 0) {
        fprintf(stderr, "fsync failed: %s\n", strerror(errno));
        goto fail;
    }

    processed = 0;
    while (processed < length_bytes) {
        fill_pattern(write_buf, block_size_bytes, &verify_state);
        if (read_full(fd, read_buf, block_size_bytes, (off_t)(offset_bytes + processed)) != 0) {
            fprintf(stderr, "pread failed at offset %" PRIu64 ": %s\n", offset_bytes + processed, strerror(errno));
            goto fail;
        }
        actual_hash = fnv1a_update(actual_hash, read_buf, block_size_bytes);
        if (memcmp(write_buf, read_buf, block_size_bytes) != 0) {
            size_t i;
            uint64_t mismatch_offset = offset_bytes + processed;
            for (i = 0; i < block_size_bytes; ++i) {
                if (write_buf[i] != read_buf[i]) {
                    mismatch_offset += i;
                    break;
                }
            }
            result->offset_bytes = offset_bytes;
            result->length_bytes = length_bytes;
            result->block_size_bytes = block_size_bytes;
            result->logical_block_size = (uint32_t)logical_block_size;
            result->alignment_bytes = alignment_bytes;
            result->seed = seed;
            result->expected_fnv1a = expected_hash;
            result->actual_fnv1a = actual_hash;
            result->mismatch_offset = mismatch_offset;
            result->match = 0;
            free(write_buf);
            free(read_buf);
            close(fd);
            return 0;
        }
        processed += block_size_bytes;
    }

    result->offset_bytes = offset_bytes;
    result->length_bytes = length_bytes;
    result->block_size_bytes = block_size_bytes;
    result->logical_block_size = (uint32_t)logical_block_size;
    result->alignment_bytes = alignment_bytes;
    result->seed = seed;
    result->expected_fnv1a = expected_hash;
    result->actual_fnv1a = actual_hash;
    result->mismatch_offset = UINT64_MAX;
    result->match = 1;

    free(write_buf);
    free(read_buf);
    close(fd);
    return 0;

fail:
    free(write_buf);
    free(read_buf);
    close(fd);
    return 1;
}

static void print_usage(const char *prog) {
    fprintf(stderr, "Usage: %s <device> <offset_mb> <length_mb> <block_size_kb> <seed>\n", prog);
}

int main(int argc, char *argv[]) {
    uint64_t offset_mb;
    uint64_t length_mb;
    uint64_t seed;
    uint64_t block_size_kb_u64;
    uint32_t block_size_kb;
    struct verify_result result;

    if (argc != 6) {
        print_usage(argv[0]);
        return 1;
    }

    if (parse_u64(argv[2], &offset_mb) != 0 ||
        parse_u64(argv[3], &length_mb) != 0 ||
        parse_u64(argv[4], &block_size_kb_u64) != 0 ||
        parse_u64(argv[5], &seed) != 0) {
        fprintf(stderr, "invalid numeric argument\n");
        return 1;
    }

    if (block_size_kb_u64 > UINT32_MAX) {
        fprintf(stderr, "block_size_kb is too large\n");
        return 1;
    }
    block_size_kb = (uint32_t)block_size_kb_u64;

    if (run_verify(argv[1], offset_mb, length_mb, block_size_kb, seed, &result) != 0) {
        return 1;
    }

    printf("{\n");
    printf("  \"device\": \"%s\",\n", argv[1]);
    printf("  \"offset_mb\": %" PRIu64 ",\n", offset_mb);
    printf("  \"length_mb\": %" PRIu64 ",\n", length_mb);
    printf("  \"block_size_kb\": %u,\n", block_size_kb);
    printf("  \"logical_block_size\": %u,\n", result.logical_block_size);
    printf("  \"alignment_bytes\": %u,\n", result.alignment_bytes);
    printf("  \"seed\": %" PRIu64 ",\n", result.seed);
    printf("  \"expected_fnv1a\": \"0x%016" PRIx64 "\",\n", result.expected_fnv1a);
    printf("  \"actual_fnv1a\": \"0x%016" PRIx64 "\",\n", result.actual_fnv1a);
    if (result.match) {
        printf("  \"mismatch_offset\": null,\n");
    } else {
        printf("  \"mismatch_offset\": %" PRIu64 ",\n", result.mismatch_offset);
    }
    printf("  \"match\": %s\n", result.match ? "true" : "false");
    printf("}\n");
    return 0;
}
