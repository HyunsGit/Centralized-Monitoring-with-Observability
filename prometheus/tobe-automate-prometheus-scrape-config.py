#!/usr/bin/python3

import re
import time
import random
import subprocess
import tempfile
import os
import paramiko
import requests
import json
from pathlib import Path
from dotenv import dotenv_values
from tqdm import tqdm


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def to_bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() == 'true'
    return False


def load_project_id_map(filename):
    mapping = {}
    pattern = re.compile(r'([a-zA-Z0-9_-]+)-id="([a-f0-9]+)"')
    with open(filename, 'r') as f:
        for line in f:
            m = pattern.search(line)
            if m:
                mapping[m.group(2)] = m.group(1)
    return mapping


OVERRIDE_TO_INVALID = {
    # Infra-managed servers excluded from phase-based alerting
    "prometheus-01",
    "prometheus-02",
    "alertmanager-01",
    "alertmanager-02",
    "grafana-01",
    "bastion",
    "ntp-01",
    "ansible-01",
    "gitlab",
    "gitlab-runner-01",
    "github-runner-01",
    "github-runner-02",
    # Add project-specific exclusions here
    # "project-db-01",
}


HIGH_THRESHOLD_HOSTS = {
    # Hosts that operate at higher-than-normal disk usage
    # (mail servers, NAS, sandbox workers, etc.)
    # These hosts skip the standard 90%/95% thresholds
    # and only alert at 99% (DiskSpaceImminent)
    # "mail-was-01",
    # "mail-nas-01",
    # "sandbox-worker-01",
}
HIGH_THRESHOLD_HOSTS_REGEX = "|".join(HIGH_THRESHOLD_HOSTS)


def get_phase(hostname):
    phase = _detect_phase(hostname)
    if phase == 'prod' and hostname.lower() in OVERRIDE_TO_INVALID:
        return 'invalid'
    return phase


def _detect_phase(hostname):
    hostname = hostname.lower()
    if 'sbox' in hostname or 'sandbox' in hostname:
        return 'sbox'
    elif 'prod' in hostname:
        return 'prod'
    elif 'dev' in hostname:
        return 'dev'
    elif 'stg' in hostname or 'stage' in hostname:
        return 'stg'
    elif 'cbt' in hostname:
        return 'cbt'
    else:
        return 'prod'


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


def batch_check_ports(prom_host, prom_user, targets,
                      password=None, key_filepath=None,
                      ssh_port=22, timeout=10):
    targets = list(targets)
    if not targets:
        return set()

    parts = [
        f'(nc -z -w2 {ip} {port} && echo "OK {ip} {port}" || echo "FAIL {ip} {port}") &'
        for ip, port in targets
    ]
    cmd = ' '.join(parts) + ' wait'

    try:
        ssh = _open_ssh(prom_host, prom_user,
                        password=password, key_filepath=key_filepath,
                        port=ssh_port, timeout=timeout)
        try:
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
            output = stdout.read().decode(errors='replace')
        finally:
            ssh.close()
    except Exception as e:
        print(f"[batch_check_ports] SSH session failed for {prom_host}: {e}")
        return set()

    reachable = set()
    for line in output.splitlines():
        parts_line = line.strip().split()
        if len(parts_line) == 3 and parts_line[0] == 'OK':
            try:
                reachable.add((parts_line[1], int(parts_line[2])))
            except ValueError:
                pass
    return reachable


def send_file_to_server(local_path, remote_path, hostname,
                        port=22, username=None, password=None, key_filepath=None):
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if key_filepath:
        key = paramiko.RSAKey.from_private_key_file(key_filepath)
        ssh_client.connect(hostname, port=port, username=username, pkey=key)
    else:
        ssh_client.connect(hostname, port=port, username=username, password=password)
    sftp = ssh_client.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
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


def reload_prometheus(hostname, port=9090, use_auth=False, username=None, password=None):
    reload_url = f"http://{hostname}:{port}/-/reload"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        if use_auth:
            from requests.auth import HTTPBasicAuth
            response = requests.post(reload_url, headers=headers,
                                     auth=HTTPBasicAuth(username, password), timeout=15)
        else:
            response = requests.post(reload_url, headers=headers, timeout=15)
        if response.status_code == 200:
            print(f"  Prometheus reloaded successfully on {hostname}.")
        else:
            print(f"  Prometheus reload failed on {hostname}. "
                  f"Status: {response.status_code}, Body: {response.text}")
    except Exception as e:
        print(f"  Prometheus reload request failed for {hostname}: {e}")


