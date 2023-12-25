#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - discovery scripts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from ramses_rf import exceptions as exc
from ramses_rf.const import (
    SZ_DISABLE_BACKOFF,
    SZ_PRIORITY,
    SZ_RETRIES,
    SZ_SCHEDULE,
    SZ_ZONE_IDX,
    __dev_mode__,
)
from ramses_tx import CODES_SCHEMA, Command, Priority
from ramses_tx.opentherm import OTB_MSG_IDS
from ramses_tx.protocol import MIN_GAP_BETWEEN_WRITES

# Beware, none of this is reliable - it is all subject to random change
# However, these serve as examples how to us eteh other modules


from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:  # mypy TypeVars and similar (e.g. Index, Verb)
    from ramses_rf.const import Index, Verb  # noqa: F401, pylint: disable=unused-import

if TYPE_CHECKING:
    from ramses_rf import Gateway


EXEC_CMD = "exec_cmd"
GET_FAULTS = "get_faults"
GET_SCHED = "get_schedule"
SET_SCHED = "set_schedule"

EXEC_SCR = "exec_scr"
SCAN_DISC = "scan_disc"
SCAN_FULL = "scan_full"
SCAN_HARD = "scan_hard"
SCAN_XXXX = "scan_xxxx"

# DEVICE_ID_REGEX = re.compile(DEVICE_ID_REGEX.ANY)

QOS_SCAN = {SZ_PRIORITY: Priority.LOW, SZ_RETRIES: 0}
QOS_HIGH = {SZ_PRIORITY: Priority.HIGH, SZ_RETRIES: 3}

DEV_MODE = __dev_mode__ and False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


def _mk_cmd(verb: Verb, code: Code, payload: str, dest_id) -> Command:
    """A convenience function, to cope with a change to the Command class."""
    return Command.from_attrs(verb, dest_id, code, payload)


def script_decorator(fnc):
    def wrapper(gwy: Gateway, *args, **kwargs):
        highest = {
            SZ_PRIORITY: Priority.HIGHEST,
            SZ_RETRIES: 3,
            SZ_DISABLE_BACKOFF: True,
        }
        gwy.send_cmd(Command._puzzle(message="Script begins:"), qos=highest)

        result = fnc(gwy, *args, **kwargs)

        lowest = {SZ_PRIORITY: Priority.LOWEST, SZ_RETRIES: 3, SZ_DISABLE_BACKOFF: True}
        gwy.send_cmd(Command._puzzle(message="Script done."), qos=lowest)

        return result

    return wrapper


async def periodic_send(
    gwy: Gateway,
    cmd: Command,
    count: int = 1,
    interval: float | None = None,
    qos=None,
):
    async def periodic_(interval_: float) -> None:
        await asyncio.sleep(interval_)
        gwy.send_cmd(cmd, qos=qos)

    if interval is None:
        interval = 0 if count == 1 else 60

    if count <= 0:
        while True:
            await periodic_(interval)
    else:
        for _ in range(count):
            await periodic_(interval)


def spawn_scripts(gwy: Gateway, **kwargs) -> list[asyncio.Task]:
    tasks = []

    if kwargs.get(EXEC_CMD):
        tasks += [gwy._loop.create_task(exec_cmd(gwy, **kwargs))]

    if kwargs.get(GET_FAULTS):
        tasks += [gwy._loop.create_task(get_faults(gwy, kwargs[GET_FAULTS]))]

    elif kwargs.get(GET_SCHED) and kwargs[GET_SCHED][0]:
        tasks += [gwy._loop.create_task(get_schedule(gwy, *kwargs[GET_SCHED]))]

    elif kwargs.get(SET_SCHED) and kwargs[SET_SCHED][0]:
        tasks += [gwy._loop.create_task(set_schedule(gwy, *kwargs[SET_SCHED]))]

    elif kwargs.get(EXEC_SCR):
        script = SCRIPTS.get(f"{kwargs[EXEC_SCR][0]}")
        if script is None:
            _LOGGER.warning(f"Script: {kwargs[EXEC_SCR][0]}() - unknown script")
        else:
            _LOGGER.info(f"Script: {kwargs[EXEC_SCR][0]}().- starts...")
            tasks += [gwy._loop.create_task(script(gwy, kwargs[EXEC_SCR][1]))]

    gwy._tasks.extend(tasks)
    return tasks


