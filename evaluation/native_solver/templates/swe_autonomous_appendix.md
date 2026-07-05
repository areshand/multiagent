
## SWE Bench Pro Autonomous Evaluation Mode

You are running in a benchmark task container. The user is not available for
follow-up. Your goal is to use the production multiagent workflow to solve the
issue below and leave the final accepted patch in the git working tree at
`/app`.

Hard requirements:

1. Use the normal multiagent structure: orchestrator-controlled workers,
   verifier review, and accepted follow-up cycles when useful.
2. Use Codex for orchestrator, workers, subagents, and verifiers.
3. The target repository is `/app`; the multiagent implementation lives at
   `/opt/multiagent`.
4. Worker worktrees/state may live under `/tmp/multiagent-prod-swe`, but the
   final accepted changes must be applied back to `/app` before completion.
5. Do not ask the user for clarification. Make a reasonable assumption and
   record it in the final status if needed.
6. Do not modify tests, lockfiles, generated assets, bundled public assets, or
   unrelated config unless the issue explicitly requires it. Benchmark-required
   fixture/testdata files are the exception: if official expected tests or the
   official test patch reference missing files under paths such as `testdata/`,
   `fixtures/`, `golden/`, or snapshot directories, add the minimal required
   fixture assets so the normative tests can run. In web repos, paths such as
   `public/assets/`, `public/build/`, `public/dist/`, bundled `*.bundle.*`, and
   minified `*.min.*` outputs are generated artifacts, not acceptable source
   fixes.
7. Run focused validation when practical. If full validation is too expensive,
   run the narrowest targeted check you can identify from nearby tests, package
   scripts, or repository conventions, and record exactly what ran.
   Prefer the whole relevant test file/package over a single guessed test name
   when the file/package is cheap enough to run. Many benchmark failures hide
   in adjacent cases inside the same file.
   If the task says a class/function/type "must be exposed as" a specific name,
   implement that exact public symbol in source before trusting visible tests.
   If the adapter lists official `FAIL_TO_PASS` or `PASS_TO_PASS` tests, treat
   those test names as normative. Do not call one stale, fixture-mismatched, or
   incompatible to justify completion; either make the selected test pass, prove
   the official harness does not run it, or write blocked status.
8. When finished, write JSON to `/tmp/multiagent-prod-swe/status.json`:
   `{"status":"completed","summary":"...","validation":"...","risk":"..."}`
   If blocked, write `{"status":"blocked","reason":"..."}`.
   If helper-scope or resend/expiry gates were relevant, the `validation` string
   must copy the exact verifier markers, including `bulk-helper-contract-checked:`
   and the inspected resend gate name such as `canSendValidation`. Verifier pane
   prose alone is not sufficient because the adapter trusts `status.json` as the
   completion contract.
9. A natural-language final answer is not completion. The benchmark adapter only
   observes `/tmp/multiagent-prod-swe/status.json` and `/app` git state.
10. The local shell is the intended benchmark interface. Do not stop because a
    command seems unavailable unless you have retried with explicit paths and
    then written a blocked status JSON.

Benchmark spawning path:

- Run multiagent helper commands from `/opt/multiagent`, while keeping
  `MULTIAGENT_ROOT=/app`.
- Do not use the manual `tmux new-window` worktree recipe from the general
  prompt in this benchmark container. Instead, use `bin/subagent.sh spawn` for
  workers and verifiers; it preserves the benchmark Codex bridge through
  `CODEX_BIN`.
- A worker can operate directly on `/app` for this benchmark. Keep worker
  instructions bounded to the relevant source files and consolidate the final
  accepted patch in `/app`.
- Never use `--owned .`, `/app`, or the whole repository root for a benchmark
  assignment. If the relevant source path is unclear, run read-only discovery
  first, then assign the narrowest likely non-test source file(s) or source
  directories.
- Before any source implementation happens, spawn at least one worker with:

  ```bash
  cd /opt/multiagent
  bin/subagent.sh assignment-create worker-01-fix --assignment-id SWE-001 --branch benchmark --owned RELATIVE_SOURCE_PATH
  bin/subagent.sh spawn worker-01-fix --instruction "You are a worker agent launched by the orchestrator. Work in /app only. Report progress and final status here. Task: ..."
  ```

- Worker and verifier names must be ordinary assignment names such as
  `worker-01-fix`, `worker-02-followup`, or `verifier-01-fix`. Never use
  option-looking names such as `--help`, `--instruction`, `-h`, or any name that
  starts with `-`; that creates a help/no-prompt process instead of a worker.
- When a worker/verifier instruction contains code identifiers, shell syntax,
  backticks, angle brackets, dollar signs, or quotes, do not pass it through a
  double-quoted shell string. Write the instruction to a temporary file or use a
  quoted heredoc, then pass the exact text to `bin/subagent.sh spawn`. A spawn
  command that lets the shell expand identifiers has changed the task and must
  be retried with literal instruction text.
- Benchmark containers can be minimal. Prefer `rg` when present, but if `rg` is
  not installed use `grep`, `find`, or language-native search instead of failing
  the task.
- If the issue has unclear ownership, multiple plausible fixes, or needs
  behavior inference from tests, first spawn a short read-only scout worker
  named `scout-01-...`. The scout must not edit files; it should identify the
  likely source files, relevant existing test files/packages, and one minimal
  behavior hypothesis. Use that output to bound the implementation worker.
  The scout must decompose the issue into every observable requirement from the
  title, description, expected behavior, and "what happened" sections. Do not
  let the scout collapse a multi-clause issue into the first obvious feature
  file.
- The scout must also name candidate helper APIs, their source files, and their
  nearby validation files when the behavior depends on database/cache/key,
  parser, serializer, adapter, or transport abstractions. Treat those helper
  files as first-class ownership candidates, not background reading.

- After worker completion, spawn a verifier the same way, with
  `SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn verifier-01-fix --instruction "Review only; do not edit files. ..."`
- A completed worker pane is not an interactive worker anymore. Do not send
  follow-up implementation instructions to an existing worker with `tmux
  send-keys`; that only writes text into a finished shell and does not run
  Codex. Every implementation follow-up must use `assignment-create` plus
  `bin/subagent.sh spawn` with a fresh bounded worker name such as
  `worker-02-followup`.
- Before spawning a replacement worker over the same source files or package,
  poll and inspect any existing worker/verifier for those paths. If it is still
  running an expensive compile/test command, wait for it or kill/finalize it
  deliberately before starting another. Do not leave duplicate workers running
  the same package validation; concurrent Go/npm/yarn/pytest jobs can contend
  for caches, consume memory, and turn a solvable task into an infra failure.
- Maintain a validation lease table for expensive commands. For each package,
  test file, component suite, or build target, keep one owner, command, state,
  and resource-risk note. A follow-up worker or verifier must inherit, wait for,
  or explicitly release the existing lease before running an equivalent command.
  When overlap is unclear, spawn a read-only validation coordinator before
  launching more workers.