def validate_rules(content: str, prom_host: str, prom_user: str, password: str) -> bool:
    try:
        ssh = _open_ssh(prom_host, prom_user, password=password)
        sftp = ssh.open_sftp()
        remote_tmp = '/tmp/validate_rules_tmp.yml'
        with sftp.open(remote_tmp, 'w') as f:
            f.write(content)
        sftp.close()
        stdin, stdout, stderr = ssh.exec_command(f'promtool check rules {remote_tmp}')
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode()
        err = stderr.read().decode()
        ssh.exec_command(f'rm -f {remote_tmp}')
        ssh.close()
        if exit_code != 0:
            print("Rules validation FAILED:")
            print(out)
            print(err)
            return False
        print("Rules validation passed.")
        return True
    except Exception as e:
        print(f"  Warning: Could not validate rules on {prom_host}: {e}")
        return True


def register_silence_for_new_vms(new_hostnames: list, alertmanager_urls: list,
                                  duration_minutes: int = 5, am_user: str = "scv",
                                  password: str = ""):
    """Register Alertmanager silence for newly provisioned VMs."""
    if not new_hostnames:
        return
    from datetime import datetime, timezone, timedelta
    now     = datetime.now(timezone.utc)
    ends_at = now + timedelta(minutes=duration_minutes)

    for hostname in new_hostnames:
        payload = {
            "matchers": [
                {"name": "hostname", "value": hostname, "isRegex": False}
            ],
            "startsAt": now.isoformat(),
            "endsAt":   ends_at.isoformat(),
            "createdBy": "automate_prometheus",
            "comment":  f"New VM provisioning silence ({duration_minutes}min) — {hostname}",
        }
        for am_url in alertmanager_urls:
            try:
                resp = requests.post(
                    f"{am_url}/api/v2/silences",
                    json=payload,
                    timeout=10,
                    auth=requests.auth.HTTPBasicAuth(am_user, password) if password else None,
                )
                if resp.status_code == 200:
                    silence_id = resp.json().get("silenceID", "")
                    print(f"  Silence registered: {hostname} ({duration_minutes}min) → {am_url} [{silence_id}]")
                else:
                    print(f"  Silence failed for {hostname} on {am_url}: {resp.status_code} {resp.text}")
            except Exception as e:
                print(f"  Silence error for {hostname} on {am_url}: {e}")



    try:
        ssh = _open_ssh(prom_host, prom_user, password=password)
        sftp = ssh.open_sftp()
        remote_tmp = '/tmp/validate_rules_tmp.yml'
        with sftp.open(remote_tmp, 'w') as f:
            f.write(content)
        sftp.close()
        stdin, stdout, stderr = ssh.exec_command(f'promtool check rules {remote_tmp}')
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode()
        err = stderr.read().decode()
        ssh.exec_command(f'rm -f {remote_tmp}')
        ssh.close()
        if exit_code != 0:
            print("Rules validation FAILED:")
            print(out)
            print(err)
            return False
        print("Rules validation passed.")
        return True
    except Exception as e:
        print(f"  Warning: Could not validate rules on {prom_host}: {e}")
        return True


# ──────────────────────────────────────────────
# Load credentials
# ──────────────────────────────────────────────

dotenv_token_path = Path('/etc/mgt-api/.tokens')
_raw_tokens = dotenv_values(dotenv_token_path)
tokens_dict = {
    k: v for k, v in _raw_tokens.items()
    if v and v.startswith('gAAAAA')
}

ssh_cred_path = Path('/etc/mgt-api/.ssh_credentials')
ssh_creds = dotenv_values(ssh_cred_path)

prom_user = ssh_creds.get('SSH_USER', 'scv')
password = ssh_creds.get('SSH_PASSWORD')
if not password:
    raise ValueError(
        "SSH_PASSWORD not found in /etc/mgt-api/.ssh_credentials — "
        "add SSH_USER and SSH_PASSWORD to that file and retry."
    )

key_filepath = None
project_id_map = load_project_id_map('/etc/mgt-api/.project_id')


# ──────────────────────────────────────────────
# Fetch instances from KakaoCloud API
# ──────────────────────────────────────────────

BASE_LIST_URL = 'https://api.your-cloud.example.com/api/v1/instances'
combined_instances = []
MAX_RETRIES = 3

for token_key, token_value in tokens_dict.items():
    if not token_value:
        continue
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Auth-Token': token_value
    }
    all_instances = []
    limit, offset = 50, 0
    while True:
        params = {'limit': limit, 'offset': offset}
        retry_count = 0
        resp = None
        while retry_count < MAX_RETRIES:
            try:
                resp = requests.get(BASE_LIST_URL, headers=headers, params=params, timeout=30)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait_sec = int(retry_after) if retry_after else min(2 ** retry_count + random.uniform(0, 1), 32)
                    print(f"Rate limited. Waiting {wait_sec:.1f}s (attempt {retry_count+1}/{MAX_RETRIES})...")
                    time.sleep(wait_sec)
                    retry_count += 1
                    continue
                resp.raise_for_status()
                break
            except Exception as e:
                retry_count += 1
                if retry_count >= MAX_RETRIES:
                    print(f"Giving up on page offset={offset} for token {token_key}: {e}")
                    break
                wait_sec = min(2 ** retry_count + random.uniform(0, 1), 32)
                print(f"Retrying in {wait_sec:.1f}s (attempt {retry_count}/{MAX_RETRIES}): {e}")
                time.sleep(wait_sec)
        if resp is None or resp.status_code != 200:
            break
        data = resp.json()
        instances = data.get('instances', [])
        if not instances:
            break
        all_instances.extend(instances)
        offset += len(instances)
        total = data.get('total', 0)
        if total and offset >= total:
            break
    combined_instances.extend(all_instances)


