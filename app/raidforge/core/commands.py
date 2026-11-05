"""Erase command construction (constraint 7).

This module is deliberately **pure**: it maps a :class:`~raidforge.core.models.DiskInfo`
onto a fully quoted argv list and never executes anything.  That keeps the dry
run shown to the operator byte-identical to what will later be submitted, and
makes the mapping trivially unit testable.

Command table
-------------
The per-vendor command strings live in :data:`METHOD_TABLE` / :data:`GENERIC_TABLE`
and are the single place to correct syntax for a given firmware generation.
Rows marked ``VERIFY`` are best-effort reconstructions: because we did not have
the authoritative command table, please validate them against the target
controller firmware before relying on them, then fix the one offending
template - nothing else in the codebase needs to change.

Method names follow the contract:

* HDD  -> ``thorough`` / ``overwrite`` / ``erase``
* SSD  -> ``blockerase`` / ``sanitize``
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..config import Config
from .models import CommandPlan, DiskInfo, Medium, Protocol, Vendor

HDD_METHODS: Tuple[str, ...] = ("thorough", "overwrite", "erase")
SSD_METHODS: Tuple[str, ...] = ("blockerase", "sanitize")

# Default policy per media: strongest safe choice that every controller accepts.
DEFAULT_METHOD: Dict[str, str] = {
    Medium.HDD.value: "thorough",
    Medium.SSD.value: "sanitize",
}

# (vendor, method) -> argv template.  Placeholders see the field list below.
#   {c}   controller index (0,1,2...)
#   {e}   enclosure id     {s} slot id
#   {ch}  arcconf channel  {dev} arcconf device number OR /dev node for generic
#   {ref} ssacli physicaldrive reference, e.g. "1I:1:1"
METHOD_TABLE: Dict[Tuple[str, str], List[str]] = {
    # ---------------- LSI / Broadcom ----------------
    ("lsi", "thorough"): ["storcli", "/c{c}/e{e}/s{s}", "start", "erase", "thorough"],
    ("lsi", "overwrite"): ["storcli", "/c{c}/e{e}/s{s}", "start", "erase", "simple"],
    ("lsi", "erase"): ["storcli", "/c{c}/e{e}/s{s}", "start", "erase", "normal"],
    # VERIFY: SED capable drives expose `secure`; non-SED SAS SSDs only accept
    # the pattern based erase that maps onto BLOCK ERASE EXT.
    ("lsi", "blockerase"): ["storcli", "/c{c}/e{e}/s{s}", "start", "erase", "patterna"],
    ("lsi", "sanitize"): ["storcli", "/c{c}/e{e}/s{s}", "start", "erase", "secure"],
    # ---------------- PMC / Microsemi ----------------
    ("pmc", "thorough"): ["arcconf", "TASK", "START", "{c}", "DEVICE", "{ch}", "{dev}", "erase", "NOPROMPT"],
    ("pmc", "overwrite"): ["arcconf", "TASK", "START", "{c}", "DEVICE", "{ch}", "{dev}", "secureerase", "NOPROMPT"],
    ("pmc", "erase"): ["arcconf", "TASK", "START", "{c}", "DEVICE", "{ch}", "{dev}", "erase", "NOPROMPT"],
    # VERIFY: `sanitize` requires MaxSAS-class firmware; older cards only ship
    # `secureerase`, which is why that doubles as the SSD default fallback.
    ("pmc", "blockerase"): ["arcconf", "TASK", "START", "{c}", "DEVICE", "{ch}", "{dev}", "secureerase", "NOPROMPT"],
    ("pmc", "sanitize"): ["arcconf", "TASK", "START", "{c}", "DEVICE", "{ch}", "{dev}", "sanitize", "NOPROMPT"],
    # ---------------- HPE Smart Array ----------------
    # Syntax per the HPE Smart Array CLI reference:
    #   <target> modify [erasepattern=zero|random_zero|random_random_zero|crypto|block|overwrite]
    #                   [unrestricted=on|off]
    # `block` is SSD-only, `overwrite` is HDD-only, `crypto` needs SED support,
    # and there is no bare `sanitize` verb - the SSD sanitize path *is*
    # erasepattern=block.  `unrestricted` defaults to off, so it is left out.
    ("hpe", "thorough"): ["ssacli", "ctrl", "slot={c}", "pd", "{ref}", "modify", "erase", "erasepattern=overwrite", "forced"],
    ("hpe", "overwrite"): ["ssacli", "ctrl", "slot={c}", "pd", "{ref}", "modify", "erase", "erasepattern=random_random_zero", "forced"],
    ("hpe", "erase"): ["ssacli", "ctrl", "slot={c}", "pd", "{ref}", "modify", "erase", "forced"],
    ("hpe", "blockerase"): ["ssacli", "ctrl", "slot={c}", "pd", "{ref}", "modify", "erase", "erasepattern=block", "forced"],
    # VERIFY: crypto erase only works on SED/self-encrypting drives; on a plain
    # SSD the operator must pick blockerase instead.
    ("hpe", "sanitize"): ["ssacli", "ctrl", "slot={c}", "pd", "{ref}", "modify", "erase", "erasepattern=crypto", "forced"],
    # ---------------- Huawei HiRAID ----------------
    # `mode=simple|normal|thorough` maps onto the LSI-style erase strengths;
    # sanitize only applies to physical drives (SED for cryptoerase).
    ("huawei", "thorough"): ["hiraidadm", "c{c}:e{e}:s{s}", "start", "erase", "mode=thorough"],
    ("huawei", "overwrite"): ["hiraidadm", "c{c}:e{e}:s{s}", "start", "erase", "mode=simple"],
    ("huawei", "erase"): ["hiraidadm", "c{c}:e{e}:s{s}", "start", "erase", "mode=normal"],
    ("huawei", "blockerase"): ["hiraidadm", "c{c}:e{e}:s{s}", "start", "sanitize", "mode=blockerase"],
    # VERIFY: cryptoerase requires a SED; plain SSDs must use blockerase.
    ("huawei", "sanitize"): ["hiraidadm", "c{c}:e{e}:s{s}", "start", "sanitize", "mode=cryptoerase"],
}

# Generic path is keyed by transport protocol because the tool set differs.
GENERIC_TABLE: Dict[Tuple[str, str], List[str]] = {
    # VERIFY: nvme-cli exposes both `nvme sanitize` and `nvme format --ses=N`.
    # We use the format path because its session semantics are stable across
    # drives; --ses=1 is user data erase, --ses=2 cryptographic erase.
    ("nvme", "sanitize"): ["nvme", "format", "{dev}", "--force", "--ses=2"],
    ("nvme", "blockerase"): ["nvme", "format", "{dev}", "--force", "--ses=1"],
    ("sata", "thorough"): [
        "hdparm",
        "--yes-i-know-what-i-am-doing",
        "--user-master",
        "u",
        "--security-erase-enhanced",
        "NULL",
        "{dev}",
    ],
    ("sata", "overwrite"): [
        "hdparm",
        "--yes-i-know-what-i-am-doing",
        "--user-master",
        "u",
        "--security-erase",
        "NULL",
        "{dev}",
    ],
    ("sata", "erase"): [
        "hdparm",
        "--yes-i-know-what-i-am-doing",
        "--user-master",
        "u",
        "--security-erase",
        "NULL",
        "{dev}",
    ],
    ("sata", "blockerase"): ["blkdiscard", "--force", "{dev}"],
    ("sata", "sanitize"): [
        "hdparm",
        "--yes-i-know-what-i-am-doing",
        "--user-master",
        "u",
        "--security-erase",
        "NULL",
        "{dev}",
    ],
    ("sas", "thorough"): ["sg_format", "--format", "--size=512", "--verify", "{dev}"],
    ("sas", "overwrite"): ["sg_format", "--format", "--size=512", "{dev}"],
    ("sas", "erase"): ["sg_format", "--format", "{dev}"],
    ("sas", "blockerase"): ["sg_sanitize", "--block", "{dev}"],
    ("sas", "sanitize"): ["sg_sanitize", "--crypto", "{dev}"],
    # Unknown transport: fall back to the block layer, which every device has.
    ("unknown", "blockerase"): ["blkdiscard", "--force", "{dev}"],
    ("unknown", "sanitize"): ["sg_sanitize", "--block", "{dev}"],
    ("unknown", "thorough"): ["sg_format", "--format", "{dev}"],
    ("unknown", "overwrite"): ["sg_format", "--format", "{dev}"],
    ("unknown", "erase"): ["sg_format", "--format", "{dev}"],
}


def available_methods(disk: DiskInfo) -> Tuple[str, ...]:
    """Return the method names meaningful for this disk's media type."""

    if disk.medium is Medium.HDD:
        return HDD_METHODS
    if disk.medium is Medium.SSD:
        return SSD_METHODS
    return ()


