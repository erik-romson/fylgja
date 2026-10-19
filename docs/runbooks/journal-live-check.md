# Check the workspace journal by hand

The journal writes its entries with a real model call (Haiku through the `claude` CLI), and the page needs a browser.
The automated tests use fakes for both, so these steps are the by-hand part (decision 0057). Run them on a scratch
daemon, never on the daily one, and write no entry text into docs or commits.

## Set up a scratch daemon

Use another port and scratch `FYLGJA_*_DIR`, with the journal on. Unlike the README's "Try a change next to the
daily daemon", keep the real `HOME`: the journal's model call needs the logged-in `claude` CLI, and under a scratch
`HOME` it fails with `nonzero_exit` ("Not logged in").

```sh
S=$PWD/target/manual-journal; mkdir -p $S/config $S/data $S/state
printf '[api]\nport = 7329\nallowed_origins = ["http://127.0.0.1:7329"]\n[journal]\nenabled = true\n' > $S/config/config.toml
( export FYLGJA_CONFIG_DIR=$S/config FYLGJA_DATA_DIR=$S/data FYLGJA_STATE_DIR=$S/state
  uv run fylgja install secret
  uv run fylgja serve )          # http://127.0.0.1:7329/, Ctrl-C to stop
```

The scratch daemon reads the real home the way the daily one does, so it sees your other agent sessions too and
may journal their checkouts. Its store, secret and log stay under `$S`, and its model calls store no transcript. Run
the `fylgja` commands with the same scratch variables, so they reach the scratch daemon.

## Feed it a session

The installed Claude Code hooks run `$HOME/.local/bin/fylgja hook`, which delivers to `$FYLGJA_DATA_DIR/hook-inbox`.
Start one agent session in a git checkout with only that variable on scratch. Keep the real `HOME`, so `claude` stays
logged in and finds its hooks:

```sh
cd <a git checkout>
FYLGJA_DATA_DIR=<repo>/target/manual-journal/data claude
```

Only this session's hook events reach the scratch daemon; the daily daemon does not see them. A session started
without the variable, such as one already running, feeds the daily daemon instead: no `hook-inbox` appears under
`$S/data`. Until an entry is written, no item on the Recall tab has a "Journal" button.

## 1. One real entry after a turn

1. In that session, work until a turn ends, then wait two quiet minutes.
2. Run `fylgja journal --here` from that checkout (with the scratch variables). It should show "Where you are", and
   the entry should cite its sources.
3. The thread's `error_kind` stays absent. If it is set, the kind names what failed (for example `not_installed`).
4. `fylgja journal --list` lists the thread.

## 2. "How do I do this?" on an item

1. Open the Recall tab, press "Journal" on the item row, and press "How do I do this?" on an item.
2. The checklist shows "pending", then up to six steps when the job finishes, with no reload.
3. Press the button again: the steps show at once, from the cache.

## 3. Six tabs

1. Open the page in six browser tabs on the Recall tab.
2. In the network panel, exactly one `/events` connection is open across all six tabs: the tab that holds the lock
   has it, and the others have none.
3. Cause a change (a todo, or a journal entry). The Recall tab updates in every tab.

## 4. A background tab holds back the journal

1. Open a journal panel, then switch to another tab so the page is hidden.
2. Cause a change. In the network panel, no `/journal/threads/` request is made while the tab is hidden.
3. Show the tab again: one `/journal/threads/` request follows, and the panel is current.

## 5. The Raycast command

Once the "Fylgja Journal" command exists in the `fylgja-raycast` repository (see its brief), check that it shows the
focused checkout's journal, that a 404 shows an empty state rather than an error toast, and that "How do I do this?"
shows the steps.

## Clean up

Stop the daemon and delete `target/manual-journal`.
