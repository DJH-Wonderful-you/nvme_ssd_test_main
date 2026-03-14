#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/nvme_ioctl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define NVME_ADMIN_FLUSH 0x00
#define NVME_ADMIN_GET_LOG_PAGE 0x02
#define NVME_ADMIN_IDENTIFY 0x06

#define IDENTIFY_DATA_SIZE 4096
#define SMART_LOG_SIZE 512
#define ERROR_LOG_ENTRY_SIZE 64

static uint16_t read_le16(const unsigned char *buf) {
    return (uint16_t)buf[0] | ((uint16_t)buf[1] << 8);
}

static uint32_t read_le32(const unsigned char *buf) {
    return (uint32_t)buf[0] |
           ((uint32_t)buf[1] << 8) |
           ((uint32_t)buf[2] << 16) |
           ((uint32_t)buf[3] << 24);
}

static uint64_t read_le64(const unsigned char *buf) {
    return (uint64_t)buf[0] |
           ((uint64_t)buf[1] << 8) |
           ((uint64_t)buf[2] << 16) |
           ((uint64_t)buf[3] << 24) |
           ((uint64_t)buf[4] << 32) |
           ((uint64_t)buf[5] << 40) |
           ((uint64_t)buf[6] << 48) |
           ((uint64_t)buf[7] << 56);
}

static uint64_t read_le128_low64(const unsigned char *buf) {
    /*
     * NVMe SMART 中很多累计计数是 128 bit。
     * 这里为了教学和常见实验场景，先读取低 64 bit，已经足够做观察和报告。
     */
    return read_le64(buf);
}

static void print_ascii_field(const char *label, const unsigned char *buf, size_t len) {
    char text[128];
    size_t i;

    if (len >= sizeof(text)) {
        len = sizeof(text) - 1;
    }
    memcpy(text, buf, len);
    text[len] = '\0';

    for (i = 0; i < len; ++i) {
        if (!isprint((unsigned char)text[i])) {
            text[i] = ' ';
        }
    }

    for (i = len; i > 0; --i) {
        if (text[i - 1] == ' ') {
            text[i - 1] = '\0';
        } else {
            break;
        }
    }

    printf("%s: %s\n", label, text);
}

static int submit_admin_cmd(
    int fd,
    uint8_t opcode,
    uint32_t nsid,
    uint32_t cdw10,
    uint32_t cdw11,
    void *data,
    uint32_t data_len
) {
    struct nvme_admin_cmd cmd;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = opcode;
    cmd.nsid = nsid;
    cmd.addr = (uint64_t)(uintptr_t)data;
    cmd.data_len = data_len;
    cmd.cdw10 = cdw10;
    cmd.cdw11 = cdw11;
    cmd.timeout_ms = 30000;

    if (ioctl(fd, NVME_IOCTL_ADMIN_CMD, &cmd) < 0) {
        fprintf(stderr, "ioctl NVME_IOCTL_ADMIN_CMD failed: %s\n", strerror(errno));
        return -1;
    }
    return 0;
}

static int do_identify_ctrl(int fd) {
    unsigned char *buf = calloc(1, IDENTIFY_DATA_SIZE);
    if (buf == NULL) {
        fprintf(stderr, "calloc failed\n");
        return 1;
    }

    if (submit_admin_cmd(fd, NVME_ADMIN_IDENTIFY, 0, 1, 0, buf, IDENTIFY_DATA_SIZE) != 0) {
        free(buf);
        return 1;
    }

    printf("Identify Controller\n");
    printf("===================\n");
    printf("VID: 0x%04x\n", read_le16(buf + 0));
    printf("SSVID: 0x%04x\n", read_le16(buf + 2));
    print_ascii_field("Serial Number", buf + 4, 20);
    print_ascii_field("Model Number", buf + 24, 40);
    print_ascii_field("Firmware Revision", buf + 64, 8);
    printf("Controller ID: %u\n", read_le16(buf + 78));
    printf("Version (raw): 0x%08x\n", read_le32(buf + 80));
    printf("IEEE OUI Identifier: %02x-%02x-%02x\n", buf[73], buf[74], buf[75]);

    free(buf);
    return 0;
}

static int do_identify_ns(int fd, uint32_t nsid) {
    unsigned char *buf = calloc(1, IDENTIFY_DATA_SIZE);
    uint8_t flbas;
    uint8_t lbaf_index;
    uint32_t lbaf_offset;
    uint16_t metadata_size;
    uint8_t lbads;

    if (buf == NULL) {
        fprintf(stderr, "calloc failed\n");
        return 1;
    }

    if (submit_admin_cmd(fd, NVME_ADMIN_IDENTIFY, nsid, 0, 0, buf, IDENTIFY_DATA_SIZE) != 0) {
        free(buf);
        return 1;
    }

    flbas = buf[26];
    lbaf_index = flbas & 0x0f;
    lbaf_offset = 128 + (uint32_t)lbaf_index * 4;
    metadata_size = read_le16(buf + lbaf_offset);
    lbads = buf[lbaf_offset + 2];

    printf("Identify Namespace\n");
    printf("==================\n");
    printf("Namespace ID: %u\n", nsid);
    printf("NSZE: %" PRIu64 "\n", read_le64(buf + 0));
    printf("NCAP: %" PRIu64 "\n", read_le64(buf + 8));
    printf("NUSE: %" PRIu64 "\n", read_le64(buf + 16));
    printf("NSFEAT: 0x%02x\n", buf[24]);
    printf("NLBAF: %u\n", buf[25]);
    printf("FLBAS: 0x%02x\n", flbas);
    printf("Selected LBA Format Index: %u\n", lbaf_index);
    printf("Selected LBA Data Size: %u bytes\n", 1U << lbads);
    printf("Selected Metadata Size: %u bytes\n", metadata_size);

    free(buf);
    return 0;
}

