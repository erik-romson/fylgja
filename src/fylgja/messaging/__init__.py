"""Opt-in messages to live local agent sessions (``PLAN.md`` §4.3.1, WP3.6, A9, A14).

:mod:`fylgja.messaging.service` is the generic entry point: it gates on ``[actions.message]``,
checks the text, dispatches by session kind and records a receipt. Each kind that can be messaged
has an adapter package (:mod:`fylgja.messaging.claude`). Messages are information, never authority:
nothing here approves a permission prompt, changes a setting or runs a command in the target.
"""
