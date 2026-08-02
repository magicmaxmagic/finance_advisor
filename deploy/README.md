# Scheduling the universe snapshot

The universe snapshot is the one dataset finlab cannot backfill. A vendor will
sell you today's constituents; the companies that delisted last year are simply
gone from their file. Every weekday this does not run is a row that no amount of
money or effort recovers later, and every backtest starting before capture began
has to be labelled survivorship-contaminated.

So: schedule it on day one, before the rest of the pipeline is finished.

## systemd (preferred)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/finlab-universe.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now finlab-universe.timer

# check it
systemctl --user list-timers finlab-universe.timer
journalctl --user -u finlab-universe.service -n 50
```

`loginctl enable-linger $USER` keeps user timers running when you are not
logged in — without it the snapshot only fires while you have a session.

## cron (alternative)

```cron
30 18 * * 1-5 cd $HOME/Documents/finance_advisor && /usr/bin/env uv run finlab ingest universe >> data/universe-cron.log 2>&1
```

## What it does and does not protect against

The capture is **idempotent**: it keys off the `File Creation Time` trailer in
the exchange's own file, not the wall clock, so running twice on one day, or on
a weekend, leaves the existing snapshot untouched rather than writing a second
version or inventing a day that never traded.

`Persistent=true` catches up after the machine was asleep, but only to a point:
Nasdaq Trader serves only the *current* file, so a machine off from Monday to
Friday recovers Friday alone. Monday through Thursday are lost permanently.
If the host is not always on, that is the gap to think about — not the code.
