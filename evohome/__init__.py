"""Evohome serial."""
import asyncio
from datetime import datetime as dt, timedelta
import json
import logging
import os
import re

# these are used to test for memory leak
import psutil
import objgraph
import gc
from guppy import hpy

import signal
import sys

from collections import deque
from queue import PriorityQueue

from .command import Command
from .const import INDEX_SQL, TABLE_SQL, INSERT_SQL, ISO_FORMAT_REGEX
from .logger import set_logging, BANDW_SUFFIX, COLOR_SUFFIX, CONSOLE_FMT, PKT_LOG_FMT
from .message import _LOGGER as msg_logger, Message
from .packet import _LOGGER as pkt_logger, RAW_PKT, Packet, PortPktProvider
from .ser2net import Ser2NetServer
from .system import EvohomeSystem

_LOGGER = logging.getLogger(__name__)
# OGGER.setLevel(logging.DEBUG)


class Gateway:
    """The gateway class."""

    def __init__(self, serial_port=None, loop=None, **config) -> None:
        """Initialise the class."""  # TODO: config.get() vs config[]
        _LOGGER.debug("**config = %s", config)

        self.serial_port = serial_port
        self.loop = loop if loop else asyncio.get_running_loop()  # get_event_loop()
        self.config = config

        self._tasks = []
        self._h = self._hp = None  # TODO: mem leak code

        if self.serial_port and config.get("input_file"):
            _LOGGER.warning(
                "Serial port specified (%s), so ignoring input file (%s)",
                self.serial_port,
                config["input_file"],
            )
            config["input_file"] = None

        config["listen_only"] = not config.get("probe_system")

        if config.get("input_file"):
            if not config.get("listen_only"):
                _LOGGER.warning(
                    "Input file specified (%s), so forcing listen_only mode",
                    config["input_file"],
                )
                config["listen_only"] = True

            if config.get("execute_cmd"):
                _LOGGER.warning(
                    "Input file specified (%s), so disabling execute_cmd (%s)",
                    config["input_file"],
                    config["execute_cmd"],
                )
                config["execute_cmd"] = None

        if config.get("raw_output", 0) > 2 and config.get("message_log"):
            _LOGGER.warning(
                "Raw output = %s, so disabling message_log (%s)",
                config["raw_output"],
                config["message_log"],
            )
            config["message_log"] = False

        set_logging(
            msg_logger,
            stream=None if config.get("raw_output", 0) > 2 else sys.stdout,
            file_name=self.config.get("message_log"),
        )

        set_logging(
            pkt_logger,
            stream=sys.stdout if config.get("raw_output", 0) > 2 else None,
            file_name=self.config.get("packet_log"),
            file_fmt=PKT_LOG_FMT + BANDW_SUFFIX,
            cons_fmt=CONSOLE_FMT + COLOR_SUFFIX,
        )

        self.cmd_que = PriorityQueue(maxsize=200)
        self._buffer = deque()

        # if self.config.get("ser2net"):
        self._relay = None

        # if self.config.get("known_devices"):
        self.known_devices = {}
        self.device_blacklist = []
        self.device_whitelist = []

        # if self.config.get("database"):
        self._output_db = self._db_cursor = None

        # if self.config.get("raw_output", 0) > 0:
        self.evo = EvohomeSystem(controller_id=None)

        self._setup_signal_handler()

    def _setup_signal_handler(self):
        _LOGGER.info("Starting evohome_rf...")

        signals = [signal.SIGINT, signal.SIGTERM]
        if os.name == "posix":  # TODO: or sys.platform is better?
            signals += [signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2]

        for sig in signals:
            self.loop.add_signal_handler(
                sig, lambda sig=sig: asyncio.create_task(self._signal_handler(sig))
            )

    async def _signal_handler(self, signal):
        _LOGGER.debug("Received signal %s...", signal.name)

        if signal == signal.SIGUSR1 and self.config.get("raw_output", 0) == 0:
            _LOGGER.info("Devices:%s", f"\r\n{json.dumps(self.evo._devices, indent=4)}")
            _LOGGER.info("Domains:%s", f"\r\n{json.dumps(self.evo._domains, indent=4)}")
            _LOGGER.info("Zones:  %s", f"\r\n{json.dumps(self.evo._zones, indent=4)}")

        if signal == signal.SIGUSR2:  # output debug data
            _LOGGER.info("Debug data is:")
            self._debug_info()  # TODO: should be %s in a _LOGGER

        if signal in [signal.SIGHUP, signal.SIGINT, signal.SIGTERM]:
            _LOGGER.info("Received a %s, exiting gracefully...", signal)
            await self.cleanup("_signal_handler")

            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            logging.info(
                f"Cancelling {len(tasks)} outstanding tasks, should next see 'done'..."
            )
            [task.cancel() for task in tasks]
            # await asyncio.gather(*tasks)
            logging.info(" - done.")

    def _debug_info(self) -> None:
        gc.collect()

        _LOGGER.info("mem_fullinfo: %s", psutil.Process(os.getpid()).memory_full_info())
        _LOGGER.info("common_types: %s", objgraph.most_common_types())
        _LOGGER.info(
            "leaking_objs: %s", objgraph.count("dict", objgraph.get_leaking_objects())
        )

        _LOGGER.info("heap_stuff:")
        _LOGGER.info("%s", self._hp.heap())
        _LOGGER.info("%s", self._hp.heap().more)

    async def cleanup(self, xxx=None) -> None:
        """Perform a graceful shutdown."""

        _LOGGER.debug("cleanup invoked by: %s", xxx)

        if self._output_db:  # close packet database
            _LOGGER.info(f"Closing packets database...")
            await self._output_db.commit()
            await self._output_db.close()
            self._output_db = None  # TODO: is this needed - if re-entrant?

        if self.config.get("known_devices"):
            try:
                _LOGGER.info("Updating known_devices file...")
                for d in self.evo.devices:
                    device_attrs = {
                        "friendly_name": d._friendly_name,
                        "blacklist": d._blacklist,
                    }
                    if d.id in self.known_devices:
                        self.known_devices[d.id].update(device_attrs)
                    else:
                        self.known_devices[d.id] = device_attrs

                with open(self.config["known_devices"], "w") as json_file:
                    json.dump(self.known_devices, json_file, sort_keys=True, indent=4)

            except (AssertionError, AttributeError, LookupError, TypeError, ValueError):
                _LOGGER.exception(
                    "Failed update of %s", self.config.get("known_devices")
                )

        if self.config.get("raw_output", 0) == 0:
            try:  # print state data
                print(f"Devices:\r\n{json.dumps(self.evo._devices, indent=4)}")
                print(f"Domains:\r\n{json.dumps(self.evo._domains, indent=4)}")
                print(f"Zones  :\r\n{json.dumps(self.evo._zones, indent=4)}")
            except (AssertionError, AttributeError, LookupError, TypeError, ValueError):
                _LOGGER.warning("Failed to print State data", exc_info=True)

    async def start(self) -> None:
        async def proc_pkts_from_file() -> None:
            """Process packets from a file, asynchonously."""

            async def file_reader(manager):
                for ts_pkt in self.config["input_file"]:
                    await asyncio.sleep(0.001)  # to enable a Ctrl-C

                    raw_pkt = RAW_PKT(ts_pkt[:26], ts_pkt[27:].strip(), None)
                    try:
                        assert re.match(ISO_FORMAT_REGEX, raw_pkt.datetime)
                        dt.fromisoformat(raw_pkt.datetime)
                    except (AssertionError, ValueError):  # TODO: log these, or not?
                        _LOGGER.debug("Packet line has invalid timestamp: %s", raw_pkt)
                        continue
                    if raw_pkt.packet:
                        await self._process_pkt(raw_pkt)

            async def port_writer(manager):
                while True:
                    await self._dispatch_pkt(destination=None)
                    await asyncio.sleep(0.1)

            reader = asyncio.create_task(file_reader(None))
            self._tasks += [reader, asyncio.create_task(port_writer(None))]
            await reader

        async def proc_pkts_from_port() -> None:
            async def port_reader(manager):
                gc.collect()  # TODO: mem leak test only
                self._hp = hpy()
                self._hp.setrelheap()

                while True:
                    raw_pkt = await manager.get_pkt()
                    if raw_pkt.packet:
                        if self.config.get("evofw_flag") and "evofw3" in raw_pkt.packet:
                            # !V, !T - print the version, or the current mask
                            # !T00   - turn off all mask bits
                            # !T01   - cause raw data for all messages to be printed
                            await manager.put_pkt(self.config["evofw_flag"], _LOGGER)

                        await self._process_pkt(raw_pkt)
                        if self._relay:  # TODO: handle socket close
                            await self._relay.write(raw_pkt.packet)
                    await asyncio.sleep(0.01)

            async def port_writer(manager):
                while True:
                    serial = manager.reader._transport.serial
                    if serial is not None and serial.in_waiting == 0:
                        await self._dispatch_pkt(destination=manager)
                    await asyncio.sleep(0.1)

            if self.config.get("ser2net_server"):
                self._relay = Ser2NetServer(
                    self.config["ser2net_server"], self.cmd_que, loop=self.loop
                )
                self._tasks.append(asyncio.create_task(self._relay.start()))

            async with PortPktProvider(self.serial_port, loop=self.loop) as manager:
                if self.config.get("execute_cmd"):  # e.g. "RQ 01:145038 1F09 FF"
                    cmd = self.config["execute_cmd"]
                    cmd = Command(cmd[:2], cmd[3:12], cmd[13:17], cmd[18:])
                    await manager.put_pkt(cmd, _LOGGER)

                self._tasks.append(asyncio.create_task(port_reader(manager)))
                self._tasks.append(asyncio.create_task(port_writer(manager)))
                await asyncio.gather(*self._tasks)

        if self.config.get("database"):
            import aiosqlite as sqlite3

            self._output_db = await sqlite3.connect(self.config["database"])
            self._db_cursor = await self._output_db.cursor()
            await self._db_cursor.execute(TABLE_SQL)  # create if not existant
            await self._db_cursor.execute(INDEX_SQL)  # index if not already
            await self._output_db.commit()

        if self.config.get("known_devices"):
            try:
                with open(self.config["known_devices"]) as json_file:
                    devices = self.known_devices = json.load(json_file)
            except FileNotFoundError:  # if it doesn't exist, we'll create it later
                self.known_devices = {}
            else:
                if self.config["device_whitelist"]:
                    # discard packets unless to/from one of our devices
                    self.device_whitelist = [
                        k for k, v in devices.items() if not v.get("blacklist")
                    ]
                else:
                    # discard packets to/from any explicitly blacklisted device
                    self.device_blacklist = [
                        k for k, v in devices.items() if v.get("blacklist")
                    ]

        # Finally, source of packets is either a text file, or a serial port:
        if self.config.get("input_file"):
            await proc_pkts_from_file()
        else:  # if self.config["serial_port"] or if self.serial_port
            await proc_pkts_from_port()

        await self.cleanup("start")

    async def _dispatch_pkt(self, destination=None) -> None:
        """Send a command unless in listen_only mode."""
        # TODO: listen_only will clear the whole queue, not only the its next element
        while not self.cmd_que.empty():
            cmd = self.cmd_que.get()

            if destination is not None and str(cmd).startswith("!"):
                await destination.put_pkt(cmd, _LOGGER)

            elif destination is None or self.config.get("listen_only"):
                pass

            elif cmd.verb == "RQ" and cmd.code == "0404":  # wait for the response..
                self._buffer.append(cmd)

                timeout_1 = dt.now() + timedelta(seconds=2.5)
                while dt.now() < timeout_1:
                    await destination.put_pkt(cmd, _LOGGER)

                    timeout_2 = dt.now() + timedelta(seconds=1)
                    while len(self._buffer) != 0:
                        await asyncio.sleep(0.005)

                        if dt.now() > timeout_2:
                            break
                    else:
                        break  # the RQ arrived
                else:
                    # print("*** I GAVE UP ***")
                    self._buffer.clear()

            else:
                await destination.put_pkt(cmd, _LOGGER)

            self.cmd_que.task_done()
            # await asyncio.sleep(0.001)  # TODO: why is this needed, for Ctrl-C?

    async def _process_pkt(self, raw_pkt) -> None:
        """Receive a packet and optionally validate it as a message."""

        def has_wanted_device(pkt, dev_whitelist=None, dev_blacklist=None) -> bool:
            """Return True only if a packet contains 'wanted' devices."""
            if " 18:" in pkt.packet:  # TODO: should we respect blacklisting of a HGI80?
                return True
            if dev_whitelist:
                return any(device in pkt.packet for device in dev_whitelist)
            return not any(device in pkt.packet for device in dev_blacklist)

        if self.config.get("debug_mode", 0) == 1:  # TODO: mem leak
            self._debug_info()

        pkt = Packet(raw_pkt)
        if not pkt.is_valid:  # this will trap/log all bad pkts appropriately
            return

        if not has_wanted_device(pkt, self.device_whitelist, self.device_blacklist):
            return  # silently drop packets with unwanted (e.g. neighbour's) devices

        # any remaining packets are both valid & wanted, so: log them
        pkt_logger.info("%s ", pkt, extra=pkt.__dict__)

        if self._output_db:  # archive all valid packets, even those not to be parsed
            ts_pkt = f"{pkt.date}T{pkt.time} {pkt.packet}"
            w = [0, 27, 31, 34, 38, 48, 58, 68, 73, 77, 165]  # 165? 199 works
            data = tuple([ts_pkt[w[i - 1] : w[i] - 1] for i in range(1, len(w))])
            await self._db_cursor.execute(INSERT_SQL, data)
            await self._output_db.commit()

        # finally, process packet payloads as messages
        self._process_payload(pkt)

    def _process_payload(self, pkt: Packet) -> None:
        """Decode the packet and its payload."""
        # if any(x in pkt.packet for x in self.config.get("blacklist", [])):
        #     return  # silently drop packets with blacklisted text

        if self.config.get("raw_output", 0) > 2:
            return

        try:
            msg = Message(pkt, self)
            if not msg.is_valid:  # trap/logs all exceptions appropriately
                return

        except (AssertionError, NotImplementedError):
            msg_logger.exception("%s", pkt.packet, extra=pkt.__dict__)
            return
        except (LookupError, TypeError, ValueError):  # TODO: shouldn't be needed
            msg_logger.exception("%s", pkt.packet, extra=pkt.__dict__)
            return

        if len(self._buffer) != 0:
            cmd = self._buffer.copy().pop()
            if msg.verb == "RP" and msg.code == cmd.code:
                self._buffer.clear()
                # print("*** IS OUR PACKET ***")
            else:
                # print("*** IS NOT OUR PACKET ***")
                pass

        if self.config.get("raw_output", 0) > 1:
            return

        # only reliable packets should become part of the state data
        if msg.dev_from[:2] == "18":  # RQs are required, but also less unreliable
            return

        try:
            msg._create_entities()  # create the devices, zones, domains

            if self.config.get("raw_output", 0) > 0:
                return

            msg._update_entities()  # update the state database

        except AssertionError:  # TODO: for dev only?
            msg_logger.exception("%s", pkt.packet, extra=pkt.__dict__)
        except (LookupError, TypeError, ValueError):  # TODO: shouldn't be needed?
            msg_logger.exception("%s", pkt.packet, extra=pkt.__dict__)
