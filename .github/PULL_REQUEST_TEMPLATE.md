<!--
Thanks for contributing. Keep the PR focused: one behavioural change per PR is
much easier to review than a sweep. See CONTRIBUTING.md for setup and conventions.
-->

## What this changes

<!-- One or two sentences. What behaviour is different after this merges? -->

## Why

<!-- The problem being solved. Link the issue: "Closes #123" -->

## Type of change

- [ ] Bug fix (no behaviour change beyond fixing the bug)
- [ ] New WhatsApp message type supported
- [ ] New feature
- [ ] Breaking change (normalized event shape, config names, or endpoint behaviour changed)
- [ ] Docs / tooling / CI only

## How it was tested

<!--
Be specific. "Added a test" is fine if you say which one. If you tested against
a real Meta test number, say so — that's valuable and hard to automate.
-->

- [ ] `make test` passes locally
- [ ] `make lint` passes locally
- [ ] Added or updated tests covering this change
- [ ] Tested against a real WhatsApp test number (describe below)

## Security checklist

Anything touching `/webhook`, signature verification, or config needs a second look.

- [ ] No secrets, tokens, app secrets or real phone numbers in the diff, tests, fixtures or logs
- [ ] Signature verification is not weakened, skipped, or made conditional on new input
- [ ] Any new comparison of secret material uses `hmac.compare_digest`, not `==`
- [ ] New config values are documented in `.env.example` and fail safely when unset
- [ ] The webhook endpoint still returns fast and never 500s back to Meta on malformed input

## Anything the reviewer should know

<!-- Trade-offs, follow-ups you deliberately left out, areas you're unsure about. -->
