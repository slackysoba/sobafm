# 0014: Treat descriptive Jamendo stream tags as description, not notices

- Status: Accepted interpretation; live qualification pending.
- Date: 2026-11-19
- Scope: [#237](https://github.com/slackysoba/sobafm/issues/237), from the
  [#220 A13 review](https://github.com/slackysoba/sobafm/issues/220#issuecomment-6999696062).
- Authority: the maintainer chose “Yes, fix and diagnose” on 2026-11-19, option (a) in
  [the #220 options comment](https://github.com/slackysoba/sobafm/issues/220#issuecomment-6999713377),
  recorded in #237.
- Interprets, without replacing: [ADR 0013](0013-jamendo-discovery-streaming.md)
  condition 3 of `jamendo-stream-1`.

## Problem

ADR 0013 condition 3 says that "additional required notice information or a
conflict" found in embedded tags stops the attempt. The checked stream compared
every embedded text value with the refreshed title, creators, backlink, licence,
notices and retained descriptions. Admission retains an album as `album: name`
and genre tags as `genres: rock`, so a bare `TALB` or `TCON` value never matched,
and year, track, length and encoder frames had nothing to match. Live attempts
A11 and A13 under #220 were refused before the first audio frame by one embedded
tag unit. The refusal did not say which check fired.

## Options

1. Keep every unmatched value as possible notice information. Ordinary provider
   files keep failing, and nobody can tell why.
2. Drop unknown frames silently. This loses the fail-closed rule for structures
   and notices that ADR 0013 requires.
3. Treat a fixed list of descriptive frames as description, not notice
   information, keep every contradiction and structure check, and log a fixed
   reason for each refusal. **Selected.**

## Decision

These ID3 text frames describe the recording, carry no rights claim, and are
not "additional required notice information" under ADR 0013 condition 3:
`TALB`, `TBPM`, `TCON`, `TDAT`, `TDOR`, `TDRC`, `TDRL`, `TENC`, `TIME`, `TLEN`,
`TORY`, `TPOS`, `TRCK`, `TSSE` and `TYER`, plus the ID3v1 album and year fields.

A value in one of these frames passes when it matches the bound recording data
(an existing bound value, or the bare album or tag value of a retained detail,
compared without case) or is neutral: at most 160 characters, with no link
(including a bare domain such as `label.fm`) and no rights, notice or
excluded-content wording such as copyright, licence, rights, terms, notice, ©,
℗, (c), cover, sample, preview or loop, and not the ID3 `(CR)` cover genre. Anything else in these frames refuses the stream.

Unchanged:

- Every embedded value still counts toward the tag, frame-count, frame-size and
  decoded-text budgets.
- Contradictions still refuse in every frame, including a different or unknown
  licence, all rights reserved, FMA-limited and a differing `creativecommons.org`
  path. So do control characters, encoding errors, unlisted frame IDs, unknown or
  oversized structures and budget overruns.
- Title, creator, people, copyright, URL, comment, lyrics, terms and user-defined
  frames keep the exact reconciliation rule. A new credit or notice is refused,
  never appended.
- Heard credits never change from embedded text.

Every stream check refusal carries a fixed reason and, for a frame, its
validated four-character ID. The `checked stream retired` log line reports both
as `check=` and `check_frame=`, with no tag text, values, titles, URLs or
identifiers.

## Consequences

Ordinary descriptive tags no longer stop a Jamendo stream. A file that puts a
rights statement in a descriptive frame still fails, with a reason. The neutral
wording check is conservative, so some harmless album names, such as one
containing "Rights", still refuse; the logged reason shows when that happens.
This interpretation does not establish that undisclosed rights or credits do not
exist, and it adds no provider or audio activity.

## Verification

Offline deterministic tests cover each refusal's reason and frame ID, descriptive
frames that match bound data or are neutral, the same frames carrying
contradictions or control characters, and the adapter log line. Whether real
Jamendo files now pass is established only by the next guided `/play` under #220,
after an image is built from the merged change.
