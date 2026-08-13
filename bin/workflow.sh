#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
export MULTIAGENT_ROOT="$ROOT"
export MULTIAGENT_STATE_DIR="$STATE_DIR"

python3 - "$@" <<'PY'
import argparse
import csv
import fcntl
import hashlib
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


STATE_DIR = Path(os.environ["MULTIAGENT_STATE_DIR"])
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PHASES = {"pre-implementation", "implementation", "post-implementation", "complete"}
ACTIVE_TODO_STATUSES = {"open", "assigned", "in-progress"}
TODO_KINDS = {"direct", "evidence", "decision"}
REVIEW_TYPES = {"decision-authority", "decision-drift", "scope", "technical", "reflection"}
POST_REVIEW_TYPES = {"decision-drift", "scope", "technical", "reflection"}
TODO_FIELDS = [
    "todo_id", "kind", "summary", "origin", "status", "assignment_id",
    "resolution", "reason_code", "reason", "evidence", "authority",
    "destination", "resume_condition", "iteration", "updated_at",
]
REVIEW_FIELDS = [
    "review_id", "type", "verdict", "diff_hash", "evidence", "iteration", "recorded_at",
]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(message):
    print(f"workflow: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_id(label, value):
    if not value or not ID_RE.fullmatch(value):
        die(f"invalid {label}: {value}")


def workflow_dir(workflow_id):
    validate_id("workflow ID", workflow_id)
    return STATE_DIR / "workflows" / workflow_id / "lifecycle"


def paths(workflow_id):
    base = workflow_dir(workflow_id)
    return {
        "base": base,
        "state": base / "lifecycle.env",
        "todos": base / "todos.tsv",
        "reviews": base / "reviews.tsv",
        "events": base / "events.log",
        "lock": base / ".lock",
    }


class Lock:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+", encoding="utf-8")

    def __enter__(self):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_env(path):
    if not path.is_file():
        die(f"workflow lifecycle does not exist: {path.parent.parent.name}")
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def write_env(path, data):
    order = [
        "workflow_id", "phase", "iteration", "preimplementation_gate",
        "decision_id", "plan_id", "decision_revision", "implementation_context",
        "implementation_context_sha256", "authority_review_id", "candidate_diff_hash",
        "reviewed_diff_hash", "resume_count", "created_at", "updated_at",
    ]
    text = "".join(f"{key}={data.get(key, '')}\n" for key in order)
    atomic_text(path, text)


def init_table(path, fields):
    if path.exists():
        return
    atomic_text(path, "\t".join(fields) + "\n")


def read_table(path, fields):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    for row in rows:
        for field in fields:
            row.setdefault(field, "")
    return rows


def write_table(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def append_event(path, event, details=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now()}\t{event}\t{details}\n")


def initial_state(workflow_id):
    stamp = now()
    return {
        "workflow_id": workflow_id,
        "phase": "pre-implementation",
        "iteration": "1",
        "preimplementation_gate": "pending",
        "decision_id": "",
        "plan_id": "",
        "decision_revision": "",
        "implementation_context": "",
        "implementation_context_sha256": "",
        "authority_review_id": "",
        "candidate_diff_hash": "",
        "reviewed_diff_hash": "",
        "resume_count": "0",
        "created_at": stamp,
        "updated_at": stamp,
    }


def initialize(workflow_id, resume):
    p = paths(workflow_id)
    with Lock(p["lock"]):
        if p["state"].exists():
            state = read_env(p["state"])
            if not resume:
                die(f"workflow already exists: {workflow_id}; use resume mode")
            if state.get("phase") not in PHASES:
                die(f"persisted workflow has invalid phase: {state.get('phase')}")
            state["resume_count"] = str(int(state.get("resume_count", "0")) + 1)
            state["updated_at"] = now()
            write_env(p["state"], state)
            init_table(p["todos"], TODO_FIELDS)
            init_table(p["reviews"], REVIEW_FIELDS)
            append_event(p["events"], "workflow_resumed", f"phase={state['phase']}")
            print(f"workflow resumed\t{workflow_id}\t{state['phase']}")
            return
        state = initial_state(workflow_id)
        p["base"].mkdir(parents=True, exist_ok=True)
        write_env(p["state"], state)
        init_table(p["todos"], TODO_FIELDS)
        init_table(p["reviews"], REVIEW_FIELDS)
        append_event(p["events"], "workflow_initialized", f"resume_requested={int(resume)}")
        print(f"workflow initialized\t{workflow_id}\tpre-implementation")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def active_todos(rows):
    return [row for row in rows if row.get("status") in ACTIVE_TODO_STATUSES]


def review_by_id(rows, review_id):
    return next((row for row in rows if row.get("review_id") == review_id), None)


def validate_implementation_context(state):
    context_text = state.get("implementation_context", "")
    if not context_text:
        die("implementation gate requires approved implementation context")
    context = Path(context_text)
    if not context.is_file():
        die(f"approved implementation context is missing: {context}")
    actual = sha256(context)
    if actual != state.get("implementation_context_sha256"):
        die("approved implementation context changed after pre-implementation approval")


def read_simple_env(path):
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def validate_committed_decision(decision_id, plan_id):
    decision_dir = STATE_DIR / "decisions" / decision_id
    metadata = read_simple_env(decision_dir / "decision.env")
    outcome = read_simple_env(decision_dir / "outcome.env")
    if metadata.get("status") != "committed":
        die(f"decision ledger is not committed: {decision_id}")
    if outcome.get("selected_plan") != plan_id:
        die(
            f"decision ledger selected plan {outcome.get('selected_plan', 'missing')} "
            f"does not match requested plan {plan_id}"
        )


def implementation_gate(workflow_id, expected_decision="", expected_plan="", allow_pre=False):
    p = paths(workflow_id)
    state = read_env(p["state"])
    valid_phases = {"implementation", "pre-implementation"} if allow_pre else {"implementation"}
    if state.get("phase") not in valid_phases:
        die(f"implementation gate requires phase=implementation, got {state.get('phase')}")
    if state.get("preimplementation_gate") != "passed":
        die("implementation gate has not passed")
    validate_implementation_context(state)
    todos = active_todos(read_table(p["todos"], TODO_FIELDS))
    blockers = [row["todo_id"] for row in todos if row.get("kind") in {"evidence", "decision"}]
    if blockers:
        die("implementation blocked by active evidence/decision TODOs: " + ",".join(blockers))
    if expected_decision and expected_decision != state.get("decision_id"):
        die(f"assignment decision {expected_decision} does not match workflow decision {state.get('decision_id')}")
    if expected_plan and expected_plan != state.get("plan_id"):
        die(f"assignment plan {expected_plan} does not match workflow plan {state.get('plan_id')}")
    return state


def required_post_reviews(p, state):
    iteration = state.get("iteration")
    diff_hash = state.get("candidate_diff_hash")
    rows = read_table(p["reviews"], REVIEW_FIELDS)
    passed = {
        row["type"]
        for row in rows
        if row.get("iteration") == iteration
        and row.get("diff_hash") == diff_hash
        and row.get("verdict") == "pass"
    }
    return sorted(POST_REVIEW_TYPES - passed)


def completion_check(workflow_id):
    p = paths(workflow_id)
    state = read_env(p["state"])
    if state.get("phase") not in {"post-implementation", "complete"}:
        die(f"completion requires phase=post-implementation, got {state.get('phase')}")
    active = active_todos(read_table(p["todos"], TODO_FIELDS))
    if active:
        die("completion blocked by active TODOs: " + ",".join(row["todo_id"] for row in active))
    if not state.get("candidate_diff_hash"):
        die("completion requires a candidate diff hash")
    missing = required_post_reviews(p, state)
    if missing:
        die("completion requires passing current-diff reviews: " + ",".join(missing))
    validate_implementation_context(state)
    return state


def cmd_status(args):
    p = paths(args.workflow_id)
    state = read_env(p["state"])
    todos = read_table(p["todos"], TODO_FIELDS)
    reviews = read_table(p["reviews"], REVIEW_FIELDS)
    print(p["state"].read_text(encoding="utf-8"), end="")
    print(f"active_todo_count={len(active_todos(todos))}")
    print(f"review_count={len(reviews)}")


def cmd_prepare(args):
    validate_id("decision ID", args.decision_id)
    validate_id("plan ID", args.plan_id)
    validate_id("review ID", args.authority_review)
    validate_committed_decision(args.decision_id, args.plan_id)
    p = paths(args.workflow_id)
    with Lock(p["lock"]):
        state = read_env(p["state"])
        if state.get("phase") != "pre-implementation":
            die("prepare-implementation requires phase=pre-implementation")
        reviews = read_table(p["reviews"], REVIEW_FIELDS)
        review = review_by_id(reviews, args.authority_review)
        if not review or review.get("type") != "decision-authority" or review.get("verdict") != "pass":
            die("prepare-implementation requires a passing decision-authority review")
        blockers = [
            row["todo_id"] for row in active_todos(read_table(p["todos"], TODO_FIELDS))
            if row.get("kind") in {"evidence", "decision"}
        ]
        if blockers:
            die("pre-implementation blocked by active evidence/decision TODOs: " + ",".join(blockers))
        context = Path(args.implementation_context).resolve()
        if not context.is_file():
            die(f"approved implementation context not found: {context}")
        state.update({
            "preimplementation_gate": "passed",
            "decision_id": args.decision_id,
            "plan_id": args.plan_id,
            "decision_revision": args.decision_revision,
            "implementation_context": str(context),
            "implementation_context_sha256": sha256(context),
            "authority_review_id": args.authority_review,
            "updated_at": now(),
        })
        write_env(p["state"], state)
        append_event(p["events"], "implementation_prepared", f"decision_id={args.decision_id}\tplan_id={args.plan_id}\treview_id={args.authority_review}")
    print(f"implementation prepared\t{args.workflow_id}\t{args.decision_id}\t{args.plan_id}")


def cmd_transition(args):
    if args.phase not in PHASES:
        die(f"invalid phase: {args.phase}")
    p = paths(args.workflow_id)
    with Lock(p["lock"]):
        state = read_env(p["state"])
        current = state.get("phase")
        target = args.phase
        allowed = {
            "pre-implementation": {"implementation"},
            "implementation": {"post-implementation"},
            "post-implementation": {"pre-implementation", "complete"},
            "complete": set(),
        }
        if target not in allowed.get(current, set()):
            die(f"invalid lifecycle transition: {current} -> {target}")
        if current == "pre-implementation":
            implementation_gate(args.workflow_id, allow_pre=True)
            state["phase"] = "implementation"
        elif current == "implementation":
            if not args.diff_hash:
                die("implementation -> post-implementation requires --diff-hash")
            state["phase"] = "post-implementation"
            state["candidate_diff_hash"] = args.diff_hash
            state["reviewed_diff_hash"] = ""
        elif target == "pre-implementation":
            active = active_todos(read_table(p["todos"], TODO_FIELDS))
            if not active:
                die("post-implementation -> pre-implementation requires an active TODO")
            state["phase"] = "pre-implementation"
            state["iteration"] = str(int(state.get("iteration", "1")) + 1)
            state["preimplementation_gate"] = "pending"
            state["decision_revision"] = ""
            state["implementation_context"] = ""
            state["implementation_context_sha256"] = ""
            state["authority_review_id"] = ""
            state["candidate_diff_hash"] = ""
            state["reviewed_diff_hash"] = ""
        elif target == "complete":
            completion_check(args.workflow_id)
            state["phase"] = "complete"
            state["reviewed_diff_hash"] = state.get("candidate_diff_hash", "")
        state["updated_at"] = now()
        write_env(p["state"], state)
        append_event(p["events"], "phase_transitioned", f"from={current}\tto={target}\titeration={state['iteration']}")
    print(f"workflow transitioned\t{args.workflow_id}\t{current}\t{target}")


def cmd_add_todo(args):
    validate_id("TODO ID", args.todo_id)
    if args.kind not in TODO_KINDS:
        die(f"invalid TODO kind: {args.kind}")
    p = paths(args.workflow_id)
    with Lock(p["lock"]):
        state = read_env(p["state"])
        rows = read_table(p["todos"], TODO_FIELDS)
        if any(row["todo_id"] == args.todo_id for row in rows):
            die(f"TODO already exists: {args.todo_id}")
        rows.append({
            "todo_id": args.todo_id, "kind": args.kind, "summary": args.summary,
            "origin": args.origin, "status": "open", "assignment_id": "",
            "resolution": "", "reason_code": "", "reason": "",
            "evidence": "", "authority": "", "destination": "",
            "resume_condition": "", "iteration": state["iteration"], "updated_at": now(),
        })
        write_table(p["todos"], TODO_FIELDS, rows)
        append_event(p["events"], "todo_added", f"todo_id={args.todo_id}\tkind={args.kind}")
    print(f"TODO added\t{args.workflow_id}\t{args.todo_id}\t{args.kind}")


def find_todo(rows, todo_id):
    row = next((row for row in rows if row.get("todo_id") == todo_id), None)
    if not row:
        die(f"TODO does not exist: {todo_id}")
    return row


def cmd_todo_status(args):
    if args.status not in ACTIVE_TODO_STATUSES:
        die(f"invalid active TODO status: {args.status}")
    p = paths(args.workflow_id)
    with Lock(p["lock"]):
        read_env(p["state"])
        rows = read_table(p["todos"], TODO_FIELDS)
        row = find_todo(rows, args.todo_id)
        if row.get("status") not in ACTIVE_TODO_STATUSES:
            die(f"cannot reactivate resolved TODO without a new TODO: {args.todo_id}")
        if args.status in {"assigned", "in-progress"} and not args.assignment_id:
            die(f"TODO status {args.status} requires --assignment-id")
        row["status"] = args.status
        row["assignment_id"] = args.assignment_id
        row["updated_at"] = now()
        write_table(p["todos"], TODO_FIELDS, rows)
        append_event(p["events"], "todo_status_changed", f"todo_id={args.todo_id}\tstatus={args.status}")
    print(f"TODO status\t{args.workflow_id}\t{args.todo_id}\t{args.status}")


def cmd_resolve_todo(args):
    if args.resolution not in {"completed", "skipped"}:
        die(f"invalid TODO resolution: {args.resolution}")
    if not args.evidence:
        die("TODO resolution requires --evidence")
    if args.resolution == "skipped":
        if args.reason_code not in {"out-of-scope", "unavailable-now"}:
            die("skipped TODO requires --reason-code out-of-scope|unavailable-now")
        if not args.reason or args.authority not in {"orchestrator", "user"}:
            die("skipped TODO requires --reason and --authority orchestrator|user")
        if args.reason_code == "unavailable-now" and not (args.destination or args.resume_condition):
            die("unavailable-now skip requires --destination or --resume-condition")
    p = paths(args.workflow_id)
    with Lock(p["lock"]):
        read_env(p["state"])
        rows = read_table(p["todos"], TODO_FIELDS)
        row = find_todo(rows, args.todo_id)
        if row.get("status") not in ACTIVE_TODO_STATUSES:
            die(f"TODO is already resolved: {args.todo_id}")
        row.update({
            "status": args.resolution,
            "resolution": args.resolution,
            "reason_code": args.reason_code,
            "reason": args.reason,
            "evidence": args.evidence,
            "authority": args.authority,
            "destination": args.destination,
            "resume_condition": args.resume_condition,
            "updated_at": now(),
        })
        write_table(p["todos"], TODO_FIELDS, rows)
        append_event(p["events"], "todo_resolved", f"todo_id={args.todo_id}\tresolution={args.resolution}\treason_code={args.reason_code}")
    print(f"TODO resolved\t{args.workflow_id}\t{args.todo_id}\t{args.resolution}")


def cmd_record_review(args):
    validate_id("review ID", args.review_id)
    if args.type not in REVIEW_TYPES:
        die(f"invalid review type: {args.type}")
    if args.verdict not in {"pass", "findings"}:
        die(f"invalid review verdict: {args.verdict}")
    if not args.evidence:
        die("review requires --evidence")
    p = paths(args.workflow_id)
    with Lock(p["lock"]):
        state = read_env(p["state"])
        if args.type == "decision-authority":
            if state.get("phase") != "pre-implementation":
                die("decision-authority review requires phase=pre-implementation")
            diff_hash = "-"
        else:
            if state.get("phase") != "post-implementation":
                die(f"{args.type} review requires phase=post-implementation")
            diff_hash = args.diff_hash or ""
            if diff_hash != state.get("candidate_diff_hash"):
                die("post-implementation review diff hash does not match candidate diff")
        rows = read_table(p["reviews"], REVIEW_FIELDS)
        if any(row["review_id"] == args.review_id for row in rows):
            die(f"review already exists: {args.review_id}")
        rows.append({
            "review_id": args.review_id, "type": args.type, "verdict": args.verdict,
            "diff_hash": diff_hash, "evidence": args.evidence,
            "iteration": state["iteration"], "recorded_at": now(),
        })
        write_table(p["reviews"], REVIEW_FIELDS, rows)
        append_event(p["events"], "review_recorded", f"review_id={args.review_id}\ttype={args.type}\tverdict={args.verdict}\tdiff_hash={diff_hash}")
    print(f"review recorded\t{args.workflow_id}\t{args.review_id}\t{args.type}\t{args.verdict}")


def cmd_gate(args):
    if args.gate == "implementation":
        state = implementation_gate(args.workflow_id, args.decision_id, args.plan_id)
        print(f"gate passed\t{args.workflow_id}\timplementation\t{state['decision_revision']}\t{state['implementation_context_sha256']}")
    else:
        state = completion_check(args.workflow_id)
        print(f"gate passed\t{args.workflow_id}\tcompletion\t{state['candidate_diff_hash']}")


def cmd_value(args):
    state = read_env(paths(args.workflow_id)["state"])
    if args.key not in state:
        die(f"unknown lifecycle field: {args.key}")
    print(state[args.key])


parser = argparse.ArgumentParser(prog="bin/workflow.sh")
sub = parser.add_subparsers(dest="command", required=True)

init = sub.add_parser("init")
init.add_argument("workflow_id")
init.set_defaults(func=lambda a: initialize(a.workflow_id, False))

ior = sub.add_parser("init-or-resume")
ior.add_argument("workflow_id")
ior.add_argument("--resume", choices=["0", "1"], required=True)
ior.set_defaults(func=lambda a: initialize(a.workflow_id, a.resume == "1"))

status = sub.add_parser("status")
status.add_argument("workflow_id")
status.set_defaults(func=cmd_status)

prepare = sub.add_parser("prepare-implementation")
prepare.add_argument("workflow_id")
prepare.add_argument("--decision-id", required=True)
prepare.add_argument("--plan-id", required=True)
prepare.add_argument("--decision-revision", required=True)
prepare.add_argument("--implementation-context", required=True)
prepare.add_argument("--authority-review", required=True)
prepare.set_defaults(func=cmd_prepare)

transition = sub.add_parser("transition")
transition.add_argument("workflow_id")
transition.add_argument("phase")
transition.add_argument("--diff-hash", default="")
transition.set_defaults(func=cmd_transition)

add_todo = sub.add_parser("add-todo")
add_todo.add_argument("workflow_id")
add_todo.add_argument("todo_id")
add_todo.add_argument("--kind", required=True)
add_todo.add_argument("--summary", required=True)
add_todo.add_argument("--origin", default="orchestrator")
add_todo.set_defaults(func=cmd_add_todo)

todo_status = sub.add_parser("todo-status")
todo_status.add_argument("workflow_id")
todo_status.add_argument("todo_id")
todo_status.add_argument("status")
todo_status.add_argument("--assignment-id", default="")
todo_status.set_defaults(func=cmd_todo_status)

resolve = sub.add_parser("resolve-todo")
resolve.add_argument("workflow_id")
resolve.add_argument("todo_id")
resolve.add_argument("--resolution", required=True)
resolve.add_argument("--evidence", required=True)
resolve.add_argument("--reason-code", default="")
resolve.add_argument("--reason", default="")
resolve.add_argument("--authority", default="")
resolve.add_argument("--destination", default="")
resolve.add_argument("--resume-condition", default="")
resolve.set_defaults(func=cmd_resolve_todo)

review = sub.add_parser("record-review")
review.add_argument("workflow_id")
review.add_argument("review_id")
review.add_argument("--type", required=True)
review.add_argument("--verdict", required=True)
review.add_argument("--diff-hash", default="")
review.add_argument("--evidence", required=True)
review.set_defaults(func=cmd_record_review)

gate = sub.add_parser("gate")
gate.add_argument("workflow_id")
gate.add_argument("gate", choices=["implementation", "completion"])
gate.add_argument("--decision-id", default="")
gate.add_argument("--plan-id", default="")
gate.set_defaults(func=cmd_gate)

complete = sub.add_parser("completion-check")
complete.add_argument("workflow_id")
complete.set_defaults(func=lambda a: print(f"completion ready\t{a.workflow_id}\t{completion_check(a.workflow_id)['candidate_diff_hash']}"))

value = sub.add_parser("value")
value.add_argument("workflow_id")
value.add_argument("key")
value.set_defaults(func=cmd_value)

args = parser.parse_args()
args.func(args)
PY
