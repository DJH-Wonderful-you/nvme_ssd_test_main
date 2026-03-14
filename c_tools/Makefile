CC := gcc
CFLAGS := -O2 -Wall -Wextra -std=c11
TARGETS := nvme_admin_tool nvme_odirect_verify

all: $(TARGETS)

nvme_admin_tool: nvme_admin_tool.c
	$(CC) $(CFLAGS) -o nvme_admin_tool nvme_admin_tool.c

nvme_odirect_verify: nvme_odirect_verify.c
	$(CC) $(CFLAGS) -o nvme_odirect_verify nvme_odirect_verify.c

clean:
	rm -f $(TARGETS)

.PHONY: all clean
