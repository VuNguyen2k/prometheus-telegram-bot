"""Telegram bot that queries Prometheus for system metrics."""

import os
import re
import logging
import asyncio

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

PROMETHEUS_URL = os.environ["PROMETHEUS_URL"].rstrip("/")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PromQL helpers
# ---------------------------------------------------------------------------

QUERIES = {
    # Node names (to map instance -> nodename)
    "node_uname": "node_uname_info",
    # CPU (per instance)
    "cpu_usage": '100 - (avg by (instance)(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)',
    "cpu_temp": "node_hwmon_temp_celsius",
    "cpu_fan": "node_hwmon_fan_rpm",
    "cpu_freq": "node_cpu_frequency_hertz",
    # RAM
    "ram_total": "node_memory_MemTotal_bytes",
    "ram_available": "node_memory_MemAvailable_bytes",
    # Swap
    "swap_total": "node_memory_SwapTotal_bytes",
    "swap_free": "node_memory_SwapFree_bytes",
    # Disk
    "disk_total": 'node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}',
    "disk_avail": 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}',
    "disk_read": 'rate(node_disk_read_bytes_total[2m])',
    "disk_write": 'rate(node_disk_written_bytes_total[2m])',
    # GPU (DCGM / nvidia_smi exporter)
    "gpu_util": "DCGM_FI_DEV_GPU_UTIL",
    "gpu_mem_used": "DCGM_FI_DEV_FB_USED",
    "gpu_mem_total": "DCGM_FI_DEV_FB_FREE + DCGM_FI_DEV_FB_USED",
    "gpu_temp": "DCGM_FI_DEV_GPU_TEMP",
    "gpu_fan": "DCGM_FI_DEV_FAN_SPEED",
    "gpu_power": "DCGM_FI_DEV_POWER_USAGE",
    "gpu_sm_clock": "DCGM_FI_DEV_SM_CLOCK",
    "gpu_mem_clock": "DCGM_FI_DEV_MEM_CLOCK",
}


async def prom_query(session: aiohttp.ClientSession, expr: str) -> list:
    """Run an instant PromQL query; return result list or []."""
    try:
        async with session.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": expr},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            if data.get("status") == "success":
                return data["data"]["result"]
    except Exception as exc:
        log.warning("Query failed (%s): %s", expr[:40], exc)
    return []


async def fetch_all(session: aiohttp.ClientSession) -> dict:
    """Fire all queries concurrently and return {name: result_list}."""
    names = list(QUERIES.keys())
    results = await asyncio.gather(*(prom_query(session, q) for q in QUERIES.values()))
    return dict(zip(names, results))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt(v: float | None, fmt: str = ".1f", suffix: str = "") -> str:
    if v is None:
        return "N/A"
    return f"{v:{fmt}}{suffix}"


def _human_bytes(b: float | None) -> str:
    """Return human-readable size string."""
    if b is None:
        return "N/A"
    gib = b / (1024 ** 3)
    if gib >= 1024:
        return f"{gib / 1024:.1f}TB"
    return f"{gib:.1f}GB"


def _human_speed(bps: float | None) -> str:
    """Return human-readable bytes/s string."""
    if bps is None:
        return "N/A"
    if bps >= 1024 ** 3:
        return f"{bps / (1024 ** 3):.1f}GB/s"
    if bps >= 1024 ** 2:
        return f"{bps / (1024 ** 2):.1f}MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f}KB/s"
    return f"{bps:.0f}B/s"


def _get_instance(metric: dict) -> str:
    return metric.get("instance", "unknown")


def _get_nodename(metric: dict) -> str:
    return metric.get("nodename", "")


def _build_instance_to_nodename(r: dict) -> dict[str, str]:
    """Build mapping from instance -> nodename using node_uname_info."""
    mapping: dict[str, str] = {}
    for res in r["node_uname"]:
        inst = _get_instance(res["metric"])
        nodename = _get_nodename(res["metric"])
        if nodename:
            mapping[inst] = nodename
    return mapping


def _filter_by_instance(results: list, instance: str) -> list:
    return [res for res in results if _get_instance(res["metric"]) == instance]


def _val_for_instance(results: list, instance: str) -> float | None:
    for res in results:
        if _get_instance(res["metric"]) == instance:
            try:
                return float(res["value"][1])
            except (KeyError, ValueError):
                pass
    return None


