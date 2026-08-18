# DECISIONS

Status: not written.
Owner: maintainers. Frozen once ratified.

## D7. Push frames parse, pubsub does not exist
Ratified 2026-08-18. Resolves O1.
The parser produces `PushMessage` for `>` frames. The client exposes no pubsub
surface. Rationale is robustness rather than feature coverage: a client that
cannot parse `>` desynchronises permanently the first time client tracking is
enabled on its connection, and that failure is silent until it corrupts a
subsequent reply. Roughly twenty lines of parser code buys protocol coverage
that is honest rather than conveniently narrow.

## D8. Blob errors are ordinary errors
Ratified 2026-08-18. Resolves O2.
`!` parses into `ErrorReply` by the same rules as `-`. The distinction is not
preserved and is not tested. The one behavioral difference is that a blob error
payload may contain CR or LF, since its length is declared.
