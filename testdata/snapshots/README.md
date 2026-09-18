# Shared decision snapshots

Fixtures used by both integrations: `plugin/pkg/strategies` (Go) and `bridge/tests` (Python)
read these files and must agree on `expected`. They are the contract that keeps the plugin
and the extender comparable — if a strategy changes on one side only, a test here fails.

`expected` maps a strategy name to the node it must select on that snapshot. Selection is
the argmax of the raw scores, ties broken by node name.