# ──────────────────────────────────────────────
# Safety guard
# ──────────────────────────────────────────────

MIN_EXPECTED_INSTANCES = 10

if not combined_instances:
    raise RuntimeError(
        "No instances returned from KakaoCloud API. "
        "Check API tokens in /etc/mgt-api/.tokens and network connectivity. "
        "Aborting to prevent overwriting valid target files with empty content."
    )

if len(combined_instances) < MIN_EXPECTED_INSTANCES:
    raise RuntimeError(
        f"Only {len(combined_instances)} instances found — suspiciously low "
        f"(expected at least {MIN_EXPECTED_INSTANCES}). "
        "Check if any API tokens have expired. "
        "Aborting to prevent overwriting valid target files."
    )

print(f"Fetched {len(combined_instances)} instances from KakaoCloud API.")


# ──────────────────────────────────────────────
# Build port-check target list
# ──────────────────────────────────────────────

total_volume_size_map = {}
for inst in combined_instances:
    volumes = inst.get('attached_volumes', [])
    total_volume_size_map[inst.get('id', '')] = sum(v.get('size', 0) for v in volumes)

valid_instances = [
    inst for inst in combined_instances
    if inst.get('addresses') and inst['addresses'][0].get('private_ip')
]

prom_host = 'prometheus-01'

primary_targets = []
for inst in valid_instances:
    ip = inst['addresses'][0]['private_ip']
    port = 59100 if inst.get('is_k8se', False) else 39100
    primary_targets.append((ip, port))

print(f"Running batch port check for {len(primary_targets)} instances via {prom_host}...")
reachable = batch_check_ports(
    prom_host, prom_user, primary_targets,
    password=password, key_filepath=key_filepath
)
print(f"  {len(reachable)} / {len(primary_targets)} ports reachable.")

k8s_fallback_targets = [
    (inst['addresses'][0]['private_ip'], 9100)
    for inst in valid_instances
    if inst.get('is_k8se', False)
    and (inst['addresses'][0]['private_ip'], 59100) not in reachable
]

reachable_fallback = set()
if k8s_fallback_targets:
    print(f"Running fallback port-9100 check for {len(k8s_fallback_targets)} k8s instances...")
    reachable_fallback = batch_check_ports(
        prom_host, prom_user, k8s_fallback_targets,
        password=password, key_filepath=key_filepath
    )
    print(f"  {len(reachable_fallback)} / {len(k8s_fallback_targets)} fallback ports reachable.")


# ──────────────────────────────────────────────
# Build target file data
# ──────────────────────────────────────────────

file_sd_data_node_exporter = []
file_sd_data_blackbox = []
file_sd_data_ssh = []

for inst in tqdm(valid_instances, desc="Building target entries"):
    ip = inst['addresses'][0]['private_ip']
    is_k8se = inst.get('is_k8se', False)

    if is_k8se:
        if (ip, 59100) in reachable:
            addr_node = f"{ip}:59100"
        elif (ip, 9100) in reachable_fallback:
            addr_node = f"{ip}:9100"
        else:
            print(f"  Warning: no reachable exporter port for k8s node {ip}, defaulting to 59100")
            addr_node = f"{ip}:59100"
    else:
        if (ip, 39100) not in reachable:
            print(f"  Warning: no reachable exporter port for vm node {ip}, defaulting to 39100")
        addr_node = f"{ip}:39100"

    hostname = inst.get('name', '')
    phase_value = get_phase(hostname)
    is_k8se_label = 'k8s' if to_bool(is_k8se) else 'vm'
    is_hyper_threading_label = 'activated' if to_bool(inst.get('is_hyper_threading', False)) else 'deactivated'
    project_value = inst.get('project_id', '')
    converted_project = project_id_map.get(project_value, project_value)
    security_groups = inst.get('security_groups', [])
    security_group_names = [sg.get('name', '') for sg in security_groups if 'name' in sg]
    security_group_str = ','.join(security_group_names) if security_group_names else ''
    vm_state = inst.get('vm_state', '').lower()
    vm_state_label = 'active' if vm_state == 'active' else 'deactivated'
    volume_size = total_volume_size_map.get(inst.get('id', ''), 0)

    labels = {
        'domain': 'example',
        'project': str(converted_project),
        'phase': str(phase_value),
        'hostname': str(hostname),
        'ipv4': str(ip),
        'service': str(inst.get('description', '')),
        'type': str(is_k8se_label),
        'hyper_thread': str(is_hyper_threading_label),
        'security_group': str(security_group_str),
        'vm_state': str(vm_state_label),
        'total_volume_size': str(volume_size)
    }

    file_sd_data_node_exporter.append({'targets': [addr_node], 'labels': labels})
    file_sd_data_blackbox.append({'targets': [ip], 'labels': labels})
    file_sd_data_ssh.append({'targets': [f"{ip}:22"], 'labels': labels})


