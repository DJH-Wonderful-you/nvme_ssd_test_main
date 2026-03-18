# `nvme_odirect_verify.c` 代码详解与 `nvme_ssd_test.py` 对照

## 1. 文件定位

- C 文件：`c_tools/nvme_odirect_verify.c`
- Python 文件：`src/nvme_ssd_test.py`
- Python 中最直接对应的函数：
  - `NvmeSsdTestProject.deterministic_rw_verify()`
  - `NvmeSsdTestProject.run_basic_write_verify()`
  - `NvmeSsdTestProject.run_c_odirect_verify()`

这个 C 文件是一个独立命令行工具，目标很明确：对块设备指定区域执行“写入固定 pattern -> 强制落盘 -> 读回校验 -> 输出 JSON 结果”的流程。

## 2. 整体实现思路

主流程可以概括成 10 步：

1. 从命令行读取设备、偏移、长度、块大小、随机种子。
2. 把 `MiB`、`KiB` 参数换算成字节。
3. 用 `O_DIRECT | O_RDWR | O_SYNC` 打开设备。
4. 通过 `BLKSSZGET` 查询逻辑块大小。
5. 检查偏移、长度、块大小是否满足对齐要求。
6. 用 `posix_memalign()` 分配满足 `O_DIRECT` 的对齐缓冲区。
7. 用固定种子生成可复现 pattern，逐块写入设备。
8. `fsync()` 强制落盘。
9. 用相同种子重新生成期望数据，逐块读回并比较。
10. 输出 JSON，包含哈希、是否匹配、首个错误偏移等信息。

关键特点：

- 可复现：固定种子下数据完全一致。
- 更底层：使用 `O_DIRECT`，尽量绕过页缓存。
- 可排查：不仅给出 `match`，还给出哈希和 `mismatch_offset`。

## 3. 数据结构

先看结果结构体：

```c
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
```

字段含义：

- `offset_bytes`：测试起始偏移，单位字节。
- `length_bytes`：测试长度，单位字节。
- `block_size_bytes`：单次 I/O 块大小。
- `logical_block_size`：设备逻辑块大小。
- `alignment_bytes`：`O_DIRECT` 所需的对齐字节数。
- `seed`：pattern 生成种子。
- `expected_fnv1a`：写入数据的期望哈希。
- `actual_fnv1a`：读回数据的实际哈希。
- `mismatch_offset`：首个错误字节的绝对偏移。
- `match`：是否完全一致。

## 4. 与 Python 实现的对应关系

### 4.1 C 与 Python 的角色分工

- C 的核心执行逻辑：`run_verify()`
- Python 的纯 Python 校验逻辑：`deterministic_rw_verify()`
- Python 的多区域调度逻辑：`run_basic_write_verify()`
- Python 对 C 工具的封装调用：`run_c_odirect_verify()`

### 4.2 相同点

- 都是“写入确定性数据，再读回校验”。
- 都使用固定种子保证 pattern 可复现。
- 都使用指定偏移的读写接口。
- 都在写完之后做 `fsync()`。
- 都返回结构化结果。

### 4.3 不同点

#### I/O 模式

C：

- `open(device, O_DIRECT | O_RDWR | O_SYNC)`
- 显式直接 I/O
- 必须处理内存和偏移对齐

Python：

- `os.open(..., os.O_RDWR | os.O_SYNC)`
- 没有使用 `O_DIRECT`
- 没有显式做逻辑块对齐检查

#### 随机数据生成

C：

- 用 `xorshift64*`
- 自己维护 `uint64_t state`

Python：

- 用 `random.Random(seed).randbytes(size)`

#### 哈希算法

C：

- 64 位 FNV-1a

Python：

- `sha256`

#### 错误定位精度

C：

- 精确到首个错误字节的绝对偏移

Python：

- 只返回出错块的相对起点 `mismatch_relative_offset`

#### 使用场景

