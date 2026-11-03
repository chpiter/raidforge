"""RAIDFORGE - a Textual TUI for RAID management and auditable disk erasure.

Runs inside a Debian 12 live environment, discovers the installed RAID
controller family, enumerates physical disks and volumes, manages RAID
configuration, and submits erasure commands to the controller while recording
cryptographic evidence.
"""

__version__ = "1.1.0"

__all__ = ["__version__"]