def _collect_all_instances(r: dict) -> set[str]:
    """Gather all known instances from node-level metrics."""
    instances: set[str] = set()
    for key in ("cpu_usage", "ram_total", "disk_total"):
        for res in r[key]:
            instances.add(_get_instance(res["metric"]))
    return instances


def _build_node_section(instance: str, nodename: str, r: dict) -> list[str]:
    """Build output lines for a single node."""
    lines: list[str] = []
    label = nodename or instance
    lines.append(f"--- {label} ---")

    # CPU
    cpu = _fmt(_val_for_instance(r["cpu_usage"], instance), suffix="%")
    lines.append(f"  CPU: {cpu}")

    # CPU Clock Speed (per core)
    freqs = _filter_by_instance(r["cpu_freq"], instance)
    if freqs:
        ghz_vals = []
        for res in freqs:
            try:
                ghz_vals.append(float(res["value"][1]) / 1e9)
            except (KeyError, ValueError):
                pass
        if ghz_vals:
            avg_ghz = sum(ghz_vals) / len(ghz_vals)
            min_ghz = min(ghz_vals)
            max_ghz = max(ghz_vals)
            lines.append(f"    Clock: avg {avg_ghz:.2f} GHz | min {min_ghz:.2f} | max {max_ghz:.2f}")
        else:
            lines.append("    Clock: N/A")
    else:
        lines.append("    Clock: N/A")

    # CPU Temp & Fan summarized (avg/min/max) grouped by chip
    temps = _filter_by_instance(r["cpu_temp"], instance)
    fans = _filter_by_instance(r["cpu_fan"], instance)

    # Group temps by chip
    chip_temps: dict[str, list[float]] = {}
    for res in temps:
        chip = res["metric"].get("chip", "unknown")
        try:
            v = float(res["value"][1])
            chip_temps.setdefault(chip, []).append(v)
        except (KeyError, ValueError):
            pass

    if chip_temps:
        for chip in sorted(chip_temps):
            vals = chip_temps[chip]
            avg_t = sum(vals) / len(vals)
            min_t = min(vals)
            max_t = max(vals)
            lines.append(f"    {chip}: avg {avg_t:.1f}C | min {min_t:.1f}C | max {max_t:.1f}C")
    else:
        lines.append("    Temp: N/A")

    fan_vals = []
    for res in fans:
        try:
            fan_vals.append(float(res["value"][1]))
        except (KeyError, ValueError):
            pass

    if fan_vals:
        avg_f = sum(fan_vals) / len(fan_vals)
        min_f = min(fan_vals)
        max_f = max(fan_vals)
        lines.append(f"    Fan: avg {avg_f:.0f} | min {min_f:.0f} | max {max_f:.0f} RPM")
    else:
        lines.append("    Fan: N/A")

    # RAM
    total_b = _val_for_instance(r["ram_total"], instance)
    avail_b = _val_for_instance(r["ram_available"], instance)
    if total_b is not None and avail_b is not None:
        used_b = total_b - avail_b
        pct = used_b / total_b * 100
        lines.append(
            f"  RAM: {_human_bytes(used_b)} / {_human_bytes(total_b)} ({pct:.0f}%)"
        )
    else:
        lines.append("  RAM: N/A")

    # Swap
    swap_total = _val_for_instance(r["swap_total"], instance)
    swap_free = _val_for_instance(r["swap_free"], instance)
    if swap_total is not None and swap_free is not None:
        swap_used = swap_total - swap_free
        swap_pct = swap_used / swap_total * 100 if swap_total else 0
        lines.append(
            f"  Swap: {_human_bytes(swap_used)} / {_human_bytes(swap_total)} ({swap_pct:.0f}%)"
        )
    else:
        lines.append("  Swap: N/A")

    # Disks (df-style output with read/write speed)
    disk_t = _filter_by_instance(r["disk_total"], instance)
    disk_a = _filter_by_instance(r["disk_avail"], instance)
    disk_r = _filter_by_instance(r["disk_read"], instance)
    disk_w = _filter_by_instance(r["disk_write"], instance)
    totals_map = {}
    avails_map = {}
    devices_map = {}
    for res in disk_t:
        mp = res["metric"].get("mountpoint", "?")
        totals_map[mp] = float(res["value"][1])
        devices_map[mp] = res["metric"].get("device", "?")
    for res in disk_a:
        mp = res["metric"].get("mountpoint", "?")
        avails_map[mp] = float(res["value"][1])

    # disk_read/write are keyed by bare device name (e.g. nvme0n1, sda)
    # filesystem devices are full paths (e.g. /dev/nvme0n1p2, /dev/sda1)
    # Match by finding the base disk: strip /dev/ and partition suffix
    read_by_dev: dict[str, float] = {}
    write_by_dev: dict[str, float] = {}
    for res in disk_r:
        dev = res["metric"].get("device", "?")
        try:
            read_by_dev[dev] = float(res["value"][1])
        except (KeyError, ValueError):
            pass
    for res in disk_w:
        dev = res["metric"].get("device", "?")
        try:
            write_by_dev[dev] = float(res["value"][1])
        except (KeyError, ValueError):
            pass

    def _match_disk_io(fs_dev: str) -> tuple[float | None, float | None]:
        """Match a filesystem device (e.g. /dev/nvme0n1p2) to disk I/O device (e.g. nvme0n1).
        Tries exact partition match first, then base disk.
        """
        bare = fs_dev.removeprefix("/dev/")
        # 1. Try exact match (partition-level I/O if available)
        if bare in read_by_dev or bare in write_by_dev:
            return read_by_dev.get(bare), write_by_dev.get(bare)
        # 2. Strip partition suffix to get base disk
        #    NVMe: nvme0n1p2 -> nvme0n1 (strip pN)
        #    SCSI/SATA: sda1 -> sda (strip trailing digits)
        #    dm-N, mdN: leave as-is
        if re.match(r'nvme\d+n\d+p\d+$', bare):
            base = re.sub(r'p\d+$', '', bare)
        elif re.match(r'[shv]d[a-z]+\d+$', bare):
            base = re.sub(r'\d+$', '', bare)
        elif re.match(r'mmcblk\d+p\d+$', bare):
            base = re.sub(r'p\d+$', '', bare)
        else:
            base = bare
        if base != bare and (base in read_by_dev or base in write_by_dev):
            return read_by_dev.get(base), write_by_dev.get(base)
        return None, None

    for mp in sorted(totals_map):
        t = totals_map[mp]
        a = avails_map.get(mp)
        dev = devices_map.get(mp, "?")
        rd_val, wr_val = _match_disk_io(dev)
        rd = _human_speed(rd_val)
        wr = _human_speed(wr_val)
        if a is not None:
            used = t - a
            pct = used / t * 100 if t else 0
            lines.append(f"  Disk {dev} [{mp}]")
            lines.append(f"    {_human_bytes(used)} / {_human_bytes(t)} ({pct:.0f}%) | R: {rd} | W: {wr}")
        else:
            lines.append(f"  Disk {dev} [{mp}]")
            lines.append(f"    N/A | R: {rd} | W: {wr}")

    if not totals_map:
        lines.append("  Disk: N/A")

    # GPUs – match by instance (or Hostname label)
    gpu_results_keys = ("gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp", "gpu_fan",
                         "gpu_power", "gpu_sm_clock", "gpu_mem_clock")
    # DCGM uses "instance" or "Hostname"; try to match
    # Collect gpu_id -> pci_bus_id mapping
    gpu_info: dict[str, str] = {}  # gid -> pci_bus_id
    for key in gpu_results_keys:
        for res in r[key]:
            res_inst = res["metric"].get("instance", "")
            res_host = res["metric"].get("Hostname", "")
            if res_inst == instance or res_host == nodename:
                gid = res["metric"].get("gpu", res["metric"].get("UUID", "0"))
                bus_id = res["metric"].get("pci_bus_id", res["metric"].get("GPU_I_ID", ""))
                if gid not in gpu_info or (bus_id and not gpu_info[gid]):
                    gpu_info[gid] = bus_id

    def _gpu_val(results: list, gid: str) -> float | None:
        for res in results:
            res_inst = res["metric"].get("instance", "")
            res_host = res["metric"].get("Hostname", "")
            if res_inst != instance and res_host != nodename:
                continue
            if res["metric"].get("gpu", res["metric"].get("UUID", "0")) == gid:
                try:
                    return float(res["value"][1])
                except (KeyError, ValueError):
                    pass
        return None

    # Sort GPUs by bus_id
    for gid in sorted(gpu_info, key=lambda g: gpu_info.get(g, "")):
        bus_id = gpu_info[gid]
        bus_label = f"[{bus_id}]" if bus_id else ""
        util = _fmt(_gpu_val(r["gpu_util"], gid), suffix="%")
        mem_used = _gpu_val(r["gpu_mem_used"], gid)
        mem_total = _gpu_val(r["gpu_mem_total"], gid)
        if mem_used is not None and mem_total is not None:
            vram = f"{mem_used / 1024:.2f}/{mem_total / 1024:.2f}GB"
        else:
            vram = "N/A"
        gt = _fmt(_gpu_val(r["gpu_temp"], gid), suffix="C")
        gf = _fmt(_gpu_val(r["gpu_fan"], gid), ".0f", "%")
        pw = _fmt(_gpu_val(r["gpu_power"], gid), ".0f", "W")
        sm = _fmt(_gpu_val(r["gpu_sm_clock"], gid), ".0f", "MHz")
        mc = _fmt(_gpu_val(r["gpu_mem_clock"], gid), ".0f", "MHz")
        lines.append(f"  GPU {gid} {bus_label}")
        lines.append(f"    {util} | {vram} | {gt} | {gf}")
        lines.append(f"    {pw} | SM {sm} | MemClk {mc}")

    if not gpu_info:
        lines.append("  GPU: N/A")

    return lines


