#!/usr/bin/env python3
"""
Prime Jake — QNAP Storage Bridge

Provides direct access to the QNAP NAS (192.168.68.84) with 45.9TB free on
ZFS19_DATA. Uses SSH/SCP for file operations (no root needed) and sshfs for
mount (if available).

Usage:
    python3 qnap-bridge.py ls                    # List rig-blackwell dir
    python3 qnap-bridge.py put <local> <remote>  # Upload file
    python3 qnap-bridge.py get <remote> <local>  # Download file
    python3 qnap-bridge.py mkdir <path>          # Create directory
    python3 qnap-bridge.py df                    # Show free space
    python3 qnap-bridge.py mount                 # Mount via sshfs to ~/qnap
"""
from __future__ import annotations
import os, sys, subprocess, argparse
from pathlib import Path
from datetime import datetime

QNAP_IP = "192.168.68.84"
QNAP_USER = "admin"
SSH_KEY = os.path.expanduser("~/.ssh/rig_id_ed25519")
BASE_PATH = "/share/ZFS19_DATA/rig-blackwell"
LOCAL_MOUNT = Path.home() / "qnap"

def ssh_cmd(cmd: str) -> str:
    """Run a command on the QNAP via SSH."""
    result = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"{QNAP_USER}@{QNAP_IP}", cmd],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout.strip()

def scp_put(local: str, remote: str) -> bool:
    """Upload a file to QNAP."""
    remote_full = f"{QNAP_USER}@{QNAP_IP}:{BASE_PATH}/{remote}"
    result = subprocess.run(
        ["scp", "-i", SSH_KEY, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         local, remote_full],
        capture_output=True, text=True, timeout=300
    )
    return result.returncode == 0

def scp_get(remote: str, local: str) -> bool:
    """Download a file from QNAP."""
    remote_full = f"{QNAP_USER}@{QNAP_IP}:{BASE_PATH}/{remote}"
    result = subprocess.run(
        ["scp", "-i", SSH_KEY, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         remote_full, local],
        capture_output=True, text=True, timeout=300
    )
    return result.returncode == 0

def cmd_ls(path: str = ""):
    """List files on QNAP."""
    target = f"{BASE_PATH}/{path}" if path else BASE_PATH
    output = ssh_cmd(f"ls -lah {target}")
    print(f"QNAP: {target}")
    print(output)

def cmd_put(local: str, remote: str):
    """Upload file."""
    if not os.path.exists(local):
        print(f"Error: {local} not found")
        sys.exit(1)
    print(f"Uploading {local} → qnap://{remote} ...")
    if scp_put(local, remote):
        size = os.path.getsize(local)
        print(f"✓ Uploaded {size:,} bytes")
    else:
        print("✗ Upload failed")

def cmd_get(remote: str, local: str):
    """Download file."""
    print(f"Downloading qnap://{remote} → {local} ...")
    if scp_get(remote, local):
        size = os.path.getsize(local) if os.path.exists(local) else 0
        print(f"✓ Downloaded {size:,} bytes")
    else:
        print("✗ Download failed")

def cmd_mkdir(path: str):
    """Create directory on QNAP."""
    output = ssh_cmd(f"mkdir -p {BASE_PATH}/{path} && echo 'created'")
    print(f"✓ {output}")

def cmd_df():
    """Show free space on QNAP."""
    output = ssh_cmd("df -h /share/ZFS19_DATA")
    print(f"QNAP storage (ZFS19_DATA — 45.9TB pool):\n{output}")
    # Also show all pools
    output2 = ssh_cmd("zpool list 2>/dev/null")
    print(f"\nAll ZFS pools:\n{output2}")

def cmd_mount():
    """Mount QNAP via sshfs to ~/qnap."""
    LOCAL_MOUNT.mkdir(parents=True, exist_ok=True)
    # Check if already mounted
    if os.path.ismount(str(LOCAL_MOUNT)):
        print(f"✓ Already mounted at {LOCAL_MOUNT}")
        return
    # Try sshfs command
    result = subprocess.run(
        ["sshfs", "-o", f"IdentityFile={SSH_KEY},reconnect,ServerAliveInterval=15",
         f"{QNAP_USER}@{QNAP_IP}:{BASE_PATH}", str(LOCAL_MOUNT)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"✓ SSHFS mounted at {LOCAL_MOUNT}")
        print(f"  {LOCAL_MOUNT} → {QNAP_USER}@{QNAP_IP}:{BASE_PATH}")
    else:
        print(f"✗ SSHFS mount failed: {result.stderr.strip()}")
        print(f"  Install with: sudo apt install sshfs")
        print(f"  Or use SCP: python3 {__file__} put/get")

def main():
    parser = argparse.ArgumentParser(description="Prime Jake — QNAP Storage Bridge")
    parser.add_argument("command", choices=["ls", "put", "get", "mkdir", "df", "mount"])
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()

    if args.command == "ls":
        cmd_ls(args.args[0] if args.args else "")
    elif args.command == "put":
        if len(args.args) < 2:
            print("Usage: put <local-file> <remote-path>")
            sys.exit(1)
        cmd_put(args.args[0], args.args[1])
    elif args.command == "get":
        if len(args.args) < 2:
            print("Usage: get <remote-path> <local-file>")
            sys.exit(1)
        cmd_get(args.args[0], args.args[1])
    elif args.command == "mkdir":
        if not args.args:
            print("Usage: mkdir <path>")
            sys.exit(1)
        cmd_mkdir(args.args[0])
    elif args.command == "df":
        cmd_df()
    elif args.command == "mount":
        cmd_mount()

if __name__ == "__main__":
    main()