async def exec_cmd(gwy: Gateway, **kwargs):
    await gwy.async_send_cmd(Command.from_cli(kwargs[EXEC_CMD], qos=QOS_HIGH))


# @script_decorator
# async def script_scan_001(gwy: Gateway, dev_id: str):
#     _LOGGER.warning("scan_001() invoked - expect a lot of nonsense")
#     qos = {SZ_PRIORITY: Priority.LOW, SZ_RETRIES: 3}
#     for idx in range(0x10):
#         gwy.send_cmd(_mk_cmd(W_, Code._000E, f"{idx:02X}0050", dev_id), qos=qos)
#         gwy.send_cmd(_mk_cmd(RQ, Code._000E, f"{idx:02X}00C8", dev_id), qos=qos)

# @script_decorator
# async def script_scan_004(gwy: Gateway, dev_id: str):
#     _LOGGER.warning("scan_004() invoked - expect a lot of nonsense")
#     cmd = Command.get_dhw_mode(dev_id)
#     return gwy._loop.create_task(
#         periodic_send(gwy: Gateway, cmd: Command, count=0, interval=5, qos=QOS_SCAN)))


async def get_faults(gwy: Gateway, ctl_id: str, start: int = 0, limit: int = 0x3F):
    ctl = gwy.get_device(ctl_id)

    try:
        await ctl.tcs.get_faultlog(start=start, limit=limit)  # 0418
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("get_faults(): Function timed out: %s", err)


async def get_schedule(gwy: Gateway, ctl_id: str, zone_idx: str) -> None:
    zone = gwy.get_device(ctl_id).tcs.get_htg_zone(zone_idx)

    try:
        await zone.get_schedule()
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("get_schedule(): Function timed out: %s", err)


async def set_schedule(gwy: Gateway, ctl_id: str, schedule) -> None:
    schedule = json.load(schedule)
    zone_idx = schedule[SZ_ZONE_IDX]

    zone = gwy.get_device(ctl_id).tcs.get_htg_zone(zone_idx)

    try:
        await zone.set_schedule(schedule[SZ_SCHEDULE])  # 0404
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("set_schedule(): Function timed out: %s", err)


async def script_bind_req(gwy: Gateway, dev_id: str):
    gwy.get_device(dev_id)._make_fake(bind=True)


async def script_bind_wait(
    gwy: Gateway, dev_id: str, code: Code = Code._2309, idx: Index = "00"
):
    gwy.get_device(dev_id)._make_fake(bind=True, code=code, idx=idx)


def script_poll_device(gwy: Gateway, dev_id: str) -> list:
    _LOGGER.warning("poll_device() invoked...")

    tasks = []

    for code in (Code._0016, Code._1FC9):
        cmd = _mk_cmd(RQ, code, "00", dev_id)
        tasks.append(
            gwy._loop.create_task(periodic_send(gwy, cmd, count=0, qos=QOS_SCAN))
        )

    gwy._tasks.extend(tasks)
    return tasks


@script_decorator
async def script_scan_disc(gwy: Gateway, dev_id: str):
    _LOGGER.warning("scan_disc() invoked...")

    await gwy.get_device(dev_id).discover()  # discover_flag=Discover.DEFAULT)


