#!/usr/bin/python3
"""
deploy_alertmanager.py
======================
Deploys alertmanager.yml and kakao_webhook.py to both AlertManager servers,
then reloads alertmanager.service and restarts kakao-webhook.service.
"""

import time
from pathlib import Path

import paramiko
import requests
from dotenv import dotenv_values


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _open_ssh(hostname, username, password=None, key_filepath=None, port=22, timeout=10):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if key_filepath:
        ssh.connect(hostname, port=port, username=username,
                    key_filename=key_filepath, timeout=timeout)
    else:
        ssh.connect(hostname, port=port, username=username,
                    password=password, timeout=timeout)
    return ssh


def send_file_to_server(local_path, remote_path, hostname,
                        port=22, username=None, password=None, key_filepath=None):
    import os
    tmp_path = f"/tmp/_deploy_{os.path.basename(remote_path)}"
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if key_filepath:
        key = paramiko.RSAKey.from_private_key_file(key_filepath)
        ssh_client.connect(hostname, port=port, username=username, pkey=key)
    else:
        ssh_client.connect(hostname, port=port, username=username, password=password)
    sftp = ssh_client.open_sftp()
    sftp.put(local_path, tmp_path)
    sftp.close()
    stdin, stdout, stderr = ssh_client.exec_command(
        f'sudo mv {tmp_path} {remote_path}', timeout=30
    )
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        err = stderr.read().decode().strip()
        ssh_client.close()
        raise RuntimeError(f"sudo mv failed on {hostname} ({remote_path}): {err}")
    ssh_client.close()
    print(f"  Sent {local_path} → {username}@{hostname}:{remote_path}")


def ensure_remote_dir(hostname, remote_dir, username, password, port=22):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(hostname, port=port, username=username, password=password)
        ssh.exec_command(f'mkdir -p {remote_dir}')
        print(f"  Ensured remote dir {remote_dir} on {hostname}")
    except Exception as e:
        print(f"  Failed to create {remote_dir} on {hostname}: {e}")
    finally:
        ssh.close()


def run_remote_command(hostname, username, password, command, timeout=30):
    try:
        ssh = _open_ssh(hostname, username, password=password)
        stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors='replace')
        err = stderr.read().decode(errors='replace')
        ssh.close()
        return exit_code, out, err
    except Exception as e:
        print(f"  [run_remote_command] SSH failed on {hostname}: {e}")
        return -1, '', str(e)


def _systemctl(hostname, username, password, service, action):
    cmd = f'sudo systemctl {action} {service}'
    exit_code, out, err = run_remote_command(hostname, username, password, cmd)
    label = f"systemctl {action} {service} on {hostname}"
    if exit_code == 0:
        print(f"  ✔ {label}")
    else:
        print(f"  ✘ {label} failed (exit {exit_code})")
        if out.strip():
            print(f"    stdout: {out.strip()}")
        if err.strip():
            print(f"    stderr: {err.strip()}")


def reload_alertmanager(hostname, port=39093):
    reload_url = f"http://{hostname}:{port}/-/reload"
    try:
        resp = requests.post(reload_url, timeout=15)
        if resp.status_code == 200:
            print(f"  AlertManager reloaded via HTTP on {hostname}.")
            return
        print(f"  HTTP reload returned {resp.status_code} on {hostname} — falling back to systemctl.")
    except Exception as e:
        print(f"  HTTP reload failed for {hostname} ({e}) — falling back to systemctl.")
    _systemctl(hostname, am_user, password, 'alertmanager.service', 'reload-or-restart')


def validate_alertmanager_config(content: str, hostname: str, username: str, password: str) -> bool:
    remote_tmp = '/tmp/validate_alertmanager_tmp.yml'
    try:
        ssh = _open_ssh(hostname, username, password=password)
        sftp = ssh.open_sftp()
        with sftp.open(remote_tmp, 'w') as f:
            f.write(content)
        sftp.close()
        stdin, stdout, stderr = ssh.exec_command(
            f'amtool check-config {remote_tmp}', timeout=30
        )
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode()
        err = stderr.read().decode()
        ssh.exec_command(f'rm -f {remote_tmp}')
        ssh.close()
        if exit_code != 0:
            print("alertmanager.yml validation FAILED:")
            print(out)
            print(err)
            return False
        print("alertmanager.yml validation passed.")
        return True
    except Exception as e:
        print(f"  Warning: Could not validate alertmanager.yml on {hostname}: {e}")
        return True


# ──────────────────────────────────────────────
# Load credentials
# ──────────────────────────────────────────────

ssh_cred_path = Path('/etc/mgt-api/.ssh_credentials')
ssh_creds = dotenv_values(ssh_cred_path)

am_user  = ssh_creds.get('SSH_USER', 'scv')
password = ssh_creds.get('SSH_PASSWORD')
if not password:
    raise ValueError(
        "SSH_PASSWORD not found in /etc/mgt-api/.ssh_credentials — "
        "add SSH_USER and SSH_PASSWORD to that file and retry."
    )

key_filepath = None

ALERTMANAGER_SERVERS = [
    'alertmanager-01',
    'alertmanager-02',
]

REMOTE_BASE_DIR  = '/data/etc/alertmanager'
REMOTE_AM_CONFIG = f'{REMOTE_BASE_DIR}/alertmanager.yml'
REMOTE_WEBHOOK   = f'{REMOTE_BASE_DIR}/kakao_webhook.py'


# ──────────────────────────────────────────────
# alertmanager.yml
# ──────────────────────────────────────────────