def _placeholders(disk: DiskInfo) -> Dict[str, str]:
    """Build the substitution map for one disk."""

    return {
        "c": str(disk.controller_index),
        "e": str(disk.enclosure if disk.enclosure is not None else 0),
        "s": str(disk.slot if disk.slot is not None else 0),
        "ch": str(disk.channel if disk.channel is not None else 0),
        "dev": disk.device_node or str(disk.device_number if disk.device_number is not None else 0),
        "ref": disk.controller_ref or f"{disk.disk_id}",
    }


def choose_default_method(disk: DiskInfo, cfg: Config) -> str:
    """Pick the default method for a disk.

    SSDs are sanitised when they advertise SED capability (``SED: Y`` on LSI),
    otherwise we use the block erase path, which is universally supported.

    Raises:
        ValueError: If the disk's medium is UNKNOWN - such disks are never
            selectable (constraint 8) so planning must refuse them.
    """

    if disk.medium is Medium.UNKNOWN:
        raise ValueError(f"disk {disk.disk_id} has unknown media type; erase refused")
    if disk.medium is Medium.HDD:
        return cfg.default_hdd_method or DEFAULT_METHOD[Medium.HDD.value]

    sed_flag = str(disk.raw.get("SED", "") or disk.raw.get("sed", "")).strip().upper()
    supported = set(available_methods(disk))
    preferred = "sanitize" if sed_flag == "Y" or disk.protocol is Protocol.NVME else "blockerase"
    if cfg.default_ssd_method:
        preferred = cfg.default_ssd_method
    return preferred if preferred in supported else (SSD_METHODS[0] if supported else "blockerase")