@script_decorator
async def script_scan_full(gwy: Gateway, dev_id: str):
    _LOGGER.warning("scan_full() invoked - expect a lot of Warnings")

    qos = {SZ_PRIORITY: Priority.DEFAULT, SZ_RETRIES: 5}
    gwy.send_cmd(_mk_cmd(RQ, Code._0016, "0000", dev_id), qos=qos)

    qos = {SZ_PRIORITY: Priority.DEFAULT, SZ_RETRIES: 1}
    for code in sorted(CODES_SCHEMA):
        if code == Code._0005:
            for zone_type in range(20):  # known up to 18
                gwy.send_cmd(_mk_cmd(RQ, code, f"00{zone_type:02X}", dev_id), qos=qos)

        elif code == Code._000C:
            for zone_idx in range(16):  # also: FA-FF?
                gwy.send_cmd(_mk_cmd(RQ, code, f"{zone_idx:02X}00", dev_id), qos=qos)

        elif code == Code._0016:
            continue

        elif code in (Code._01D0, Code._01E9):
            for zone_idx in ("00", "01", "FC"):  # type: ignore[assignment]
                gwy.send_cmd(_mk_cmd(W_, code, f"{zone_idx}00", dev_id), qos=qos)
                gwy.send_cmd(_mk_cmd(W_, code, f"{zone_idx}03", dev_id), qos=qos)

        elif code == Code._0404:  # FIXME
            gwy.send_cmd(Command.get_schedule_fragment(dev_id, "HW", 1, 0), qos=qos)
            gwy.send_cmd(Command.get_schedule_fragment(dev_id, "00", 1, 0), qos=qos)

        elif code == Code._0418:
            for log_idx in range(2):
                gwy.send_cmd(Command.get_system_log_entry(dev_id, log_idx), qos=qos)

        elif code == Code._1100:
            gwy.send_cmd(Command.get_tpi_params(dev_id), qos=qos)

        elif code == Code._2E04:
            gwy.send_cmd(Command.get_system_mode(dev_id), qos=qos)

        elif code == Code._3220:
            for data_id in (0, 3):  # these are mandatory READ_DATA data_ids
                gwy.send_cmd(Command.get_opentherm_data(dev_id, data_id), qos=qos)

        elif code == Code._PUZZ:
            continue

        elif (
            code in CODES_SCHEMA
            and RQ in CODES_SCHEMA[code]
            and re.match(CODES_SCHEMA[code][RQ], "00")
        ):
            gwy.send_cmd(_mk_cmd(RQ, code, "00", dev_id), qos=qos)

        else:
            gwy.send_cmd(_mk_cmd(RQ, code, "0000", dev_id), qos=qos)

    # these are possible/difficult codes
    qos = {SZ_PRIORITY: Priority.DEFAULT, SZ_RETRIES: 2}
    for code in (Code._0150, Code._2389):
        gwy.send_cmd(_mk_cmd(RQ, code, "0000", dev_id), qos=qos)


@script_decorator
async def script_scan_hard(gwy: Gateway, dev_id: str, *, start_code: None | int = None):
    _LOGGER.warning("scan_hard() invoked - expect some Warnings")

    start_code = start_code or 0

    for code in range(start_code, 0x5000):
        gwy.send_cmd(_mk_cmd(RQ, f"{code:04X}", "0000", dev_id), qos=QOS_SCAN)  # type:ignore[arg-type]
        await asyncio.sleep(MIN_GAP_BETWEEN_WRITES)


@script_decorator
async def script_scan_fan(gwy: Gateway, dev_id: str):
    _LOGGER.warning("scan_fan() invoked - expect a lot of nonsense")
    qos = {SZ_PRIORITY: Priority.LOW, SZ_RETRIES: 3}

    from ramses_tx.ramses import _DEV_KLASSES_HVAC

    OUT_CODES = (
        Code._0016,
        Code._1470,
    )

    OLD_CODES = dict.fromkeys(
        c for k in _DEV_KLASSES_HVAC.values() for c in k if c not in OUT_CODES
    )
    for code in OLD_CODES:
        gwy.send_cmd(_mk_cmd(RQ, code, "00", dev_id), qos=qos)

    NEW_CODES = (
        Code._0150,
        Code._042F,
        Code._1030,
        Code._10D0,
        Code._10E1,
        Code._2210,
        Code._22B0,
        Code._22E0,
        Code._22E5,
        Code._22E9,
        Code._22F1,
        Code._22F2,
        Code._22F3,
        Code._22F4,
        Code._22F7,
        Code._22F8,
        Code._2400,
        Code._2410,
        Code._2420,
        Code._313E,
        Code._3221,
        Code._3222,
    )

    for code in NEW_CODES:
        if code not in OLD_CODES and code not in OUT_CODES:
            gwy.send_cmd(_mk_cmd(RQ, code, "00", dev_id), qos=qos)


