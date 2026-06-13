#!/usr/bin/env python3
"""
Test the modbus-proxy integration in two modes:

  Mock mode (default) — spins up a fake Modbus TCP "inverter", starts the
  proxy in front of it, then hammers it with multiple concurrent clients to
  prove serialization works and all clients get valid responses.

  Live mode (--host) — starts the proxy pointing at the real inverter and
  sends a handful of read requests through it, useful for a quick sanity
  check before enabling the option in HA.

Usage:
  python3 tools/test_proxy.py                          # mock mode
  python3 tools/test_proxy.py --host 192.168.1.100     # live mode
  python3 tools/test_proxy.py --host 192.168.1.100 --port 502 --proxy-port 1502
"""

import asyncio
import argparse
import struct
import time


# ---------------------------------------------------------------------------
# Minimal Modbus TCP frame helpers
# ---------------------------------------------------------------------------

def build_read_holding_registers(transaction_id: int, unit: int, address: int, count: int) -> bytes:
    pdu = struct.pack(">BBH H", unit, 0x03, address, count)
    return struct.pack(">HHH", transaction_id, 0, len(pdu)) + pdu


def parse_mbap(data: bytes):
    """Return (transaction_id, unit_id, function_code, registers) or raise."""
    if len(data) < 9:
        raise ValueError(f"Frame too short ({len(data)} bytes): {data.hex()}")
    tid, _proto, length = struct.unpack(">HHH", data[:6])
    unit = data[6]
    fc = data[7]
    if fc == 0x03:
        byte_count = data[8]
        n = byte_count // 2
        regs = list(struct.unpack(f">{n}H", data[9:9 + byte_count]))
        return tid, unit, fc, regs
    if fc & 0x80:
        raise ValueError(f"Modbus exception 0x{data[8]:02X}")
    raise ValueError(f"Unexpected function code 0x{fc:02X}")


# ---------------------------------------------------------------------------
# Mock Modbus TCP server (simulates the inverter)
# ---------------------------------------------------------------------------

MOCK_REGISTERS: dict[int, int] = {
    0x0000: 0x0103,   # device type  (Deye-style)
    0x0064: 75,       # battery SOC  75 %
    0x006B: 480,      # battery voltage  48.0 V
    0x006C: 50,       # battery current   5.0 A
}


async def _mock_inverter_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    print(f"  [mock-inverter] client connected from {peer}")
    try:
        while True:
            header = await reader.readexactly(6)
            tid, _proto, length = struct.unpack(">HHH", header)
            body = await reader.readexactly(length)
            unit, fc, addr, count = struct.unpack(">BBHH", body)

            if fc != 0x03:
                # Return exception
                writer.write(struct.pack(">HHHBBB", tid, 0, 3, unit, fc | 0x80, 0x01))
            else:
                regs = [MOCK_REGISTERS.get(addr + i, 0) for i in range(count)]
                pdu = struct.pack(f">BBB{count}H", unit, 0x03, count * 2, *regs)
                writer.write(struct.pack(">HHH", tid, 0, len(pdu)) + pdu)

            await writer.drain()
    except asyncio.IncompleteReadError:
        pass
    except Exception as exc:
        print(f"  [mock-inverter] error: {exc}")
    finally:
        writer.close()
        print(f"  [mock-inverter] client {peer} disconnected")


async def start_mock_inverter(host: str = "127.0.0.1", port: int = 0):
    server = await asyncio.start_server(_mock_inverter_handler, host, port)
    actual_port = server.sockets[0].getsockname()[1]
    print(f"[mock-inverter] listening on {host}:{actual_port}")
    return server, actual_port


# ---------------------------------------------------------------------------
# Client helper — sends one read request through the proxy
# ---------------------------------------------------------------------------

async def client_read(proxy_host: str, proxy_port: int, address: int, count: int, label: str):
    t0 = time.monotonic()
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    try:
        tid = address & 0xFFFF
        frame = build_read_holding_registers(tid, 1, address, count)
        writer.write(frame)
        await writer.drain()

        header = await asyncio.wait_for(reader.readexactly(6), timeout=5)
        _tid, _proto, length = struct.unpack(">HHH", header)
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
        full = header + body
        _, _, fc, regs = parse_mbap(full)
        elapsed = time.monotonic() - t0
        print(f"  [{label}] addr=0x{address:04X} count={count} -> {regs}  ({elapsed * 1000:.0f} ms)")
        return regs
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# Mock-mode test — everything runs locally, no real inverter needed
# ---------------------------------------------------------------------------