- Do not spawn a verifier while a worker still owns a running validation lease.
  If a worker final message appears before its `go test`, `npm test`, `pytest`,
  or equivalent selected command exits, poll the worker/process list until the
  command result is captured, then pass that result to the verifier. A verifier
  without an explicit released validation lease must not rerun the same command.
- If worker/verifier spawning fails, record the exact blocker in
  `/tmp/multiagent-prod-swe/status.json` only after retrying once with a fresh,
  differently named bounded worker or verifier. Do not abandon a task with an
  empty diff if a bounded worker can still be spawned.
- If the benchmark adapter sends an additional follow-up after a completion
  marker, treat it as a verifier rejection. Remove the weak status marker and
  continue the orchestration loop. If the follow-up names implementation-scope
  blockers, spawn a new bounded worker whose owned paths include the named
  helper-layer source directories/files, even if the first patch was only in a
  top-level feature module.
- `apply_patch` should be available on `PATH`; if a shell cannot find it, use
  `/usr/local/bin/apply_patch`.

Worker quality bar:

- The worker must first restate the issue as an observable behavior change and
  identify the likely source files before editing.
- The worker must maintain an explicit requirement checklist from the issue
  text. Each checklist item needs one of: a source change, a source-level reason
  no change is needed, or a blocked note. Do not finish after fixing only the
  first visible symptom.
- The worker must prefer the smallest source-only patch that directly addresses
  the issue. Broad rewrites and speculative cleanups usually fail hidden tests.
- For UI/component tasks, classify the issue before editing. If it asks for an
  additive public surface such as Storybook coverage, a story named `Basic`, an
  export, example, or component exposure, preserve the existing component
  implementation and add the smallest public surface. Do not rewrite focus,
  input, paste, keyboard, accessibility, or form integration behavior unless the
  issue explicitly requires behavior changes. If those interaction paths are
  touched, run or attempt the full nearby component interaction test file, not
  only a new story or smoke case.
- The worker must inspect existing tests or call sites that encode the expected
  behavior, even if it cannot run the full suite.
- If the issue, contract ledger, or official test excerpt shows a literal
  expected value, command argv, serialized output, error text, or ordered list,
  the worker must treat that exact shape as normative. Preserve order and
  punctuation unless source evidence proves the excerpt is only illustrative.
  If the exact official test is unavailable locally, create a temporary
  source-level probe that asserts the same literal shape; do not substitute a
  weaker semantic smoke check.
- Treat every symbol referenced by issue text, visible tests, official expected
  tests, or official test excerpts as a compatibility contract, including
  package-private or unexported helpers in same-package tests. Do not change a
  referenced helper's name, arity, parameter order, return shape, or package
  placement unless you have source evidence that all expected tests and callers
  use the new shape. Hidden tests may compile package-private helpers directly.
- For compiled languages, a timed-out compile/test command is not validation
  success. If a package compile check cannot complete, explicitly inspect
  test-referenced helper signatures and record the timeout as unresolved risk
  unless a narrower compile check or source-level compatibility proof covers it.
- The worker must trace helper APIs called by the feature path. If the issue
  mentions missing keys, fallback lookup, arrays/lists of keys, falsy inputs,
  expired records, or alternative sources, inspect the relevant database/cache
  abstraction methods and nearby tests, not only the top-level feature module.
- If the issue uses plural key language ("keys", "sources", "fallbacks",
  "records") or the implementation needs to read more than one possible key,
  inspect bulk key helper contracts too, such as multi-get/get-many APIs and
  empty/falsy input behavior. If the abstraction is missing or inconsistent
  across adapters, include the database/cache helper source files in scope
  instead of emulating the behavior only in the feature module.
- When plural keys, fallback sources, or alternative data sources are in the
  issue and the repository has database/cache adapters, the first implementation
  plan must include a helper-layer ownership decision before coding. If a
  portable bulk string-key helper is absent or uncertain, spawn a bounded
  database/cache helper worker up front. Do not wait until after a feature-only
  worker and verifier have finished to discover this requirement.
- For database/cache tasks, a missing portable bulk string-key getter is not a
  skip reason when plural keys, fallbacks, or multiple records are in scope.
  Search source and tests for names such as `mget`, `getMany`, `multiGet`, and
  "multiple keys". If the repository expects such a helper or neighboring
  helper APIs imply it, implement the minimal cross-adapter helper contract in
  the database/cache source layer. The contract should preserve input order,
  return `null` for missing keys, return `[]` for empty/falsy key arrays, and
  behave consistently across adapters.
- A feature-level scan/getObject/getObjects fallback is not a substitute for an
  issue-required repository-level bulk string-key helper when the issue/source
  names a helper such as `mget`, `getMany`, `multiGet`, or equivalent string-key
  bulk lookup. In that case, spawn a helper-layer worker whose owned files
  include the database/cache adapters and implement or prove the portable helper
  contract before changing only the feature module. If the fallback is over
  existing hash/object records, an existing portable hash-object helper such as
  `getObjects` can satisfy this requirement, but the verifier/status must say
  that explicitly with `bulk-helper-contract-checked:`.
- If the issue mentions re-send, resend, retry, throttling, expiry, expiration,
  TTL, or "after some time", the worker must inspect and reason through every
  resend/expiry gate in the flow, not only confirmation. For email-validation
  style tasks this includes send, can-send, pending, expiry, expire, confirm,
  and status helpers. A fallback that finds old confirmation data must not make
  an expired resend throttle look permanently pending.
- If the issue mentions Validate/validation actions and fallback for missing
  expected keys, inspect both the predicate and the action path. For NodeBB-style
  user email flows this means checking API/ACP paths such as `usersAPI.confirmEmail`;
  a patch is incomplete if `isValidationPending` can find fallback data but the
  later confirm action still reads `confirm:byUid:<uid>` directly and passes a
  missing code to `confirmByCode`.
- For resend/expiry fixes, preserve legacy near-expiry TTL behavior unless the
  issue explicitly removes it. If a patch adds `sentAt`/`expiresAt`, the resend
  gate still must return true when existing DB TTL state has been shortened so
  that `ttl + interval < max`; new timestamp fields must not override that
  legacy can-send path.
- For email confirmation resend fixes, treat live database TTL as authoritative
  for the resend throttle when the legacy `confirm:byUid:<uid>` key exists. A
  durable fallback record may recover status after the code path expires, but it
  must not replace or lengthen the live `pttl(confirm:byUid:<uid>)` decision
  used by `canSendValidation`.
- If the existing confirmation object has a stored expiry timestamp field such
  as `expires` or `expiresAt`, `canSendValidation` must treat that timestamp as
  a source of remaining TTL for the legacy resend interval check. A hidden/public
  test may shorten `confirm:<code>.expires`; a correct resend gate allows resend
  when that stored remaining time plus the configured interval is less than the
  max confirmation period, even if another TTL source is longer.
- For NodeBB email validation specifically, support both resend timing shapes.
  Some tests shorten the live `confirm:byUid:<uid>` TTL with `db.pexpire(...)`;
  the official task tests check out an updated `test/user/emails.js` and shorten
  `confirm:<code>.expires` with `db.setObjectField(...)`. `canSendValidation`
  must compare the shortest positive remaining time from the live byUid TTL and
  stored `expires`/`expiresAt` timestamp before applying `ttl + interval < max`.
  A direct `return db.pttl(confirm:byUid) + interval < max` branch is incomplete
  when the confirmation object has a shorter stored expiry.