ALERTMANAGER_CONFIG = """\
global:
  resolve_timeout: 5m
route:
  group_by: ['alertname', 'instance']
  group_wait: 5s
  group_interval: 1m
  repeat_interval: 12h
  receiver: 'kakaowork'
  routes:
    # ── SSH always blocked ────────────────────────────────────────
    - receiver: 'blackhole'
      matchers:
        - alertname=~"SSHDown|SSHDServiceDown|SSHDownCausedByOOM"
        - hostname=~"infra-ist-dkt-fw-01|kw-prod-cdr-01|infra-ist-sk-int-fw-02|infra-ist-sk-fw-01|infra-ist-sk-fw-02|infra-ist-dkt-fw-02|infra-ist-kw-int-fw-02|infra-ist-dmz-fw-02|infra-ist-sk-int-fw-01|kw-prod-cdr-02|infra-ist-dmz-fw-01|kw-sbox-low-spec-windows|infra-ist-kw-int-fw-01"
      continue: false
    - receiver: 'kakaowork'
      matchers:
        - alertname=~"SSHDown|SSHDServiceDown"
      group_by: ['alertname', 'instance']
      group_wait: 45s
      group_interval: 2m
      repeat_interval: 12h
      continue: false
    # ── ICMP always blocked ───────────────────────────────────────
    - receiver: 'blackhole'
      matchers:
        - alertname=~"HostDown"
        - hostname=~"infra-ist-sk-fw-02|infra-ist-sk-int-fw-01|infra-ist-dmz-fw-02|infra-ist-kw-int-fw-02|infra-ist-sk-int-fw-02|infra-ist-dmz-fw-01|infra-ist-dkt-fw-02|infra-ist-kw-int-fw-01"
      continue: false
    # ── CPU / Memory alerts ───────────────────────────────────────
    - receiver: 'kakaowork'
      matchers:
        - alertname=~"CPUCritical|MemoryCritical|MemoryNearExhaustion"
      group_by: ['alertname', 'instance']
      group_wait: 5s
      group_interval: 1m
      repeat_interval: 30m
      continue: false
    # ── Disk alerts — group by mountpoint ────────────────────────
    - receiver: 'kakaowork'
      matchers:
        - alertname=~"DiskSpaceCritical|DiskSpaceImminent|InodeCritical"
      group_by: ['alertname', 'instance', 'mountpoint']
      group_wait: 5s
      group_interval: 30s
      repeat_interval: 30m
      continue: false
    - receiver: 'kakaowork'
      matchers:
        - alertname=~"DiskSpaceWarning|InodeWarning"
      group_by: ['alertname', 'instance', 'mountpoint']
      group_wait: 5s
      group_interval: 30s
      repeat_interval: 1h
      continue: false
receivers:
  - name: 'kakaowork'
    webhook_configs:
      - url: 'http://localhost:5001/alert/kakaowork'
        send_resolved: true
  - name: 'blackhole'
inhibit_rules:
  # ── SSH inhibit rules ─────────────────────────────────────────
  - source_matchers:
      - alertname="HostDown"
    target_matchers:
      - alertname="SSHDown"
    equal: ['instance']
  - source_matchers:
      - alertname="SSHDownCausedByOOM"
    target_matchers:
      - alertname="SSHDown"
    equal: ['instance']
  # ── Memory inhibit rules ──────────────────────────────────────
  - source_matchers:
      - alertname="MemoryCritical"
    target_matchers:
      - alertname="MemoryNearExhaustion"
    equal: ['instance']
  # ── Disk inhibit rules ────────────────────────────────────────
  - source_matchers:
      - alertname="DiskSpaceImminent"
    target_matchers:
      - alertname="DiskSpaceCritical"
    equal: ['instance', 'mountpoint']
  - source_matchers:
      - alertname="DiskSpaceImminent"
    target_matchers:
      - alertname="DiskSpaceWarning"
    equal: ['instance', 'mountpoint']
  - source_matchers:
      - alertname="DiskSpaceCritical"
    target_matchers:
      - alertname="DiskSpaceWarning"
    equal: ['instance', 'mountpoint']
  - source_matchers:
      - alertname="InodeCritical"
    target_matchers:
      - alertname="InodeWarning"
    equal: ['instance', 'mountpoint']
"""


# ──────────────────────────────────────────────
# kakao_webhook.py  (embedded — deployed as-is)
# ──────────────────────────────────────────────

