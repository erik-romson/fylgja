"""SQLite persistence (``PLAN-final-plan.md`` §4.1).

:mod:`fylgja.store.db` opens connections and runs migrations, :mod:`fylgja.store.queries` holds
the explicit SQL (there is no ORM), and :mod:`fylgja.store.rows` the typed rows read back. Only
the daemon's writer task (:mod:`fylgja.core.writer`) writes; everything else reads through
read-only connections.
"""