# ──────────────────────────────────────────────
# Write target JSON files locally
# ──────────────────────────────────────────────

output_path_node = Path('/home/scv/targets_node_exporter.json')
output_path_blackbox = Path('/home/scv/targets_blackbox_icmp.json')
output_path_ssh = Path('/home/scv/targets_blackbox_ssh.json')

with output_path_node.open('w') as f:
    json.dump(file_sd_data_node_exporter, f, indent=2)

with output_path_blackbox.open('w') as f:
    json.dump(file_sd_data_blackbox, f, indent=2)

with output_path_ssh.open('w') as f:
    json.dump(file_sd_data_ssh, f, indent=2)

print(f"\nGenerated:")
print(f"  {output_path_node}  ({len(file_sd_data_node_exporter)} targets)")
print(f"  {output_path_blackbox}  ({len(file_sd_data_blackbox)} targets)")
print(f"  {output_path_ssh}  ({len(file_sd_data_ssh)} targets)")


# ──────────────────────────────────────────────
# Prometheus annotation templates
# ──────────────────────────────────────────────

# Disk annotations
DISK_USED_PCT = '{{ $value | printf "%.1f" }}'
DISK_AVAIL = (
    '{{ with query (print "node_filesystem_avail_bytes{instance=\\"" '
    '.Labels.instance "\\",mountpoint=\\"" .Labels.mountpoint "\\"}") }}'
    '{{ . | first | value | humanize1024 }}{{ end }}'
)
DISK_TOTAL = (
    '{{ with query (print "node_filesystem_size_bytes{instance=\\"" '
    '.Labels.instance "\\",mountpoint=\\"" .Labels.mountpoint "\\"}") }}'
    '{{ . | first | value | humanize1024 }}{{ end }}'
)

# Inode annotations
INODE_USED_PCT = '{{ $value | printf "%.1f" }}'
INODE_AVAIL = (
    '{{ with query (print "node_filesystem_files_free{instance=\\"" '
    '.Labels.instance "\\",mountpoint=\\"" .Labels.mountpoint "\\"}") }}'
    '{{ . | first | value | humanize }}{{ end }}'
)
INODE_TOTAL = (
    '{{ with query (print "node_filesystem_files{instance=\\"" '
    '.Labels.instance "\\",mountpoint=\\"" .Labels.mountpoint "\\"}") }}'
    '{{ . | first | value | humanize }}{{ end }}'
)

# CPU annotations
CPU_USED_PCT = '{{ $value | printf "%.1f" }}'
CPU_AVAIL_PCT = (
    '{{ with query (print "100 - (avg by(instance) '
    '(rate(node_cpu_seconds_total{instance=\\"" .Labels.instance '
    '"\\" ,mode=\\"idle\\"}[2m])) * 100)") }}'
    '{{ . | first | value | printf "%.1f" }}{{ end }}'
)
CPU_CORE_COUNT = (
    '{{ with query (print "count by(instance) '
    '(node_cpu_seconds_total{instance=\\"" .Labels.instance '
    '"\\" ,mode=\\"idle\\"})") }}'
    '{{ . | first | value | printf "%.0f" }}{{ end }}'
)

# Memory annotations — MemoryCritical
# $value = used% (100 - avail/total * 100), so used_percent is $value directly
MEM_USED_PCT = '{{ $value | printf "%.1f" }}'
MEM_AVAIL_BYTES = (
    '{{ with query (print "node_memory_MemAvailable_bytes'
    '{instance=\\"" .Labels.instance "\\"}") }}'
    '{{ . | first | value | humanize1024 }}{{ end }}'
)
MEM_TOTAL_BYTES = (
    '{{ with query (print "node_memory_MemTotal_bytes'
    '{instance=\\"" .Labels.instance "\\"}") }}'
    '{{ . | first | value | humanize1024 }}{{ end }}'
)