def build_command(disk: DiskInfo, method: str, cfg: Optional[Config] = None) -> CommandPlan:
    """Construct a single, ready-to-run erase command for ``disk``.

    Args:
        disk: Target disk.  Must be selectable, i.e. medium is not UNKNOWN.
        method: One of :data:`HDD_METHODS` / :data:`SSD_METHODS`.
        cfg: Optional configuration (currently unused; kept for symmetry).

    Returns:
        A :class:`CommandPlan` carrying both the argv list and the shell-quoted
        dry-run text.

    Raises:
        ValueError: Unknown media type, unsupported vendor/method combination.
    """

    if disk.medium is Medium.UNKNOWN:
        raise ValueError(f"refusing to build an erase command for unknown media: {disk.disk_id}")

    if method not in available_methods(disk):
        raise ValueError(f"method {method!r} not valid for medium {disk.medium.value}")

    substitutions = _placeholders(disk)

    if disk.vendor is Vendor.GENERIC or disk.device_node:
        protocol_key = {
            Protocol.NVME: "nvme",
            Protocol.SATA: "sata",
            Protocol.SAS: "sas",
            Protocol.UNKNOWN: "unknown",
        }[disk.protocol]
        template = GENERIC_TABLE.get((protocol_key, method))
        if template is None:
            raise ValueError(f"no generic command for protocol={protocol_key} method={method}")
        kind = f"{disk.medium.value.lower()}_{method}"
    else:
        template = METHOD_TABLE.get((disk.vendor.value, method))
        if template is None:
            raise ValueError(f"no command template for vendor={disk.vendor.value} method={method}")
        kind = f"{disk.medium.value.lower()}_{method}"

    argv = [part.format(**substitutions) for part in template]
    label = f"{disk.medium.value} {method}"
    notes = _notes_for(disk, method)
    return CommandPlan(
        disk_id=disk.disk_id,
        kind=kind,
        label=label,
        argv=argv,
        medium=disk.medium,
        vendor=disk.vendor,
        notes=notes,
    )


def _notes_for(disk: DiskInfo, method: str) -> str:
    """Short human note explaining what the command will actually do."""

    if method == "sanitize":
        return "Cryptographic / firmware sanitize - instant, runs asynchronously on the controller"
    if method == "blockerase":
        return "Block erase of every user block - runs asynchronously on the controller"
    if method == "thorough":
        return "Full-surface overwrite incl. spare areas - long running"
    if method == "overwrite":
        return "Single-pass overwrite of the user area"
    return "Controller defined erase"


def plan_for_selection(disks: List[DiskInfo], cfg: Config) -> List[CommandPlan]:
    """Build one :class:`CommandPlan` per selected disk.

    Args:
        disks: The operator's selection.  Disks with UNKNOWN media are filtered
            out defensively so a desynchronised UI state can never erase them.
        cfg: Loaded configuration (default method policy).

    Returns:
        Commands in the same order as the input selection.
    """

    plans: List[CommandPlan] = []
    for disk in disks:
        if not disk.selectable:
            continue
        method = choose_default_method(disk, cfg)
        plans.append(build_command(disk, method, cfg))
    return plans