KAKAO_WEBHOOK_CODE = r"""
#!/usr/bin/python3

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path
import hashlib
import requests
import logging
import json
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

KAKAOWORK_WEBHOOK_URL = "https://kakaowork.com/bots/hook/<YOUR_BOT_WEBHOOK_TOKEN>"

KST = timezone(timedelta(hours=9))

# ──────────────────────────────────────────────
# Disk-persisted outage tracker
# ──────────────────────────────────────────────

TRACKER_FILE = Path("/data/etc/alertmanager/outage_tracker.json")
tracker_lock = threading.Lock()


def load_tracker() -> dict:
    if TRACKER_FILE.exists():
        try:
            with TRACKER_FILE.open("r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load tracker file: {e}")
    return {}


def save_tracker(tracker: dict):
    try:
        with TRACKER_FILE.open("w") as f:
            json.dump(tracker, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save tracker file: {e}")


def tracker_get(alertname: str) -> dict:
    with tracker_lock:
        tracker = load_tracker()
        return tracker.get(alertname, {"total": 0, "resolved": 0})


def tracker_set(alertname: str, total: int, resolved: int):
    with tracker_lock:
        tracker = load_tracker()
        tracker[alertname] = {"total": total, "resolved": resolved}
        save_tracker(tracker)


def tracker_increment_resolved(alertname: str, count: int) -> dict:
    with tracker_lock:
        tracker = load_tracker()
        entry = tracker.get(alertname, {"total": 0, "resolved": 0})
        entry["resolved"] = entry.get("resolved", 0) + count
        tracker[alertname] = entry
        save_tracker(tracker)
        return entry


def tracker_reset(alertname: str):
    with tracker_lock:
        tracker = load_tracker()
        tracker.pop(alertname, None)
        save_tracker(tracker)


# ──────────────────────────────────────────────
# Alert statistics tracker
# ──────────────────────────────────────────────

STATS_FILE = Path("/data/etc/alertmanager/alert_stats.json")
stats_lock  = threading.Lock()

# Maps alertname prefix → category code
ALERT_CATEGORY = {
    "DiskSpace": "VOL",
    "Inode":     "VOL",
    "Memory":    "MEM",
    "CPU":       "CPU",
    "SSH":       "CON",
    "Host":      "CON",
}

CATEGORIES = ["VOL", "MEM", "CPU", "CON"]

# Inhibit map — if the suppressor is currently fired, skip recording the suppressed alert
# Mirrors alertmanager.yml inhibit_rules
INHIBITED_BY = {
    "DiskSpaceWarning":     "DiskSpaceImminent",
    "DiskSpaceCritical":    "DiskSpaceImminent",
    "InodeWarning":         "InodeCritical",
    "SSHDown":              "HostDown",
    "SSHDownCausedByOOM":   "HostDown",
    "MemoryNearExhaustion": "MemoryCritical",
}

# Reverse map: suppressor -> [suppressed alertnames]
# When the suppressor FIRES, immediately close any pending (unmatched) fired
# events for these suppressed alertnames on the same host. This prevents the
# suppressed alert's own resolved (which Alertmanager still sends independently)
# from later double-counting or going unmatched.
SUPPRESSOR_CLOSES = {}
for _suppressed_name, _suppressor_name in INHIBITED_BY.items():
    SUPPRESSOR_CLOSES.setdefault(_suppressor_name, []).append(_suppressed_name)


def _get_category(alertname: str) -> str:
    for prefix, cat in ALERT_CATEGORY.items():
        if alertname.startswith(prefix):
            return cat
    return "OTHER"


def _load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            with STATS_FILE.open("r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load stats file: {e}")
    # Default structure
    return {
        "summary": {c: {"fired": 0, "resolved": 0, "total_duration_seconds": 0} for c in CATEGORIES + ["OTHER"]},
        "by_alertname": {},
        "by_host": {},
        "events": []
    }


def _save_stats(stats: dict):
    try:
        # Auto-create file with correct permissions if it doesn't exist
        if not STATS_FILE.exists():
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATS_FILE.touch()
            STATS_FILE.chmod(0o664)
            logger.info(f"Created stats file: {STATS_FILE}")
        with STATS_FILE.open("w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save stats file: {e}")


def stats_record_fired(alertname: str, hostname: str, instance: str, starts_at: str, fingerprint: str = ""):
    cat = _get_category(alertname)
    with stats_lock:
        stats = _load_stats()

        # Dedup check — skip if same alertname+hostname+starts_at already fired
        # This prevents repeat_interval re-sends from being counted as new fires
        already_fired = any(
            e.get("type") == "fired"
            and e.get("alertname") == alertname
            and e.get("hostname") == hostname
            and e.get("starts_at") == starts_at
            for e in stats["events"]
        )
        if already_fired:
            logger.info(f"[stats] duplicate fired skipped — {cat} / {alertname} / {hostname} / {starts_at}")
            return

        # Inhibit check — skip if a higher-severity alert is currently active for same host
        # Mirrors alertmanager inhibit_rules so suppressed alerts are not counted
        suppressor = INHIBITED_BY.get(alertname)
        if suppressor:
            is_suppressed = any(
                e.get("type") == "fired"
                and e.get("alertname") == suppressor
                and e.get("hostname") == hostname
                for e in stats["events"]
                if not any(
                    r.get("type") == "resolved"
                    and r.get("alertname") == suppressor
                    and r.get("hostname") == hostname
                    and r.get("logged_at", "") > e.get("logged_at", "")
                    for r in stats["events"]
                )
            )
            if is_suppressed:
                logger.info(f"[stats] inhibited fired skipped — {alertname} suppressed by {suppressor} on {hostname}")
                return

        # Supersede check — if there's already an unmatched pending fired for the
        # same alertname+hostname but a DIFFERENT starts_at, it means Alertmanager
        # sent a new fired without a resolved for the previous one (happens when
        # an alert resolves and immediately re-fires, causing AM to skip the resolved).
        # Close the old pending with ends_at = new starts_at before recording the new fired.
        host_events = sorted(
            [e for e in stats["events"]
             if e.get("alertname") == alertname
             and e.get("hostname") == hostname],
            key=lambda x: x.get("starts_at", x.get("logged_at", ""))
        )
        pending_to_supersede = []
        for e in host_events:
            if e.get("type") == "fired":
                pending_to_supersede.append(e)
            elif e.get("type") == "resolved" and pending_to_supersede:
                pending_to_supersede.pop(0)

        if pending_to_supersede:
            now_kst_supersede = datetime.now(KST).isoformat()
            for pe in pending_to_supersede:
                dur = 0
                try:
                    t_s = datetime.fromisoformat(pe.get("starts_at", "").replace("Z", "+00:00"))
                    t_e = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                    dur = max(int((t_e - t_s).total_seconds()), 0)
                except Exception:
                    pass
                stats["summary"][cat]["resolved"] += 1
                stats["summary"][cat]["total_duration_seconds"] += dur
                if alertname not in stats["by_alertname"]:
                    stats["by_alertname"][alertname] = {"fired": 0, "resolved": 0, "category": cat}
                stats["by_alertname"][alertname]["resolved"] += 1
                if hostname not in stats["by_host"]:
                    stats["by_host"][hostname] = {"fired": 0, "resolved": 0}
                stats["by_host"][hostname]["resolved"] += 1
                stats["events"].append({
                    "type":             "resolved",
                    "category":         cat,
                    "alertname":        alertname,
                    "hostname":         hostname,
                    "instance":         instance,
                    "starts_at":        pe.get("starts_at", ""),
                    "ends_at":          starts_at,
                    "duration_seconds": dur,
                    "logged_at":        now_kst_supersede,
                    "closed_by":        "superseded",
                })
            logger.info(f"[stats] superseded {len(pending_to_supersede)}x pending {alertname} on {hostname} (new fired starts_at={starts_at})")

        # summary
        stats["summary"][cat]["fired"] += 1

        # by alertname
        if alertname not in stats["by_alertname"]:
            stats["by_alertname"][alertname] = {"fired": 0, "resolved": 0, "category": cat}
        stats["by_alertname"][alertname]["fired"] += 1

        # by host
        if hostname not in stats["by_host"]:
            stats["by_host"][hostname] = {"fired": 0, "resolved": 0}
        stats["by_host"][hostname]["fired"] += 1

        # event log
        now_kst = datetime.now(KST).isoformat()
        stats["events"].append({
            "type":        "fired",
            "category":    cat,
            "alertname":   alertname,
            "hostname":    hostname,
            "instance":    instance,
            "starts_at":   starts_at,
            "fingerprint": fingerprint,
            "logged_at":   now_kst,
        })

        # If this alertname suppresses other alertnames (e.g. HostDown suppresses
        # SSHDown), immediately close any pending unmatched fired events for those
        # suppressed alertnames on this host. Alertmanager still sends an
        # independent resolved for the suppressed alert later — closing the pending
        # now means that later resolved has nothing to match (and is safely ignored
        # as a no-op) instead of going unmatched or double-counting.
        for suppressed_name in SUPPRESSOR_CLOSES.get(alertname, []):
            sup_events = sorted(
                [e for e in stats["events"]
                 if e.get("alertname") == suppressed_name
                 and e.get("hostname") == hostname],
                key=lambda x: x.get("starts_at", x.get("logged_at", ""))
            )
            pending = []
            for e in sup_events:
                if e.get("type") == "fired":
                    pending.append(e)
                elif e.get("type") == "resolved" and pending:
                    pending.pop(0)
            if pending:
                sup_cat = _get_category(suppressed_name)
                for pe in pending:
                    stats["summary"][sup_cat]["resolved"] += 1
                    if suppressed_name not in stats["by_alertname"]:
                        stats["by_alertname"][suppressed_name] = {"fired": 0, "resolved": 0, "category": sup_cat}
                    stats["by_alertname"][suppressed_name]["resolved"] += 1
                    if hostname not in stats["by_host"]:
                        stats["by_host"][hostname] = {"fired": 0, "resolved": 0}
                    stats["by_host"][hostname]["resolved"] += 1
                    # duration from suppressed alert's own start to now (suppressor fired)
                    dur = 0
                    try:
                        t_s = datetime.fromisoformat(pe.get("starts_at", "").replace("Z", "+00:00"))
                        t_e = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                        dur = max(int((t_e - t_s).total_seconds()), 0)
                    except Exception:
                        pass
                    stats["summary"][sup_cat]["total_duration_seconds"] += dur
                    stats["events"].append({
                        "type":             "resolved",
                        "category":         sup_cat,
                        "alertname":        suppressed_name,
                        "hostname":         hostname,
                        "instance":         instance,
                        "starts_at":        pe.get("starts_at", ""),
                        "ends_at":          starts_at,
                        "duration_seconds": dur,
                        "logged_at":        now_kst,
                        "closed_by":        alertname,
                    })
                logger.info(f"[stats] closed {len(pending)}x pending {suppressed_name} on {hostname} (suppressed by {alertname} firing)")

        _save_stats(stats)
        logger.info(f"[stats] fired — {cat} / {alertname} / {hostname}")


def stats_record_resolved(alertname: str, hostname: str, instance: str,
                          starts_at: str, ends_at: str, fingerprint: str = ""):
    cat = _get_category(alertname)

    # calculate duration
    duration_seconds = 0
    try:
        t_start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        t_end   = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
        duration_seconds = max(int((t_end - t_start).total_seconds()), 0)
    except Exception:
        pass

    with stats_lock:
        stats = _load_stats()

        # Dedup / orphan check — only record resolved if there's an unmatched
        # (pending) fired for this alertname+hostname. If the suppressor already
        # closed this alert's pending (see SUPPRESSOR_CLOSES in stats_record_fired),
        # there is nothing left to match, so this resolved is a safe no-op.
        host_events = sorted(
            [e for e in stats["events"]
             if e.get("alertname") == alertname
             and e.get("hostname") == hostname],
            key=lambda x: x.get("starts_at", x.get("logged_at", ""))
        )
        pending = []
        for e in host_events:
            if e.get("type") == "fired":
                pending.append(e)
            elif e.get("type") == "resolved" and pending:
                pending.pop(0)
        if not pending:
            logger.info(f"[stats] resolved with no pending fired skipped (already closed) — {cat} / {alertname} / {hostname}")
            return

        # summary
        stats["summary"][cat]["resolved"] += 1
        stats["summary"][cat]["total_duration_seconds"] += duration_seconds

        # by alertname
        if alertname not in stats["by_alertname"]:
            stats["by_alertname"][alertname] = {"fired": 0, "resolved": 0, "category": cat}
        stats["by_alertname"][alertname]["resolved"] += 1

        # by host
        if hostname not in stats["by_host"]:
            stats["by_host"][hostname] = {"fired": 0, "resolved": 0}
        stats["by_host"][hostname]["resolved"] += 1

        # event log
        now_kst = datetime.now(KST).isoformat()
        stats["events"].append({
            "type":              "resolved",
            "category":          cat,
            "alertname":         alertname,
            "hostname":          hostname,
            "instance":          instance,
            "starts_at":         starts_at,
            "ends_at":           ends_at,
            "duration_seconds":  duration_seconds,
            "fingerprint":       fingerprint,
            "logged_at":         now_kst,
        })

        _save_stats(stats)
        logger.info(f"[stats] resolved — {cat} / {alertname} / {hostname} / {duration_seconds}s")


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

SEVERITY_EMOJI = {
    "critical": "🔴",
    "warning":  "🟡",
    "imminent": "⚫",
    "info":     "🔵",
}

ALERT_WHAT_KO = {
    "MemoryNearExhaustion": "메모리 임계치 초과 (90% 초과)",
    "SSHDServiceDown":      "sshd 서비스 중단",
    "SSHDown":              "SSH 연결 불가 (호스트는 정상)",
    "HostDown":             "호스트 통신 불가 (SSH + ICMP 모두 실패)",
    "SSHDownCausedByOOM":   "SSH 다운 — OOM으로 인한 sshd 종료 확인됨",
    "DiskSpaceWarning":     "디스크 사용량 경고 (90% 초과)",
    "DiskSpaceCritical":    "디스크 사용량 위험 (95% 초과)",
    "DiskSpaceImminent":    "디스크 사용량 긴급 (99% 초과)",
    "InodeWarning":         "아이노드 사용량 경고 (80% 초과)",
    "InodeCritical":        "아이노드 사용량 위험 (90% 초과)",
    "CPUCritical":          "CPU 사용량 위험 (95% 초과)",
    "MemoryCritical":       "메모리 사용량 위험 (95% 초과)",
}

ALERT_WHY_KO = {
    "MemoryNearExhaustion": "메모리 사용량이 90%를 초과함. 지속 증가 시 OOM Kill 위험.",
    "SSHDServiceDown":      "sshd.service가 비활성 상태. 서비스 재시작 필요.",
    "SSHDown":              "ICMP는 정상이나 SSH 불가. sshd 크래시, 방화벽 규칙 변경 또는 설정 오류 가능성.",
    "HostDown":             "SSH와 ICMP 모두 실패. VM 전원 꺼짐, 커널 패닉, 또는 네트워크 단절 가능성.",
    "SSHDownCausedByOOM":   "SSH 다운 + 최근 10분 내 OOM Kill 감지. 메모리 부족으로 인한 sshd 종료.",
    "DiskSpaceWarning":     "디스크 사용량이 90%를 초과함. 지속적으로 증가 중일 수 있음.",
    "DiskSpaceCritical":    "디스크 사용량이 95%를 초과함. 즉시 조치 필요.",
    "DiskSpaceImminent":    "디스크 사용량이 99%를 초과함. 즉각적인 조치 없으면 서비스 중단 가능.",
    "InodeWarning":         "아이노드 사용량 80% 초과. 디스크 여유 공간이 있어도 파일 생성 불가 가능.",
    "InodeCritical":        "아이노드 사용량 90% 초과. 즉시 조치 필요.",
    "CPUCritical":          "CPU 사용량이 95%를 초과함. 즉시 프로세스 점검 필요.",
    "MemoryCritical":       "메모리 사용량이 95%를 초과함. OOM Kill 임박. 즉시 조치 필요.",
}

ALERT_CHECK_KO = {
    "MemoryNearExhaustion": "free -h && ps aux --sort=-%mem | head -20",
    "SSHDServiceDown":      "journalctl -u sshd -n 50 --no-pager",
    "SSHDown":              "systemctl status sshd && journalctl -u sshd -n 30",
    "HostDown":             "클라우드 콘솔에서 VM 상태 확인",
    "SSHDownCausedByOOM":   "journalctl -k | grep -i 'oom|killed process'",
    "DiskSpaceWarning":     "df -h | grep -v tmpfs && du -sh /*  | sort -rh | head -20",
    "DiskSpaceCritical":    "df -h | grep -v tmpfs && du -sh /* | sort -rh | head -20",
    "DiskSpaceImminent":    "df -h | grep -v tmpfs && du -sh /* | sort -rh | head -20",
    "InodeWarning":         "df -i 로 아이노드 사용량 확인 (마운트포인트는 알림 내 Disk 섹션 참고)",
    "InodeCritical":        "df -i 로 아이노드 사용량 확인 (마운트포인트는 알림 내 Disk 섹션 참고)",
    "CPUCritical":          "top -bn1 | head -20 && ps aux --sort=-%cpu | head -20",
    "MemoryCritical":       "free -h && ps aux --sort=-%mem | head -20",
}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def format_kst(iso_str: str) -> str:
    if not iso_str:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


ALERT_PREFIX = {
    "HostDown":             "CON",
    "SSHDown":              "CON",
    "SSHDServiceDown":      "CON",
    "SSHDownCausedByOOM":   "CON",
    "MemoryNearExhaustion": "MEM",
    "MemoryCritical":       "MEM",
    "CPUCritical":          "CPU",
    "DiskSpaceWarning":     "VOL",
    "DiskSpaceCritical":    "VOL",
    "DiskSpaceImminent":    "VOL",
    "InodeWarning":         "VOL",
    "InodeCritical":        "VOL",
}


def generate_event_id(alertname: str, instance: str, starts_at: str, fingerprint: str = "") -> str:
    prefix = ALERT_PREFIX.get(alertname, "ALT")
    try:
        dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        date_str = dt.astimezone(KST).strftime("%Y%m%d")
    except Exception:
        date_str = datetime.now(KST).strftime("%Y%m%d")
    # Use fingerprint if available — it's stable across fired/resolved for the same alert
    # Fall back to hash of alertname+instance+starts_at if fingerprint is missing
    if fingerprint:
        hash_str = fingerprint[:8].upper()
    else:
        raw = f"{alertname}-{instance}-{starts_at}"
        hash_str = hashlib.sha256(raw.encode()).hexdigest()[:8].upper()
    return f"{prefix}-{date_str}-{hash_str}"


# ──────────────────────────────────────────────
# DU API
# ──────────────────────────────────────────────

DU_API_HOSTS = [
    "http://prometheus-01.internal.example.com:5002",
    "http://prometheus-02.internal.example.com:5002",
]


def get_top_dirs(hostname: str, mountpoint: str, top_n: int = 3) -> list:
    for api_url in DU_API_HOSTS:
        try:
            resp = requests.get(
                f"{api_url}/du",
                params={"host": hostname, "mountpoint": mountpoint, "top_n": top_n},
                timeout=30
            )
            if resp.status_code == 200:
                dirs = resp.json().get("dirs", [])
                return [(d["path"], d["size_human"]) for d in dirs if "path" in d]
        except Exception as e:
            logger.warning(f"DU API call failed for {api_url}: {e}, trying next...")
            continue
    logger.error(f"All DU API hosts failed for {hostname}{mountpoint}")
    return []


def get_top_procs(hostname: str, metric: str, top_n: int = 3) -> dict:
    for api_url in DU_API_HOSTS:
        try:
            resp = requests.get(
                f"{api_url}/top_procs",
                params={"host": hostname, "metric": metric, "top_n": top_n},
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data:
                    return data
        except Exception as e:
            logger.warning(f"top_procs API call failed for {api_url}: {e}, trying next...")
            continue
    logger.error(f"All DU API hosts failed for top_procs {hostname} metric={metric}")
    return {}


# ──────────────────────────────────────────────
# Prometheus live queries (used at resolve time)
# ──────────────────────────────────────────────

PROMETHEUS_HOSTS = [
    "http://prometheus-01.internal.example.com:9090",
    "http://prometheus-02.internal.example.com:9090",
]


def _prom_query(promql: str) -> str:
    for api_url in PROMETHEUS_HOSTS:
        try:
            resp = requests.get(
                f"{api_url}/api/v1/query",
                params={"query": promql},
                timeout=10
            )
            if resp.status_code == 200:
                results = resp.json().get("data", {}).get("result", [])
                if results:
                    return results[0]["value"][1]
        except Exception as e:
            logger.warning(f"Prometheus query failed for {api_url}: {e}, trying next...")
            continue
    return ""


def _bytes_to_human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def get_current_disk_usage(hostname: str, mountpoint: str) -> dict:
    queries = {
        "used_percent": (
            f'100 - ((node_filesystem_avail_bytes{{hostname="{hostname}",'
            f'mountpoint="{mountpoint}"}}) / '
            f'(node_filesystem_size_bytes{{hostname="{hostname}",'
            f'mountpoint="{mountpoint}"}})) * 100'
        ),
        "avail_bytes": (
            f'node_filesystem_avail_bytes{{hostname="{hostname}",'
            f'mountpoint="{mountpoint}"}}'
        ),
        "total_bytes": (
            f'node_filesystem_size_bytes{{hostname="{hostname}",'
            f'mountpoint="{mountpoint}"}}'
        ),
    }

    for api_url in PROMETHEUS_HOSTS:
        try:
            result = {}
            all_fetched = True
            for key, promql in queries.items():
                resp = requests.get(
                    f"{api_url}/api/v1/query",
                    params={"query": promql},
                    timeout=10
                )
                if resp.status_code != 200:
                    all_fetched = False
                    break
                data = resp.json()
                prom_results = data.get("data", {}).get("result", [])
                if prom_results:
                    raw_value = float(prom_results[0]["value"][1])
                    result[key] = f"{raw_value:.1f}" if key == "used_percent" else _bytes_to_human(int(raw_value))
                else:
                    result[key] = ""
            if all_fetched:
                return result
        except Exception as e:
            logger.warning(f"Prometheus query failed for {api_url}: {e}, trying next...")
            continue
    return {}


def get_current_cpu_usage(instance: str) -> dict:
    raw = _prom_query(
        f'100 - (avg by(instance) '
        f'(rate(node_cpu_seconds_total{{instance="{instance}",mode="idle"}}[2m]))'
        f' * 100)'
    )
    if raw:
        try:
            used = float(raw)
            return {"used_percent": f"{used:.1f}", "avail_percent": f"{100 - used:.1f}"}
        except Exception:
            pass
    return {}


def get_current_memory_usage(instance: str) -> dict:
    raw = _prom_query(
        f'100 - (node_memory_MemAvailable_bytes{{instance="{instance}"}}'
        f' / node_memory_MemTotal_bytes{{instance="{instance}"}} * 100)'
    )
    if raw:
        try:
            used = float(raw)
            return {"used_percent": f"{used:.1f}", "avail_percent": f"{100 - used:.1f}"}
        except Exception:
            pass
    return {}


# ──────────────────────────────────────────────
# Individual alert messages (<= 3 alerts)
# ──────────────────────────────────────────────

def build_individual_firing(alert: dict) -> dict:
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alertname   = labels.get("alertname", "Unknown")
    severity    = labels.get("severity", "info")
    hostname    = labels.get("hostname", "unknown")
    ipv4        = labels.get("ipv4", "unknown")
    project     = labels.get("project", "unknown")
    service     = labels.get("service", "unknown")
    phase       = labels.get("phase", "unknown")
    instance    = labels.get("instance", ipv4)
    starts_at   = alert.get("startsAt", "")
    fingerprint = alert.get("fingerprint", "")

    mountpoint   = labels.get("mountpoint", "")
    device       = labels.get("device", "")
    used_percent = annotations.get("used_percent", "")
    avail_bytes  = annotations.get("avail_bytes", "")
    total_bytes  = annotations.get("total_bytes", "")

    event_id      = generate_event_id(alertname, instance, starts_at, fingerprint)
    starts_at_kst = format_kst(starts_at)
    what_ko       = ALERT_WHAT_KO.get(alertname, alertname)

    emoji = SEVERITY_EMOJI.get(severity, "🔵")
    sev   = severity.upper()

    lines = [
        f"{emoji} [{sev}] {what_ko} — 발생",
        f"─────────────────────────",
        f"📌 Target",
        f"• Project    : {project}",
        f"• S-Code     : {service}",
        f"• Phase      : {phase}",
        f"• Hostname   : {hostname}",
        f"• Private_IP : {ipv4}",
    ]

    if mountpoint:
        lines += [
            f"─────────────────────────",
            f"💾 Disk",
            f"• Mountpoint : {mountpoint}",
            f"• Device     : {device}",
        ]
        if total_bytes:
            lines.append(f"• Total      : {total_bytes}")
        if used_percent:
            lines.append(f"• Used       : {used_percent}%")
        if avail_bytes:
            lines.append(f"• Available  : {avail_bytes}")
        top_dirs = get_top_dirs(hostname, mountpoint)
        if top_dirs:
            lines.append(f"─────────────────────────")
            lines.append(f"📂 Top Directories or Files")
            for path, size in top_dirs:
                lines.append(f"  • {path} : {size}")

    elif alertname == "CPUCritical":
        core_count = annotations.get("core_count", "")
        try:
            avail_percent = f"{100 - float(used_percent):.1f}"
        except Exception:
            avail_percent = annotations.get("avail_percent", "")
        try:
            total_c = float(core_count)
            used_c  = total_c * float(used_percent) / 100
            avail_c = total_c - used_c
            cores_used_str  = f"{used_c:.1f}"
            cores_avail_str = f"{avail_c:.1f}"
        except Exception:
            cores_used_str = cores_avail_str = ""

        lines += [f"─────────────────────────", f"🖥️  CPU"]
        if core_count:
            lines.append(f"• Total Cores : {core_count}")
        if used_percent:
            suffix = f"  ({cores_used_str} Cores)" if cores_used_str else ""
            lines.append(f"• Used        : {used_percent}%{suffix}")
        if avail_percent:
            suffix = f"  ({cores_avail_str} Cores)" if cores_avail_str else ""
            lines.append(f"• Available   : {avail_percent}%{suffix}")
        top_procs = get_top_procs(hostname, metric="cpu")
        procs = top_procs.get("procs", [])
        if procs:
            lines += [f"─────────────────────────", f"⚙️  Top Processes (CPU)"]
            for p in procs:
                cores_str = f"{p['cores_used']} Core" if p.get("cores_used") else ""
                lines.append(f"  • {p['user']:<10} PID:{p['pid']:<7} {p['percent']}%  {cores_str}  {p['command']}")

    elif alertname in ("MemoryCritical", "MemoryNearExhaustion"):
        mem_avail = annotations.get("avail_bytes", "")
        mem_total = annotations.get("total_bytes", "")
        lines += [f"─────────────────────────", f"🧠 Memory"]
        if mem_total:
            lines.append(f"• Total      : {mem_total}")
        if used_percent:
            lines.append(f"• Used       : {used_percent}%")
        if mem_avail:
            lines.append(f"• Available  : {mem_avail}")
        top_procs = get_top_procs(hostname, metric="mem")
        procs = top_procs.get("procs", [])
        if procs:
            lines += [f"─────────────────────────", f"⚙️  Top Processes (MEM)"]
            for p in procs:
                lines.append(f"  • {p['user']:<10} PID:{p['pid']:<7} {p['percent']}%  {p.get('mem_used','')}  {p['command']}")

    lines += [
        f"─────────────────────────",
        f"🔍 Event",
        f"• {what_ko}",
        f"─────────────────────────",
        f"🔖 Event_ID : {event_id}",
        f"🕐 Start_Time : {starts_at_kst} (KST)",
    ]

    return {"text": "\n".join(lines)}


def build_individual_resolved(alert: dict) -> dict:
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alertname   = labels.get("alertname", "Unknown")
    hostname    = labels.get("hostname", "unknown")
    ipv4        = labels.get("ipv4", "unknown")
    project     = labels.get("project", "unknown")
    service     = labels.get("service", "unknown")
    phase       = labels.get("phase", "unknown")
    instance    = labels.get("instance", ipv4)
    starts_at   = alert.get("startsAt", "")
    ends_at     = alert.get("endsAt", "")
    fingerprint = alert.get("fingerprint", "")

    mountpoint = labels.get("mountpoint", "")
    device     = labels.get("device", "")

    if mountpoint:
        live         = get_current_disk_usage(hostname, mountpoint)
        used_percent = live.get("used_percent") or annotations.get("used_percent", "")
        avail_bytes  = live.get("avail_bytes")  or annotations.get("avail_bytes", "")
        total_bytes  = live.get("total_bytes")  or annotations.get("total_bytes", "")
    else:
        used_percent = annotations.get("used_percent", "")
        avail_bytes  = annotations.get("avail_bytes", "")
        total_bytes  = annotations.get("total_bytes", "")

    event_id      = generate_event_id(alertname, instance, starts_at, fingerprint)
    starts_at_kst = format_kst(starts_at)
    ends_at_kst   = format_kst(ends_at)
    what_ko       = ALERT_WHAT_KO.get(alertname, alertname)

    lines = [
        f"🟢 [RESOLVED] {what_ko} — 해소",
        f"─────────────────────────",
        f"📌 Target",
        f"• Project    : {project}",
        f"• S-Code     : {service}",
        f"• Phase      : {phase}",
        f"• Hostname   : {hostname}",
        f"• Private_IP : {ipv4}",
    ]

    if mountpoint:
        lines += [f"─────────────────────────", f"💾 Disk", f"• Mountpoint : {mountpoint}", f"• Device     : {device}"]
        if total_bytes:
            lines.append(f"• Total      : {total_bytes}")
        if used_percent:
            lines.append(f"• Used       : {used_percent}%")
        if avail_bytes:
            lines.append(f"• Available  : {avail_bytes}")

    elif alertname == "CPUCritical":
        live = get_current_cpu_usage(instance)
        used_percent  = live.get("used_percent",  "")
        avail_percent = live.get("avail_percent", "")
        lines += [f"─────────────────────────", f"🖥️  CPU (해소 시점)"]
        if used_percent:
            lines.append(f"• Used      : {used_percent}%")
        if avail_percent:
            lines.append(f"• Available : {avail_percent}%")

    elif alertname in ("MemoryCritical", "MemoryNearExhaustion"):
        live = get_current_memory_usage(instance)
        used_percent  = live.get("used_percent",  "")
        avail_percent = live.get("avail_percent", "")
        lines += [f"─────────────────────────", f"🧠 Memory (해소 시점)"]
        if used_percent:
            lines.append(f"• Used      : {used_percent}%")
        if avail_percent:
            lines.append(f"• Available : {avail_percent}%")

    lines += [
        f"─────────────────────────",
        f"🔍 Event",
        f"• {what_ko}",
        f"─────────────────────────",
        f"🕐 Start_Time : {starts_at_kst} (KST)",
        f"🕐 End_Time   : {ends_at_kst} (KST)",
        f"─────────────────────────",
        f"🔖 Event_ID : {event_id}",
        f"─────────────────────────",
        f"✅ 해당 이슈가 해소되었습니다.",
    ]

    return {"text": "\n".join(lines)}


# ──────────────────────────────────────────────
# Mass alert messages (> 3 alerts)
# ──────────────────────────────────────────────

def build_mass_firing(alerts: list) -> list:
    grouped = defaultdict(list)
    for alert in alerts:
        alertname = alert.get("labels", {}).get("alertname", "Unknown")
        grouped[alertname].append(alert)

    messages = []
    for alertname, group_alerts in grouped.items():
        what_ko = ALERT_WHAT_KO.get(alertname, alertname)
        count   = len(group_alerts)
        tracker_set(alertname, total=count, resolved=0)

        lines = [
            f"🔴 [대규모 장애] {what_ko} — {count}개 VM 발생",
            f"─────────────────────────",
            f"🔍 Event",
            f"• {what_ko}",
            f"─────────────────────────",
            f"📋 Affected VM List",
        ]
        for a in group_alerts[:10]:
            hostname = a.get("labels", {}).get("hostname", "unknown")
            ipv4     = a.get("labels", {}).get("ipv4", "unknown")
            lines.append(f"  • {hostname} ({ipv4})")
        if count > 10:
            lines.append(f"  • ... 외 {count - 10}개")
        lines += [f"─────────────────────────", f"🕐 Start_Time : {now_kst()} (KST)"]
        messages.append({"text": "\n".join(lines)})

    return messages


def build_mass_resolved(alerts: list) -> list:
    grouped = defaultdict(list)
    for alert in alerts:
        alertname = alert.get("labels", {}).get("alertname", "Unknown")
        grouped[alertname].append(alert)

    messages = []
    for alertname, group_alerts in grouped.items():
        what_ko = ALERT_WHAT_KO.get(alertname, alertname)
        count   = len(group_alerts)

        entry     = tracker_increment_resolved(alertname, count)
        total     = entry.get("total", 0)
        resolved  = entry.get("resolved", 0)
        remaining = max(total - resolved, 0)
        fully_resolved = resolved >= total and total > 0

        if fully_resolved:
            header = f"🟢 [완전 복구] {what_ko} — 전체 {total}개 VM 모두 해결됨"
        elif total == 0:
            header = f"🟢 [복구] {what_ko} — {count}개 VM 해결됨 (재시작으로 인해 전체 수량 불명확)"
        else:
            header = f"🟡 [부분 복구] {what_ko} — {count}개 VM 해결됨 (전체 {total}개 중, 미복구 {remaining}개)"

        lines = [header, f"─────────────────────────", f"✅ 복구된 VM 목록"]
        for a in group_alerts[:10]:
            hostname  = a.get("labels", {}).get("hostname", "unknown")
            ipv4      = a.get("labels", {}).get("ipv4", "unknown")
            starts_at = format_kst(a.get("startsAt", ""))
            ends_at   = format_kst(a.get("endsAt", ""))
            lines.append(f"  • {hostname} ({ipv4})")
            lines.append(f"    장애: {starts_at} → {ends_at} (KST)")
        if count > 10:
            lines.append(f"  • ... 외 {count - 10}개")
        if not fully_resolved and total > 0:
            lines += [f"─────────────────────────", f"⏳ 미복구 VM : {remaining}개 아직 장애 중"]
        lines += [f"─────────────────────────", f"🕐 복구 시각 : {now_kst()} (KST)"]

        if fully_resolved:
            tracker_reset(alertname)

        messages.append({"text": "\n".join(lines)})

    return messages


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.post("/alert/kakaowork")
async def kakaowork_alert(request: Request):
    payload = await request.json()
    alerts  = payload.get("alerts", [])
    status  = payload.get("status", "firing")

    firing_alerts   = [a for a in alerts if a.get("status", status) == "firing"]
    resolved_alerts = [a for a in alerts if a.get("status", status) == "resolved"]

    messages = []

    if firing_alerts:
        # record stats for each fired alert
        for alert in firing_alerts:
            stats_record_fired(
                alertname   = alert.get("labels", {}).get("alertname", "Unknown"),
                hostname    = alert.get("labels", {}).get("hostname", "unknown"),
                instance    = alert.get("labels", {}).get("instance", ""),
                starts_at   = alert.get("startsAt", ""),
                fingerprint = alert.get("fingerprint", ""),
            )
        if len(firing_alerts) > 3:
            messages.extend(build_mass_firing(firing_alerts))
        else:
            for alert in firing_alerts:
                messages.append(build_individual_firing(alert))

    if resolved_alerts:
        # record stats for each resolved alert
        for alert in resolved_alerts:
            stats_record_resolved(
                alertname   = alert.get("labels", {}).get("alertname", "Unknown"),
                hostname    = alert.get("labels", {}).get("hostname", "unknown"),
                instance    = alert.get("labels", {}).get("instance", ""),
                starts_at   = alert.get("startsAt", ""),
                ends_at     = alert.get("endsAt", ""),
                fingerprint = alert.get("fingerprint", ""),
            )
        if len(resolved_alerts) > 3:
            messages.extend(build_mass_resolved(resolved_alerts))
        else:
            for alert in resolved_alerts:
                messages.append(build_individual_resolved(alert))

    for message in messages:
        try:
            resp = requests.post(KAKAOWORK_WEBHOOK_URL, json=message, timeout=5)
            logger.info(f"KakaoWork response: {resp.status_code} — {resp.text}")
        except Exception as e:
            logger.error(f"Failed to send to KakaoWork: {e}")

    return JSONResponse(content={"status": "ok"})


@app.get("/healthz")
def healthz():
    tracker = load_tracker()
    return {"status": "ok", "outage_tracker": tracker}



def _compute_stats_from_events(events: list) -> dict:
    # Compute all statistics directly from events — no counters, no drift.

    summary      = {c: {"fired": 0, "resolved": 0, "total_duration_seconds": 0}
                    for c in CATEGORIES + ["OTHER"]}
    by_alertname = {}
    by_host      = {}

    for e in events:
        cat  = e.get("category", "OTHER")
        name = e.get("alertname", "Unknown")
        host = e.get("hostname",  "unknown")
        typ  = e.get("type", "")

        if cat not in summary:
            summary[cat] = {"fired": 0, "resolved": 0, "total_duration_seconds": 0}

        if name not in by_alertname:
            by_alertname[name] = {"fired": 0, "resolved": 0, "category": cat}
        if host not in by_host:
            by_host[host] = {"fired": 0, "resolved": 0}

        if typ == "fired":
            summary[cat]["fired"]       += 1
            by_alertname[name]["fired"] += 1
            by_host[host]["fired"]      += 1
        elif typ == "resolved":
            summary[cat]["resolved"]                   += 1
            summary[cat]["total_duration_seconds"]     += e.get("duration_seconds", 0)
            by_alertname[name]["resolved"]             += 1
            by_host[host]["resolved"]                  += 1

    return {"summary": summary, "by_alertname": by_alertname, "by_host": by_host}

@app.get("/stats/summary")
def get_summary():
    with stats_lock:
        stats = _load_stats()
    events = stats.get("events", [])

    # Compute from events directly
    computed  = _compute_stats_from_events(events)
    summary   = computed["summary"]
    total_fired    = sum(v["fired"]    for v in summary.values())
    total_resolved = sum(v["resolved"] for v in summary.values())

    # Still Active from sequential pairing — consistent with /stats/active
    grouped = defaultdict(list)
    for e in events:
        key = f"{e.get('alertname')}|{e.get('hostname')}"
        grouped[key].append(e)
    still_active = 0
    for key, evts in grouped.items():
        evts_sorted = sorted(evts, key=lambda x: x.get("starts_at", x.get("logged_at", "")))
        pending = []
        for e in evts_sorted:
            if e.get("type") == "fired":
                pending.append(e)
            elif e.get("type") == "resolved" and pending:
                pending.pop(0)
        still_active += len(pending)

    still_active   = max(0, still_active)
    total_fired    = max(0, total_fired)
    total_resolved = max(0, total_resolved)

    return JSONResponse(content=[
        {"metric": "Total Fired",    "value": total_fired},
        {"metric": "Total Resolved", "value": total_resolved},
        {"metric": "Still Active",   "value": still_active},
    ])


@app.get("/stats/by_category")
def get_by_category():
    with stats_lock:
        stats = _load_stats()
    events   = stats.get("events", [])
    computed = _compute_stats_from_events(events)
    summary  = computed["summary"]
    return JSONResponse(content=[
        {
            "category": cat,
            "fired":    summary.get(cat, {}).get("fired", 0),
            "resolved": summary.get(cat, {}).get("resolved", 0),
        }
        for cat in ["VOL", "MEM", "CPU", "CON"]
    ])




@app.get("/stats/active")
def get_active(
    from_date: str = Query(None, alias="from", description="Start date YYYY-MM-DD (KST)"),
    to_date:   str = Query(None, alias="to",   description="End date YYYY-MM-DD (KST)"),
):
    with stats_lock:
        events = _load_stats().get("events", [])

    # date filter
    if from_date:
        try:
            try:
                from_dt = datetime.strptime(from_date[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=KST)
            except ValueError:
                from_dt = datetime.strptime(from_date[:10], "%Y-%m-%d").replace(tzinfo=KST)
            events = [e for e in events if datetime.fromisoformat(e.get("logged_at", "")).astimezone(KST) >= from_dt]
        except Exception:
            pass
    if to_date:
        try:
            try:
                to_dt = datetime.strptime(to_date[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=KST)
            except ValueError:
                to_dt = datetime.strptime(to_date[:10], "%Y-%m-%d").replace(tzinfo=KST) + timedelta(days=1)
            events = [e for e in events if datetime.fromisoformat(e.get("logged_at", "")).astimezone(KST) < to_dt]
        except Exception:
            pass

    # Group events by alertname + hostname, sorted by starts_at
    from collections import defaultdict
    grouped = defaultdict(list)
    for e in events:
        key = f"{e.get('alertname')}|{e.get('hostname')}"
        grouped[key].append(e)

    # Sort each group by starts_at ascending (fallback to logged_at)
    for key in grouped:
        grouped[key].sort(key=lambda x: x.get("starts_at", x.get("logged_at", "")))

    active = []
    for key, evts in grouped.items():
        # Walk through events in order, matching each fired with the next resolved
        pending_fired = []
        for e in evts:
            if e.get("type") == "fired":
                pending_fired.append(e)
            elif e.get("type") == "resolved" and pending_fired:
                # Match with the oldest unmatched fired
                pending_fired.pop(0)

        # Any remaining unmatched fired events are still active
        for e in pending_fired:
            try:
                dt = datetime.fromisoformat(e.get("starts_at", "").replace("Z", "+00:00"))
                started_kst = dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                started_kst = e.get("starts_at", "")
            event_id = generate_event_id(
                e.get("alertname", ""),
                e.get("instance", e.get("hostname", "")),
                e.get("starts_at", ""),
                e.get("fingerprint", "")
            )
            active.append({
                "category":  e.get("category", ""),
                "alertname": e.get("alertname", ""),
                "hostname":  e.get("hostname", ""),
                "started":   started_kst,
                "event_id":  event_id,
            })

    active.sort(key=lambda x: x["started"], reverse=True)
    return JSONResponse(content={"count": len(active), "active": active})

@app.get("/stats")
def get_stats(category: str = Query(None, description="Filter by category: VOL, MEM, CPU, CON, OTHER")):
    with stats_lock:
        stats = _load_stats()

    events = stats.get("events", [])

    # Compute all stats directly from events — no counter drift possible
    computed     = _compute_stats_from_events(events)
    summary      = computed["summary"]
    by_alertname = computed["by_alertname"]
    by_host      = computed["by_host"]

    # top 10 hosts by fired count
    top_hosts = sorted(by_host.items(), key=lambda x: x[1].get("fired", 0), reverse=True)[:10]
    top_hosts = [{"hostname": h, **v} for h, v in top_hosts]

    # average duration per category (seconds)
    avg_duration = {}
    for cat, data in summary.items():
        resolved = data.get("resolved", 0)
        total_s  = data.get("total_duration_seconds", 0)
        avg_duration[cat] = round(total_s / resolved, 1) if resolved > 0 else 0

    if category:
        cat = category.upper()
        filtered_alertnames = {k: v for k, v in by_alertname.items() if v.get("category") == cat}
        filtered_hosts = {}
        for ev in events:
            if ev.get("category") == cat:
                h = ev.get("hostname", "")
                if h not in filtered_hosts:
                    filtered_hosts[h] = {"fired": 0, "resolved": 0}
                if ev["type"] == "fired":
                    filtered_hosts[h]["fired"] += 1
                else:
                    filtered_hosts[h]["resolved"] += 1
        top_cat_hosts = sorted(filtered_hosts.items(), key=lambda x: x[1]["fired"], reverse=True)[:10]

        return JSONResponse(content={
            "category":         cat,
            "summary":          summary.get(cat, {}),
            "avg_duration_sec": avg_duration.get(cat, 0),
            "by_alertname":     filtered_alertnames,
            "top_hosts":        [{"hostname": h, **v} for h, v in top_cat_hosts],
        })

    return JSONResponse(content={
        "summary":          summary,
        "avg_duration_sec": avg_duration,
        "by_alertname":     by_alertname,
        "top_hosts":        top_hosts,
        "total_events":     len(events),
    })


@app.get("/stats/events")
def get_events(
    category:   str = Query(None, description="Filter: VOL, MEM, CPU, CON, OTHER"),
    alertname:  str = Query(None, description="Filter by alertname"),
    hostname:   str = Query(None, description="Filter by hostname"),
    event_type: str = Query(None, description="Filter: fired or resolved"),
    limit:      int = Query(50,   description="Max events to return (default 50, max 200)"),
    from_date:  str = Query(None, alias="from", description="Start date YYYY-MM-DD (KST)"),
    to_date:    str = Query(None, alias="to",   description="End date YYYY-MM-DD (KST)"),
):
    limit = min(limit, 200)
    with stats_lock:
        events = _load_stats().get("events", [])

    # date filter
    if from_date:
        try:
            try:
                from_dt = datetime.strptime(from_date[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=KST)
            except ValueError:
                from_dt = datetime.strptime(from_date[:10], "%Y-%m-%d").replace(tzinfo=KST)
            events = [e for e in events if datetime.fromisoformat(e.get("logged_at", "")).astimezone(KST) >= from_dt]
        except Exception:
            pass
    if to_date:
        try:
            try:
                to_dt = datetime.strptime(to_date[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=KST)
            except ValueError:
                to_dt = datetime.strptime(to_date[:10], "%Y-%m-%d").replace(tzinfo=KST) + timedelta(days=1)
            events = [e for e in events if datetime.fromisoformat(e.get("logged_at", "")).astimezone(KST) < to_dt]
        except Exception:
            pass

    if category:
        events = [e for e in events if e.get("category") == category.upper()]
    if alertname:
        events = [e for e in events if e.get("alertname") == alertname]
    if hostname:
        events = [e for e in events if e.get("hostname") == hostname]
    if event_type:
        events = [e for e in events if e.get("type") == event_type.lower()]

    events = list(reversed(events))[:limit]
    return JSONResponse(content={"count": len(events), "events": events})


@app.post("/stats/reset")
def reset_stats(category: str = Query(None, description="Reset specific category only, or omit to reset all")):
    # Resets alert statistics. Use with caution.

    with stats_lock:
        stats = _load_stats()
        if category:
            cat = category.upper()
            if cat in stats["summary"]:
                stats["summary"][cat] = {"fired": 0, "resolved": 0, "total_duration_seconds": 0}
            # remove matching alertnames and events
            stats["by_alertname"] = {
                k: v for k, v in stats["by_alertname"].items()
                if v.get("category") != cat
            }
            stats["events"] = [e for e in stats["events"] if e.get("category") != cat]
            logger.info(f"[stats] reset category: {cat}")
        else:
            stats = {
                "summary": {c: {"fired": 0, "resolved": 0, "total_duration_seconds": 0} for c in CATEGORIES + ["OTHER"]},
                "by_alertname": {},
                "by_host": {},
                "events": []
            }
            logger.info("[stats] full reset")
        _save_stats(stats)

    return JSONResponse(content={"status": "ok", "reset": category.upper() if category else "all"})
"""


