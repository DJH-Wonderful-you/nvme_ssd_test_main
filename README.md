# NVMe SSD 基础验证与自动化测试项目

这是一个面向存储测试岗位学习场景的实际项目，目标是围绕一块 NVMe SSD，完成一套可执行、可复盘、可写进简历的基础测试工程。

项目重点覆盖两类能力：

1. 使用 Python 开发自动化测试脚本，负责测试编排、结果采集、日志落盘和 Markdown 报告生成。
2. 使用 C 开发低层 NVMe Admin 命令工具和 `O_DIRECT` 数据校验工具，覆盖管理命令测试和裸盘直通 I/O 校验。

## 项目结构

```text
.
├── README.md
├── configs
│   └── default_test_plan.json
├── c_tools
│   ├── Makefile
│   ├── nvme_admin_tool.c
│   └── nvme_odirect_verify.c
├── docs
│   ├── NVMe基础与工具说明.md
│   ├── 测试执行指南.md
│   └── 测试项详解.md
├── reports
└── src
    └── nvme_ssd_test.py
```

## 已实现测试项

1. 设备识别与基础信息采集
2. Python 裸盘 pattern 写入/回读校验
3. Flush 测试
4. TRIM / Discard 测试
5. `fio` 顺序、随机、混合读写性能冒烟测试
6. SMART 前后对比检查
7. C 语言 NVMe Admin 命令测试
8. C 语言 `O_DIRECT` 裸盘写入/回读校验

## 运行前的关键提醒

1. 本项目包含破坏性测试，会直接写入和 discard 指定设备。
2. 你当前指定的测试盘是 `/dev/nvme1n1`，执行前必须再次确认它不是系统盘。
3. 推荐在 Ubuntu 下以 `root` 运行。

## 快速开始

### 1. 编译 C 工具

```bash
make -C c_tools
```

编译后会生成：

```text
c_tools/nvme_admin_tool
c_tools/nvme_odirect_verify
```

### 2. 执行 Python 主测试脚本

```bash
sudo python3 src/nvme_ssd_test.py \
  --device /dev/nvme1n1 \
  --yes-i-understand-this-will-destroy-data
```

### 3. 查看输出报告

每次执行都会在 `reports/run_时间戳/` 下生成：

1. `report.md`：人类可读的测试报告
2. `summary.json`：结构化结果汇总
3. `command_log.json`：所有命令执行记录
4. 各测试项对应的原始 JSON 输出

## 常用命令

只编译、不跑测试：

```bash
make -C c_tools
python3 -m py_compile src/nvme_ssd_test.py
```

跳过 `fio`：

```bash
sudo python3 src/nvme_ssd_test.py \
  --device /dev/nvme1n1 \
  --skip-fio \
  --yes-i-understand-this-will-destroy-data
```

跳过 C 工具测试：

```bash
sudo python3 src/nvme_ssd_test.py \
  --device /dev/nvme1n1 \
  --skip-c-tool \
  --yes-i-understand-this-will-destroy-data
```

## 建议阅读顺序

1. [测试执行指南](docs/测试执行指南.md)
2. [测试项详解](docs/测试项详解.md)
3. [NVMe基础与工具说明](docs/NVMe基础与工具说明.md)