- For NodeBB `.well-known/webfinger` tasks, inspect and preferably run
  `test/controllers.js`, not only lint or module-load checks. The official
  controller tests exercise the configured forum URL, guest `view:users`
  privilege, nonexistent local users, and the valid JRD response. In NodeBB test
  config `nconf.get('url')` can include a relative path such as
  `http://127.0.0.1:4567/forum`; a correct WebFinger implementation must accept
  the local resource shape the existing controller tests derive from that
  configured site URL instead of rejecting it as a malformed/remote host. It
  must return 403 when guests lack `view:users`, 404 for a well-formed local
  resource whose user does not exist, and 200 for an existing local user.
- If the expected behavior requires a helper API that is missing, inconsistent
  across adapters/backends, or only works for one input shape, the worker must
  include the helper source files in the implementation scope. Do not work
  around a missing helper contract only in the top-level feature module. If the
  issue can be solved using an existing portable helper contract, prove that
  source-level reason in the final report/status instead of adding a speculative
  helper API.
- If the issue text names a specific helper interface, implement that exact
  interface name and contract. Do not substitute a nearby overload or renamed
  helper. For example, if the issue says `db.mget(keys)` or `mget`, add
  `module.mget`/`db.mget` across the relevant adapters; overloading `db.get`
  with array support is not an acceptable substitute unless the issue explicitly
  asks for `db.get(array)`.
- For JavaScript database/cache bulk string-key helpers, expose both the
  repository-facing `module.mget`/`db.mget` name and any local convenience alias
  such as `getMany` if you introduce one. Hidden/official tests may assert the
  named interface even when visible source does not yet call it. Do not remove a
  newly required named helper as "unused" when the issue or adapter names it.
- For NodeBB email validation fallback tasks involving missing `confirm:byUid`
  or alternative confirmation sources, treat plural key lookup as requiring a
  real string-key bulk helper. Official tests may assert `db.mget(keys)` directly:
  implement `module.mget` in `src/database/redis/main.js`,
  `src/database/mongo/main.js`, and `src/database/postgres/main.js`; expose the
  promisified repository-facing `db.mget` from the corresponding adapter entry
  files if needed; preserve input order; return `null` for missing keys; return
  `[]` for empty/falsy key arrays; and make `getMany` only an alias if present.
  Run or attempt `test/database/keys.js` or `test/database.js` so the bulk key
  helper contract is actually covered.
- For NodeBB `canSendValidation`, preserve the existing visible behavior:
  it must return `true` once enough time has elapsed to re-send confirmation.
  The public NodeBB regression may shorten only `confirm:byUid:<uid>` with
  `db.pexpire(..., 1000)`. The official task test may instead shorten only the
  stored `confirm:<code>.expires` timestamp. Therefore `getValidationExpiry(uid)`
  or the direct `canSendValidation` branch must read the live
  `db.pttl('confirm:byUid:<uid>')`/template-literal equivalent and the matched
  confirmation object's `expires`/`expiresAt` timestamp, then apply
  `ttl + interval < max` to the shortest positive remaining TTL. Only after the
  legacy byUid key is missing should a fallback scan/object path decide status
  from unrelated confirmation objects.
- Stored confirmation expiry fields may be returned from NodeBB database
  helpers as numeric strings. Parse `expires`/`expiresAt` with
  `Number(...)`/`parseInt(...)` before subtracting `Date.now()`. Do not use only
  `new Date(value).getTime()` for millisecond timestamp strings; Node treats
  strings such as `"1712345678901"` as invalid dates, which makes the official
  resend assertion fail.
- For the same NodeBB resend gate, implement `db.mget` for the database helper
  contract, but do not route the legacy `confirm:byUid:<uid>` lookup in
  `canSendValidation`/`getValidationExpiry`/`getValidationData` through
  `db.mget([key])`. That path must preserve the old string-key semantics:
  read the byUid code with `db.get(confirmByUidKey(uid))` or equivalent, then
  make the resend decision from `db.pttl(confirmByUidKey(uid))`. `db.mget` is
  for the bulk helper/API regression, not for replacing the live byUid throttle
  path whose TTL the official test mutates directly.
- If `canSendValidation` is changed for NodeBB, put the live byUid TTL decision
  directly in that function or in a helper that it calls before any generalized
  status/fallback scan. After confirming the byUid code exists and its
  `confirm:<code>` object matches the requested email, build candidate remaining
  TTLs from `await db.pttl('confirm:byUid:<uid>')`, `confirmObj.expires -
  Date.now()`, and `confirmObj.expiresAt - Date.now()` when each value is
  positive. Use the shortest candidate and apply `ttl + interval < max`.
  Hidden/public tests may shorten either source independently; a patch that
  only uses one source will fail whichever official/public regression shortens
  the other. Only when there is no byUid code/matching object should the code
  call fallback status/search helpers.
- The worker must run or attempt the most relevant existing test file/package,
  not only a single hand-picked test case, when that is practical. For example:
  a Node/TS task should prefer the nearby Jest/Mocha test file or workspace test
  script; a Go task should prefer the owning package with `go test`; a Python
  task should prefer the nearby pytest module or test class.
- If an official expected test or patch excerpt reads fixture/testdata files
  that are absent from the checkout, add the minimal required fixture files
  rather than reporting the test as stale or fixture-mismatched. Fixture assets
  under paths such as `testdata/`, `fixtures/`, `golden/`, or snapshots are
  allowed when they are required for normative benchmark tests to execute.
- The worker must not launch duplicate expensive compile/test commands for the
  same package. If an identical package validation is already running in another
  live worker/verifier, wait for that result or report the overlap to the
  orchestrator. One active validator per package/path is the default. If the
  first instruction did not grant a validation lease for that package/path, use
  source inspection and cheap probes until the orchestrator assigns or releases
  the lease.
- If a source-only patch makes existing same-package tests fail to compile,
  the patch is not acceptable merely because tests are outside the editable
  scope. Preserve source-level compatibility for test-facing package APIs when
  needed, for example with a small compatibility alias/wrapper, or choose a
  narrower implementation that does not remove the visible API. Do not report
  completion with `go test ./changed/package` failing on undefined exported
  types/functions introduced by the patch.
- Do not call existing visible same-package tests "stale" to justify removing a
  compatibility shim. If a rename/unexporting task conflicts with visible tests,
  make the new source path use the renamed/unexported API, but keep the smallest
  source-only compatibility alias, wrapper, or extra struct field needed for the
  old tests to compile. The official scorer can reject bad behavior; the adapter
  must not submit a patch that fails package compilation.
- If helper-layer behavior was inspected or changed, the worker must also run
  or attempt the helper-layer test file/package when one exists and is practical.
  Running only the feature-level test is insufficient for issues about keys,
  fallback lookup, arrays/lists, falsy inputs, expired records, adapters, or
  missing data.
