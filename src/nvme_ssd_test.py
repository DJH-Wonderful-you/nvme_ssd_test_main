#!/usr/bin/env python3
"""
NVMe SSD 基础验证与自动化测试脚本。

设计目标：
1. 用 Python 串起真实工作中常见的 SSD 测试流程。
2. 尽量依赖 Ubuntu 上常见工具：nvme-cli、fio、smartctl、lsblk、blkdiscard。
3. 所有测试结果落盘，便于后续写简历、整理测试报告、复盘问题。

注意：
1. 本脚本包含破坏性测试，会直接对目标裸盘写入、校验、discard。
2. 运行前请务必确认目标盘不是系统盘，也没有重要数据。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import random
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import Any


MiB = 1024 * 1024
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


class ProjectError(RuntimeError):
    """项目内的可预期错误。"""


@dataclasses.dataclass
class CommandResult:
    """保存命令执行结果，便于后续写入报告。"""

    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float


@dataclasses.dataclass
class TestResult:
    """统一的测试结果结构。"""

    name: str
    status: str
    summary: str
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)
    artifacts: list[str] = dataclasses.field(default_factory=list)


class NvmeSsdTestProject:
    """NVMe SSD 自动化测试主流程。"""

    def __init__(
        self,
        device: str,
        controller: str | None,
        config: dict[str, Any],
        report_root: pathlib.Path,
        destructive_confirmed: bool,
        run_fio: bool,
        run_c_tool: bool,
    ) -> None:
        self.device = pathlib.Path(device)
        self.controller = pathlib.Path(controller) if controller else pathlib.Path(self.derive_controller(device))
        self.namespace_id = self.derive_namespace_id(device)
        self.config = config
        self.report_root = report_root
        self.destructive_confirmed = destructive_confirmed
        self.run_fio_enabled = run_fio
        self.run_c_tool_enabled = run_c_tool
        self.command_log: list[dict[str, Any]] = []
        self.results: list[TestResult] = []

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.report_dir = self.report_root / f"run_{timestamp}"
        self.report_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def derive_controller(device: str) -> str:
        """把 /dev/nvme1n1 转成控制器节点 /dev/nvme1。"""
        match = re.match(r"^(/dev/nvme\d+)n\d+(?:p\d+)?$", device)
        if not match:
            raise ProjectError(f"无法从设备路径推导控制器路径: {device}")
        return match.group(1)

    @staticmethod
    def derive_namespace_id(device: str) -> int:
        """从 /dev/nvme1n1 中提取 namespace id。"""
        match = re.search(r"n(\d+)(?:p\d+)?$", device)
        if not match:
            raise ProjectError(f"无法从设备路径推导 namespace id: {device}")
        return int(match.group(1))

    def log(self, message: str) -> None:
        """同时打印时间戳，方便终端观察。"""
        now = dt.datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] {message}")

    def save_text(self, name: str, content: str) -> pathlib.Path:
        path = self.report_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def save_json(self, name: str, payload: Any) -> pathlib.Path:
        path = self.report_dir / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def resolve_project_path(self, path_str: str) -> pathlib.Path:
        path = pathlib.Path(path_str)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    def run_command(
        self,
        cmd: list[str],
        *,
        check: bool = False,
        artifact_name: str | None = None,
    ) -> CommandResult:
        """统一执行外部命令，并把 stdout/stderr 落到报告目录。"""
        start = time.perf_counter()
        process = subprocess.run(cmd, text=True, capture_output=True)
        duration = time.perf_counter() - start
        result = CommandResult(
            cmd=cmd,
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            duration_sec=duration,
        )
        self.command_log.append(
            {
                "cmd": cmd,
                "returncode": result.returncode,
                "duration_sec": round(duration, 3),
                "stdout_preview": result.stdout[:4000],
                "stderr_preview": result.stderr[:4000],
            }
        )
        if artifact_name:
            artifact = {
                "cmd": " ".join(shlex.quote(part) for part in cmd),
                "returncode": result.returncode,
                "duration_sec": round(duration, 3),
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            self.save_json(artifact_name, artifact)
        if check and result.returncode != 0:
            raise ProjectError(
                f"命令执行失败: {' '.join(cmd)}\n"
                f"returncode={result.returncode}\n"
                f"stderr={result.stderr.strip()}"
            )
        return result

    def run_json_command(
        self,
        cmd: list[str],
        *,
        artifact_name: str,
        required: bool = False,
    ) -> dict[str, Any] | None:
        """执行输出 JSON 的命令，并进行解析。"""
        result = self.run_command(cmd, check=False, artifact_name=artifact_name)
        if result.returncode != 0:
            if required:
                raise ProjectError(f"命令失败: {' '.join(cmd)}")
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            if required:
                raise ProjectError(f"命令输出不是合法 JSON: {' '.join(cmd)}")
            return None

    def parse_json_text(self, content: str, context: str) -> dict[str, Any]:
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProjectError(f"{context} 输出不是合法 JSON: {exc}") from exc

    def ensure_root(self) -> None:
        """裸盘测试通常需要 root 权限。"""
        if os.geteuid() != 0:
            raise ProjectError("请使用 root 运行，例如: sudo python3 src/nvme_ssd_test.py ...")

    def ensure_commands(self) -> None:
        """检查依赖工具是否齐全。"""
        required = ["lsblk", "nvme", "smartctl", "fio", "blkdiscard", "blockdev"]
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise ProjectError(f"缺少依赖工具: {', '.join(missing)}")

    def validate_device(self) -> None:
        """确认目标盘确实存在，并且是块设备。"""
        if not self.destructive_confirmed:
            raise ProjectError("必须显式传入 --yes-i-understand-this-will-destroy-data 才会执行。")
        if not self.device.exists():
            raise ProjectError(f"目标设备不存在: {self.device}")
        mode = self.device.stat().st_mode
        if not stat.S_ISBLK(mode):
            raise ProjectError(f"目标路径不是块设备: {self.device}")
        if not self.controller.exists():
            raise ProjectError(f"控制器节点不存在: {self.controller}")

    def get_device_size_bytes(self) -> int:
        result = self.run_command(
            ["blockdev", "--getsize64", str(self.device)],
            check=True,
            artifact_name="device_size.json",
        )
        return int(result.stdout.strip())

    def resolve_region(
        self,
        *,
        preferred_offset_mb: int,
        length_mb: int,
        slot_index: int,
        device_size_bytes: int,
    ) -> tuple[int, int, bool]:
        """
        解析测试区域。

        如果配置文件给出的 offset 超出盘容量，就自动把区域挪到盘内可用范围，
        避免因为不同容量 SSD 导致脚本完全不可用。
        """
        length_bytes = length_mb * MiB
        if length_bytes <= 0:
            raise ProjectError("测试区域长度必须大于 0")
        if device_size_bytes <= length_bytes + 8 * MiB:
            raise ProjectError("目标盘容量太小，无法执行当前测试配置。")

        preferred_offset = preferred_offset_mb * MiB
        if preferred_offset + length_bytes <= device_size_bytes:
            return preferred_offset, length_bytes, False

        usable_bytes = device_size_bytes - length_bytes - 8 * MiB
        ratio = min(0.15 + slot_index * 0.18, 0.82)
        fallback_offset = int(usable_bytes * ratio)
        fallback_offset = (fallback_offset // MiB) * MiB
        return fallback_offset, length_bytes, True

    def collect_snapshot(self, tag: str) -> dict[str, Any]:
        """采集测试前后的关键设备信息。"""
        self.log(f"采集 {tag} 设备信息")
        snapshot: dict[str, Any] = {}
        snapshot["lsblk"] = self.run_json_command(
            ["lsblk", "-J", "-O", str(self.device)],
            artifact_name=f"{tag}_lsblk.json",
        )
        snapshot["nvme_id_ctrl"] = self.run_json_command(
            ["nvme", "id-ctrl", str(self.controller), "-o", "json"],
            artifact_name=f"{tag}_nvme_id_ctrl.json",
        )
        snapshot["nvme_id_ns"] = self.run_json_command(
            ["nvme", "id-ns", str(self.device), "-o", "json"],
            artifact_name=f"{tag}_nvme_id_ns.json",
        )
        snapshot["nvme_smart_log"] = self.run_json_command(
            ["nvme", "smart-log", str(self.device), "-o", "json"],
            artifact_name=f"{tag}_nvme_smart_log.json",
        )
        snapshot["smartctl"] = self.run_json_command(
            ["smartctl", "-a", "-j", str(self.device)],
            artifact_name=f"{tag}_smartctl.json",
        )
        return snapshot

    def extract_smart_summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """
        smartctl 的 JSON 结构比较复杂，只提取最适合写报告的几个关键项。
        """
        smartctl = snapshot.get("smartctl") or {}
        nvme_info = smartctl.get("nvme_smart_health_information_log") or {}
        return {
            "critical_warning": nvme_info.get("critical_warning"),
            "temperature_celsius": nvme_info.get("temperature"),
            "available_spare": nvme_info.get("available_spare"),
            "available_spare_threshold": nvme_info.get("available_spare_threshold"),
            "percentage_used": nvme_info.get("percentage_used"),
            "data_units_read": nvme_info.get("data_units_read"),
            "data_units_written": nvme_info.get("data_units_written"),
            "media_errors": nvme_info.get("media_errors"),
            "num_err_log_entries": nvme_info.get("num_err_log_entries"),
        }

    def compare_smart(self, before: dict[str, Any], after: dict[str, Any]) -> TestResult:
        before_summary = self.extract_smart_summary(before)
        after_summary = self.extract_smart_summary(after)
        warnings: list[str] = []
        status = "PASS"

        if before_summary.get("critical_warning") not in (None, 0):
            warnings.append("测试前 critical_warning 非 0，说明盘本身可能已经处于异常或警告状态。")
            status = "WARN"
        if after_summary.get("critical_warning") not in (None, 0):
            warnings.append("测试后 critical_warning 非 0，需要重点检查 SMART 日志。")
            status = "FAIL"

        before_media = before_summary.get("media_errors")
        after_media = after_summary.get("media_errors")
        if isinstance(before_media, int) and isinstance(after_media, int) and after_media > before_media:
            warnings.append(f"media_errors 从 {before_media} 增加到 {after_media}。")
            status = "FAIL"

        before_err = before_summary.get("num_err_log_entries")
        after_err = after_summary.get("num_err_log_entries")
        if isinstance(before_err, int) and isinstance(after_err, int) and after_err > before_err and status != "FAIL":
            warnings.append(f"error log entries 从 {before_err} 增加到 {after_err}。")
            status = "WARN"

        return TestResult(
            name="SMART 对比检查",
            status=status,
            summary="比对测试前后 SMART 关键字段，判断是否出现新的健康风险。",
            details={"before": before_summary, "after": after_summary},
            warnings=warnings,
            artifacts=["before_smartctl.json", "after_smartctl.json"],
        )

    def deterministic_rw_verify(
        self,
        *,
        offset_bytes: int,
        length_bytes: int,
        chunk_bytes: int,
        seed: int,
    ) -> dict[str, Any]:
        """
        使用固定随机种子生成可复现的数据模式。

        为什么不用全 0x00 或 0xFF？
        因为固定模式更容易命中真实工作中的一致性问题，也更像测试中的 pattern data。
        """
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
            region_info = {
                "requested_offset_mb": offset_mb,
                "actual_offset_mb": offset_bytes // MiB,
                "length_mb": length_bytes // MiB,
                "seed": seed,
                "verify_result": verify_result,
            }
            if used_fallback:
                warnings.append(
                    f"第 {index + 1} 个测试区原始 offset={offset_mb}MiB 超出盘容量，已自动调整为 {offset_bytes // MiB}MiB。"
                )
            regions.append(region_info)
            if not verify_result["match"]:
                return TestResult(
                    name="Python 基础写入/回读校验",
                    status="FAIL",
                    summary=f"第 {index + 1} 个测试区出现数据不一致。",
                    details={"regions": regions},
                    warnings=warnings,
                )

        return TestResult(
            name="Python 基础写入/回读校验",
            status="PASS",
            summary="多个测试区完成 pattern 写入与回读校验，未发现数据不一致。",
            details={"regions": regions},
            warnings=warnings,
        )

    def run_flush_test(self, device_size_bytes: int) -> TestResult:
        self.log("执行 Flush 测试")
        cfg = self.config["flush_test"]
        offset_bytes, length_bytes, used_fallback = self.resolve_region(
            preferred_offset_mb=cfg["offset_mb"],
            length_mb=cfg["length_mb"],
            slot_index=5,
            device_size_bytes=device_size_bytes,
        )
        verify_result = self.deterministic_rw_verify(
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            chunk_bytes=cfg["chunk_size_kb"] * 1024,
            seed=cfg["seed"],
        )
        if not verify_result["match"]:
            return TestResult(
                name="Flush 测试",
                status="FAIL",
                summary="写入后回读校验失败，无法继续判断 flush 行为。",
                details={"verify_result": verify_result},
            )

        flush_result = self.run_command(
            ["nvme", "flush", str(self.device), "-n", str(self.namespace_id)],
            check=False,
            artifact_name="flush_command.json",
        )
        warnings: list[str] = []
        if used_fallback:
            warnings.append(f"Flush 测试区域已自动调整到 {offset_bytes // MiB}MiB。")
        if flush_result.returncode != 0:
            return TestResult(
                name="Flush 测试",
                status="FAIL",
                summary="NVMe Flush 命令执行失败。",
                details={
                    "offset_mb": offset_bytes // MiB,
                    "length_mb": length_bytes // MiB,
                    "stderr": flush_result.stderr.strip(),
                },
                warnings=warnings,
                artifacts=["flush_command.json"],
            )

        reopen_fd = os.open(str(self.device), os.O_RDONLY)
        try:
            sample = os.pread(reopen_fd, min(4096, length_bytes), offset_bytes)
        finally:
            os.close(reopen_fd)

        return TestResult(
            name="Flush 测试",
            status="PASS",
            summary="写入校验通过且 NVMe Flush 命令返回成功。",
            details={
                "offset_mb": offset_bytes // MiB,
                "length_mb": length_bytes // MiB,
                "sample_hex_after_flush": sample[:64].hex(),
            },
            warnings=warnings,
            artifacts=["flush_command.json"],
        )

    def run_trim_test(self, device_size_bytes: int) -> TestResult:
        self.log("执行 TRIM/Discard 测试")
        cfg = self.config["trim_test"]
        offset_bytes, length_bytes, used_fallback = self.resolve_region(
            preferred_offset_mb=cfg["offset_mb"],
            length_mb=cfg["length_mb"],
            slot_index=6,
            device_size_bytes=device_size_bytes,
        )
        chunk_bytes = cfg["chunk_size_kb"] * 1024
        before_discard = self.deterministic_rw_verify(
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            chunk_bytes=chunk_bytes,
            seed=cfg["write_seed"],
        )
        if not before_discard["match"]:
            return TestResult(
                name="TRIM/Discard 测试",
                status="FAIL",
                summary="Discard 前的写入校验失败，无法继续。",
                details={"before_discard": before_discard},
            )

        discard_result = self.run_command(
            [
                "blkdiscard",
                "-o",
                str(offset_bytes),
                "-l",
                str(length_bytes),
                str(self.device),
            ],
            check=False,
            artifact_name="trim_blkdiscard.json",
        )
        warnings: list[str] = []
        if used_fallback:
            warnings.append(f"TRIM 测试区域已自动调整到 {offset_bytes // MiB}MiB。")
        if discard_result.returncode != 0:
            return TestResult(
                name="TRIM/Discard 测试",
                status="FAIL",
                summary="blkdiscard 执行失败。",
                details={"stderr": discard_result.stderr.strip()},
                warnings=warnings,
                artifacts=["trim_blkdiscard.json"],
            )

        fd = os.open(str(self.device), os.O_RDONLY)
        try:
            post_discard_sample = os.pread(fd, min(4096, length_bytes), offset_bytes)
        finally:
            os.close(fd)

        rewrite_result = self.deterministic_rw_verify(
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            chunk_bytes=chunk_bytes,
            seed=cfg["rewrite_seed"],
        )
        if not rewrite_result["match"]:
            return TestResult(
                name="TRIM/Discard 测试",
                status="FAIL",
                summary="Discard 后重新写入并校验失败。",
                details={
                    "offset_mb": offset_bytes // MiB,
                    "length_mb": length_bytes // MiB,
                    "rewrite_result": rewrite_result,
                },
                warnings=warnings,
                artifacts=["trim_blkdiscard.json"],
            )

        warnings.append("注意：NVMe/SSD 在 discard 后读回全 0、全 1 或其他值都可能是合法行为，不能把“是否为全 0”作为唯一判定标准。")
        return TestResult(
            name="TRIM/Discard 测试",
            status="PASS",
            summary="blkdiscard 成功，且 discard 后重写与回读校验通过。",
            details={
                "offset_mb": offset_bytes // MiB,
                "length_mb": length_bytes // MiB,
                "post_discard_sample_hex": post_discard_sample[:64].hex(),
                "rewrite_result": rewrite_result,
            },
            warnings=warnings,
            artifacts=["trim_blkdiscard.json"],
        )

    def parse_fio_metrics(self, payload: dict[str, Any], job_name: str) -> dict[str, Any]:
        jobs = payload.get("jobs") or []
        if not jobs:
            raise ProjectError(f"fio 结果中缺少 jobs 字段: {job_name}")
        job = jobs[0]
        read_metrics = job.get("read") or {}
        write_metrics = job.get("write") or {}

        def extract(direction: dict[str, Any]) -> dict[str, Any]:
            latency = direction.get("clat_ns") or direction.get("lat_ns") or {}
            return {
                "io_bytes": direction.get("io_bytes"),
                "iops": direction.get("iops"),
                "bandwidth_bytes_per_sec": direction.get("bw_bytes"),
                "mean_latency_ns": latency.get("mean"),
                "p99_latency_ns": (latency.get("percentile") or {}).get("99.000000"),
            }

        is_mixed = bool(read_metrics.get("io_bytes", 0) and write_metrics.get("io_bytes", 0))
        if is_mixed:
            return {
                "mode": "mixed",
                "read": extract(read_metrics),
                "write": extract(write_metrics),
            }

        target = write_metrics if write_metrics.get("io_bytes", 0) else read_metrics
        return {
            "mode": "single_direction",
            "active_direction": "write" if write_metrics.get("io_bytes", 0) else "read",
            "metrics": extract(target),
        }

    def build_fio_command(self, job_cfg: dict[str, Any], *, offset_bytes: int, length_bytes: int) -> list[str]:
        cmd = [
            "fio",
            f"--name={job_cfg['name']}",
            f"--filename={self.device}",
            f"--rw={job_cfg['rw']}",
            f"--bs={job_cfg['bs']}",
            f"--iodepth={job_cfg['iodepth']}",
            f"--numjobs={job_cfg['numjobs']}",
            "--ioengine=libaio",
            "--direct=1",
            "--thread=1",
            "--group_reporting=1",
            f"--size={length_bytes}",
            f"--offset={offset_bytes}",
            "--output-format=json",
        ]
        optional_fields = {
            "rwmixread": "--rwmixread",
            "rwmixwrite": "--rwmixwrite",
            "runtime_sec": "--runtime",
            "ramp_time_sec": "--ramp_time",
        }
        for key, option in optional_fields.items():
            value = job_cfg.get(key)
            if value is not None:
                cmd.append(f"{option}={value}")
        if job_cfg.get("time_based"):
            cmd.append("--time_based=1")
        return cmd

    def run_fio_smoke(self, device_size_bytes: int) -> TestResult:
        self.log("执行 fio 性能冒烟测试")
        jobs_cfg = self.config["fio_jobs"]
        job_results: list[dict[str, Any]] = []
        warnings: list[str] = []

        for index, job_cfg in enumerate(jobs_cfg):
            offset_bytes, length_bytes, used_fallback = self.resolve_region(
                preferred_offset_mb=job_cfg["offset_mb"],
                length_mb=job_cfg["size_mb"],
                slot_index=10 + index,
                device_size_bytes=device_size_bytes,
            )
            cmd = self.build_fio_command(job_cfg, offset_bytes=offset_bytes, length_bytes=length_bytes)
            result = self.run_command(
                cmd,
                check=False,
                artifact_name=f"fio_{job_cfg['name']}.json",
            )
            if used_fallback:
                warnings.append(
                    f"fio 任务 {job_cfg['name']} 的测试区域已自动调整到 {offset_bytes // MiB}MiB。"
                )
            if result.returncode != 0:
                return TestResult(
                    name="fio 性能冒烟测试",
                    status="FAIL",
                    summary=f"fio 任务 {job_cfg['name']} 执行失败。",
                    details={"stderr": result.stderr.strip(), "job": job_cfg},
                    warnings=warnings,
                    artifacts=[f"fio_{job_cfg['name']}.json"],
                )
            payload = json.loads(result.stdout)
            metrics = self.parse_fio_metrics(payload, job_cfg["name"])
            job_results.append(
                {
                    "name": job_cfg["name"],
                    "rw": job_cfg["rw"],
                    "bs": job_cfg["bs"],
                    "rwmixread": job_cfg.get("rwmixread"),
                    "offset_mb": offset_bytes // MiB,
                    "size_mb": length_bytes // MiB,
                    "metrics": metrics,
                }
            )

        return TestResult(
            name="fio 性能冒烟测试",
            status="PASS",
            summary="顺序、随机以及混合读写基础性能测试执行完成。",
            details={"jobs": job_results},
            warnings=warnings,
            artifacts=[f"fio_{job['name']}.json" for job in jobs_cfg],
        )

    def run_c_admin_tool(self) -> TestResult:
        self.log("执行 C 语言 NVMe Admin 命令测试")
        binary = self.resolve_project_path(self.config["c_tool"]["binary"])
        if not binary.exists():
            return TestResult(
                name="C 语言 NVMe Admin 命令测试",
                status="SKIP",
                summary="未找到已编译的 C 工具，跳过。",
                warnings=[f"请先执行 make -C c_tools，期望生成文件: {binary}"],
            )

        commands = [
            [str(binary), str(self.controller), "id-ctrl"],
            [str(binary), str(self.controller), "id-ns", str(self.namespace_id)],
            [str(binary), str(self.controller), "smart-log"],
            [str(binary), str(self.controller), "error-log", "1"],
        ]
        outputs: dict[str, str] = {}

        for index, cmd in enumerate(commands):
            result = self.run_command(
                cmd,
                check=False,
                artifact_name=f"c_tool_{index + 1}.json",
            )
            if result.returncode != 0:
                return TestResult(
                    name="C 语言 NVMe Admin 命令测试",
                    status="FAIL",
                    summary=f"C 工具命令失败: {' '.join(cmd)}",
                    details={"stderr": result.stderr.strip()},
                    artifacts=[f"c_tool_{index + 1}.json"],
                )
            outputs[" ".join(cmd[2:])] = result.stdout.strip()

        return TestResult(
            name="C 语言 NVMe Admin 命令测试",
            status="PASS",
            summary="通过 ioctl 成功执行 Identify、SMART Log、Error Log 查询。",
            details={"outputs": outputs},
            artifacts=[f"c_tool_{index + 1}.json" for index in range(len(commands))],
        )

    def run_c_odirect_verify(self, device_size_bytes: int) -> TestResult:
        self.log("执行 C 语言 O_DIRECT 校验测试")
        cfg = self.config["c_odirect_test"]
        binary = self.resolve_project_path(cfg["binary"])
        if not binary.exists():
            return TestResult(
                name="C 语言 O_DIRECT 校验测试",
                status="SKIP",
                summary="未找到已编译的 O_DIRECT 校验工具，跳过。",
                warnings=[f"请先执行 make -C c_tools，期望生成文件: {binary}"],
            )

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
        warnings: list[str] = []
        if used_fallback:
            warnings.append(f"C O_DIRECT 测试区域已自动调整到 {actual_offset_mb}MiB。")
        if result.returncode != 0:
            return TestResult(
                name="C 语言 O_DIRECT 校验测试",
                status="FAIL",
                summary="O_DIRECT 校验工具返回失败。",
                details={
                    "offset_mb": actual_offset_mb,
                    "length_mb": length_bytes // MiB,
                    "stderr": result.stderr.strip(),
                    "stdout": result.stdout.strip(),
                },
                warnings=warnings,
                artifacts=["c_odirect_verify.json"],
            )

        payload = self.parse_json_text(result.stdout, "C O_DIRECT 校验工具")
        return TestResult(
            name="C 语言 O_DIRECT 校验测试",
            status="PASS" if payload.get("match") else "FAIL",
            summary="使用 C 和 O_DIRECT 完成裸盘写入/回读校验。",
            details=payload,
            warnings=warnings,
            artifacts=["c_odirect_verify.json"],
        )

    def generate_markdown_report(
        self,
        *,
        device_size_bytes: int,
        before_snapshot: dict[str, Any],
        after_snapshot: dict[str, Any],
    ) -> pathlib.Path:
        overall_status = "PASS"
        if any(item.status == "FAIL" for item in self.results):
            overall_status = "FAIL"
        elif any(item.status == "WARN" for item in self.results):
            overall_status = "WARN"

        lines: list[str] = []
        lines.append("# NVMe SSD 自动化测试报告")
        lines.append("")
        lines.append(f"- 测试时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 目标设备: `{self.device}`")
        lines.append(f"- 控制器节点: `{self.controller}`")
        lines.append(f"- Namespace ID: `{self.namespace_id}`")
        lines.append(f"- 设备容量: `{device_size_bytes / (1024 ** 3):.2f} GiB`")
        lines.append(f"- 总体结论: `{overall_status}`")
        lines.append("")
        lines.append("## 测试前 SMART 摘要")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(self.extract_smart_summary(before_snapshot), indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
        lines.append("## 测试结果")
        lines.append("")

        for result in self.results:
            lines.append(f"### {result.name}")
            lines.append("")
            lines.append(f"- 状态: `{result.status}`")
            lines.append(f"- 结论: {result.summary}")
            if result.warnings:
                lines.append("- 告警/说明:")
                for warning in result.warnings:
                    lines.append(f"  - {warning}")
            lines.append("- 详细信息:")
            lines.append("```json")
            lines.append(json.dumps(result.details, indent=2, ensure_ascii=False))
            lines.append("```")
            if result.artifacts:
                lines.append("- 产物文件:")
                for artifact in result.artifacts:
                    lines.append(f"  - `{artifact}`")
            lines.append("")

        lines.append("## 测试后 SMART 摘要")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(self.extract_smart_summary(after_snapshot), indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
        lines.append("## 命令执行记录")
        lines.append("")
        lines.append("详情见 `command_log.json`。")
        lines.append("")

        return self.save_text("report.md", "\n".join(lines))

    def run(self) -> int:
        self.ensure_root()
        self.ensure_commands()
        self.validate_device()

        device_size_bytes = self.get_device_size_bytes()
        before_snapshot = self.collect_snapshot("before")

        self.results.append(self.run_basic_write_verify(device_size_bytes))
        self.results.append(self.run_flush_test(device_size_bytes))
        self.results.append(self.run_trim_test(device_size_bytes))

        if self.run_fio_enabled:
            self.results.append(self.run_fio_smoke(device_size_bytes))
        else:
            self.results.append(
                TestResult(
                    name="fio 性能冒烟测试",
                    status="SKIP",
                    summary="用户通过参数关闭了 fio 性能测试。",
                )
            )

        if self.run_c_tool_enabled:
            self.results.append(self.run_c_admin_tool())
            self.results.append(self.run_c_odirect_verify(device_size_bytes))
        else:
            self.results.append(
                TestResult(
                    name="C 语言 NVMe Admin 命令测试",
                    status="SKIP",
                    summary="用户通过参数关闭了 C 工具测试。",
                )
            )
            self.results.append(
                TestResult(
                    name="C 语言 O_DIRECT 校验测试",
                    status="SKIP",
                    summary="用户通过参数关闭了 C 工具测试。",
                )
            )

        after_snapshot = self.collect_snapshot("after")
        self.results.append(self.compare_smart(before_snapshot, after_snapshot))
        self.save_json("command_log.json", self.command_log)
        self.save_json(
            "summary.json",
            {
                "device": str(self.device),
                "controller": str(self.controller),
                "namespace_id": self.namespace_id,
                "results": [dataclasses.asdict(item) for item in self.results],
            },
        )
        report_path = self.generate_markdown_report(
            device_size_bytes=device_size_bytes,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        self.log(f"测试完成，报告已生成: {report_path}")
        return 1 if any(item.status == "FAIL" for item in self.results) else 0


def load_config(config_path: pathlib.Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ProjectError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NVMe SSD 自动化测试项目")
    default_config = PROJECT_ROOT / "configs" / "default_test_plan.json"
    parser.add_argument("--device", default="/dev/nvme1n1", help="目标 NVMe namespace 设备，例如 /dev/nvme1n1")
    parser.add_argument("--controller", default=None, help="控制器节点，例如 /dev/nvme1；不传则自动推导")
    parser.add_argument("--config", default=str(default_config), help="测试配置文件路径")
    parser.add_argument("--report-root", default="reports", help="报告输出目录")
    parser.add_argument(
        "--yes-i-understand-this-will-destroy-data",
        action="store_true",
        help="显式确认本脚本会破坏目标盘数据",
    )
    parser.add_argument("--skip-fio", action="store_true", help="跳过 fio 性能测试")
    parser.add_argument("--skip-c-tool", action="store_true", help="跳过 C 语言低层工具测试")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    config = load_config(pathlib.Path(args.config).resolve())

    project = NvmeSsdTestProject(
        device=args.device,
        controller=args.controller,
        config=config,
        report_root=pathlib.Path(args.report_root).resolve(),
        destructive_confirmed=args.yes_i_understand_this_will_destroy_data,
        run_fio=not args.skip_fio,
        run_c_tool=not args.skip_c_tool,
    )
    return project.run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProjectError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("[ERROR] 用户中断执行。", file=sys.stderr)
        sys.exit(130)
