#!/usr/bin/env python3
"""
stress_runner.py
================
Launches cpu/mem/vol stressors on a remote target VM via SSH.
Copies the stress scripts to the target, executes them remotely,
streams output back to the terminal, and cleans up on exit.

Usage:
  sudo python3 stress_runner.py --host <hostname> --only cpu --cpu-percent 96 --duration 180
  sudo python3 stress_runner.py --host <hostname> --only mem --mem-percent 96 --duration 120
  sudo python3 stress_runner.py --host <hostname> --only vol --vol-percent 92 --duration 120
  sudo python3 stress_runner.py --host <hostname> --duration 180  # all three at once
"""

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

import paramiko
from dotenv import dotenv_values

# ──────────────────────────────────────────────
# Load credentials (ansible server credentials)
# ──────────────────────────────────────────────

ssh_cred_path = Path('/etc/mgt-api/.ssh_credentials')
ssh_creds     = dotenv_values(ssh_cred_path)

SSH_USER     = ssh_creds.get('SSH_USER', 'scv')
SSH_PASSWORD = ssh_creds.get('SSH_PASSWORD')

if not SSH_PASSWORD:
    raise ValueError(
        "SSH_PASSWORD not found in /etc/mgt-api/.ssh_credentials — "
        "add SSH_USER and SSH_PASSWORD to that file and retry."
    )

# ──────────────────────────────────────────────
# Stress script filenames
# ──────────────────────────────────────────────

SCRIPTS = {
    "cpu": "cpu_stress.py",
    "mem": "mem_stress.py",
    "vol": "vol_stress.py",
}

REMOTE_STRESS_DIR = "/tmp/.stress_runner"


# ──────────────────────────────────────────────
# SSH helpers
# ──────────────────────────────────────────────

def _open_ssh(hostname: str, timeout: int = 10) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname, port=22,
        username=SSH_USER,
        password=SSH_PASSWORD,
        timeout=timeout,
    )
    return ssh


def _upload_scripts(sftp: paramiko.SFTPClient, local_base: str, targets: list):
    # Ensure remote stress dir exists
    try:
        sftp.stat(REMOTE_STRESS_DIR)
    except FileNotFoundError:
        sftp.mkdir(REMOTE_STRESS_DIR)

    for name in targets:
        local  = os.path.join(local_base, SCRIPTS[name])
        remote = f"{REMOTE_STRESS_DIR}/{SCRIPTS[name]}"
        if not os.path.exists(local):
            raise FileNotFoundError(
                f"Stress script not found: {local}\n"
                f"Make sure {SCRIPTS[name]} is in the same directory as stress_runner.py"
            )
        sftp.put(local, remote)
        print(f"  Uploaded {local} → {hostname}:{remote}")


def _cleanup_remote(hostname: str):
    try:
        ssh = _open_ssh(hostname)
        ssh.exec_command(f"rm -rf {REMOTE_STRESS_DIR}")
        ssh.close()
        print(f"\n[runner] Cleaned up {REMOTE_STRESS_DIR} on {hostname}")
    except Exception as e:
        print(f"\n[runner] Cleanup failed on {hostname}: {e}")


# ──────────────────────────────────────────────
# Remote execution
# ──────────────────────────────────────────────

def stream_output(channel, label: str, stop_event: threading.Event):
    # Stream stdout/stderr from a remote channel to local terminal
    while not stop_event.is_set():
        if channel.recv_ready():
            data = channel.recv(4096).decode(errors='replace')
            for line in data.splitlines():
                print(f"[{label}] {line}")
        if channel.recv_stderr_ready():
            data = channel.recv_stderr(4096).decode(errors='replace')
            for line in data.splitlines():
                print(f"[{label}|err] {line}", file=sys.stderr)
        if channel.exit_status_ready():
            # Drain remaining output
            while channel.recv_ready():
                data = channel.recv(4096).decode(errors='replace')
                for line in data.splitlines():
                    print(f"[{label}] {line}")
            while channel.recv_stderr_ready():
                data = channel.recv_stderr(4096).decode(errors='replace')
                for line in data.splitlines():
                    print(f"[{label}|err] {line}", file=sys.stderr)
            break
        time.sleep(0.1)


def run_stressor_remote(hostname: str, name: str, script_args: list) -> tuple:
    # Returns (ssh_client, channel, stop_event, thread) for the running stressor
    ssh       = _open_ssh(hostname)
    transport = ssh.get_transport()
    channel   = transport.open_session()
    channel.set_combine_stderr(False)

    remote_script = f"{REMOTE_STRESS_DIR}/{SCRIPTS[name]}"
    cmd = f"python3 {remote_script} {' '.join(script_args)}"
    print(f"[runner] → {name} on {hostname}: {cmd}")

    channel.exec_command(cmd)

    stop_event = threading.Event()
    thread = threading.Thread(
        target=stream_output,
        args=(channel, name, stop_event),
        daemon=True,
    )
    thread.start()

    return ssh, channel, stop_event, thread


# ──────────────────────────────────────────────
# Argument builder
# ──────────────────────────────────────────────

