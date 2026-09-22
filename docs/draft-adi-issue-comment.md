<!--
DRAFT ONLY. Not posted. For review before deciding whether/how to post to:
https://github.com/ADI-Foundation-Labs/ADI-Stack-EN-Setup-script/issues/21

Written in plain first-person-plural voice as if posted from the ZKDoctor project.
Edit the "we/our" framing, add/remove signature, before actually posting.
-->

This issue's shape (main node upgraded past the EN's pinned version, EN falls
behind, then stops applying blocks entirely while `:3071/status/health` keeps
reporting `{"healthy": true}` and `:3050` keeps serving the stale head) matched
a question we'd been building a small tool to test: can an external, black-box
watcher — no access to your internals, just polling two public RPC endpoints —
tell a stalled node apart from a healthy one, without relying on the node's own
health check?

We're not affiliated with ADI. We ran a real reproduction attempt against your
public infrastructure to find out, and wanted to share the result here since
it's directly relevant to this issue and #17.

**What we did:** stood up an external node from this repo's
`docker-compose.mainnet.yml`, pinned to `v0.20.12-b1` (the exact tag this issue
describes), and watched it with our tool
([zkdoctor](https://github.com/easynfastsolutions-cpu/zkdoctor), specifically
its `watch` command) against your public mainnet RPC
(`https://rpc.adifoundation.ai/`) as the reference — comparing block height and
block-hash agreement between the two, and separately recording what
`:3071/status/health` reported, without trusting it. Chain ID was confirmed
matching before anything else ran.

**Attempt 1** ([run log](https://github.com/easynfastsolutions-cpu/zkdoctor/actions/runs/35577380179)):
the EN synced from genesis past block 1,253,580 — the exact block this issue
reports the stall at — with 282/282 block-hash agreements against your live
mainnet the whole way. It didn't hit the stall condition; instead our own
runner's disk filled up (unrelated to your node — our sync database plus
uncapped container logs exceeded the disk we'd given it) and the container
died. That did surface a real gap in our own tool: it took over two hours of
the node being unreachable before our watcher escalated it as a failure
instead of just logging transient errors. We've since fixed that.

**Attempt 2** ([run log](https://github.com/easynfastsolutions-cpu/zkdoctor/actions/runs/35615807966),
after fixing the disk issue): the EN ran cleanly for the full ~5.6 hours, synced
all the way to your live chain tip, and stayed there — 652/652 block-hash
agreements, no crash (confirmed via exit code and OOM checks, not just "it
seemed fine"). It hit exactly one 123-second pause where its height briefly
didn't move, self-resolved on the next poll, and its health endpoint was
accurate throughout — correctly `healthy` both before and after, with no
mismatch to flag.

**The honest result:** across both attempts (about 1,200 real polls total), we
never actually caught a node in the specific state this issue describes — up,
healthy-looking, and silently stalled for a real stretch of time. That's not
a negative result exactly, but it's not a confirmed catch either: two runs of
roughly 5-6 hours each may simply be short compared to however long your
EN sat frozen before anyone noticed, and our reproduction never hit the
main-node-version-skew condition that triggered the actual incident (mainnet
in our runs was never mid-upgrade). What we can say with more confidence: the
detection approach — comparing height and block hash against an independent
reference, rather than trusting the node's own health check — behaved
correctly and produced zero false alarms across every real observation we
took, including the one brief hiccup.

We don't have a fix to offer for the version-skew/upgrade-notice question this
issue actually asks about — that's a real gap in the tooling here, separate
from what we were testing. Mostly wanted to share the data point in case it's
useful: an external reference-node comparison would have caught the *symptom*
(height frozen, hash divergence once your main node moves on) even without any
change to the EN or its health endpoint, for whatever that's worth in thinking
about #17's proposed fix.

Happy to share the full run logs/evidence if useful.