async def run_mock_test(proxy_port: int, clients: int):
    from modbus_proxy import ModBus

    print("\n=== Mock mode: fake inverter + proxy + concurrent clients ===\n")

    # Start the fake inverter on a random port
    inv_server, inv_port = await start_mock_inverter()

    # Start the proxy pointing at the fake inverter
    proxy = ModBus({
        "modbus": {"url": f"tcp://127.0.0.1:{inv_port}"},
        "listen": {"bind": f"tcp://0.0.0.0:{proxy_port}"}
    })
    await proxy.start()
    actual_proxy_port = proxy.server.sockets[0].getsockname()[1]
    print(f"[proxy] listening on 0.0.0.0:{actual_proxy_port} -> 127.0.0.1:{inv_port}\n")

    try:
        # Fire N concurrent clients
        print(f"Launching {clients} concurrent clients...\n")
        tasks = [
            client_read("127.0.0.1", actual_proxy_port, 0x0064, 1, f"client-{i+1}")
            for i in range(clients)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = [r for r in results if isinstance(r, Exception)]
        ok = [r for r in results if not isinstance(r, Exception)]
        print(f"\nResult: {len(ok)}/{len(results)} clients succeeded"
              + (f", {len(errors)} errors: {errors}" if errors else ""))

        # Quick read of a few registers via a single client
        print("\nSpot-check registers via proxy:")
        checks = [
            (0x0000, 1, "device type"),
            (0x0064, 1, "battery SOC (%)"),
            (0x006B, 1, "battery voltage (*0.1 V)"),
            (0x006C, 1, "battery current (*0.1 A)"),
        ]
        for addr, count, desc in checks:
            regs = await client_read("127.0.0.1", actual_proxy_port, addr, count, desc)
            val = regs[0] if regs else "??"
            print(f"    0x{addr:04X}  {desc}: {val}")

    finally:
        await proxy.stop()
        inv_server.close()
        await inv_server.wait_closed()

    print("\n[done] Proxy stopped cleanly.\n")


# ---------------------------------------------------------------------------
# Live mode — proxy pointed at the real inverter
# ---------------------------------------------------------------------------

async def run_live_test(inv_host: str, inv_port: int, proxy_port: int, clients: int):
    from modbus_proxy import ModBus

    print(f"\n=== Live mode: proxy -> {inv_host}:{inv_port}, listening on 0.0.0.0:{proxy_port} ===\n")

    proxy = ModBus({
        "modbus": {"url": f"tcp://{inv_host}:{inv_port}"},
        "listen": {"bind": f"tcp://0.0.0.0:{proxy_port}"}
    })
    await proxy.start()
    actual_port = proxy.server.sockets[0].getsockname()[1]
    print(f"[proxy] up on port {actual_port}\n")

    try:
        print(f"Sending {clients} concurrent requests through the proxy...\n")
        tasks = [
            client_read("127.0.0.1", actual_port, 0x0064, 1, f"client-{i+1}")
            for i in range(clients)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        ok = [r for r in results if not isinstance(r, Exception)]
        print(f"\nResult: {len(ok)}/{len(results)} clients succeeded"
              + (f", errors: {errors}" if errors else ""))

        print(f"\nYou can also connect external tools to port {actual_port} while this runs.")
        print("Press Ctrl+C to stop the proxy.\n")
        await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await proxy.stop()

    print("\n[done] Proxy stopped.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Test the Modbus proxy integration")
    parser.add_argument("--host", default=None, help="Inverter IP (omit for mock mode)")
    parser.add_argument("--port", type=int, default=502, help="Inverter port (default 502)")
    parser.add_argument("--proxy-port", type=int, default=0,
                        help="Proxy listen port (default 0 = OS-assigned in mock, 1502 in live)")
    parser.add_argument("--clients", type=int, default=5,
                        help="Number of concurrent test clients (default 5)")
    args = parser.parse_args()

    if args.host:
        proxy_port = args.proxy_port or 1502
        asyncio.run(run_live_test(args.host, args.port, proxy_port, args.clients))
    else:
        asyncio.run(run_mock_test(args.proxy_port, args.clients))


if __name__ == "__main__":
    main()