def build_args(cfg) -> dict:
    return {
        "cpu": [
            "--cores",    str(cfg.cpu_cores),
            "--duration", str(cfg.duration),
            "--percent",  str(cfg.cpu_percent),
        ],
        "mem": [
            "--percent",  str(cfg.mem_percent),
            "--chunk-mb", str(cfg.mem_chunk),
            "--hold",     str(cfg.duration),
        ],
        "vol": (
            [
                "--percent", str(cfg.vol_percent),
                "--path",    cfg.vol_path,
                "--rw-mb",   str(cfg.vol_rw_mb),
                "--hold",    str(cfg.duration),
            ] if cfg.vol_rw else [
                "--percent", str(cfg.vol_percent),
                "--path",    cfg.vol_path,
                "--rw-mb",   str(cfg.vol_rw_mb),
                "--hold",    str(cfg.duration),
                "--skip-rw",
            ]
        ),
    }


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Remote stress orchestrator — runs stressors on a target VM via SSH",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host",        required=True,
                   help="Target hostname or IP to stress")
    p.add_argument("--duration",    type=int,   default=180,
                   help="Test duration in seconds")
    p.add_argument("--cpu-cores",   type=int,   default=0,
                   help="Cores to stress (0 = all cores on target)")
    p.add_argument("--cpu-percent", type=float, default=96.0,
                   help="CPU usage %% per core via duty cycle")
    p.add_argument("--mem-percent", type=float, default=96.0,
                   help="%% of MemAvailable to consume")
    p.add_argument("--mem-chunk",   type=int,   default=64,
                   help="Allocation chunk size MB")
    p.add_argument("--vol-percent", type=float, default=92.0,
                   help="%% of free disk space to consume")
    p.add_argument("--vol-path",    default="/tmp/stress_vol",
                   help="Directory defining target filesystem")
    p.add_argument("--vol-rw-mb",   type=int,   default=256,
                   help="Extra sequential r/w MB after fallocate")
    p.add_argument("--vol-rw",      action="store_true",
                   help="Enable sequential r/w phase (disabled by default)")
    p.add_argument("--only",        choices=["cpu", "mem", "vol"],
                   help="Run only one stressor")
    args = p.parse_args()

    global hostname
    hostname = args.host

    # If cpu_cores not specified, query the target VM for its core count
    if args.cpu_cores == 0:
        try:
            ssh = _open_ssh(hostname)
            _, stdout, _ = ssh.exec_command("nproc")
            args.cpu_cores = int(stdout.read().decode().strip())
            ssh.close()
            print(f"[runner] Detected {args.cpu_cores} cores on {hostname}")
        except Exception as e:
            print(f"[runner] Could not detect core count on {hostname}: {e} — defaulting to 1")
            args.cpu_cores = 1

    arg_map = build_args(args)
    targets = [args.only] if args.only else list(SCRIPTS)
    base    = os.path.dirname(os.path.abspath(__file__))

    print(f"[runner] target={hostname} duration={args.duration}s stressors={targets}")
    print(f"[runner] cpu={args.cpu_percent}% ({args.cpu_cores} cores)"
          f"  mem={args.mem_percent}%  vol={args.vol_percent}%")

    # Upload stress scripts to target VM
    print(f"\n[runner] Uploading stress scripts to {hostname}:{REMOTE_STRESS_DIR}")
    try:
        ssh_upload = _open_ssh(hostname)
        sftp = ssh_upload.open_sftp()
        _upload_scripts(sftp, base, targets)
        sftp.close()
        ssh_upload.close()
    except Exception as e:
        print(f"[runner] Failed to upload scripts: {e}")
        sys.exit(1)

    # Launch all stressors in parallel
    running = {}  # name → (ssh, channel, stop_event, thread)
    for name in targets:
        try:
            ssh, channel, stop_event, thread = run_stressor_remote(
                hostname, name, arg_map[name]
            )
            running[name] = (ssh, channel, stop_event, thread)
        except Exception as e:
            print(f"[runner] Failed to start {name}: {e}")

    if not running:
        print("[runner] No stressors started — exiting")
        _cleanup_remote(hostname)
        sys.exit(1)

    # Handle Ctrl+C / SIGTERM — kill all remote stressors
    def kill_all(s=None, f=None):
        print("\n[runner] Interrupted — killing remote stressors...")
        for name, (ssh, channel, stop_event, thread) in running.items():
            try:
                channel.send(b'\x03')   # send Ctrl+C to remote process
                stop_event.set()
                ssh.close()
                print(f"[runner] Killed {name} on {hostname}")
            except Exception:
                pass
        _cleanup_remote(hostname)
        sys.exit(0)

    signal.signal(signal.SIGINT,  kill_all)
    signal.signal(signal.SIGTERM, kill_all)

    # Wait for all stressors to finish
    for name, (ssh, channel, stop_event, thread) in running.items():
        thread.join()
        rc = channel.recv_exit_status()
        print(f"[runner] {name} exited rc={rc}")
        stop_event.set()
        ssh.close()

    _cleanup_remote(hostname)
    print("[runner] All stressors complete")


if __name__ == "__main__":
    main()