static int do_smart_log(int fd) {
    unsigned char *buf = calloc(1, SMART_LOG_SIZE);
    uint32_t numd;
    uint16_t temp_kelvin;
    int temp_celsius;

    if (buf == NULL) {
        fprintf(stderr, "calloc failed\n");
        return 1;
    }

    numd = (SMART_LOG_SIZE / 4) - 1;
    if (submit_admin_cmd(fd, NVME_ADMIN_GET_LOG_PAGE, 0xffffffffU, (numd << 16) | 0x02U, 0, buf, SMART_LOG_SIZE) != 0) {
        free(buf);
        return 1;
    }

    temp_kelvin = read_le16(buf + 1);
    temp_celsius = (int)temp_kelvin - 273;

    printf("SMART / Health Log\n");
    printf("==================\n");
    printf("Critical Warning: 0x%02x\n", buf[0]);
    printf("Temperature: %d C (%u K)\n", temp_celsius, temp_kelvin);
    printf("Available Spare: %u %%\n", buf[3]);
    printf("Available Spare Threshold: %u %%\n", buf[4]);
    printf("Percentage Used: %u %%\n", buf[5]);
    printf("Data Units Read (low64): %" PRIu64 "\n", read_le128_low64(buf + 32));
    printf("Data Units Written (low64): %" PRIu64 "\n", read_le128_low64(buf + 48));
    printf("Host Read Commands (low64): %" PRIu64 "\n", read_le128_low64(buf + 64));
    printf("Host Write Commands (low64): %" PRIu64 "\n", read_le128_low64(buf + 80));
    printf("Media Errors (low64): %" PRIu64 "\n", read_le128_low64(buf + 176));
    printf("Number of Error Log Entries (low64): %" PRIu64 "\n", read_le128_low64(buf + 192));

    free(buf);
    return 0;
}

static int do_error_log(int fd, uint32_t entries) {
    unsigned char *buf;
    uint32_t size;
    uint32_t numd;
    uint32_t i;

    if (entries == 0) {
        fprintf(stderr, "entries must be greater than 0\n");
        return 1;
    }

    size = entries * ERROR_LOG_ENTRY_SIZE;
    buf = calloc(1, size);
    if (buf == NULL) {
        fprintf(stderr, "calloc failed\n");
        return 1;
    }

    numd = (size / 4) - 1;
    if (submit_admin_cmd(fd, NVME_ADMIN_GET_LOG_PAGE, 0xffffffffU, (numd << 16) | 0x01U, 0, buf, size) != 0) {
        free(buf);
        return 1;
    }

    printf("Error Log\n");
    printf("=========\n");
    for (i = 0; i < entries; ++i) {
        const unsigned char *entry = buf + i * ERROR_LOG_ENTRY_SIZE;
        uint64_t error_count = read_le64(entry + 0);
        uint16_t sqid = read_le16(entry + 8);
        uint16_t cmdid = read_le16(entry + 10);
        uint16_t status = read_le16(entry + 12);
        uint64_t lba = read_le64(entry + 16);
        uint32_t nsid = read_le32(entry + 24);

        printf("Entry %u\n", i);
        printf("  Error Count: %" PRIu64 "\n", error_count);
        printf("  SQID: %u\n", sqid);
        printf("  CMDID: %u\n", cmdid);
        printf("  Status Field: 0x%04x\n", status);
        printf("  LBA: %" PRIu64 "\n", lba);
        printf("  Namespace ID: %u\n", nsid);
    }

    free(buf);
    return 0;
}

static int do_flush(int fd, uint32_t nsid) {
    if (submit_admin_cmd(fd, NVME_ADMIN_FLUSH, nsid, 0, 0, NULL, 0) != 0) {
        return 1;
    }
    printf("Flush command succeeded for namespace %u\n", nsid);
    return 0;
}

static void print_usage(const char *prog) {
    fprintf(stderr, "Usage:\n");
    fprintf(stderr, "  %s <controller> id-ctrl\n", prog);
    fprintf(stderr, "  %s <controller> id-ns <nsid>\n", prog);
    fprintf(stderr, "  %s <controller> smart-log\n", prog);
    fprintf(stderr, "  %s <controller> error-log <entries>\n", prog);
    fprintf(stderr, "  %s <controller> flush <nsid>\n", prog);
}

int main(int argc, char *argv[]) {
    int fd;
    const char *controller;
    const char *command;
    int rc = 1;

    if (argc < 3) {
        print_usage(argv[0]);
        return 1;
    }

    controller = argv[1];
    command = argv[2];

    fd = open(controller, O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "failed to open %s: %s\n", controller, strerror(errno));
        return 1;
    }

    if (strcmp(command, "id-ctrl") == 0) {
        rc = do_identify_ctrl(fd);
    } else if (strcmp(command, "id-ns") == 0) {
        if (argc < 4) {
            print_usage(argv[0]);
            goto out;
        }
        rc = do_identify_ns(fd, (uint32_t)strtoul(argv[3], NULL, 10));
    } else if (strcmp(command, "smart-log") == 0) {
        rc = do_smart_log(fd);
    } else if (strcmp(command, "error-log") == 0) {
        uint32_t entries = 1;
        if (argc >= 4) {
            entries = (uint32_t)strtoul(argv[3], NULL, 10);
        }
        rc = do_error_log(fd, entries);
    } else if (strcmp(command, "flush") == 0) {
        if (argc < 4) {
            print_usage(argv[0]);
            goto out;
        }
        rc = do_flush(fd, (uint32_t)strtoul(argv[3], NULL, 10));
    } else {
        print_usage(argv[0]);
    }

out:
    close(fd);
    return rc;
}
