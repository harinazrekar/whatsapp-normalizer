# Contributing

Thanks for wanting to improve this. This service sits directly in front of a
webhook that Meta calls with real customer messages, so the bar for anything
touching `/webhook`, signature verification, or the queue is deliberately high.
Everything else — new message types, docs, tooling — is easy to land.

By contributing you agree that your contribution is licensed under the same
license as this project (see `LICENSE`).

## Setting up a dev environment

You need Python 3.11+ and Docker (for Redis, and for the compose stack).

```bash
git clone <your fork>
cd whatsapp-normalizer

python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

make install                     # pip install -r requirements-dev.txt
make hooks                       # install the git pre-commit hooks

cp .env.example .env             # then fill in the values you need
```

Run `make` (or `make help`) at any time for the full task list.

| Task | What it does |
| --- | --- |
| `make dev` | API with auto-reload on `127.0.0.1:8000` |
| `make worker` | The delivery worker, locally |
| `make test` | Test suite with a coverage report |
| `make lint` | `ruff check` + `black --check` + `mypy` — changes nothing |
| `make fmt` | `ruff --fix` + `black` — fixes what it can |
| `make up` / `make down` / `make logs` | The docker compose stack |

For local dev you can set `REQUIRE_SIGNATURE=false` in `.env` before you have an
app secret. **Never** do that on anything reachable from the internet — an
unsigned request is an unauthenticated one.

## Running tests

```bash
make test          # with coverage
make test-fast     # without
pytest tests/test_normalizer.py::test_extract_text_message   # a single test
```

The suite must pass with **no external services running** — Redis is faked with
`fakeredis`, and downstream HTTP calls are mocked. If your change makes the
tests need a real Redis or a real network call, that's a sign the change should
be structured differently; say so in the PR and we'll work it out.

## Lint, format, types

Config lives in `pyproject.toml`. Line length is **100**, formatting is **black**,
linting is **ruff** (pycodestyle, pyflakes, isort, pyupgrade, bugbear, simplify),
and `mypy` runs over `app/` at moderate strictness (`tests/` is excluded).

Run `make fmt` before you commit and `make lint` before you push. The pre-commit
hooks do most of this for you; CI runs the same checks, so a green local `make
lint` means a green CI lint job.

New code in `app/` should carry type annotations on function signatures. Existing
untyped internals are being annotated incrementally — don't feel obliged to
annotate the whole file you touched, but don't remove annotations either.

## Commits and pull requests

Commit messages use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(normalizer): extract text from contacts messages
fix(security): reject signatures with a missing sha256= prefix
docs(readme): document the ngrok tunnel setup
chore(deps): bump httpx to 0.27.2
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`. Use `!`
after the scope (`feat(models)!:`) for anything that changes the normalized event
shape, a config variable name, or endpoint behaviour — downstream consumers parse
what we emit, so that is a breaking change even if the code looks small.

For PRs:

- Branch off `main`, one behavioural change per PR.
- Fill in the PR template, including the security checklist.
- Include tests. A PR that changes behaviour with no test won't be merged.
- Update `.env.example` and the README if you add a config variable.
- Add a `CHANGELOG.md` entry under "Unreleased" for anything user-visible.
- Never commit a `.env`, an app secret, a verify token, or a real phone number —
  redact identifiers in tests and fixtures (the existing fixtures use obviously
  fake numbers; keep doing that).

Found a security problem? Don't open an issue — see `SECURITY.md` for private
disclosure.

## Adding support for a new WhatsApp message type

This is the most common contribution and it's a small, well-defined change.

WhatsApp keeps adding message types (`contacts`, `reaction`, `order`, `system`,
`referral`, flow replies…). The normalizer already emits an event for every
message it sees — `message_type` and the untouched `raw` payload always come
through. What a new type needs is a sensible `text`: the one human-readable
string that represents the message for a downstream consumer.

**1. Add a branch to `_extract_text` in `app/normalizer.py`.**

Each branch takes the raw `message` dict and returns a `str` or `None`. Follow
the existing shape: read defensively with `.get()`, never index, and return
`None` rather than raising when a field is missing. This function runs inside the
webhook request path, and the endpoint must never 500 back to Meta.

```python
if msg_type == "reaction":
    return message.get("reaction", {}).get("emoji")
```

If the type needs more than a string — a new field on the event — that's a
change to `NormalizedEvent` in `app/models.py`, and it's a breaking change for
consumers. Open an issue first so we can agree on the field name.

**2. Add a test to `tests/test_normalizer.py`.**

Copy the shape of the existing payload constants (`TEXT_MESSAGE_PAYLOAD`,
`BUTTON_REPLY_PAYLOAD`, `STATUS_PAYLOAD`): a full `entry -> changes -> value ->
messages` envelope, since `extract_events` walks that whole structure. Use a real
(redacted) payload from the Meta docs or your own test number — invented payload
shapes are the main source of bugs here.

Assert on the outcome, not the internals:

```python
def test_extract_reaction_message():
    events = extract_events(REACTION_PAYLOAD)

    assert len(events) == 1
    assert events[0].message_type == "reaction"
    assert events[0].text == "👍"
```

Add a negative case too — the same type with the field missing — and assert
`text is None` and that no exception escapes.

**3. Mention the new type in the README's supported-types list, and add a
`CHANGELOG.md` entry.**

Then `make fmt && make lint && make test`, and open the PR.

## Questions

If something in the setup doesn't work, that's a documentation bug — open an
issue. The goal is that a stranger can clone this and get a verified webhook
receiving real messages without asking anyone a question.