# Memory annotations — MemoryNearExhaustion
# $value = avail/total ratio (e.g. 0.037), so used% = (1 - $value) * 100
# Memory annotations — MemoryNearExhaustion
# $value = avail/total ratio (e.g. 0.037)
# used_percent is queried directly from Prometheus instead of computed in template
MEM_NEAR_USED_PCT = (
    '{{ with query (print "100 - (node_memory_MemAvailable_bytes'
    '{instance=\\"" .Labels.instance "\\"} / node_memory_MemTotal_bytes'
    '{instance=\\"" .Labels.instance "\\"} * 100)") }}'
    '{{ . | first | value | printf "%.1f" }}{{ end }}'
)


# ──────────────────────────────────────────────
# Write alert rules file locally
# ──────────────────────────────────────────────

rules_content = f"""\
groups:
  - name: blackbox_connectivity
    rules:

      # ── sshd service not active ───────────────────────────────────
      - alert: SSHDServiceDown
        expr: |
          node_systemd_unit_state{{job="public", name="sshd.service", state="active"}} == 0
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "[CRITICAL] sshd stopped: {{{{ $labels.hostname }}}}"
          description: |
            sshd.service is not active on this host.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            ▶ Check: journalctl -u sshd -n 50 --no-pager

      # ── SSH down, ICMP up ─────────────────────────────────────────
      - alert: SSHDown
        expr: |
          probe_success{{job="blackbox_ssh"}} == 0
          unless on(instance)
          probe_success{{job="blackbox_icmp"}} == 0
        for: 0s
        labels:
          severity: critical
          probe_type: ssh
        annotations:
          summary: "[CRITICAL] SSH down, host reachable: {{{{ $labels.hostname }}}}"
          description: |
            SSH probe failed but ICMP ping is succeeding.
            The host is UP — SSH service is the problem.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Domain  : {{{{ $labels.domain }}}}
            Phase   : {{{{ $labels.phase }}}}
            ▶ Possible causes:
              - sshd crashed (check SSHDServiceDown alert)
              - OOM killed sshd (check SSHDownCausedByOOM alert)
              - sshd config changed and failed to reload
              - Firewall rule added blocking port 22
            ▶ Commands:
              systemctl status sshd
              journalctl -u sshd -n 50 --no-pager
              journalctl -k | grep -i oom

      # ── Both SSH and ICMP down ────────────────────────────────────
      - alert: HostDown
        expr: |
          probe_success{{job="blackbox_ssh"}} == 0
          and on(instance)
          probe_success{{job="blackbox_icmp"}} == 0
        for: 0s
        labels:
          severity: critical
          probe_type: host
        annotations:
          summary: "[CRITICAL] Host down (no ping, no SSH): {{{{ $labels.hostname }}}}"
          description: |
            Both SSH and ICMP probes are failing.
            Host is completely unreachable from Prometheus.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Domain  : {{{{ $labels.domain }}}}
            SG      : {{{{ $labels.security_group }}}}
            ▶ Possible causes:
              - VM powered off or suspended
              - Kernel panic / hard crash
              - Network-level outage (check other VMs in same segment)
              - Security group rule blocking all traffic
            ▶ Check cloud console for VM state.

      # ── SSH down caused by OOM ────────────────────────────────────
      - alert: SSHDownCausedByOOM
        expr: |
          probe_success{{job="blackbox_ssh"}} == 0
          and on(instance)
          increase(node_vmstat_oom_kill{{job="public"}}[10m]) > 0
        for: 0m
        labels:
          severity: critical
          probe_type: ssh
          root_cause: oom
        annotations:
          summary: "[CRITICAL] SSH down — OOM confirmed: {{{{ $labels.hostname }}}}"
          description: |
            SSH is down AND the OOM killer fired within the last 10 minutes.
            Root cause is almost certainly memory exhaustion.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            ▶ Confirmed cause: OOM kill
            ▶ Check: journalctl -k | grep -i "oom|killed process"
            ▶ After recovery: review memory usage, consider adding swap
              or increasing VM memory allocation.

  - name: cpu_memory_monitoring
    rules:

      # ── CPU critical (>95%) ───────────────────────────────────────
      - alert: CPUCritical
        expr: |
          100 - (avg by(instance, hostname, ipv4, project, service, phase)
            (rate(node_cpu_seconds_total{{job="public", mode="idle"}}[2m]))
          * 100) > 95
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "[CRITICAL] CPU 사용량 위험: {{{{ $labels.hostname }}}}"
          description: |
            CPU 사용량이 95%를 초과했습니다. 즉시 프로세스 점검이 필요합니다.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            ▶ Check: top -bn1 | head -20 && ps aux --sort=-%cpu | head -20
          used_percent: '{CPU_USED_PCT}'
          avail_percent: '{CPU_AVAIL_PCT}'
          core_count: '{CPU_CORE_COUNT}'

      # ── Memory critical (>95%) ────────────────────────────────────
      - alert: MemoryCritical
        expr: |
          100 - (node_memory_MemAvailable_bytes{{job="public"}}
          / node_memory_MemTotal_bytes{{job="public"}} * 100) > 95
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "[CRITICAL] 메모리 사용량 위험: {{{{ $labels.hostname }}}}"
          description: |
            메모리 사용량이 95%를 초과했습니다. OOM Kill 임박. 즉시 조치가 필요합니다.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            ▶ Check: free -h && ps aux --sort=-%mem | head -20
          used_percent: '{MEM_USED_PCT}'
          avail_bytes: '{MEM_AVAIL_BYTES}'
          total_bytes: '{MEM_TOTAL_BYTES}'

      # ── Memory near exhaustion (>90% used) ───────────────────────
      - alert: MemoryNearExhaustion
        expr: |
          100 - (node_memory_MemAvailable_bytes{{job="public"}}
          / node_memory_MemTotal_bytes{{job="public"}} * 100) > 90
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "[WARNING] 메모리 임계치 초과 (90% 초과): {{{{ $labels.hostname }}}}"
          description: |
            메모리 사용량이 90%를 초과했습니다. 지속 증가 시 OOM Kill 위험.
            Host    : {{{{ $labels.hostname }}}}
            IP      : {{{{ $labels.ipv4 }}}}
            Project : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            ▶ Check: free -h && ps aux --sort=-%mem | head -20
          used_percent: '{MEM_USED_PCT}'
          avail_bytes: '{MEM_AVAIL_BYTES}'
          total_bytes: '{MEM_TOTAL_BYTES}'

  - name: disk_monitoring
    rules:

      # ── Disk space warning (>90%) ─────────────────────────────────
      - alert: DiskSpaceWarning
        expr: |
          (
            node_filesystem_size_bytes{{job="public"}} - node_filesystem_avail_bytes{{job="public"}}
          ) / node_filesystem_size_bytes{{job="public"}} * 100 > 90
          and node_filesystem_readonly{{job="public"}} == 0
          and node_filesystem_size_bytes{{job="public",
            hostname!~"{HIGH_THRESHOLD_HOSTS_REGEX}",
            mountpoint!~"/run.*|/boot/efi|/efi|/var/lib/lxcfs",
            device!~"tmpfs|shm|none|lxcfs|overlay",
            mountpoint!~".*/sandboxes/.*"
          }}
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "[WARNING] 디스크 사용량 경고: {{{{ $labels.hostname }}}}"
          description: |
            디스크 사용량이 90%를 초과했습니다.
            Host       : {{{{ $labels.hostname }}}}
            IP         : {{{{ $labels.ipv4 }}}}
            Project    : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Mountpoint : {{{{ $labels.mountpoint }}}}
            Device     : {{{{ $labels.device }}}}
            ▶ Check: df -h {{{{ $labels.mountpoint }}}}
            ▶ Find large files: du -sh {{{{ $labels.mountpoint }}}}/* | sort -rh | head -20
          used_percent: '{DISK_USED_PCT}'
          avail_bytes: '{DISK_AVAIL}'
          total_bytes: '{DISK_TOTAL}'

      # ── Disk space critical (>95%) ────────────────────────────────
      - alert: DiskSpaceCritical
        expr: |
          (
            node_filesystem_size_bytes{{job="public"}} - node_filesystem_avail_bytes{{job="public"}}
          ) / node_filesystem_size_bytes{{job="public"}} * 100 > 95
          and node_filesystem_readonly{{job="public"}} == 0
          and node_filesystem_size_bytes{{job="public",
            hostname!~"{HIGH_THRESHOLD_HOSTS_REGEX}",
            mountpoint!~"/run.*|/boot/efi|/efi|/var/lib/lxcfs",
            device!~"tmpfs|shm|none|lxcfs|overlay",
            mountpoint!~".*/sandboxes/.*"
          }}
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "[CRITICAL] 디스크 사용량 위험: {{{{ $labels.hostname }}}}"
          description: |
            디스크 사용량이 95%를 초과했습니다. 즉시 조치가 필요합니다.
            Host       : {{{{ $labels.hostname }}}}
            IP         : {{{{ $labels.ipv4 }}}}
            Project    : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Mountpoint : {{{{ $labels.mountpoint }}}}
            Device     : {{{{ $labels.device }}}}
            ▶ Check: df -h {{{{ $labels.mountpoint }}}}
            ▶ Find large files: du -sh {{{{ $labels.mountpoint }}}}/* | sort -rh | head -20
          used_percent: '{DISK_USED_PCT}'
          avail_bytes: '{DISK_AVAIL}'
          total_bytes: '{DISK_TOTAL}'

      # ── Disk space imminent (>99%) ────────────────────────────────
      - alert: DiskSpaceImminent
        expr: |
          (
            node_filesystem_size_bytes{{job="public"}} - node_filesystem_avail_bytes{{job="public"}}
          ) / node_filesystem_size_bytes{{job="public"}} * 100 > 99
          and node_filesystem_readonly{{job="public"}} == 0
          and node_filesystem_size_bytes{{job="public",
            hostname!~"{HIGH_THRESHOLD_HOSTS_REGEX}",
            mountpoint!~"/run.*|/boot/efi|/efi|/var/lib/lxcfs",
            device!~"tmpfs|shm|none|lxcfs|overlay",
            mountpoint!~".*/sandboxes/.*"
          }}
        for: 30s
        labels:
          severity: imminent
        annotations:
          summary: "[IMMINENT] 디스크 사용량 긴급: {{{{ $labels.hostname }}}}"
          description: |
            디스크 사용량이 99%를 초과했습니다. 즉각 조치하지 않으면 서비스가 중단됩니다.
            Host       : {{{{ $labels.hostname }}}}
            IP         : {{{{ $labels.ipv4 }}}}
            Project    : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Mountpoint : {{{{ $labels.mountpoint }}}}
            Device     : {{{{ $labels.device }}}}
            ▶ Check: df -h {{{{ $labels.mountpoint }}}}
            ▶ Find large files: du -sh {{{{ $labels.mountpoint }}}}/* | sort -rh | head -20
          used_percent: '{DISK_USED_PCT}'
          avail_bytes: '{DISK_AVAIL}'
          total_bytes: '{DISK_TOTAL}'

      # ── Disk space imminent for high-usage VMs (>99%) ─────────────
      - alert: DiskSpaceImminent
        expr: |
          (
            node_filesystem_size_bytes{{job="public"}} - node_filesystem_avail_bytes{{job="public"}}
          ) / node_filesystem_size_bytes{{job="public"}} * 100 > 99
          and node_filesystem_readonly{{job="public"}} == 0
          and node_filesystem_size_bytes{{job="public",
            hostname=~"{HIGH_THRESHOLD_HOSTS_REGEX}",
            mountpoint!~"/run.*|/boot/efi|/efi|/var/lib/lxcfs",
            device!~"tmpfs|shm|none|lxcfs|overlay",
            mountpoint!~".*/sandboxes/.*"
          }}
        for: 30s
        labels:
          severity: imminent
        annotations:
          summary: "[IMMINENT] 디스크 사용량 긴급: {{{{ $labels.hostname }}}}"
          description: |
            디스크 사용량이 99%를 초과했습니다. 즉각 조치하지 않으면 서비스가 중단됩니다.
            Host       : {{{{ $labels.hostname }}}}
            IP         : {{{{ $labels.ipv4 }}}}
            Project    : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Mountpoint : {{{{ $labels.mountpoint }}}}
            Device     : {{{{ $labels.device }}}}
            ▶ Check: df -h {{{{ $labels.mountpoint }}}}
            ▶ Find large files: du -sh {{{{ $labels.mountpoint }}}}/* | sort -rh | head -20
          used_percent: '{DISK_USED_PCT}'
          avail_bytes: '{DISK_AVAIL}'
          total_bytes: '{DISK_TOTAL}'

      # ── Inode warning (>80%) ──────────────────────────────────────
      - alert: InodeWarning
        expr: |
          (
            node_filesystem_files{{job="public"}} - node_filesystem_files_free{{job="public"}}
          ) / node_filesystem_files{{job="public"}} * 100 > 80
          and node_filesystem_readonly{{job="public"}} == 0
          and node_filesystem_files{{job="public",
            mountpoint!~"/run.*|/boot/efi|/efi|/var/lib/lxcfs",
            device!~"tmpfs|shm|none|lxcfs|overlay",
            mountpoint!~".*/sandboxes/.*"
          }} > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "[WARNING] 아이노드 사용량 경고: {{{{ $labels.hostname }}}}"
          description: |
            아이노드 사용량이 80%를 초과했습니다.
            디스크 여유 공간이 있어도 파일 생성이 불가할 수 있습니다.
            Host       : {{{{ $labels.hostname }}}}
            IP         : {{{{ $labels.ipv4 }}}}
            Project    : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Mountpoint : {{{{ $labels.mountpoint }}}}
            Device     : {{{{ $labels.device }}}}
            ▶ Check: df -i {{{{ $labels.mountpoint }}}}
            ▶ Find inode hogs: df -i {{{{ $labels.mountpoint }}}}
          used_percent: '{INODE_USED_PCT}'
          avail_bytes: '{INODE_AVAIL}'
          total_bytes: '{INODE_TOTAL}'

      # ── Inode critical (>90%) ─────────────────────────────────────
      - alert: InodeCritical
        expr: |
          (
            node_filesystem_files{{job="public"}} - node_filesystem_files_free{{job="public"}}
          ) / node_filesystem_files{{job="public"}} * 100 > 90
          and node_filesystem_readonly{{job="public"}} == 0
          and node_filesystem_files{{job="public",
            mountpoint!~"/run.*|/boot/efi|/efi|/var/lib/lxcfs",
            device!~"tmpfs|shm|none|lxcfs|overlay",
            mountpoint!~".*/sandboxes/.*"
          }} > 0
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "[CRITICAL] 아이노드 사용량 위험: {{{{ $labels.hostname }}}}"
          description: |
            아이노드 사용량이 90%를 초과했습니다. 즉시 조치가 필요합니다.
            Host       : {{{{ $labels.hostname }}}}
            IP         : {{{{ $labels.ipv4 }}}}
            Project    : {{{{ $labels.project }}}} / {{{{ $labels.service }}}}
            Mountpoint : {{{{ $labels.mountpoint }}}}
            Device     : {{{{ $labels.device }}}}
            ▶ Check: df -i {{{{ $labels.mountpoint }}}}
            ▶ Find inode hogs: df -i {{{{ $labels.mountpoint }}}}
          used_percent: '{INODE_USED_PCT}'
          avail_bytes: '{INODE_AVAIL}'
          total_bytes: '{INODE_TOTAL}'
"""

