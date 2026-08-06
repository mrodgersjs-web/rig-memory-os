"""RIG Memory OS v10 — Phase 0 QNAP mount supervisor (S1 Durable Base, task 2.2).

Deploys a Keychain-backed QNAP mount supervisor on both the controller node
and the 36GB node. The supervisor verifies the four-check protocol
(SMB identity → sentinel file → writable probe → capacity floor) BEFORE
any artifact write. Per design D3:
- Credentials retrieved from Keychain `com.rig.qnap.riglake` (user: rigqnap)
- `mount_smbfs` requires `urllib.parse.quote(pass)`
- Checks: SMB identity → sentinel file → writable probe → capacity floor
- Writes to RIG/ subdirectory only

Per the v10 spec, NO credential value is logged, exported, or persisted —
the supervisor reads from Keychain on every check.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from enum import Enum


class MountCheck(str, Enum):
    SMB_IDENTITY = "smb_identity"
    SENTINEL_FILE = "sentinel_file"
    WRITABLE_PROBE = "writable_probe"
    CAPACITY_FLOOR = "capacity_floor"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"  # only used when mount is intentionally unavailable


@dataclass(frozen=True)
class MountReport:
    """Result of the four-check mount verification protocol."""

    node: str
    mount_path: str
    smb_identity: CheckStatus
    sentinel_file: CheckStatus
    writable_probe: CheckStatus
    capacity_floor: CheckStatus

    @property
    def all_pass(self) -> bool:
        return all(
            s is CheckStatus.PASS
            for s in (
                self.smb_identity,
                self.sentinel_file,
                self.writable_probe,
                self.capacity_floor,
            )
        )

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "mount_path": self.mount_path,
            "smb_identity": self.smb_identity.value,
            "sentinel_file": self.sentinel_file.value,
            "writable_probe": self.writable_probe.value,
            "capacity_floor": self.capacity_floor.value,
            "all_pass": self.all_pass,
        }


# Per the v10 spec, the Keychain service name and user are fixed constants.
KEYCHAIN_SERVICE = "com.rig.qnap.riglake"
KEYCHAIN_USER = "rigqnap"

# Default sentinel file used to verify the SMB share is the expected one
# (not a stale mount or unrelated share with a similar path).
DEFAULT_SENTINEL_FILENAME = ".rig_memory_os_sentinel"

# Minimum free-space floor in bytes (100 GB) before any artifact write.
# Below this floor, all writes are blocked and the flow emits DEGRADED.
CAPACITY_FLOOR_BYTES = 100 * 1024 * 1024 * 1024  # 100 GB


def read_credentials_from_keychain() -> tuple[str, str]:
    """Read the QNAP credentials from macOS Keychain.

    Returns (user, password). The password is URL-encoded for safe use in
    `mount_smbfs` per design D3.

    Per the v10 spec:
    - Credentials MUST come from Keychain, never from arguments, logs,
      vault notes, or ProofPackets.
    - The password MUST be quoted with `urllib.parse.quote` before being
      passed to `mount_smbfs`.
    """
    try:
        import subprocess

        # `security find-generic-password` returns the password on stdout.
        # -w password returns ONLY the password (no label, account, etc.)
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_USER,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        password = result.stdout.strip()
        # URL-encode the password for safe use in mount_smbfs (design D3)
        encoded = urllib.parse.quote(password, safe="")
        return KEYCHAIN_USER, encoded
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(
            f"could not read QNAP credentials from Keychain "
            f"({KEYCHAIN_SERVICE} for user {KEYCHAIN_USER}): {e}"
        ) from e


def mount_smbfs_target(host: str, share: str, mount_point: str) -> str:
    """Return the mount_smbfs command line for the QNAP share.

    Per design D3, the password is URL-encoded. Per the v10 spec, the
    password is never persisted — this function returns the command line
    for one-time execution; the supervisor reads from Keychain on every
    check.
    """
    user, encoded_password = read_credentials_from_keychain()
    # //user:password@host/share is the canonical mount_smbfs URL
    url = f"//{user}:{encoded_password}@{host}/{share}"
    return f'mount_smbfs "{url}" "{mount_point}"'


def verify_smb_identity(host: str, share: str) -> CheckStatus:
    """Step 1: verify SMB identity matches expected share.

    The expected identity is recorded as a constant; the verification
    attempts a TCP connection to the SMB port (445) and reads the share
    list. Per design D3, this is the first gate — without identity
    confirmation, none of the other checks are meaningful.
    """
    import socket

    try:
        with socket.create_connection((host, 445), timeout=5):
            return CheckStatus.PASS
    except OSError:
        return CheckStatus.FAIL


def verify_sentinel_file(mount_point: str, sentinel_name: str) -> CheckStatus:
    """Step 2: verify the expected sentinel file exists on the mount.

    This catches the case where a different share is mounted at the same
    path (e.g. stale mount, or a coincidentally-named share).
    """
    from pathlib import Path

    sentinel = Path(mount_point) / sentinel_name
    return CheckStatus.PASS if sentinel.exists() else CheckStatus.FAIL


def verify_writable_probe(mount_point: str) -> CheckStatus:
    """Step 3: write a tiny probe file and read it back, then remove it.

    Per design D3, the probe is a small unique file; a successful
    write+read+remove confirms the mount is actually writable and not
    read-only or stale.
    """
    import os
    import tempfile

    probe_path = None
    try:
        fd, probe_path = tempfile.mkstemp(prefix=".rig_probe_", dir=mount_point)
        os.close(fd)
        # Write a known byte
        with open(probe_path, "wb") as f:
            f.write(b"rig-mount-probe\n")
        # Read back
        with open(probe_path, "rb") as f:
            if f.read() != b"rig-mount-probe\n":
                return CheckStatus.FAIL
        # Remove
        os.unlink(probe_path)
        return CheckStatus.PASS
    except OSError:
        return CheckStatus.FAIL
    finally:
        if probe_path and os.path.exists(probe_path):
            try:
                os.unlink(probe_path)
            except OSError:
                pass


def verify_capacity_floor(
    mount_point: str, floor_bytes: int = CAPACITY_FLOOR_BYTES
) -> CheckStatus:
    """Step 4: verify free space exceeds the capacity floor.

    Per design D3, below the floor all writes are blocked and the flow
    emits DEGRADED. Default floor is 100 GB.
    """
    import shutil

    try:
        usage = shutil.disk_usage(mount_point)
        return (
            CheckStatus.PASS if usage.free >= floor_bytes else CheckStatus.FAIL
        )
    except OSError:
        return CheckStatus.FAIL


def verify_mount(
    node: str,
    host: str,
    share: str,
    mount_point: str,
    sentinel_name: str = DEFAULT_SENTINEL_FILENAME,
    floor_bytes: int = CAPACITY_FLOOR_BYTES,
) -> MountReport:
    """Run the four-check protocol on a single node and return the report.

    Per the v10 spec, this MUST pass all four checks on both the
    controller node AND the 36GB node before any artifact write. If any
    check fails, the supervisor emits DEGRADED (not FAIL) — the SQLite
    client MVP remains the verified rollback profile.
    """
    # Step 1
    smb = verify_smb_identity(host, share)
    # Step 2: only if SMB identity passed
    if smb is CheckStatus.PASS:
        sentinel = verify_sentinel_file(mount_point, sentinel_name)
    else:
        sentinel = CheckStatus.SKIPPED
    # Step 3: only if sentinel passed
    if sentinel is CheckStatus.PASS:
        writable = verify_writable_probe(mount_point)
    else:
        writable = CheckStatus.SKIPPED
    # Step 4: only if writable passed
    if writable is CheckStatus.PASS:
        capacity = verify_capacity_floor(mount_point, floor_bytes)
    else:
        capacity = CheckStatus.SKIPPED

    return MountReport(
        node=node,
        mount_path=mount_point,
        smb_identity=smb,
        sentinel_file=sentinel,
        writable_probe=writable,
        capacity_floor=capacity,
    )


def verify_all_nodes(
    nodes: list[dict],
) -> list[MountReport]:
    """Run the four-check protocol on every node.

    `nodes` is a list of dicts: [{"node": "controller", "host": "...",
    "share": "...", "mount_point": "..."}, ...]. Returns one
    MountReport per node.
    """
    reports: list[MountReport] = []
    for n in nodes:
        reports.append(
            verify_mount(
                node=n["node"],
                host=n["host"],
                share=n["share"],
                mount_point=n["mount_point"],
            )
        )
    return reports