- For Flipt database configuration tasks that ask for separate database
  credential keys, treat the config parser/validator and database opener as a
  single contract. Inspect `config/config.go`, `config/config_test.go`,
  `internal/storage/db/db.go`, and nearby migrator/open tests before editing.
  Preserve URL precedence: if `db.url` is present, it wins and key/value fields
  must not be silently merged into it. When URL is absent, expose an explicit
  database protocol concept for sqlite/file, postgres, and mysql; reject
  unsupported protocols instead of coercing them to zero values. The official
  patched tests compile against the exact exported names
  `config.DatabaseSQLite`, `config.DatabasePostgres`, and
  `config.DatabaseMySQL`; shorter constants such as `SQLite`, `Postgres`, or
  `MySQL` are not sufficient unless these compatibility aliases also exist.
  `DatabaseProtocol.String()` should return `file` for SQLite, `postgres` for
  Postgres, and `mysql` for MySQL so DB URL generation matches expected DSNs.
  Validate key/value database mode with field-qualified messages such as
  `database.protocol`, `database.host`, `database.name`, and the official TLS messages
  `server.cert_file cannot be empty when using HTTPS`,
  `server.cert_key cannot be empty when using HTTPS`,
  `cannot find TLS server.cert_file at "..."`, and
  `cannot find TLS server.cert_key at "..."`. Add the official fixture
  `config/testdata/config/database.yml`; the official `TestLoad` reads it.
  This fixture must be a full config-style fixture, not a minimal three-line
  database fragment. For the common Flipt database-credentials row it must set
  MySQL key/value credentials: `db.protocol: mysql`, `db.host: localhost`,
  `db.port: 3306`, `db.name: flipt`, `db.user: flipt`,
  `db.password: s3cr3t!`, `db.migrations.path: /etc/flipt/config/migrations`,
  `db.max_idle_conn: 2`, plus the expected surrounding config values such as
  server defaults and `meta.check_for_updates: true`.
  Official `TestValidate` makes HTTP configs without `db.url` enter database
  validation: `DatabaseConfig{}` must fail as
  `database.protocol cannot be empty`, `DatabaseSQLite` without Host must fail
  as `database.host cannot be empty`, and `DatabaseSQLite` with Host but no
  Name must fail as `database.name cannot be empty`. HTTPS certificate failures
  should still return the TLS error before database validation. SQLite parsing
  may still use `Host` as the file path for the final DSN.
  Do not expose `DatabaseConfig.Password` through JSON; `/meta/config`
  marshals `Config`, so the password field must use `json:"-"` or equivalent
  while preserving loaded struct values.
  For official `TestParse`, SQLite key/value config uses `Host: "flipt.db"`
  with no `Name` and must still parse to `flipt.db?_fk=true&cache=shared`.
  MySQL with no port should use `3306`; Postgres with no port should not force
  an explicit `port=5432` into the parsed DSN. Build the final driver
  target internally for `Parse`, `Open`, and migrator paths. In this checkout,
  official patched `storage/db/db_test.go` calls the unexported helpers as
  `parse(config.Config, migrate)` and `open(config.Config, migrate)`, not the
  old string signatures; update these helper signatures and route URL/string
  mode through `config.Config{Database: config.DatabaseConfig{URL: ...}}` if a
  compatibility path is needed. Official code also changes `NewMigrator` to take
  `config.Config` by value and updates command call sites; do not leave only a
  pointer-only `NewMigrator(*config.Config, ...)` path when hidden tests compile
  against the value signature. Run or attempt the official selected-test shape:
  `go test -v -run '^(TestLoad|TestValidate|TestOpen|TestParse|TestMigratorRun|TestMigratorRun_NoChange)$' ./...`.
- For Flipt export determinism / `--sort-by-key` tasks, official `TestExport`
  may check out a patched `internal/ext/exporter_test.go` that reads sorted
  fixture files not present in the base image. Add the required
  `internal/ext/testdata/export_sorted.yml`,
  `internal/ext/testdata/export_sorted.json`,
  `internal/ext/testdata/export_default_and_foo_sorted.yml`,
  `internal/ext/testdata/export_default_and_foo_sorted.json`,
  `internal/ext/testdata/export_all_namespaces_sorted.yml`, and
  `internal/ext/testdata/export_all_namespaces_sorted.json` files when the
  patched test references them. Do not claim `TestExport` passed if those
  fixtures are missing; the official verifier treats missing testdata as a
  failed source patch.
- For Flipt OFREP bulk-evaluation tasks, the absence of `context.flags` is not
  an invalid-context error. Wire a store dependency into the OFREP server,
  resolve namespace from request metadata with default `default`, list flags for
  that namespace, and evaluate only boolean flags plus enabled variant flags.
  When `context.flags` is present, split it as comma-separated keys and trim
  whitespace. Preserve the existing bulk response shape with key, variant,
  typed value, and metadata. Run or attempt the OFREP evaluation package tests.
- For Flipt BatchEvaluate disabled-flag tasks, add the exact exported
  `errors.ErrDisabled` type and `ErrDisabledf` constructor, make single
  evaluation return that error for disabled flags, and make batch evaluation
  detect it with `errors.As` so the outer batch continues and returns one
  response per input in order. Each per-flag response still needs timestamp and
  request duration, and the outer response needs total duration.
- If tests require a local service already present in the image or repo scripts
  (`redis-server`, `mongod`, `postgres`, project docker-compose, or a documented
  setup script), the worker must attempt to start the service once before
  claiming validation is unavailable. Keep service state local to the container.
- If the relevant test file is too expensive or cannot run, the worker must
  create a temporary repro outside the repository or run a source-level command
  that exercises the exact behavior. Do not add or submit benchmark tests.
- The worker must not report final completion with an empty `git diff`.
- If the worker creates a new source file, it must ensure that file is part of
  the final patch. Do not leave required source files merely untracked.
- The worker must remove generated/bundled artifacts from `git diff` before
  reporting completion. If validation rewrites bundled assets or lockfiles,
  restore those files and keep only hand-written source changes.
- For NodeBB email validation/resend tasks, the worker should run or attempt
  the official selected-test composition before claiming completion:
  `NODE_ENV=test TEST_ENV=development npx mocha test/database.js test/database/keys.js test/user/emails.js --grep="should contain every translation key contained in its source counterpart" --invert --reporter=json --timeout=8000 --bail=false`.
  Running only `test/user/emails.js`, a single guessed assertion, or a custom
  runtime probe is not sufficient, because `test/database.js` setup has exposed
  resend TTL failures that the narrower checks missed.
- For NodeBB `.well-known/webfinger` tasks, the worker should run or attempt
  `NODE_ENV=test TEST_ENV=development npx mocha test/controllers.js --grep=".well-known webfinger|user data export" --reporter=json --timeout=10000 --bail=false`,
  or the full `test/controllers.js` file when the grep is unreliable. A source
  regex check or `require()` smoke test is not enough for this task.
