"""Historical version-scoped tests (v6.1 – v8.5).

These files are kept runnable but are not the current contract — see the
top-level tests/ for the active suite.

Two import-order guards live here:

1. Repo root on sys.path: the moved test modules use ``parent.parent``-relative
   sys.path math that assumed they lived in tests/.
2. Real ``app`` modules first: test_v61_hardening.py stubs ``app.paypal_client``
   and ``app.quote`` in sys.modules "so it runs standalone". The stub is a no-op
   when the real modules are already imported (its guard is
   ``if name not in sys.modules``) — which was always the case in the full suite,
   because alphabetically-earlier top-level tests imported the real app first.
   Since tests/history/ now collects before those files, import the real modules
   here so the stub stays a no-op and cannot poison later imports.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import app.paypal_client  # noqa: F401,E402
import app.quote  # noqa: F401,E402
