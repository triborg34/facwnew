"""FACW - Face Access Control Watch

Single-host, on-premise face recognition and access-control system.

Run the service::

    python -m facw.server          # serves console + API on port 8770

Run the helper tools::

    python -m facw.tools.service_control   # start / stop / status
    python -m facw.tools.enrolment_helper  # desktop enrolment (Tkinter)
    python -m facw.tools.zone_editor       # desktop zone editor (Tkinter)

Run tests::

    python -m pytest
"""
from __future__ import annotations