def build_messages_per_host(r: dict) -> list[list]:
    """Return a list of line-lists, one per host."""
    inst_to_node = _build_instance_to_nodename(r)
    instances = _collect_all_instances(r)
    for key in ("gpu_util", "gpu_temp"):
        for res in r[key]:
            instances.add(_get_instance(res["metric"]))

    per_host: list[list] = []
    for inst in sorted(instances, key=lambda i: inst_to_node.get(i, i)):
        nodename = inst_to_node.get(inst, "")
        per_host.append(_build_node_section(inst, nodename, r))

    return per_host


def render_markdown(lines: list) -> str:
    """Render lines to Telegram MarkdownV2 as a code block."""
    text = "```\n" + "\n".join(lines) + "\n```"
    return text


TELEGRAM_MAX_LEN = 4096


async def _send_long(message, text: str, parse_mode: str = "MarkdownV2") -> None:
    """Split text into chunks that fit Telegram's message length limit."""
    lines = text.split("\n")
    chunk_lines: list[str] = []
    chunk_len = 0
    for line in lines:
        # +1 for the newline character
        if chunk_len + len(line) + 1 > TELEGRAM_MAX_LEN and chunk_lines:
            await message.reply_text("\n".join(chunk_lines), parse_mode=parse_mode)
            chunk_lines = []
            chunk_len = 0
        chunk_lines.append(line)
        chunk_len += len(line) + 1
    if chunk_lines:
        await message.reply_text("\n".join(chunk_lines), parse_mode=parse_mode)


