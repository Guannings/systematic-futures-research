# Pre-session operational notes

> **Note.** This file is documentation, not runnable instructions. The
> repository as published cannot drive live trading — the vendor broker
> bridge SDK is omitted (not redistributable) and the live wrapper's
> connection parameters are sample values. This is kept as a record of the
> operational discipline the system required, not as a procedure to follow.

Running this system live was never "launch it and walk away." The vendor
platform it depended on was unreliable in specific, recurring ways (see
[docs/BROKER_BRIDGE_NOTES.md](docs/BROKER_BRIDGE_NOTES.md)), and a short
pre-session routine existed purely to work around them. The routine itself
is not reproduced here — it was specific to one platform and is exactly the
kind of hard-won operational detail this repo intentionally doesn't hand
over. What's worth recording is the *shape* of the discipline it enforced.

## What the pre-session routine had to guarantee

- **A live, non-stale data subscription.** The platform's data
  subscriptions decayed silently, so the first job each session was to
  re-establish the feed from scratch rather than trust that the previous
  day's was still alive. "The dashboard shows a price" was never accepted
  as proof that the code was actually receiving data.

- **Real trades, not keep-alive noise.** The feed pushed a constant stream
  of placeholder messages carrying no real price. A naive "is data
  arriving?" check read healthy on that noise alone, so the routine had to
  confirm *real* trade activity specifically, not just message volume.

- **A clean, flat starting state.** Confirm no leftover position from the
  prior session before starting, since the strategy assumes it begins each
  day flat.

## What to expect during a session

- On a thin product, long stretches of no real trades are normal, not a
  bug. The system is designed to do nothing when there's nothing to do.
- A monitoring alert distinguishes "the platform is connected but the
  market isn't trading" from "the system has actually lost the feed" —
  because on an illiquid product those look identical to a naive check, and
  only the second one calls for intervention.
- Near session close, a wall-clock-driven flatten runs if any position is
  still open, so nothing is ever carried overnight.

## The takeaway

The operational overhead was as much a part of this project as the strategy
— arguably more. A system pointed at an unreliable platform and an illiquid
market needs its reliability and monitoring built and trusted *before* the
trading logic matters at all. That, not any specific morning ritual, is the
transferable lesson.
