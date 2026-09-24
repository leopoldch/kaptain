# Shared decision snapshots

Fixtures read by both sides of the policy: `plugin/pkg/strategies` (Go) and `bridge/tests`
(Python) load these files and must agree on `expected`. They are the contract that keeps an
in-process policy and an out-of-process decider comparable — if a strategy changes on one
side only, a test here fails.

`expected` maps a strategy name to the node it must select on that snapshot. Selection is
the argmax of the raw scores, ties broken by node name.
