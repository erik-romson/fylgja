"""Pluggable rankers: the ``fylgja.rankers`` entry-point group (``docs/decisions/0052``).

A ranker orders the navigation items (``docs/decisions/0051``). It is a plugin like a collector:
:mod:`fylgja.rankers.base` is the public API a ranker package imports,
:mod:`fylgja.rankers.registry` discovers and loads rankers by the collector enable rule, and
:mod:`fylgja.testing.rankers` is the contract every ranker passes. ``docs/rankers.md`` explains how
to write one.

This package imports nothing on its own, so importing :mod:`fylgja.rankers.base` stays light.
"""