# ──────────────────────────────────────────────
# Write files locally
# ──────────────────────────────────────────────

local_am_config = Path('/home/scv/alertmanager.yml')
local_webhook   = Path('/home/scv/kakao_webhook.py')

local_am_config.write_text(ALERTMANAGER_CONFIG)
print(f"Generated: {local_am_config}")

local_webhook.write_text(KAKAO_WEBHOOK_CODE)
print(f"Generated: {local_webhook}")


# ──────────────────────────────────────────────
# Validate alertmanager.yml before deploying
# ──────────────────────────────────────────────

first_server = ALERTMANAGER_SERVERS[0]
if not validate_alertmanager_config(
    ALERTMANAGER_CONFIG,
    hostname=first_server,
    username=am_user,
    password=password,
):
    raise RuntimeError(
        "Aborting deploy — alertmanager.yml failed amtool check-config validation."
    )


# ──────────────────────────────────────────────
# Deploy to both AlertManager servers
# ──────────────────────────────────────────────

for server in ALERTMANAGER_SERVERS:
    print(f"\n── Deploying to {server} ──")

    ensure_remote_dir(server, REMOTE_BASE_DIR, am_user, password)

    send_file_to_server(
        local_path=str(local_am_config),
        remote_path=REMOTE_AM_CONFIG,
        hostname=server,
        username=am_user,
        password=password,
    )
    reload_alertmanager(hostname=server, port=39093)

    send_file_to_server(
        local_path=str(local_webhook),
        remote_path=REMOTE_WEBHOOK,
        hostname=server,
        username=am_user,
        password=password,
    )
    run_remote_command(server, am_user, password, f'chmod +x {REMOTE_WEBHOOK}')
    print(f"  Set executable: {REMOTE_WEBHOOK}")

    stats_file = f'{REMOTE_BASE_DIR}/alert_stats.json'
    run_remote_command(
        server, am_user, password,
        f'sudo touch {stats_file} && sudo chmod 664 {stats_file} && sudo chown {am_user}:{am_user} {stats_file}'
    )
    print(f"  Ensured stats file: {stats_file}")

    print(f"  Restarting kakao-webhook.service on {server}...")
    _systemctl(server, am_user, password, 'kakao-webhook.service', 'restart')

    time.sleep(2)
    _systemctl(server, am_user, password, 'kakao-webhook.service', 'status')

print("\nDone.")