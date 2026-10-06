"""Local Claude Code session messages through the documented session inbox socket (WP3.6).

* :mod:`~fylgja.messaging.claude.resolve`: native session id to a live registry row, the A9
  version gates, the A14 socket rules and the pid and start-time check right before connecting;
* :mod:`~fylgja.messaging.claude.encoders`: the version-gated frame encoders. ``v1`` is empty
  because spike 0004 found no documented frame (verdict ``no-go``), so every send is
  ``unsupported`` and focus-and-copy is the action;
* :mod:`~fylgja.messaging.claude.transport`: one ``AF_UNIX`` connect and write within 2 s, no retry;
* :mod:`~fylgja.messaging.claude.receipts`: the receipt states and the ``message_receipts`` rows;
* :mod:`~fylgja.messaging.claude.messenger`: the adapter that runs those steps in order;
* :mod:`~fylgja.messaging.claude.policy`: the effective inbound policy doctor explains.

No module here reads a ``*.key`` file or ``CLAUDE_CODE_MESSAGING_TOKEN``, and none sends an auth
line (``PLAN.md`` §4.3.1).
"""