- For NodeBB chat privacy / allow-list / deny-list tasks, preserve the legacy
  blocked-user error path (`[[error:chat-user-blocked]]`) separately from new
  privacy restrictions (`[[error:chat-restricted]]`). If you add new
  `[[user:...]]` translation keys, either update every locale `user.json` key
  set or avoid new template-visible keys; the official full suite checks that
  every language contains all keys from the source locale. Run or attempt
  `NODE_ENV=test TEST_ENV=development npx mocha test/messaging.js test/i18n.js --reporter=json --timeout=10000 --bail=false`.
- For Element Web `useWindowWidth` hook tasks, create the source module
  `src/hooks/useWindowWidth.ts` and export `useWindowWidth`. Do not add or
  modify `test/hooks/useWindowWidth-test.ts`; official tests already import the
  hook from source. Inspect `src/stores/UIStore` and `UI_EVENTS`, initialize
  the hook state from the current UI/window width, subscribe to the UI resize
  event, update state when width changes, and remove the listener on cleanup.
  Run or attempt `npx jest --verbose --silent test/hooks/useWindowWidth-test.ts`.
- For qutebrowser host-blocking tasks that mention subdomains, parent domains,
  or widening hostnames, inspect `qutebrowser/utils/urlutils.py` and
  `tests/unit/utils/test_urlutils.py` in addition to
  `qutebrowser/components/hostblock.py`. Official tests expect a reusable
  `urlutils.widened_hostnames(hostname)` helper and benchmark it directly. Do
  not implement hostname widening only as a private loop in `hostblock.py`.
  Run or attempt both `python -m pytest tests/unit/components/test_hostblock.py`
  and `python -m pytest tests/unit/utils/test_urlutils.py -k Widen`.
- For qutebrowser duration parsing / `:later` tasks, implement the reusable
  public helper in `qutebrowser/utils/utils.py` as `parse_duration(duration)`;
  do not hide the parser as a private helper in `qutebrowser/misc/utilcmds.py`.
  Official tests import `qutebrowser.utils.utils.parse_duration` directly.
  Inspect that row's `tests/unit/utils/test_utils.py::test_parse_duration`
  contract before choosing semantics: some rows require plain integers to mean
  seconds and invalid inputs such as `-1`, `-1s`, `34ss`, and `60.4s` to return
  `-1`; other rows require plain integers to preserve millisecond
  compatibility, allow decimal unit values, allow whitespace between units, and
  raise `ValueError` for invalid inputs. Follow the row-specific expected tests,
  then make `:later` call `utils.parse_duration(...)` and translate invalid
  sentinel/exception behavior into `CommandError` as appropriate. If you add a
  config `Duration` type, wire only appropriate nonnegative millisecond
  settings in `configdata.yml` and preserve sentinel integer settings such as
  `downloads.remove_finished = -1`.
- For qutebrowser command rename/deprecation tasks such as making
  `:tab-select` canonical and `:buffer` deprecated, inspect existing tab
  completion helpers and run or attempt `tests/unit/completion/test_models.py`.
  Do not assume `miscmodels.buffer` is the tab completion API on that checkout;
  older official tests exercise `miscmodels.tabs()` and
  `miscmodels.other_tabs()`. If you rename helpers, preserve compatibility
  aliases for both ordinary tab completion and other-window tab completion.
- For qutebrowser `:open` filesystem completion tasks, inspect
  `qutebrowser/completion/models/urlmodel.py`,
  `qutebrowser/config/configdata.yml`, and
  `tests/unit/completion/test_models.py`. Official tests expect a new
  `Filesystem` category governed by `completion.open_categories` and
  `completion.favorite_paths`. The category rows should use the raw local path
  as the first column and `None` for the display/description columns, e.g.
  `(path, None, None)`, not `file://...` URLs or duplicated display text.
  `file:///tmp/...` input should be converted to the same raw path suggestions
  as `/tmp/...`; do not re-encode suggestions with `QUrl.fromLocalFile`.
  If a helper parses path patterns, the file-URL branch should use
  `QUrl(...).toLocalFile()` (or equivalent) for both matching and the displayed
  suggestion prefix, so `file:///tmp/x/a` yields `/tmp/x/alpha`, not
  `file:///tmp/x/alpha`.
  Directory suggestions must include one trailing path separator in the first
  column, e.g. `/tmp/x/alpha_dir/`, for both absolute path and `file:///` input;
  file suggestions must not have an added separator.
  Preserve tilde display for bare `~`/`~/` suggestions rather than returning a
  home-directory basename such as `root/`. Keep the category present/orderable
  even when quickmarks/bookmarks are absent or no favorite paths are configured,
  so existing URL/search/history categories and
  `test_url_completion_no_quickmarks`/`no_bookmarks` still match. Do not insert
  Filesystem before History in the default `completion.open_categories` order or
  in `urlmodel.url()`; appending it after the existing History category preserves
  search/history pattern counts and delete behavior in the existing tests. Run or attempt
  `python -m pytest -q tests/unit/completion/test_models.py
  -k 'filesystem_completion or default_filesystem_completion or url_completion_no_quickmarks or url_completion_no_bookmarks or open_categories or url_completion_pattern or url_completion_delete_history'`.
  In `configdata.yml`, define `completion.favorite_paths` as a `List` of
  `String` with `none_ok: true` and default `[]`; without `none_ok: true`, this
  checkout's config validation can reject the empty default and break existing
  URL completion tests.
- For qutebrowser version/changelog-after-upgrade tasks, implement the public
  contract in `qutebrowser/config/configfiles.py`, not only in `app.py`.
  Official `tests/unit/config/test_configfiles.py` imports
  `configfiles.VersionChange` with members `unknown`, `equal`, `patch`,
  `minor`, `major`, and `downgrade`, and exercises
  `configfiles.qutebrowser_version_changed(...)`,
  `configfiles.qt_version_changed(...)`, and
  `configfiles.version_change_filter(...)`. The filter levels are `never`,
  `major`, `minor`, and `patch`, where patch includes patch/minor/major,
  minor includes minor/major, major includes only major, and never includes
  none. Unparsable or missing previous qutebrowser versions should report
  `VersionChange.unknown`; older current versions should report downgrade.
  For unparsable old versions, official tests assert the exact warning message
  `Unable to parse old version <value>` without quotes or the word
  `qutebrowser`.
  The three helper APIs must be literal module-level functions named exactly
  `def qutebrowser_version_changed(...)`, `def qt_version_changed(...)`, and
  `def version_change_filter(...)` in `qutebrowser/config/configfiles.py`.
  Methods, properties, attributes, enum methods, or differently named private
  helpers are not sufficient because the official tests import/call the
  module-level functions directly.
  Run or attempt `python -m pytest -q tests/unit/config/test_configfiles.py`.
