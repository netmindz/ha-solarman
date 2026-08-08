# AGENTS.md

## About This Repository

This is a Home Assistant custom integration for **Solarman Stick Loggers**. It enables Home Assistant to communicate with solar inverters via Solarman data loggers and compatible Modbus TCP bridges (such as ESPHome Modbus bridges, Waveshare RS485-to-ETH adapters, and Ethernet loggers).

The integration is built on top of the asynchronous [pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5) library and supports both the Solarman V5 protocol and raw Modbus TCP.

## Bexie Hybrid Inverter — Modbus TCP at 192.168.178.134

A device profile has been created at:
`custom_components/solarman/inverter_definitions/bexie_hybrid.yaml`

### Device Details

| Field       | Value                  |
|-------------|------------------------|
| IP          | 192.168.178.134        |
| Port        | 502                    |
| Protocol    | Modbus TCP             |
| Phases      | Single phase           |
| MPPTs       | 2                      |
| HA instance | 192.168.178.231:8123   |

### Register Map (confirmed via live probe)

| Block         | Range           | FC  | Contents                          |
|---------------|-----------------|-----|-----------------------------------|
| PV / Inverter | 0x1010–0x104C   | 03  | PV voltage, current, power, temps |
| Grid / Load   | 0x1300–0x1338   | 03  | Grid/load power, voltage, freq    |
| Battery       | 0x2000–0x200F   | 03  | SOC, voltage, current, power      |
| Control (RW)  | 0x2100–0x2115   | 03/06 | Work mode, grid charge           |

### Key Device-Specific Notes

- **Battery Temperature**: `0x201B` (U16) — confirmed correct. `0x2001` returns 0 on this device (differs from CHINT).
- **32-bit registers**: use inverted word order `[high_addr, low_addr]` — same as CHINT CPS-SCETL.
- **Work Mode** (`0x2100`): writable. Currently reads `0` (Self Use). Confirmed write/readback works.
- **Grid Charge** (`0x2115`): writable switch. Currently reads `0` (Disabled).
- **Port 502** is only open while the inverter is active. The port closes at night / when HA holds the connection — disable HA before probing directly.

### Reference

- Modbus protocol document (same as CHINT): https://github.com/user-attachments/files/17295986/Hybrid.Modbus.Protocol.per.inverter.ibridi.pdf
- Similar profile: `custom_components/solarman/inverter_definitions/chint_cps-scetl.yaml`

### Tools

- `tools/probe_registers.py` — reads and validates all key registers, flags implausible values. Run with:
  ```
  python3 tools/probe_registers.py --host 192.168.178.134
  ```
