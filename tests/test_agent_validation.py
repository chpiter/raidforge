from __future__ import annotations

import pytest

from raidforge.agent.models import TaskRejected
from raidforge.agent.topology import normalize_wwn, topology_diff, topology_hash, topology_projection
from raidforge.agent.validation import parse_task, validate_envelope

from agent_fakes import make_envelope
from datetime import datetime, timezone


def test_parse_read_task_and_shared_params():
    task = parse_task(make_envelope(params={"controller_ids": ["lsi:SN1"], "include_raw_output": True}))
    assert task.type.value == "refresh_hardware"
    assert task.params["controller_ids"] == ["lsi:SN1"]
    assert task.params["include_raw_output"] is True


def test_unknown_field_is_rejected():
    with pytest.raises(TaskRejected) as exc:
        parse_task(make_envelope(params={"unknown": 1}))
    assert exc.value.error_code == "invalid_params"
    assert exc.value.field == "unknown"


def test_unknown_task_type_is_rejected():
    with pytest.raises(TaskRejected) as exc:
        parse_task(make_envelope(task_type="format_everything"))
    assert exc.value.error_code == "unsupported_task_type"


def test_wwn_format_and_both_identities_required():
    with pytest.raises(TaskRejected) as exc:
        parse_task(
            make_envelope(
                task_type="erase_disks",
                params={
                    "controller_id": "lsi:SN1",
                    "mode": "overwrite",
                    "verify": False,
                    "pds": [{"serial": "ZA1", "wwn": "5000c500a1b2c3d4", "expected_size_bytes": 100}],
                },
            )
        )
    assert exc.value.error_code == "invalid_params"
    with pytest.raises(TaskRejected) as exc:
        parse_task(
            make_envelope(
                task_type="erase_disks",
                params={
                    "controller_id": "lsi:SN1",
                    "mode": "overwrite",
                    "verify": True,
                    "pds": [{"serial": "ZA1", "wwn": "0x5000c500a1b2c3d4", "expected_size_bytes": 100}],
                },
            )
        )
    assert exc.value.error_code == "invalid_params"  # verify=true 未实现


def test_create_rejects_unsupported_policies():
    base = {
        "controller_id": "lsi:SN1",
        "name": "vd1",
        "raid_level": "raid5",
        "pds": [{"serial": "ZA1", "wwn": "0x5000c500a1b2c3d4", "expected_size_bytes": 100}],
    }
    with pytest.raises(TaskRejected) as exc:
        parse_task(make_envelope(task_type="create_raid", params={**base, "read_policy": "read_ahead"}))
    assert exc.value.error_code == "invalid_params"
    task = parse_task(make_envelope(task_type="create_raid", params=base))
    assert task.params["raid_level"] == "RAID5"


def test_create_stripe_and_init_defaults():
    base = {
        "controller_id": "lsi:SN1",
        "name": "vd1",
        "raid_level": "raid5",
        "pds": [{"serial": "ZA1", "wwn": "0x5000c500a1b2c3d4", "expected_size_bytes": 100}],
    }
    task = parse_task(make_envelope(task_type="create_raid", params=base))
    assert task.params["strip_size_kb"] is None
    assert task.params["init"] == "fast"

    explicit = parse_task(
        make_envelope(
            task_type="create_raid",
            params={**base, "strip_size_kb": 512, "init": "none"},
        )
    )
    assert explicit.params["strip_size_kb"] == 512
    assert explicit.params["init"] == "none"

    for omitted in (0, None, ""):
        task = parse_task(
            make_envelope(task_type="create_raid", params={**base, "strip_size_kb": omitted})
        )
        assert task.params["strip_size_kb"] is None

    with pytest.raises(TaskRejected) as exc:
        parse_task(
            make_envelope(task_type="create_raid", params={**base, "strip_size_kb": 8})
        )
    assert exc.value.error_code == "invalid_params"
    assert exc.value.field == "strip_size_kb"

    with pytest.raises(TaskRejected) as exc:
        parse_task(make_envelope(task_type="create_raid", params={**base, "init": "quick"}))
    assert exc.value.error_code == "invalid_params"
    assert exc.value.field == "init"


def test_envelope_mismatch_and_expiry():
    task = parse_task(make_envelope())
    now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(TaskRejected) as exc:
        validate_envelope(task, "OTHER-SN", now)
    assert exc.value.error_code == "server_sn_mismatch"
    with pytest.raises(TaskRejected) as exc:
        validate_envelope(task, "SN12345678", datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc))
    assert exc.value.error_code == "task_expired"


def test_wwn_normalisation():
    assert normalize_wwn("5000C500A4A2B001") == "0x5000c500a4a2b001"
    assert normalize_wwn("0x5000c500a4a2b001") == "0x5000c500a4a2b001"
    assert normalize_wwn("5 00a075 1161abcd1") == "0x500a0751161abcd1"
    assert normalize_wwn("garbage") == ""


def test_topology_hash_is_order_independent():
    controllers = [{"controller_id": "lsi:SN1", "vendor": "lsi", "model": "9361", "serial": "SN1", "firmware": "1", "tool": "storcli"}]
    pds = [
        {"controller_id": "lsi:SN1", "serial": "B", "wwn": "0x5000c500a1b2c3d4", "size_bytes": 2, "raid_state": "UGood"},
        {"controller_id": "lsi:SN1", "serial": "A", "wwn": "0x5000c500a1b2c3d5", "size_bytes": 1, "raid_state": "UGood"},
    ]
    first = topology_projection(controllers, pds, [])
    second = topology_projection(controllers, list(reversed(pds)), [])
    assert topology_hash(first) == topology_hash(second)
    diff = topology_diff(first, topology_projection(controllers, pds[:1], []))
    assert diff["pds_removed"]