C：

- 单次、低层、严格、接近设备语义

Python：

- 高层调度、报告生成、自动化测试框架集成

## 5. 逐函数解释

这一节采用“先贴代码，再讲功能和逐行说明”的方式。

### 5.1 `next_pattern_u64(uint64_t *state)`

```c
static uint64_t next_pattern_u64(uint64_t *state) {
    /*
     * 使用简单且可复现的 xorshift64* 作为 pattern 生成器。
     * 对测试工具来说，它比 rand() 更稳定，也更适合重复验证。
     */
    *state ^= *state >> 12;
    *state ^= *state << 25;
    *state ^= *state >> 27;
    return *state * 2685821657736338717ULL;
}
```

函数功能：

- 输入一个 64 位状态。
- 按 `xorshift64*` 推进状态。
- 返回新的 64 位伪随机值。

逐行解释：

- `static uint64_t next_pattern_u64(uint64_t *state)`：定义文件内可见的辅助函数，参数是状态指针。
- `*state ^= *state >> 12;`：第一次位扰动，右移后异或。
- `*state ^= *state << 25;`：第二次位扰动，左移后异或。
- `*state ^= *state >> 27;`：第三次位扰动，再次右移后异或。
- `return *state * 2685821657736338717ULL;`：乘以固定常数并返回结果。

### 5.2 `fill_pattern(unsigned char *buf, size_t len, uint64_t *state)`

```c
static void fill_pattern(unsigned char *buf, size_t len, uint64_t *state) {
    size_t i = 0;
    while (i < len) {
        uint64_t value = next_pattern_u64(state);
        size_t copy_len = len - i < sizeof(value) ? len - i : sizeof(value);
        memcpy(buf + i, &value, copy_len);
        i += copy_len;
    }
}
```

函数功能：

- 不断生成 64 位随机值。
- 将这些值按字节复制进缓冲区，直到填满。

逐行解释：

- `size_t i = 0;`：定义当前写入位置。
- `while (i < len)`：缓冲区没填满就继续。
- `uint64_t value = next_pattern_u64(state);`：生成一个新的 8 字节值。
- `size_t copy_len = ...`：如果剩余空间不足 8 字节，则只复制剩余长度。
- `memcpy(buf + i, &value, copy_len);`：把生成的数据写到缓冲区当前位置。
- `i += copy_len;`：推进写入下标。

### 5.3 `fnv1a_update(uint64_t hash, const unsigned char *buf, size_t len)`

```c
static uint64_t fnv1a_update(uint64_t hash, const unsigned char *buf, size_t len) {
    size_t i;
    for (i = 0; i < len; ++i) {
        hash ^= (uint64_t)buf[i];
        hash *= 1099511628211ULL;
    }
    return hash;
}
```

函数功能：

- 对一段缓冲区做 FNV-1a 哈希更新。

逐行解释：

- `size_t i;`：定义循环变量。
- `for (i = 0; i < len; ++i)`：逐字节处理。
- `hash ^= (uint64_t)buf[i];`：先异或当前字节。
- `hash *= 1099511628211ULL;`：再乘以 FNV-1a 固定质数。
- `return hash;`：返回更新后的哈希。

### 5.4 `write_full(int fd, const unsigned char *buf, size_t len, off_t offset)`

```c
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
```

函数功能：

- 从指定偏移写入完整的 `len` 字节。
- 一次没写完就继续写。

逐行解释：

- `size_t total = 0;`：记录已写入字节数。
- `while (total < len)`：还没写满就继续。
- `ssize_t written = pwrite(...)`：从 `offset + total` 写剩余数据。
- `if (written < 0)`：系统调用失败。
- `if (written == 0)`：意外写入 0 字节，按 I/O 错误处理。
- `errno = EIO;`：设置统一错误码。
- `total += (size_t)written;`：累加已写入长度。
- `return 0;`：全部写完返回成功。

