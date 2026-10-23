# Terminal connection retirement

Issue #240 adopts the published `2.26.0+sobafm.3` wheel. It supplies the
storage acknowledgement already required by `Deck._close_owned`; no application
private graph mutation or new cleanup deadline is needed.

The bounded input context owns its factory connection even when setup fails.
After public connection closure, it closes the bounded receive generator, joins
keepalive, and retires terminal parser, assembler, diagnostic and transport
references before returning. Caller cancellation cannot abandon this operation.
The public WebSockets close and receive APIs cannot drain a poisoned partial
stream: closed assemblers preserve queued frames. The adapter therefore contains
one explicit WebSockets 16.1.1 teardown; an unsupported version or outstanding
borrow fails acknowledgement and leaves the Deck backing and debt retained.
Default SDK connections without caller-funded input storage keep their existing
path. Admitted Deck PCM survives connection retirement.

`prepare_retirement.py` reuses the authenticated upstream archive, published
base patch and buffered-input extension, then applies `connection-retirement.patch`.
It checks exact source hashes before and after the new patch and verifies wheel,
source archive, notices and build materials correspondence. Blank context lines
are encoded as unchanged delete/add pairs so repository whitespace checks do not
rewrite the patch; `--unidiff-zero` permits the resulting boundary hunks only
after exact base verification. The published recipes and artifacts are preserved.

Run `build_retirement.ps1` with the same signed Python and retained upstream
archive used by `build_buffered.ps1`, supplying a fresh owned output directory.
Install the resulting wheel without dependency resolution into an owned SobaFM
development environment, then run `check_retirement_candidate.py`. The checks
use the actual SDK adapter and Deck with fake I/O, including repeated backing
reuse, queued audio, parser/receive/setup failure, cancellation, delayed closure,
sticky timeout, PCM preservation and unsupported teardown. Connection and Frame
weakrefs must disappear without forced garbage collection. No socket, TLS engine,
provider, authentication or native decoder is constructed. The check requires
the SobaFM checkout; the existing default SDK checks run in the same suite.

This component supplies no aggregate physical memory bound, profile selection
or readiness claim. Final artifact identities and local verification
are recorded in `CONNECTION-RETIREMENT-CANDIDATE.md`.