rules_local_path = Path('/home/scv/blackbox_alerts.yml')
rules_local_path.write_text(rules_content)
print(f"Generated: {rules_local_path}")

if not validate_rules(rules_content, prom_host='prometheus-01', prom_user=prom_user, password=password):
    raise RuntimeError("Aborting deploy — rules file failed promtool validation")


# ──────────────────────────────────────────────
# Detect newly added VMs and register silence
# ──────────────────────────────────────────────

ALERTMANAGER_URLS = [
    "http://alertmanager-01.internal.example.com:39093",
    "http://alertmanager-02.internal.example.com:39093",
]
REMOTE_TARGET_PATH = '/data/etc/prometheus/targets_node_exporter.json'

# Load existing hostnames from Prometheus-01 (before overwriting)
existing_hostnames = set()
try:
    ssh = _open_ssh('prometheus-01', prom_user, password=password)
    sftp = ssh.open_sftp()
    with sftp.open(REMOTE_TARGET_PATH, 'r') as f:
        existing_targets = json.load(f)
    sftp.close()
    ssh.close()
    for entry in existing_targets:
        h = entry.get("labels", {}).get("hostname", "")
        if h:
            existing_hostnames.add(h)
    print(f"Existing targets: {len(existing_hostnames)} hostnames loaded from Prometheus")