### 5.5 `read_full(int fd, unsigned char *buf, size_t len, off_t offset)`

```c
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
```

函数功能：

- 从指定偏移读取完整的 `len` 字节。
- 一次没读满就继续读。

逐行解释：

- `size_t total = 0;`：记录已读取字节数。
- `while (total < len)`：只要没读满就继续。
- `ssize_t read_bytes = pread(...)`：从 `offset + total` 读取剩余数据。
- `if (read_bytes < 0)`：读取失败。
- `if (read_bytes == 0)`：意外读到 0 字节，按 I/O 错误处理。
- `errno = EIO;`：设置统一错误码。
- `total += (size_t)read_bytes;`：累加已读取长度。
- `return 0;`：全部读满返回成功。

### 5.6 `parse_u64(const char *text, uint64_t *value)`

```c
static int parse_u64(const char *text, uint64_t *value) {
    char *end = NULL;
    unsigned long long parsed = strtoull(text, &end, 10);
    if (text[0] == '\0' || (end != NULL && *end != '\0')) {
        return -1;
    }
    *value = (uint64_t)parsed;
    return 0;
}
```

函数功能：

- 把十进制字符串解析成 `uint64_t`。

逐行解释：

- `char *end = NULL;`：保存解析停止位置。
- `unsigned long long parsed = strtoull(...)`：执行十进制字符串转整数。
- `if (text[0] == '\0' || ...)`：空串或含非法尾随字符则失败。
- `*value = (uint64_t)parsed;`：写回解析结果。
- `return 0;`：返回成功。

### 5.7 `max_u32(uint32_t a, uint32_t b)`

```c
static uint32_t max_u32(uint32_t a, uint32_t b) {
    return a > b ? a : b;
}
```

函数功能：

- 返回两个 `uint32_t` 中较大的那个。

逐行解释：

- `return a > b ? a : b;`：没有副作用，只做单纯比较。

### 5.8 `run_verify(...)`

```c
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
```

函数功能：

- 这是整个程序的核心。
- 负责从参数转换到最终校验结果的完整闭环。

重点理解：

- `O_DIRECT` 需要严格的地址、长度、偏移对齐。
- 这里分别维护了两个状态：
  - `expected_state`：写入阶段使用
  - `verify_state`：验证阶段使用
- 两者从同一个 `seed` 出发，因此能在读回阶段重建完全相同的期望数据。

### 5.9 `print_usage(const char *prog)`

```c
static void print_usage(const char *prog) {
    fprintf(stderr, "Usage: %s <device> <offset_mb> <length_mb> <block_size_kb> <seed>\n", prog);
}
```

函数功能：

- 打印命令行用法。

### 5.10 `main(int argc, char *argv[])`

```c
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
```

函数功能：

- 接收命令行参数。
- 调用 `run_verify()`。
- 把结果格式化成 JSON 输出。

## 6. 逐段逐行解释

这一节按源码片段分段，每段前先贴代码，再做逐行解释。

### 6.1 第 1 行到第 29 行

```c
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
```