- For OpenLibrary MARC author/linkage tasks, inspect
  `openlibrary/catalog/marc/parse.py` and run or attempt
  `python -m pytest -q openlibrary/catalog/marc/tests/test_parse.py`. Official
  fixtures compare full parsed edition shape, not only the new target cases. Do
  not globally delete legacy `contributions`: many pass-to-pass fixtures use it
  for non-author contributors. Instead, move only the responsible 7xx
  people/org/event entities required by the issue into structured `authors`, and
  preserve existing `contributions` output for unrelated contributor records.
  Conversely, do not introduce a `contributions` key into records whose existing
  fixture key set lacks it, and do not leave an equally responsible 7xx creator
  only as a plain string contribution when the task says it belongs in
  `authors`.
  Preserve existing parser output shape for unaffected fixtures: no redundant
  `personal_name` should be changed only for affected author records, role
  strings from subfield `e` keep their trailing period, and linked 880
  alternate-script names should follow the row's expected direction without
  reversing already-correct visible fixtures. A patch that passes only
  hand-written examples but leaves broad failures in `test_parse.py` is not
  acceptable.
- For OpenLibrary Wikidata statement-value tasks, inspect
  `openlibrary/core/wikidata.py` and run or attempt
  `python -m pytest -q openlibrary/tests/core/test_wikidata.py`. Official tests
  call `WikidataEntity.get_statement_values(property_id)` directly. Implement
  that exact instance method; do not add a differently named helper or a
  top-level function. The method must read `self.statements[property_id]`,
  preserve statement order, and return only non-empty string
  `statement.value.content` values. Missing properties, malformed statements,
  missing `value`/`content`, non-string content, and empty strings must be
  skipped and should produce `[]` when nothing valid remains.
- For OpenLibrary list form/query precedence tasks, inspect the `/lists/add`
  request path and `openlibrary/plugins/openlibrary/tests/test_lists.py`.
  Official tests exercise `TestListRecord.test_from_input_with_data` and
  pass-to-pass `test_from_input_no_data` plus seeded variants. Fix
  `ListRecord.from_input`/nearby normalization so explicit POST body data is
  used independently of conflicting URL query parameters and independently of
  `web.ctx.method`, `web.ctx.env`, `REQUEST_METHOD`, or `CONTENT_LENGTH`
  heuristics. Hidden official tests can monkeypatch `web.input` without
  setting request metadata, and can expose body form data through raw
  `web.data()` bytes while `web.input()` returns query/default values; a
  `web.input(_method="post")`-only fix is not enough for this row. When
  `web.data()` is non-empty, parse those form bytes and use the body
  exclusively; fall back to `web.input(...)` only when raw body data is empty.
  Body values should take precedence for fields such as `key`, `name`,
  `description`, and `seeds`; the known hidden case expects `key='/lists/OL1L'`,
  `name='foo data'`, `description='bar'`, and two book seeds from body form
  data, not query defaults. Preserve no-data and seeds parsing. Run or attempt
  `python -m pytest -q openlibrary/plugins/openlibrary/tests/test_lists.py`;
  hidden official `TestListRecord` cases may not be present in the visible tree,
  so source-probe `ListRecord.from_input` directly when needed.
- For Navidrome client-unique-id/SSE filtering tasks, official `TestEvents`
  compiles against the filtering seam. Store the sender request context on
  `message` as `senderCtx context.Context` and implement
  `broker.shouldSend(message, client) bool`; call that helper from the broker
  delivery loop. Hidden/public tests may instantiate `message{senderCtx: ...}`
  and call `b.shouldSend(...)` directly. Do not implement the filtering only as
  inline logic over copied `username`/`clientUniqueId` fields, even if local
  visible tests pass. Keep `diode.set`, `message.ID/Event/Data`, and
  `cookieExpiry` as tiny source compatibility shims if visible same-package
  tests require them, while production paths use `put`, unexported fields, and
  `consts.CookieExpiry`.
- For Navidrome MIME/content-type/server tasks, official `TestServer` exercises
  the server/static file MIME registry and imports
  `github.com/navidrome/navidrome/conf/mime` directly. Put any new public MIME
  loader/registry package at `conf/mime`, not `core/mime`, `pkg/mime`, or an
  unimported private table. Use the repository MIME resources, especially
  `consts/mime_types.go` and `resources/mime_types.yaml` when present, preserve
  compatibility for existing `consts.LosslessFormats` callers, and keep the
  server path that sets HTTP `Content-Type` wired through the same registry.
  Run or attempt `go test ./... -tags netgo -run '^TestServer$'` plus package
  tests for touched callers such as `go test ./model`. A patch that passes only
  by adding a differently named MIME package will compile locally but fail the
  official hidden `TestServer`.
- For Ansible `uri`/URL-helper tasks that add a public option such as
  `use_netrc`, propagate the option explicitly through every helper layer,
  including default `True` values. Do not hide the new default behind
  conditional `kwargs` insertion to satisfy older visible mock assertions;
  official tests may update those mocks and expect
  `fetch_url(...)->open_url(..., use_netrc=True)->Request.open(...,
  use_netrc=True)` exactly.
- For Ansible multipart/form-data tasks, official
  `test/units/module_utils/urls/test_prepare_multipart.py` exercises the public
  `prepare_multipart(fields)` helper in `lib/ansible/module_utils/urls.py`.
  Match its structured contract exactly: a dict/list of fields returns
  `(content_type, body_bytes)`; a bare string body or a field value of `None`
  raises `TypeError`; an empty field mapping raises `ValueError`; a mapping with
  both `filename` and `content` is an in-memory file part and must not read that
  filename from disk; only a `filename` mapping without `content` reads the file.
  MIME guessing errors or unknown types fall back to
  `application/octet-stream`, while explicit `mime_type` is honored. The hidden
  fixture compares body bytes: every part must emit `Content-Type` before
  `Content-Disposition` after the boundary, including plain string fields, and
  filename-backed parts must be emitted before every non-filename field,
  including mappings that have `content`/`mime_type` but no `filename`. In the
  official fixture the first part is `file1`, not `form_field_1` or
  `form_field_2`, even though the sample input mapping lists form fields first.
  Do not hand-roll the full MIME serializer unless it exactly matches Python's
  email package output. The reference implementation uses
  `email.mime.multipart.MIMEMultipart`, `email.mime.nonmultipart.MIMENonMultipart`,
  `email.mime.application.MIMEApplication`, `email.parser`, `email.utils`, and
  `cStringIO` for Python 2. That matters because filename-only file fields
  (`file4`, `file5`, `file6` in the official fixture) are base64 encoded with
  wrapped lines and emit `Content-Transfer-Encoding: base64` before
  `Content-Type`, while inline `filename` + `content` fields (`file1`..`file3`)
  are not base64 encoded. Content-only mapping field `form_field_2` uses
  `application/octet-stream`. The safest fix is to port the reference
  email.mime-based `prepare_multipart` shape rather than maintaining a custom
  multipart byte writer.
  Run or attempt
  `test/units/module_utils/urls/test_prepare_multipart.py` and keep Galaxy
  publish API tests passing because they are selected with it.
- For Ansible play iterator/state enum refactors, preserve public import
  compatibility for `IteratingStates` and `FailedStates` in
  `ansible.executor.play_iterator`. Official tests import those names directly
  even if the new implementation uses nested or renamed state containers.
  Run or attempt `python -m pytest test/units/executor/test_play_iterator.py`.
