# Historical trace anchors for RQ3

These three RQ1 pairs were selected retrospectively as examples for the RQ3
mechanism questions. They are not preregistered repetitions and are excluded
from the RQ3 denominators. The six source histories are preserved in
`results/raw/rq3-historical-anchors/`. The current 256-history RQ1 campaign is
in `results/raw/experiment/`. Each historical pair uses different seeds, so it
illustrates recorded mechanism evidence without estimating a causal effect.
The matched-seed RQ3 replays and their formal exemplars are recorded in
`results/raw/rq3/campaign-manifest.json` and
`results/analysis/rq3/selection-manifest.json`.

`Raw-file SHA-256` is the digest of the JSON bytes. `history_hash` is the
canonical history digest validated by the repository history reader.

## M1: causal session, C5 versus C6, RYW

- C5: `results/raw/rq3-historical-anchors/experiment-00080-C5-ryw.json`; raw-file
  SHA-256 `41a13d65366f2db68b7b70cce65a15e9d06306f573d9e4b802aa848ac1ebeb16`;
  `history_hash` `07e9f4af74fb7b1c740adaaf62deae4aacdbc360f19436d33990b57ece25ef23`.
- C6: `results/raw/rq3-historical-anchors/experiment-00087-C6-ryw.json`; raw-file
  SHA-256 `d644bbe829097e22fd41e0ad3ddac1116410efcc505560114a3b77daba131be2`;
  `history_hash` `bee626ef9970d440c064eaf2733e648f4c48404af7f89bdca58133ac6cbe15f1`.

The C5 history contains a completed stale read and an RYW violation. The C6
history does not complete the required read and is therefore unavailable. The
seeds and realized routes differ, so this is an illustrative pair rather than a
controlled causal comparison.

## M2: read concern, C8 versus C5, WFR

- C8: `results/raw/rq3-historical-anchors/experiment-00026-C8-wfr.json`; raw-file
  SHA-256 `624ca2da7078163a8a5eb16426cb6d5ee48a5d0d2a9e8c19a3f7e91b920004ea`;
  `history_hash` `a6b262c7ddda5555cd97f040455668e86980b50735cb63f71c789df27ca71f05`.
- C5: `results/raw/rq3-historical-anchors/experiment-00160-C5-wfr.json`; raw-file
  SHA-256 `ffb3524a8bc4cced2d7295fcfe58ac0e04aa59ec99408e2f6b1cc89f31b1ef40`;
  `history_hash` `e746327f92f580c7d3bf8395e16381cbeff722dfd63abb2dce0ddcbcc619561b`.

The C8 history reads v1 and its dependent write uses a pre-image at v0, which
is a WFR violation. The C5 history reads v0 and its dependent write uses that
same version, which passes the recorded oracle. The seeds and realized routes
differ, so the pair illustrates the mechanism without claiming a matched
causal effect.

The RQ1 records capture the setup W1 member, timing, and success but not its
command-level write concern. The protocol specifies `w:1` for this WFR setup
at `docs/experimental-protocol.md` (steps 2-4 of the WFR schedule); treat that
concern as protocol-defined rather than directly observed in these two
histories.

## M3: write concern, C3 versus C6, MW

- C3: `results/raw/rq3-historical-anchors/experiment-00120-C3-mw.json`; raw-file
  SHA-256 `7e45fd7bf9b615d56c9bb2a49b9d7cef9ccee07e7e98db9a9a39acece7629264`;
  `history_hash` `890ff552de909270f94d27aca11af2a57ce296af3bc7d89b81e8c63fad1d284e`.
- C6: `results/raw/rq3-historical-anchors/experiment-00009-C6-mw.json`; raw-file
  SHA-256 `5f4768116f432d28ed8288e7929b5213c2420d4611eeb6da9bbaef443d204802`;
  `history_hash` `23df9197944b155c8370ce6f39be0cc093b183cabb2b3852632c385ec716a114`.

The C3 history completes W1, then the successive write runs on a state whose
pre-image does not contain W1, producing an MW violation. The C6 history does
not complete the required majority write and is indeterminate. The seeds and
election paths differ, so this pair is a mechanism illustration rather than a
controlled causal estimate.