- 第 1 行：定义 `_GNU_SOURCE`，启用 GNU 扩展。
- 第 2 行：空行。
- 第 3 行：引入 `errno`、`EIO` 等错误相关定义。
- 第 4 行：引入 `open()` 和 `O_DIRECT`、`O_SYNC` 等标志。
- 第 5 行：引入跨平台整数打印宏。
- 第 6 行：引入块设备逻辑块大小查询常量 `BLKSSZGET`。
- 第 7 行：引入 `uint64_t`、`uint32_t` 等类型。
- 第 8 行：引入标准输入输出函数。
- 第 9 行：引入 `strtoull()`、`posix_memalign()`、`free()`。
- 第 10 行：引入 `memcpy()`、`memcmp()`、`strerror()`。
- 第 11 行：引入 `ioctl()`。
- 第 12 行：引入 `stat` 相关定义。
- 第 13 行：引入 `off_t`、`ssize_t` 等类型。
- 第 14 行：引入 `pread()`、`pwrite()`、`close()`、`fsync()`。
- 第 15 行：空行。
- 第 16 行：定义 `MiB`，后续统一用它做 MiB 到字节换算。
- 第 17 行：空行。
- 第 18 行：开始定义结果结构体。
- 第 19 行：记录测试起始偏移。
- 第 20 行：记录测试总长度。
- 第 21 行：记录单次 I/O 块大小。
- 第 22 行：记录逻辑块大小。
- 第 23 行：记录对齐要求。
- 第 24 行：记录种子。
- 第 25 行：记录期望哈希。
- 第 26 行：记录实际哈希。
- 第 27 行：记录首个错误字节偏移。
- 第 28 行：记录是否匹配。
- 第 29 行：结构体结束。

### 6.2 第 31 行到第 57 行

```c
static uint64_t next_pattern_u64(uint64_t *state) {
    /*
     * 使用简单且可复现的 xorshift64* 作为 pattern 生成器。
     * 对测试工具来说，它比 rand() 更稳定，也更适合重复验证。
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
```

- 第 31 行：定义随机值生成函数 `next_pattern_u64()`。
- 第 32 行到第 33 行：注释说明选用 `xorshift64*` 的原因。
- 第 34 行：第一次位运算扰动。
- 第 35 行：第二次位运算扰动。
- 第 36 行：第三次位运算扰动。
- 第 37 行：乘以固定常数并返回结果。
- 第 38 行：函数结束。
- 第 40 行：定义缓冲区填充函数 `fill_pattern()`。
- 第 41 行：从缓冲区起始位置开始填充。
- 第 42 行：缓冲区没满就继续。
- 第 43 行：获取一个新的 64 位随机值。
- 第 44 行：决定本次复制字节数。
- 第 45 行：把随机值复制到目标缓冲区。
- 第 46 行：更新下一个写入位置。
- 第 48 行：函数结束。
- 第 50 行：定义哈希更新函数 `fnv1a_update()`。
- 第 51 行：定义循环变量。
- 第 52 行：逐字节遍历缓冲区。
- 第 53 行：把当前字节异或进哈希。
- 第 54 行：再乘上固定质数。
- 第 56 行：返回更新后的哈希值。

### 6.3 第 59 行到第 103 行

```c
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
```

- 第 59 行：定义 `write_full()`。
- 第 60 行：初始化累计写入长度。
- 第 61 行：未写满时继续循环。
- 第 62 行：调用 `pwrite()` 写剩余数据。
- 第 63 行到第 65 行：写失败则返回 `-1`。
- 第 66 行到第 69 行：写 0 字节也视为 I/O 错误。
- 第 70 行：累计成功写入长度。
- 第 72 行：全部写完返回 0。
- 第 75 行：定义 `read_full()`。
- 第 76 行：初始化累计读取长度。
- 第 77 行：未读满时继续循环。
- 第 78 行：调用 `pread()` 读剩余数据。
- 第 79 行到第 81 行：读失败则返回 `-1`。
- 第 82 行到第 85 行：读 0 字节也视为 I/O 错误。
- 第 86 行：累计成功读取长度。
- 第 88 行：全部读满返回 0。
- 第 91 行：定义 `parse_u64()`。
- 第 92 行：保存解析结束位置。
- 第 93 行：执行十进制字符串转整数。
- 第 94 行：判定参数是否合法。
- 第 95 行：非法则返回 `-1`。
- 第 97 行：写回解析结果。
- 第 98 行：返回成功。
- 第 101 行：定义 `max_u32()`。
- 第 102 行：返回较大值。

### 6.4 第 105 行到第 240 行

```c
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
```

