# Documentation map

Each document has one job. If you are about to write something that fits two of them, it
probably belongs in a decision record instead.

| Document | Answers | Changes when |
|---|---|---|
| [architecture.md](architecture.md) | How the two integrations are built and what they share | The design changes |
| [plugin-design.md](plugin-design.md) | What the in-scheduler plugin does, extension point by extension point | The plugin changes |
| [measurement-protocol.md](measurement-protocol.md) | What we compare, how each metric is measured, what the testbed can support | The protocol changes |
| [experiments/](experiments/) | One file per experiment: its question, design, guards and outputs | A new experiment is specified or run |
| [decisions/](decisions/) | Why things are the way they are, one record per decision | A decision is taken or superseded |
| [related-work.md](related-work.md) | What the eight papers establish, and the gaps they leave | A paper is read or re-checked |
| [code-audit.md](code-audit.md) | What the code actually does, fixed and open findings | The code changes |
| [roadmap.md](roadmap.md) | What is next, and what is still unknown | Priorities move |

Project-level decisions, in French, live in the [root README](../../README.md). Paper
availability and reproduction status live in [reproduction/](../../reproduction/README.md).

## Conventions

**Language.** Everything written to a file is in English, including comments and commit
messages. Conversation is in French.

**Claims.** A factual claim about a paper is checked in that paper's PDF, never in our own
notes. A factual claim about Kubernetes behaviour is checked in the Kubernetes source at the
version we target, never in memory or documentation prose. Two examples where doing this
changed the design: scoring is skipped entirely when a single node survives filtering, and
extender calls sit outside the framework extension points, so the extension-point metric
does not contain them.

**Numbers.** Every measured number carries its boundary: where the clock starts, where it
stops, what it excludes. A number whose boundary is not stated does not go in a document.

**Status.** A document says plainly whether it describes something built, something
specified, or something hoped for. "Planned protocol, no results claimed" at the top of a
file is worth more than a hedge in the middle of it.

## Decision records

`decisions/` holds one short file per decision, numbered in the order taken:
context, the decision, its consequences, and what would make us revisit it. A decision that
turns out wrong is not deleted — it gets a `Superseded by` line, because the reasoning that
led to it is usually still worth reading.

Write one when a choice constrains later work, when it was contested, or when the obvious
option was rejected. Do not write one for something the code already states clearly.