- For Ansible display multiprocessing/locking tasks, inspect
  `lib/ansible/utils/display.py` and `test/units/utils/test_display.py`.
  Preserve the public `Display.set_queue(queue)` method and instance `_lock`
  attribute. The parent/original process should reject `set_queue(...)` with
  `RuntimeError`, forked child processes should be able to install a queue and
  send display payloads through it, and `display()` must acquire `_lock` around
  terminal writes using the context-manager protocol (`with self._lock:`), not
  explicit `acquire()`/`release()`, because official tests monkeypatch `_lock`
  and assert `__enter__`/`__exit__`. Run or attempt
  `python -m pytest -q test/units/utils/test_display.py`.
- For Ansible collection FQCN validation tasks, inspect the Galaxy collection
  dataclass/validation source and `test/units/utils/collection_loader/`.
  Official tests exercise names such as `import.that`, `def.coll3`,
  `assert.this`, and `this.return`, and expect them to be rejected because
  either the namespace or collection segment is a Python keyword. Implement the
  reusable helper named by the issue, `is_python_identifier`, using Python
  identifier semantics plus `keyword.iskeyword`; remove or bypass legacy
  `_is_py_id`/`_is_fqcn` compatibility logic only when the source package still
  imports cleanly. `is_valid_collection_name` must return a boolean and reject
  invalid identifiers and keywords in either segment. If the public
  collection-loader tests do not expose a `fqcn_validation` selector, validate
  with a direct `AnsibleCollectionRef.is_valid_collection_name` /
  `is_python_identifier` API probe against the collection loader package or
  `_collection_finder`, `test/units/cli/test_galaxy.py -k
  invalid_collection_name`, and the full
  `test/units/utils/collection_loader/test_collection_loader.py` file.
- For Vuls Alpine scanner fixes, preserve existing parser method names used by
  visible tests, including `parseApkInstalledList`, `parseApkIndex`, and
  `parseApkUpgradableList`. If source/origin package support is needed, add
  compatibility wrappers instead of replacing the old APIs. Run or attempt
  `go test ./scanner ./oval`.
- For Vuls Trivy conversion fixes, do not accept a source-only patch while
  `go test ./contrib/trivy/...` fails because parser/golden expectations still
  show the old duplicated `CveContents` shape. Either make the source behavior
  compatible with existing visible tests or identify the exact source-level
  path official expects; do not mark visible fixture failures as acceptable.
  Preserve `trivy-db/pkg/types.SourceID` as the map key type for
  `VendorSeverity`/`CVSS`; convert to string only for display keys after map
  lookup.
- For Vuls config/TOML server host expansion fixes, inspect
  `config/tomlloader.go`, `config/config.go`, and
  `config/tomlloader_test.go`. Preserve existing test helper names and package
  compile compatibility while adding CIDR/ignore behavior. The official
  `TestHosts` contract expects plain non-CIDR hosts such as
  `hosts("127.0.0.1", nil)` and `hosts("ssh/host", nil)` to return that host as
  a single item, but valid ignore entries still apply to literal IP hosts:
  `hosts("127.0.0.1", []string{"127.0.0.1"})` must return `[]`. IPv4 CIDR
  expansion returns usable addresses only: for `192.168.1.1/30`, return
  `192.168.1.1` and `192.168.1.2`, excluding network and broadcast. Applying
  an ignore entry for `192.168.1.1` must leave only `192.168.1.2`. Run or
  attempt `go test ./config -run '^TestHosts$'`.
- For Teleport benchmark linear/ramp-rate tasks, inspect hidden-test-shaped
  source expectations before wiring CLI flags. Official tests may compile a
  `lib/benchmark` package and expect public names such as `Config`, `Linear`,
  and `validateConfig`; do not implement the core generator only in
  `lib/client` and `tool/tsh`.
- If validation cannot run because of missing tools or excessive cost, the
  worker must still explain the targeted command it selected and why it could
  not run.

Verifier quality bar:

- The verifier is not a summary writer. It is a gate.
- It must inspect the issue text, the current `git diff`, and at least the
  relevant changed files.
- It must reject an empty diff.
- It must reject patches that change tests, lockfiles, generated artifacts, or
  unrelated formatting unless the issue explicitly requires those files. This
  includes bundled public assets and generated/minified JavaScript or CSS.
- It must inspect `git status --short --untracked-files=all` and reject if any
  required source file is untracked rather than included in the patch.
- Dirty submodule or untracked-directory status outside `git diff --name-only`
  is not a blocker by itself. Report it as non-blocking unless the submitted
  diff changes that path or a required source file is missing from the patch.
- It must inspect the worker's validation claim. If the worker only ran an
  unrelated smoke check, a single guessed case while a relevant test file was
  available, or no check due to a service that could be locally started, the
  verifier must run the stronger relevant check itself or reject with exact
  follow-up instructions.
- Before running expensive validation, it must inspect whether the same package
  validation is already running in another live worker/verifier. It should not
  spawn duplicate Go/npm/yarn/pytest jobs against the same package; wait for the
  active command, use its result if captured, or reject with an orchestration
  finding that stale overlapping workers must be killed first. If no verifier
  validation lease was granted, report the exact command needed instead of
  starting a duplicate expensive command.
- If the worker's selected package command is still running, the verifier must
  report `blocked-validations:` with the active worker/command and stop. The
  orchestrator should poll the worker result and respawn or continue verification
  only after the lease is released.
- It must reject source patches that make visible same-package tests fail to
  compile because an exported type, constructor, method, or helper was removed
  or renamed. Test files are outside the submitted patch, but their compile
  failures still prove the source package contract was broken.
- It must not turn a compatibility alias/wrapper into a blocker solely because a
  task asks for a rename or unexported internal field. If visible same-package
  tests still compile against the old name, keeping a tiny compatibility shim is
  non-blocking when the production source uses the new API and the required
  public symbols/behavior are present.
- It must compare the patch against neighboring call sites and tests for
  semantic completeness, not just syntax. Reject broad patches that satisfy one
  path while obviously missing adjacent cases in the same file/package.
- It must classify UI/component tasks as additive public-surface work versus
  behavior rewrites. For story/export/example/component-exposure tasks, reject a
  broad rewrite of existing input, focus, paste, keyboard, accessibility, or
  form integration behavior unless the issue explicitly requires that rewrite
  and the full nearby component interaction test file/package passes.
- If the issue or official test excerpt includes a concrete expected command
  argv, serialized output, error string, return value, or ordered collection,
  the verifier must reproduce that exact assertion with a temporary probe or
  source-level comparison before accepting. Reject patches that only prove a
  weaker semantic property when the hidden/official excerpt requires exact
  ordering, punctuation, argument placement, or output shape.
- It must build its own issue-requirement checklist from the prompt and map the
  current diff plus validation to each item. Reject if any requirement is merely
  assumed covered.
- It must trace at least one layer below the changed feature code into helper
  APIs when the issue text mentions keys, fallback sources, expired records, or
  missing data. If those helper contracts have nearby tests, the verifier should
  run or request the relevant helper test file/package too.
