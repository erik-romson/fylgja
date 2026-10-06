"""Application context: what the focused window says about the work (WP5.3).

Tier 1 is the pure title parsers (:mod:`fylgja.collectors.appcontext.titles`) applied to every
window. Tier 2 runs on focus changes only: the selected cmux workspace and tab, the Chrome tab
URL, the JetBrains project path, the Obsidian vault and link, and iTerm2's path and job (see
:mod:`fylgja.collectors.appcontext.collector`). The package exports the ``plugin`` descriptor.
"""

from fylgja.collectors.appcontext.plugin import plugin

__all__ = ["plugin"]
