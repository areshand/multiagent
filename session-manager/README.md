# Session Manager

This component owns the durable `Thread` model and the mapping from one thread
to its sequential execution sessions. It is transport-independent: the HTTP
gateway supplies authentication and execution adapters, while the session
manager performs thread transitions, routing, fencing, review decisions, and
result projection.

The MVP is hosted in the same process and StatefulSet as `control-server`; this
package boundary does not create another network service.
