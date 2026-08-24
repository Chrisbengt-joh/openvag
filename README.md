# VAGDIAG

Open, VCDS-like diagnostic software for older VAG cars that speak
**KWP1281 over the K-line** - roughly 1996 to 2003 models, before CAN
diagnostics. Built while troubleshooting a 1.9 TDI with a VP37 injection pump
(EDC15, engine codes AHF/ALH/AFN/AGR), but it works against any KWP1281
control module.

> **Hobby tool - use at your own risk. VCDS is the reference.**
> This software reads from and writes to your car's control modules. A wrong
> adaptation or basic setting can, in the worst case, leave you with a car that
> will not start. Read the warnings the program shows you; do not skip them.

**Contents**

- [What you need](#what-you-need)
- [Installation](#installation)
- [FTDI latency timer - 1 ms is mandatory](#ftdi-latency-timer---1-ms-is-mandatory)
- [Try it without a car](#try-it-without-a-car)
- [Quick start: diagnosing a smoking TDI](#quick-start-diagnosing-a-smoking-tdi)
- [Command line](#command-line)
- [The menu](#the-menu)
- [The dashboard (GUI)](#the-dashboard-gui)
- [The CSV logs](#the-csv-logs)
- [Interpreting the readings](#interpreting-the-readings)
- [Troubleshooting the program itself](#troubleshooting-the-program-itself)
- [Known limitations](#known-limitations)
- [For developers](#for-developers)

---

## What you need

| Item | Notes |
|------|-------|
| **A KKL "VAG 409.1" cable** | It must have a genuine **FTDI FT232RL**. Cheap cables with a CH340 or a cloned FTDI chip often handle the 5-baud init badly or not at all. |
| **Python 3.10 or newer** | Tested on 3.10-3.13. |
| **pyserial** | The only hard dependency. |
| **Windows or Linux** | Windows is the primary platform; Linux works. |
| **The car** | Ignition ON. The engine only needs to run when you record a test drive. |

This program does **not** speak ordinary OBD2 with standard PIDs, and it does
**not** speak CAN. It is KWP1281 on the K-line (pin 7 of the OBD connector).

---

## Installation

### 1. Install Python

Windows: download from <https://www.python.org/downloads/> and tick
**"Add python.exe to PATH"** in the installer. Verify in a fresh terminal:

```
python --version
```

Linux (Debian/Ubuntu):

```
sudo apt install python3 python3-pip python3-tk
```

### 2. Install VAGDIAG

From the project directory:

```
pip install -e .
```

That pulls in pyserial and adds a `vagdiag` command. If you would rather not
install anything, this is enough:

```
pip install pyserial
python -m vagdiag --simulator
```

### 3. Install the cable driver

Windows usually finds the FTDI cable on its own. If not, grab the VCP drivers
from <https://ftdichip.com/drivers/vcp-drivers/>.

On Linux the FTDI driver ships with the kernel. Add yourself to the right group
so you do not need `sudo`:

```
sudo usermod -aG dialout $USER
```

Log out and back in.

### 4. Find your port

```
python -m vagdiag --list-ports
```

The output looks roughly like this:

```
  COM3       USB Serial Port  [FTDI latency timer: 16 ms - SET IT TO 1!]
```

On Linux the port is usually `/dev/ttyUSB0`.

If nothing shows up, check **Device Manager -> Ports (COM & LPT)** for a
"USB Serial Port (COMx)" entry.

---

## FTDI latency timer - 1 ms is mandatory

KWP1281 acknowledges **every single byte** and has tight timing windows. By
default the FTDI chip buffers for 16 ms before passing data on, which makes the
acknowledgements arrive too late and kills the session mid-conversation.

### Windows, manually (recommended)

1. Right-click Start -> **Device Manager**.
2. Expand **Ports (COM & LPT)**.
3. Right-click **USB Serial Port (COMx)** -> **Properties**.
4. **Port Settings** tab -> **Advanced...** button.
5. Under **BM Options** set **Latency Timer (msec)** to **1**.
6. OK, OK. Unplug and replug the USB cable.

### Windows, automatically

Run the terminal **as administrator**, then:

```
python -m vagdiag COM3 --set-latency
```

Unplug and replug the cable afterwards. The program warns you at startup if the
latency is above 2 ms.

### Linux

```
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
```

To make it permanent, add a udev rule in
`/etc/udev/rules.d/99-ftdi-latency.rules`:

```
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
```

---

## Try it without a car

VAGDIAG ships with a virtual ECU. It answers the full protocol, serves
measuring values that vary over time, and holds two fault codes in memory. No
cable, no car, no com0com:

```
python -m vagdiag --simulator                 # the menu
python -m vagdiag --simulator --faults        # read fault codes
python -m vagdiag --simulator --groups 3 4 11
python -m vagdiag --simulator --gui           # the dashboard
```

Use it to learn the menu somewhere comfortable before you are bent over the
engine bay.

---

## Quick start: diagnosing a smoking TDI

This is the workflow for black smoke at full throttle, white smoke that smells
of diesel, limp mode under load, and a sooted-up EGR system.

### 1. Map the car

Ignition ON, engine off:

```
python -m vagdiag COM3 --autoscan
```

Write down **every** fault code before you do anything else. The auto-scan takes
a couple of minutes; most addresses will not answer, which is normal in a car
from 1999.

### 2. Clear and reset

Menu -> **1 Read fault codes** -> **2 Clear fault codes** (you have to type `YES`).

The point of clearing is to see **which codes come back**. A code that returns
after a test drive is an active fault; one that does not was history.

### 3. Test drive

Drive the car until the symptoms appear - full throttle, heavy load, ideally a
hill until it drops into limp mode.

### 4. Scan again

```
python -m vagdiag COM3 --faults
```

Now you know which codes are actually active.

### 5. Warm idle - group 013 (smooth running)

With the engine **fully warmed up** and idling:

```
python -m vagdiag COM3 --groups 13
```

Read off the per-cylinder deviations. See
[interpreting the readings](#interpreting-the-readings).

### 6. Log a hill climb - groups 003, 004, 011

Start logging before you set off, drive up the hill at full throttle in a high
gear until it gives up, then press `q`:

```
python -m vagdiag COM3 --groups 3 4 11 --log hill-2026-08-24.csv
```

Open the CSV in Excel and plot charge pressure SPEC/ACTUAL and air mass
SPEC/ACTUAL against time. That is where you see exactly when it falls apart.

> **Do not drive and read the screen at the same time.** Bring a passenger, or
> start the log, drive, and analyse afterwards. Every row is flushed to disk, so
> the log is complete even if you stop in the middle.

---

## Command line

```
python -m vagdiag [PORT] [options]
```

| Option | Meaning |
|--------|---------|
| `PORT` | Serial port, e.g. `COM3` or `/dev/ttyUSB0`. |
| `--simulator` | Run against the virtual ECU instead of a car. |
| `--gui` | Start the dashboard instead of the terminal menu. |
| `--autoscan` | Scan every control module and exit. |
| `--faults` | Read the fault codes and exit. |
| `--groups 3 4 11` | Live view of the given measuring blocks. |
| `--module 01` | Module address in hex for direct commands (default `01` = engine). |
| `--log file.csv` | Log the live view to CSV. |
| `--list-ports` | Show serial ports and the FTDI latency. |
| `--set-latency` | Try to set the latency timer to 1 ms (Windows, needs admin). |
| `--interval 0.2` | Pause between polling cycles (default: as fast as possible). |
| `--timeout 1.0` | Per-byte timeout. Raise it if the module is sluggish. |
| `--init-timeout 2.0` | Timeout for the answer to the 5-baud wake-up. |
| `--csv-dot` | CSV with commas and a decimal point instead of the European Excel style. |
| `--debug` | Trace all block traffic to stderr. |

Examples:

```
python -m vagdiag COM3
python -m vagdiag COM3 --autoscan
python -m vagdiag COM3 --groups 3 4 11 --log hill.csv
python -m vagdiag COM3 --module 17 --faults     # instrument cluster
python -m vagdiag COM3 --gui --groups 3 11
```

---

## The menu

With no session open:

| Choice | Function |
|--------|----------|
| 1 | **Auto-scan** - probes every common module address, fetches the identification, counts fault codes and closes each session cleanly. Copes with most addresses being silent. |
| 2 | **Connect to a control module** - 5-baud wake-up of the chosen address. |
| 3 | **Connection information** - port, latency, protocol parameters. |

With a session open:

| Choice | Function |
|--------|----------|
| 1 | **Read fault codes** - five-digit code, plain-text description, static/intermittent and the decoded status byte. |
| 2 | **Clear fault codes** - requires typing `YES`. Re-reads immediately so you can see which codes are still active. |
| 3 | **Live measuring blocks** - any groups, four values each, with optional CSV logging. `q` goes back, `l` starts/stops logging. |
| 4 | **Basic setting** - like measuring blocks but via block 0x28. Warning plus `YES` required. |
| 5 | **Actuator test** - steps through the actuators. The engine must be off. `Enter` for the next one, `q` to abort. |
| 6 | **Adaptation** - reads the channel, always shows the old value, tests the new one, and only stores it if you type `SAVE`. |
| 7 | **Login** - five-digit code (0-65535). |
| 8 | **Show identification** |
| 9 | **Disconnect** |

A background keep-alive thread holds the session open, so it does not die while
you sit and think in a menu.

---

## The dashboard (GUI)

```
python -m vagdiag COM3 --gui --groups 3 11
```

- Round gauges for engine speed, charge pressure and air mass. **The SPEC value
  sits in the same gauge as a second, orange needle**, so the deviation is
  immediately visible.
- A scrolling real-time chart of the last 60 seconds covering every numeric
  value in the selected groups.
- One-button CSV logging.
- A separate fault-code tab with read and clear (clearing requires `YES`).

All serial communication runs in its own thread that never touches tkinter; the
GUI updates through a queue and `after()`. The window will not freeze even if
the K-line drops out.

---

## The CSV logs

The default format targets European Excel: **semicolon** delimiters, a
**decimal comma**, and UTF-8 with a BOM. For pandas or gnuplot, use `--csv-dot`.

The header row names every column with its group, position, label and unit:

```
timestamp;seconds;003.1 Engine speed [rpm];003.2 Air mass SPEC [mg/stroke];...
```

Every row is written and flushed straight to disk, so a 30-minute test drive
log is complete even if the program crashes or the cable is pulled out.

---

## Interpreting the readings

> The figures below are **approximate** and differ between engine codes. Always
> compare against the workshop data for your exact engine code, and against
> VCDS.

### Group 003 - air mass SPEC/ACTUAL and EGR duty cycle

At **full throttle** ACTUAL should sit close to SPEC. On a healthy 1.9 TDI it
typically lands around 700-900 mg/stroke under full load.

- **ACTUAL below roughly 700 mg/stroke at full throttle, clearly under SPEC** -
  the engine is not getting enough air for the fuel being injected. That is
  exactly what produces **black smoke**. Look for:
  - a sooted-up intake manifold and EGR system (classic at high mileage),
  - an EGR valve stuck open,
  - a leaking intercooler hose or intercooler,
  - a blocked air filter,
  - a tired mass air flow sensor (G70).
- **ACTUAL tracking SPEC but the engine still smokes** - air is not the problem;
  move on to the start of injection and the injected quantity.
- **A tired MAF** usually reads *low*, which in turn causes a power loss and
  codes 16485-16487 or 00553.
- **The EGR duty cycle** should fall towards zero at full throttle. If it stays
  high under load, the EGR system is choking the intake.

### Group 004 - start of injection SPEC/ACTUAL

- ACTUAL should track SPEC within about one degree.
- **ACTUAL consistently later than SPEC (retarded injection)** gives poor
  combustion, hard starting and **white smoke that smells of diesel** - that is
  unburnt fuel. Suspect the N108 valve, a tired injection pump, low fuel supply
  pressure, or air in the fuel.
- Check the N108 duty cycle in position 4 at the same time: if it is pinned at
  either end, the control loop has given up.
- Related codes: 00550, 01248, 17910-17912.

### Group 011 - charge pressure SPEC/ACTUAL and N75 duty cycle

Log a hill climb at full throttle and plot SPEC and ACTUAL against time.

- **ACTUAL overshooting SPEC and then dropping off a cliff** means overboost,
  and it is the most common cause of **limp mode**. Code **17965** (P1557,
  positive deviation) belongs here. On a high-mileage TDI the usual culprit is
  **sooted, sticking VNT vanes** in the turbo, followed by a faulty N75 valve or
  cracked vacuum lines.
- **ACTUAL never reaching SPEC** means underboost. Code 17964 (P1556). Look for
  leaks in the charge air system, a turbo sticking the other way, or a vacuum
  actuator that cannot pull.
- **N75 duty pinned high while ACTUAL stays low** means the control loop is
  trying everything and getting nowhere - a mechanical fault, not an electrical
  one.

### Group 013 - smooth running control (per-cylinder deviation)

Only read this with a **fully warm engine at idle**.

- All four cylinders should sit within roughly **+/-2 mg/stroke**, and
  preferably much closer to zero.
- **A cylinder that consistently deviates by more than +/-2 mg/stroke** points
  at that cylinder: a tired injector, poor compression, or a leaking return
  line. Swap the injectors between cylinders and read again - if the deviation
  follows the injector it is the injector; if it stays with the cylinder it is
  mechanical.
- The values jitter naturally - look at the trend over 30 seconds, not at a
  single cycle.

### Putting the symptoms together

Black smoke plus limp mode plus 17965 plus a sooted EGR points at the **air
path**: a sooted intake, sticking VNT vanes, and an EGR that will not close.
White smoke smelling of diesel points at the **injection side**: check groups
004 and 013.

**A dead MIL bulb** means the engine module cannot warn you at all, so you are
entirely dependent on reading fault codes by hand. Replace the bulb in the
instrument cluster - an active code with no lamp is easy to miss for months.

---

## Troubleshooting the program itself

### "No answer from module 0x01 after the 5-baud wake-up"

1. Is the ignition ON? (Key position 2; the engine does not need to run.)
2. Is the latency timer set to 1 ms? See above - this is by far the most common
   cause.
3. Is the cable properly seated in the OBD port?
4. Switch the ignition off, wait five seconds, switch it back on and retry. The
   module has to be idle when the wake-up starts.
5. Try another address, e.g. `--module 17` (instrument cluster), to see whether
   the cable works at all.
6. Does the cable really have an FT232RL? Clones often cannot produce the break
   signal the 5-baud init depends on.

### "Echo mismatch: sent 0xNN but got 0xMM back"

The K-line echoes everything you send. A wrong echo is almost always hardware: a
bad cable, a bad connection, or another program holding the port open (close
VCDS).

### "Bad acknowledgement for byte 0xNN"

The module did not answer in time, or a byte was lost. Check the latency and try
raising `--timeout` to 2.0. If it happens constantly it is usually interference
on the K-line or a poor-quality cable.

### "Block counter jumped"

The session is out of sync. The program deliberately drops the connection -
reconnect. If it happens often, run with `--debug` and inspect the block traffic.

### The session dies while I sit in a menu

It should not - the keep-alive thread sends ACK blocks automatically. If it
happens anyway: switch the ignition off for five seconds and reconnect.

### Odd characters in the terminal

The program switches the console to UTF-8 at startup and falls back to plain
ASCII box drawing if that fails. Prefer Windows Terminal over the old `cmd.exe`
window.

### No COM port shows up

`python -m vagdiag --list-ports` reports separately when pyserial is missing -
run `pip install pyserial` in that case.

---

## Known limitations

- **Recoding (block 0x10) is not implemented.** It is far too easy to render a
  car unusable with the wrong coding, and it is not needed for diagnosis.
- **Measuring formulas with id 22 and above are reconstructed**, not verified
  against ground truth. They render with a trailing `?`. The common ones
  (engine speed, temperature, voltage, pressure, air mass in g/s) are ids 1-21
  and 25, and those are reliable. If a value disagrees with VCDS you can fix the
  table in `vagdiag/formulas.py` - it is data driven, and
  `formulas.register()` exists to override a single entry.
- **The group labels are approximate** and describe a 1.9 TDI VP37. They differ
  between engine codes and software versions. The numbers are always right; it
  is the names beside them that are an educated guess.
- **The elaboration codes in the status byte** (the part that says "short to
  ground" and so on) are an interpretation. The intermittent bit (0x80) is
  reliable. The raw value is always shown as `status 0xNN` for comparison.
- **The actuator component codes** only have a small name table. Unknown
  actuators are shown with their hex code.
- **Only the engine module has group labels.** Other modules work, but their
  values are shown as "Value 1-4".

---

## For developers

### Project layout

```
vagdiag/
├── exceptions.py  - error hierarchy with readable messages and hints
├── transport.py   - the Transport abstraction: serial port or virtual bus
├── kwp1281.py     - the protocol (byte, block and command level), no UI
├── formulas.py    - measuring-value formulas, data-driven table
├── faults.py      - fault-code database + status-byte decoding
├── modules.py     - control module addresses and group labels
├── datalog.py     - CSV logger
├── menu.py        - terminal UI
├── gui.py         - tkinter dashboard
├── simulator.py   - virtual ECU
└── __main__.py    - CLI
```

### Tests

The entire protocol stack is tested against the ECU simulator over a virtual
in-memory K-line bus. No hardware, no serial ports, no com0com:

```
pip install -e ".[test]"
pytest
```

The simulator can inject faults - a dropped acknowledgement, a corrupt
acknowledgement, silence, a bad terminating byte and a jumping block counter -
and the tests verify that the client detects each one, raises the right
exception, and can reconnect afterwards.

### Design decisions

- **A transport abstraction instead of pty/com0com.** The simulator plugs
  straight into memory, which makes the tests OS-independent and fast. The
  virtual bus echoes written bytes back exactly like a real K-line, so the echo
  handling is genuinely tested rather than special-cased away.
- **Keep-alive on a background thread.** Both the terminal and the GUI have
  states where the main thread blocks on input; a pump in the main loop would
  have dropped the session while the user was thinking. All block traffic is
  guarded by an `RLock`, so the thread can never land in the middle of another
  command. If you need a strictly single-threaded mode, call
  `KWP1281.keep_alive()` yourself.
- **Shared block and acknowledgement logic** (`KWPChannel`) for both the client
  and the simulator, so the tests exercise the same code real hardware meets.

### Licence

MIT.