- 第 105 行到第 112 行：定义核心函数和参数列表。
- 第 113 行：文件描述符初始化为 `-1`。
- 第 114 行：定义逻辑块大小变量。
- 第 115 行：把块大小从 KiB 转成字节。
- 第 116 行：声明最终对齐值。
- 第 117 行：把偏移从 MiB 转成字节。
- 第 118 行：把长度从 MiB 转成字节。
- 第 119 行：初始化期望哈希。
- 第 120 行：初始化实际哈希。
- 第 121 行：初始化写入阶段的随机状态。
- 第 122 行：初始化验证阶段的随机状态。
- 第 123 行：写缓冲区置 `NULL`。
- 第 124 行：读缓冲区置 `NULL`。
- 第 125 行：`processed` 记录已处理字节数。
- 第 127 行到第 130 行：块大小或长度为 0 时直接报错。
- 第 132 行：以直接 I/O 方式打开设备。
- 第 133 行到第 136 行：打开失败时打印错误并返回。
- 第 138 行：查询逻辑块大小。
- 第 139 行到第 142 行：查询失败时关闭设备并返回。
- 第 144 行到第 145 行：计算 `alignment_bytes`。
- 第 147 行到第 151 行：要求测试长度必须是块大小整数倍。
- 第 153 行到第 159 行：检查 offset、length、block_size 是否按逻辑块对齐。
- 第 161 行到第 168 行：申请满足对齐要求的读写缓冲区。
- 第 170 行到第 178 行：写入阶段，逐块生成 pattern、写入并更新期望哈希。
- 第 180 行到第 183 行：执行 `fsync()`，确保数据落盘。
- 第 185 行：把已处理字节数清零，进入验证阶段。
- 第 186 行到第 218 行：逐块读回、生成期望数据并比较。
- 第 193 行：如果 `memcmp()` 失败，说明当前块有差异。
- 第 194 行到第 201 行：逐字节定位当前块中第一个错误位置。
- 第 202 行到第 215 行：把失败信息写入结果并返回。
- 第 220 行到第 229 行：全部通过时，写入成功结果。
- 第 231 行到第 234 行：成功路径下释放资源并返回。
- 第 236 行到第 240 行：统一失败清理路径。

### 6.5 第 243 行到第 296 行

```c
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
```

- 第 243 行到第 245 行：实现 `print_usage()`。
- 第 247 行：程序入口 `main()`。
- 第 248 行到第 253 行：声明用于接收参数和结果的变量。
- 第 255 行到第 258 行：参数数量错误时打印帮助。
- 第 260 行到第 266 行：解析数值参数。
- 第 268 行到第 272 行：检查 `block_size_kb` 是否溢出 `uint32_t`。
- 第 274 行到第 276 行：调用核心逻辑 `run_verify()`。
- 第 278 行到第 294 行：把结果打印成 JSON。
- 第 295 行：正常结束返回 0。

## 7. Python 对照代码

### 7.1 `deterministic_rw_verify()` 对照

```python
def deterministic_rw_verify(
    self,
    *,
    offset_bytes: int,
    length_bytes: int,
    chunk_bytes: int,
    seed: int,
) -> dict[str, Any]:
    if chunk_bytes <= 0 or length_bytes <= 0:
        raise ProjectError("length_bytes 和 chunk_bytes 必须大于 0")

    sha_written = hashlib.sha256()
    sha_read = hashlib.sha256()
    bytes_written = 0
    fd = os.open(str(self.device), os.O_RDWR | os.O_SYNC)
    try:
        write_rng = random.Random(seed)
        while bytes_written < length_bytes:
            current_size = min(chunk_bytes, length_bytes - bytes_written)
            current_offset = offset_bytes + bytes_written
            payload = write_rng.randbytes(current_size)
            os.pwrite(fd, payload, current_offset)
            sha_written.update(payload)
            bytes_written += current_size

        os.fsync(fd)

        verify_rng = random.Random(seed)
        bytes_verified = 0
        while bytes_verified < length_bytes:
            current_size = min(chunk_bytes, length_bytes - bytes_verified)
            current_offset = offset_bytes + bytes_verified
            expected = verify_rng.randbytes(current_size)
            actual = os.pread(fd, current_size, current_offset)
            sha_read.update(actual)
            if actual != expected:
                mismatch_index = bytes_verified
                return {
                    "match": False,
                    "mismatch_relative_offset": mismatch_index,
                    "expected_sha256": sha_written.hexdigest(),
                    "actual_sha256": sha_read.hexdigest(),
                }
            bytes_verified += current_size
    finally:
        os.close(fd)

    return {
        "match": True,
        "expected_sha256": sha_written.hexdigest(),
        "actual_sha256": sha_read.hexdigest(),
    }
```

