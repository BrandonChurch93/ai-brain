"""Placeholder proving the harness runs and the three packages import.

Replaced by real coverage as each package grows; the import assertions stay
useful as a check that the src layout is still wired up.
"""

import bodies
import brain
import wire


def test_packages_import() -> None:
    for package in (brain, bodies, wire):
        assert package.__doc__
