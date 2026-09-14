"""Exclusive target leases live in the kernel since P9 (stations lease the same way); this
module keeps the P7 import path working."""

from __future__ import annotations

from slas_kernel.leases import Lease as Lease
from slas_kernel.leases import LeaseError as LeaseError
from slas_kernel.leases import LeaseTable as LeaseTable

__all__ = ["Lease", "LeaseError", "LeaseTable"]
