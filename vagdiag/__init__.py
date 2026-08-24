"""VAGDIAG - open, VCDS-like diagnostic software for older VAG cars (KWP1281).

The package is split into clean layers:

* :mod:`vagdiag.transport`  - physical layer (serial port or virtual bus)
* :mod:`vagdiag.kwp1281`    - the protocol, with no user interface at all
* :mod:`vagdiag.formulas`   - measuring-value conversion (formula id, a, b)
* :mod:`vagdiag.faults`     - fault-code database and status-byte decoding
* :mod:`vagdiag.modules`    - control module addresses and group labels
* :mod:`vagdiag.datalog`    - CSV logging of test drives
* :mod:`vagdiag.menu`       - terminal interface
* :mod:`vagdiag.gui`        - tkinter dashboard
* :mod:`vagdiag.simulator`  - virtual ECU for testing without a car

Hobby tool - use at your own risk. VCDS is the reference.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