# ---------------------------------------------------------------------------
# Bot command
# ---------------------------------------------------------------------------

async def cmd_latest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async with aiohttp.ClientSession() as session:
        try:
            results = await fetch_all(session)
        except Exception as exc:
            await update.message.reply_text(f"Error querying Prometheus: {exc}")
            return

    host_messages = build_messages_per_host(results)
    if not host_messages:
        await update.message.reply_text("No hosts found.")
        return

    for host_lines in host_messages:
        text = render_markdown(host_lines)
        await _send_long(update.message, text)


PWM_ENABLE_PATH = "/sys/class/hwmon/hwmon0/pwm2_enable"
PWM_VALUE_PATH = "/sys/class/hwmon/hwmon0/pwm2"


async def cmd_fanset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set fan speed via PWM. Usage: /fanset <1-255>"""
    if not ctx.args or len(ctx.args) != 1:
        await update.message.reply_text("Usage: /fanset <1-255>")
        return

    try:
        value = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Invalid value. Must be an integer 1-255.")
        return

    if not 1 <= value <= 255:
        await update.message.reply_text("Value must be between 1 and 255.")
        return

    try:
        with open(PWM_ENABLE_PATH, "w") as f:
            f.write("1")
        with open(PWM_VALUE_PATH, "w") as f:
            f.write(str(value))
        await update.message.reply_text(f"Fan PWM set to {value}/255.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_fanreset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset fan to automatic control. Usage: /fanreset"""
    try:
        with open(PWM_ENABLE_PATH, "w") as f:
            f.write("5")
        await update.message.reply_text("Fan reset to automatic control.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("fanset", cmd_fanset))
    app.add_handler(CommandHandler("fanreset", cmd_fanreset))
    log.info("Bot started – listening for /latest, /fanset, /fanreset")
    app.run_polling()


if __name__ == "__main__":
    main()