- It must reject if plural-key/fallback behavior was implemented without
  checking bulk key helper contracts and empty/falsy input behavior in the
  relevant database/cache abstraction.
- If a key/fallback/expired-record issue is fixed using only direct single-key
  calls such as `db.get(...)`, the verifier must reject unless it can prove from
  helper source that no bulk/get-many helper contract is implicated. An accepted
  verifier report must include `bulk-helper-contract-checked:` followed by the
  exact helper source files and methods inspected, or a blocking finding that
  asks for a helper-layer worker.
- For plural-key/fallback issues, "no portable bulk getter exists" is a blocker,
  not an acceptance rationale, unless the verifier can prove the task never
  needs multiple string-key reads and no test/call-site convention expects such
  a helper. If the codebase has multiple database/cache adapters, the verifier
  should require a cross-adapter helper implementation rather than a one-backend
  feature workaround.
- The verifier must reject scan/getObject/getObjects feature workarounds when
  the repository lacks the expected bulk string-key helper and plural/fallback
  behavior is in scope. `bulk-helper-contract-checked:` only satisfies the audit
  when it names an existing portable helper or a new helper implementation, not
  merely when it says a helper is absent.
- It must reject if the issue mentions resend/retry/expiry/TTL/after-some-time
  behavior and the patch does not trace the resend throttle path as well as the
  confirmation path. The verifier should explicitly name the resend gate it
  inspected, for example a can-send or retry limiter helper.
- It must reject if a patch depends on a helper API that is missing, only exists
  for one backend/adapter, or has nearby tests that were skipped without a
  concrete cost/tooling reason.
- It must reject if the issue names an exact helper interface but the patch
  implements a different interface. In particular, `db.mget(keys)` requirements
  require a `module.mget`/`db.mget` implementation across adapters; `db.get`
  array overloading is not sufficient evidence for the named interface.
- It must not reject a named helper as speculative merely because visible source
  does not call it yet. Official benchmark tests may assert the named interface.
  For JS bulk string-key helper work, require `module.mget`/`db.mget`; `getMany`
  may exist only as an alias or implementation detail.
- For resend/expiry tasks, it must reject if a new `sentAt`/`expiresAt` path
  makes `canSendValidation` ignore the legacy near-expiry TTL condition
  `ttl + interval < max`.
- If it runs helper-layer validation, its final report must include
  `helper-validation-passed:` followed by the exact command when the helper
  validation passes. If no helper-layer test is relevant, it must include
  `helper-validation-skip-justified:` followed by the concrete source-level
  reason. Do not use either marker for a failed or unrun helper check.
- For NodeBB email validation/resend tasks, verifier acceptance requires the
  official selected-test composition when practical: `test/database.js
  test/user/emails.js` with the translation-key grep inverted. Reject a patch
  that only proves `test/user/emails.js` or a custom inline probe, because that
  has produced 299/300 official failures on the resend TTL assertion.
- In benchmark containers, the task repository may be in detached `HEAD`. A
  branch-name mismatch from assignment tooling is non-blocking when the changed
  files are inside the assigned source scope; treat file ownership and diff
  quality as authoritative.
- It must list concrete blocking findings. If it cannot prove the patch is
  wrong but sees risk, it should name the risk separately from blockers.

Required orchestration loop:

1. Spawn a bounded worker with `bin/subagent.sh assignment-create` and
   `bin/subagent.sh spawn`.
   If the task mentions keys/fallback/alternative sources/expired records and
   the repository contains database/cache adapter directories, that worker's
   owned paths must include the relevant helper-layer directory/file, or the
   orchestrator must first spawn a separate helper-layer worker to inspect and,
   if needed, implement or explicitly prove the portable helper contract. Do
   not add a new string-key bulk helper when the issue does not name one and an
   existing hash/object helper covers the actual source path.
2. Poll until the worker is done, blocked, or clearly failed:
   `MULTIAGENT_ROOT=/app MULTIAGENT_STATE_DIR=/tmp/multiagent-prod-swe bin/subagent.sh poll worker-01-fix`.
3. Inspect the worker output and current `/app` git state. Remove generated
   runtime artifacts such as `appendonlydir/` and `dump.rdb` if they appear.
4. Spawn one read-only verifier with bounded ownership over the same source
   files. The verifier must not edit files.
5. Poll and inspect the verifier. If it reports blocking findings, run one
   bounded worker follow-up using the verifier's exact findings, then run a
   second verifier pass. Do not mark completed immediately after a verifier
   rejection.
   Before spawning a follow-up over the same owned paths, poll existing workers
   and verifiers. Kill or finalize stale duplicate windows first, especially
   when they are running the same package validation command. Never leave two
   live agents compiling/testing the same package unless the user explicitly
   requested that stress test. Maintain a validation lease table with
   package/path, command, owner, state, and resource-risk; a replacement agent
   may run an equivalent command only after the old lease is passed to it or
   explicitly released.
6. Before writing completed status, perform a final helper-scope audit against
   the issue text and current `git diff`. If the issue mentions keys, fallback,
   missing data, cache/database behavior, expired records, expiry, or TTL, and
   the patch uses database/cache helper APIs, completion requires one of:
   - verifier output with `bulk-helper-contract-checked:` naming the helper
     source files/methods inspected; or
   - a source-level reason that no database/cache bulk/get-many helper contract
     is relevant; or
   - a follow-up worker whose owned paths include the helper-layer source
     directory/file, such as `src/database` when it exists.
   Do not write completed status for a feature-only patch while this audit is
   unresolved. When the audit is satisfied, copy `bulk-helper-contract-checked:`
   plus the inspected files/methods into the `validation` field of
   `/tmp/multiagent-prod-swe/status.json`.
7. If the verifier accepts or only non-blocking risk remains, the helper-scope
   audit is satisfied, and `/app` has a
   non-empty source diff, write completion:

   ```bash
   python3 - <<'PY'
   import json
   from pathlib import Path
   Path("/tmp/multiagent-prod-swe/status.json").write_text(json.dumps({
       "status": "completed",
       "summary": "source patch prepared in /app",
       "validation": "focused checks described in worker/verifier output",
       "risk": "see verifier output",
   }))
   PY
   ```

For this benchmark, prefer instructing workers to leave final source changes
uncommitted in `/app`. The official scorer reads a patch, not a git commit, and
read-only verifier workers inspect `git diff`. If a worker follows the normal
production policy and commits anyway, immediately materialize that commit back
into the working tree before spawning a verifier or deciding that the diff is
empty:

Before deciding that a worker produced no source diff, and before spawning the
verifier, materialize worker commits back into the working tree:

```bash
cd /app
if [ "$(git rev-parse HEAD)" != "$MULTIAGENT_START_HEAD" ]; then
  git reset --mixed "$MULTIAGENT_START_HEAD"
fi
```

This is benchmark adapter state handling, not source implementation. It is
allowed for the orchestrator so that worker commits can be reviewed and scored
as the official uncommitted patch. Verifier findings based only on an empty
`git diff` after a worker commit are not meaningful until this reset has been
performed.

The benchmark will score only `git diff --binary` from `/app`.

## SWE Issue Text For Worker Assignments