except Exception as e:
    print(f"  Warning: Could not load existing targets for new VM detection: {e}")

# Find newly added hostnames
new_hostnames = [
    entry.get("labels", {}).get("hostname", "")
    for entry in file_sd_data_node_exporter
    if entry.get("labels", {}).get("hostname", "") not in existing_hostnames
    and entry.get("labels", {}).get("hostname", "")
]

if new_hostnames:
    print(f"\nDetected {len(new_hostnames)} new VM(s): {new_hostnames}")
    register_silence_for_new_vms(
        new_hostnames    = new_hostnames,
        alertmanager_urls= ALERTMANAGER_URLS,
        duration_minutes = 5,
        am_user          = prom_user,
        password         = password,
    )
else:
    print("No new VMs detected — skipping silence registration")


# ──────────────────────────────────────────────
# Deploy to both Prometheus servers
# ──────────────────────────────────────────────

PROMETHEUS_SERVERS = ['prometheus-01', 'prometheus-02']

for prom_server in PROMETHEUS_SERVERS:
    print(f"\n── Deploying to {prom_server} ──")

    ensure_remote_dir(prom_server, '/data/etc/prometheus/rules', prom_user, password)

    send_file_to_server(
        local_path=str(output_path_node),
        remote_path='/data/etc/prometheus/targets_node_exporter.json',
        hostname=prom_server,
        username=prom_user,
        password=password,
    )
    send_file_to_server(
        local_path=str(output_path_blackbox),
        remote_path='/data/etc/prometheus/targets_blackbox_icmp.json',
        hostname=prom_server,
        username=prom_user,
        password=password,
    )
    send_file_to_server(
        local_path=str(output_path_ssh),
        remote_path='/data/etc/prometheus/targets_blackbox_ssh.json',
        hostname=prom_server,
        username=prom_user,
        password=password,
    )
    send_file_to_server(
        local_path=str(rules_local_path),
        remote_path='/data/etc/prometheus/rules/blackbox_alerts.yml',
        hostname=prom_server,
        username=prom_user,
        password=password,
    )

    reload_prometheus(hostname=prom_server, port=9090, use_auth=False)

print("\nDone.")