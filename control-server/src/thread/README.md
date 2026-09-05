# Thread

This internal Control Server module owns the durable `Thread` model and the
mapping from one thread to its sequential Sessions. It is transport-independent:
the HTTP gateway supplies authentication and platform adapters, while Thread
performs Thread transitions, routing, fencing, review decisions, immutable
Session grants, and result projection.

Execution is an internal runtime abstraction for one authority step in the
existing Session loop. Thread does not assign or persist Execution
IDs. A direct authenticated Session has `user` origin and starts read-only; the
Supervisor may advance it in place to exact requested effects. An external Slack
Session has `observe` origin and cannot self-activate mutation. Approval creates
a fresh `user` Session with only the reviewed effects; rejection closes the
Thread.

This placement keeps Thread in the existing Control Server process and
StatefulSet; the module boundary does not create another network service.