对照说明：

- 这段 Python 逻辑和 `run_verify()` 的思想是一致的。
- 它也分成写入阶段和验证阶段，并且都用同一个 `seed` 重建数据。
- 但它没有 `O_DIRECT`，也没有对齐检查，属于高层版本。

### 7.2 `run_basic_write_verify()` 对照

```python
def run_basic_write_verify(self, device_size_bytes: int) -> TestResult:
    self.log("执行 Python 基础写入/回读校验测试")
    cfg = self.config["write_verify"]
    offset_list = cfg["offsets_mb"]
    length_mb = cfg["region_length_mb"]
    chunk_bytes = cfg["chunk_size_kb"] * 1024
    seed_base = cfg["seed_base"]

    regions: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, offset_mb in enumerate(offset_list):
        offset_bytes, length_bytes, used_fallback = self.resolve_region(
            preferred_offset_mb=offset_mb,
            length_mb=length_mb,
            slot_index=index,
            device_size_bytes=device_size_bytes,
        )
        seed = seed_base + index
        verify_result = self.deterministic_rw_verify(
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            chunk_bytes=chunk_bytes,
            seed=seed,
        )
```

对照说明：

- C 工具一次只校验一个区域。
- Python 这里负责“调度多个区域”。
- 所以它更像 `run_verify()` 的上层编排器，而不是一一对应的底层函数。

### 7.3 `run_c_odirect_verify()` 对照

```python
def run_c_odirect_verify(self, device_size_bytes: int) -> TestResult:
    self.log("执行 C 语言 O_DIRECT 校验测试")
    cfg = self.config["c_odirect_test"]
    binary = self.resolve_project_path(cfg["binary"])

    offset_bytes, length_bytes, used_fallback = self.resolve_region(
        preferred_offset_mb=cfg["offset_mb"],
        length_mb=cfg["length_mb"],
        slot_index=20,
        device_size_bytes=device_size_bytes,
    )
    actual_offset_mb = offset_bytes // MiB
    cmd = [
        str(binary),
        str(self.device),
        str(actual_offset_mb),
        str(length_bytes // MiB),
        str(cfg["block_size_kb"]),
        str(cfg["seed"]),
    ]
    result = self.run_command(
        cmd,
        check=False,
        artifact_name="c_odirect_verify.json",
    )
```

对照说明：

- 这段 Python 不再重复实现底层逻辑。
- 它只是把参数整理好，调用 C 二进制，再解析结果。
- 这说明项目里的定位是：
  - Python 负责编排
  - C 负责底层精确执行

## 8. 总结

如果你想高效学习这份代码，建议按下面顺序对照：

1. 先看本文件的 `main()`，理解输入输出。
2. 再看 `run_verify()`，掌握主流程。
3. 然后看 `fill_pattern()`、`write_full()`、`read_full()` 等辅助函数。
4. 最后对照 Python 的 `deterministic_rw_verify()` 和 `run_c_odirect_verify()`，理解“高层调度”和“底层执行”的分工。

这次文档已经改成“先附代码，再解释”的形式，更适合逐段对照学习。
