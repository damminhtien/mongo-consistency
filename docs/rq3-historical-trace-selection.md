# Historical trace anchors for RQ3

These three RQ1 pairs were selected retrospectively as concrete examples that
motivated the RQ3 mechanism questions. They are not preregistered repetitions
and are not included in the RQ3 denominators. Each pair matches the named
configuration contrast and several observed topology/route fields, but the
seeds differ. The new, matched-seed RQ3 replays and their formal exemplars are
recorded in `results/raw/rq3/campaign-manifest.json` and
`results/analysis/rq3/selection-manifest.json`. The previous canonical campaign
is retained in Git history when the protocol-v2 outputs are regenerated.

`Raw-file SHA-256` is the digest of the JSON bytes. `history_hash` is the
canonical history digest validated by the repository history reader.

## M1: causal session, C5 versus C6, RYW

- C5: `results/raw/experiment/experiment-00089-C5-ryw.json`; raw-file
  SHA-256 `3772a533544c900965d37609bb1b5a5c50d1088be6087246a491ce1685b1b182`;
  `history_hash` `9498a573c8d93c771d2a299bd498e79507236d30ac2c6cb68dde1e6ba826bdd2`.
- C6: `results/raw/experiment/experiment-00335-C6-ryw.json`; raw-file
  SHA-256 `687660fee9e18a88f552c6c35e0ce1db2d1a1fffe49dff709ae0bb9356b72eb2`;
  `history_hash` `be06737266178e3b910dddddbe798104c5e5fb7fe4a817866796d109dccb934d`.

Both histories record initial primary `mongo3`, isolation target `mongo1`, W1
routed through `mongo3:27017`, and R1 routed to `mongo1:27017` while it is a
secondary. C5 returns v0 without `afterClusterTime`; C6 carries an
`afterClusterTime` equal to W1's operation time, then ends `UNAVAILABLE` with
`NetworkTimeout` and no value. Both histories later observe W1 on all three
members after healing. The pair is matched on these observed fields, not seed
(20261004 versus 20261250); the timeout is not evidence of an internal server
wait.

## M2: read concern, C8 versus C5, WFR

- C8: `results/raw/experiment/experiment-00019-C8-wfr.json`; raw-file
  SHA-256 `2f8d05aa81b0362890265b6e522924c71a524633b8a0f14ef28910cb1e5f5a3c`;
  `history_hash` `f256d5534bdbb36b323ef86592b7af98cfc4af3e6c7526aa82fb739ec6c4a1fb`.
- C5: `results/raw/experiment/experiment-00144-C5-wfr.json`; raw-file
  SHA-256 `31339fd1ef0d453612dbc6e5ca5904d1d3d00025b15ba9fe21325ce5f00fb144`;
  `history_hash` `b3a069014fd7a4a6a2658a370333f0662d8460816729129e1bfca270552b7a4b`.

Both histories record initial primary and isolation target `mongo2`, R1 routed
to `mongo2:27017` while it is primary, a majority-side election to `mongo3`,
and W2 routed to `mongo3:27017`. C8's local read returns v1 and its dependent
W2 records version 1; the converged final state contains versions `[0,2]` and
write IDs `init,w2`, so the returned v1 is absent. C5's majority read returns
v0 and its dependent W2 records version 0; it converges to the same final
state. The pair has different seeds (20260934 versus 20261059).

The RQ1 records capture the setup W1 member, timing, and success but not its
command-level write concern. The protocol specifies `w:1` for this WFR setup
at `docs/experimental-protocol.md` (steps 2-4 of the WFR schedule); treat that
concern as protocol-defined rather than directly observed in these two
histories.

## M3: write concern, C3 versus C6, MW

- C3: `results/raw/experiment/experiment-00001-C3-mw.json`; raw-file
  SHA-256 `745187ceb120d2feb22165f38c79f240ebe03ec568b0d631ab902ac896c7bc51`;
  `history_hash` `003f01945e43225183664ebf00872ed9800bc0dd3f00b6e837bfd8f5f933c2da`.
- C6: `results/raw/experiment/experiment-00086-C6-mw.json`; raw-file
  SHA-256 `f4ab238ace126d430f1ad2d8472db7f5c59374a33f6d7243a26e5b9a3bb658e7`;
  `history_hash` `82b972c46db9d934646385d2b359bc8df0c42fda07a3525d5f94ea55e68d5b62`.

Both histories start with `mongo3` primary, isolate `mongo3`, route W1 to
`mongo3:27017`, and apply the fault before W1. C3's `w:1` W1 is acknowledged;
after election and healing, W1 is absent from all three converged members. C6's
majority W1 ends indeterminate with `NetworkTimeout` and no response, but W1 is
present on all three converged members after healing. Thus its timeout did not
establish write failure.

This historical pair has different seeds (20260916 versus 20261001) and
different election paths: C3 elects `mongo1`, while C6 records no replacement
primary and ends with `mongo3` primary. It illustrates acknowledgement,
indeterminate outcome, and later effect; it is not a controlled causal pair.