@script_decorator
async def script_scan_otb(gwy: Gateway, dev_id: str):
    _LOGGER.warning("script_scan_otb_full invoked - expect a lot of nonsense")

    qos = {SZ_PRIORITY: Priority.LOW, SZ_RETRIES: 1}
    for msg_id in OTB_MSG_IDS:
        gwy.send_cmd(Command.get_opentherm_data(dev_id, msg_id), qos=qos)


@script_decorator
async def script_scan_otb_hard(gwy: Gateway, dev_id: str):
    _LOGGER.warning("script_scan_otb_hard invoked - expect a lot of nonsense")

    for msg_id in range(0x80):
        gwy.send_cmd(Command.get_opentherm_data(dev_id, msg_id), qos=QOS_SCAN)


@script_decorator
async def script_scan_otb_map(gwy: Gateway, dev_id: str):  # Tested only upon a R8820A
    _LOGGER.warning("script_scan_otb_map invoked - expect a lot of nonsense")

    RAMSES_TO_OPENTHERM = {
        Code._22D9: "01",  # boiler setpoint        / ControlSetpoint
        Code._3EF1: "11",  # rel. modulation level  / RelativeModulationLevel
        Code._1300: "12",  # cv water pressure      / CHWaterPressure
        Code._12F0: "13",  # dhw_flow_rate          / DHWFlowRate
        Code._3200: "19",  # boiler output temp     / BoilerWaterTemperature
        Code._1260: "1A",  # dhw temp               / DHWTemperature
        Code._1290: "1B",  # outdoor temp           / OutsideTemperature
        Code._3210: "1C",  # boiler return temp     / ReturnWaterTemperature
        Code._10A0: "38",  # dhw params[SZ_SETPOINT] / DHWSetpoint
        Code._1081: "39",  # max ch setpoint        / MaxCHWaterSetpoint
    }

    for code, msg_id in RAMSES_TO_OPENTHERM.items():
        gwy.send_cmd(_mk_cmd(RQ, code, "00", dev_id), qos=QOS_SCAN)
        gwy.send_cmd(Command.get_opentherm_data(dev_id, msg_id), qos=QOS_SCAN)


@script_decorator
async def script_scan_otb_ramses(
    gwy: Gateway, dev_id: str
):  # Tested only upon a R8820A
    _LOGGER.warning("script_scan_otb_ramses invoked - expect a lot of nonsense")

    CODES = (
        Code._042F,
        Code._10E0,  # device_info
        Code._10E1,  # device_id
        Code._1FD0,
        Code._2400,
        Code._2401,
        Code._2410,
        Code._2420,
        Code._1300,  # cv water pressure      / CHWaterPressure
        Code._1081,  # max ch setpoint        / MaxCHWaterSetpoint
        Code._10A0,  # dhw params[SZ_SETPOINT] / DHWSetpoint
        Code._22D9,  # boiler setpoint        / ControlSetpoint
        Code._1260,  # dhw temp               / DHWTemperature
        Code._1290,  # outdoor temp           / OutsideTemperature
        Code._3200,  # boiler output temp     / BoilerWaterTemperature
        Code._3210,  # boiler return temp     / ReturnWaterTemperature
        Code._0150,
        Code._12F0,  # dhw flow rate          / DHWFlowRate
        Code._1098,
        Code._10B0,
        Code._3221,
        Code._3223,
        Code._3EF0,  # rel. modulation level  / RelativeModulationLevel (also, below)
        Code._3EF1,  # rel. modulation level  / RelativeModulationLevel
    )  # excl. 3220

    # 3EF0 also includes:
    #  - boiler status        /
    #  - ch setpoint          /
    #  - max. rel. modulation /

    [gwy.send_cmd(_mk_cmd(RQ, c, "00", dev_id), qos=QOS_SCAN) for c in CODES]


SCRIPTS = {
    k[7:]: v for k, v in locals().items() if callable(v) and k.startswith("script_")
}
