# Notes on integrating against an undocumented vendor trading bridge

A large share of this project's effort went not into the trading strategy
but into making a vendor-supplied broker bridge behave reliably when driven
from code. The bridge's real behavior diverged from its documentation in
several ways that were only discoverable by hitting them in production.

This document records those **categories of problem** as a heads-up for
anyone doing similar integration work. It deliberately does **not** publish
the specific field values, symbol-coding tables, API call names, timing
constants, or code-level workarounds that resolved each one. Those were the
expensive part to discover, and listing them would hand the entire
integration to anyone targeting the same platform. The point here is the
*shape* of the problems and the defensive mindset they teach — not a recipe.

---

## 1. Symbol coding may not follow the convention you expect

The exchange's contract-symbol scheme did not match the convention most
public references assume. A symbol that looks plausible but is subtly wrong
is accepted **silently** — no error, no warning, simply no data. The first
thing to verify when nothing is flowing is that the symbol string is
actually valid on the target venue, not that your code is wrong.

**Lesson:** never assume a market follows the "standard" coding convention;
confirm it against the venue directly, and treat silent no-data as a likely
symbol problem before anything else.

## 2. Subscriptions are not durable

The bridge's data subscriptions did not survive restarts or re-logins, and
— more insidiously — could **go dormant while still appearing active**. The
feed would look subscribed and even show occasional updates while the
machine-readable stream had quietly stopped.

**Lesson:** treat a data subscription as something that decays. Build a
pre-session routine that re-establishes it from scratch rather than trusting
that yesterday's subscription is still alive, and don't equate "the GUI
shows a price" with "my code is receiving data."

## 3. A health check that counts messages can lie

The feed emitted a high volume of **placeholder messages** that carried no
real trade information. Naive code both (a) crashed trying to parse the
non-numeric placeholder fields as prices, and (b) reported a perfectly
healthy message rate while *zero* real trades were occurring.

**Lesson:** distinguish "the pipe is alive" from "the market is trading."
Count and log the messages that pass your validity filter separately from
raw arrivals, so your monitoring reflects real activity rather than
keep-alive noise.

## 4. "Accepted" is not "filled"

The acknowledgement returned synchronously when an order was submitted
indicated that the order had been **accepted**, not that it had **filled**.
Code that mutated its own position state on acknowledgement would drift out
of sync with the broker's actual book any time a resting order didn't fill.

**Lesson:** never treat an order acknowledgement as a fill. Persist your
position state to disk on every change, reconcile it on restart, and prefer
unambiguous order types for anything time-critical (such as an end-of-session
flatten) so the accept-versus-fill gap can't leave you unexpectedly exposed.

## 5. Session-relative logic must use exchange time and a wall clock

Two timing traps recurred. First, any "is the session open / is it time to
flatten" check has to be evaluated in the **exchange's** timezone, not the
host machine's — otherwise it fires at the wrong hour on a differently-zoned
host. Second, on an illiquid product the final in-session bar may never
"close" within the session, because a bar only closes when the next bar's
first trade arrives. An exit that waits for a bar-close event can therefore
never fire in time.

**Lesson:** drive end-of-session flattening from a **wall-clock timer in
exchange-local time**, independent of any bar event, and guard it so it
won't submit against a missing price.

## 6. Sparse markets break naive watchdogs

An external watchdog that restarts the system after N minutes of silence has
to set N longer than the longest *legitimate* quiet stretch — which, on a
thin product, can be surprisingly long. Set it too tight and the watchdog
thrashes, restarting a perfectly healthy process through a normal dry spell.
A more liquid product on the same venue tolerates a much tighter threshold.

**Lesson:** size silence-based failure detectors to the product's real
liquidity profile, not to an assumption of continuous activity, and tune
them per product rather than globally.

## 7. Toolchain version locking

The vendor SDK was pinned to one specific language-runtime minor version and
failed cryptically under any other. This is a category of friction worth
checking for early, since it constrains the whole deployment environment.

**Lesson:** pin and document the exact runtime the vendor SDK requires
before building anything on top of it.

---

## The meta-lesson

On an obscure or poorly-documented platform, the integration and
reliability engineering *is* the project. The trading logic was a small
fraction of the code; the majority was defensive handling of the
behaviors above. Anyone planning similar work should budget for the
plumbing first and the strategy second — the reverse of most people's
instinct.
