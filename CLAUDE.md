# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## Workflow Orchestration

### 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions).
- If something goes sideways, STOP and re-plan immediately - don't keep pushing.
- Use plan mode for verification steps, not just building.
- Write detailed specs upfront to reduce ambiguity.

### 2. Subagent Strategy

- Use subagents liberally to keep main context window clean.
- Offload research, exploration, and parallel analysis to subagents.
- For complex problems, throw more compute at it via subagents.
- One task per subagent for focused execution.

### 3. Self-Improvement Loop

- After ANY correction from the user: update `tasks/lessons.md` with the pattern.
- Write rules for yourself that prevent the same mistake.
- Ruthlessly iterate on these lessons until mistake rate drops.
- Review Lessons at session start for relevant project.

### 4. Verification Before Done

- Never mark a task complete without proving it works.
- Diff behavior between main and your changes when relevant.
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness.

### 5. Demand Elegance (Balanced)

- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution."
- Skip this for simple, obvious fixes - don't over-engineer.
- Challenge your own work before presenting it.

### 6. Autonomous Bug Fixing

- When given a bug report: just fix it. Don't ask for hand-holding.
- Point at logs, errors, failing tests - then resolve them.
- Zero context switching required from the user.
- Go fix failing CI tests without being told how.

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items.
2. **Verify Plan**: Check in before starting implementation.
3. **Track Progress**: Mark items complete as you go.
4. **Explain Changes**: High-Level summary at each step.
5. **Document Results**: Add review section to `tasks/todo.md`.
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections.

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```text
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Finance PSU Project Guardrails

Added after the documentation freeze (DECISIONS.md D9/D10/D11). These are load-bearing for this specific project — read `/docs` first, but treat these as the rules that survive any single doc going stale.

- `/docs` is the engineering source of truth after freeze.
- No feature without a `REQ-*` ID or an explicitly approved engineering decision (`DECISIONS.md`).
- Never promote example/"can"/"could" wording from a source document into contractual scope without labeling it as a retained implementation choice, not a mandate.
- Never use rejected architecture drafts (the Fastify/Node or Next.js/Vercel stacks — DECISIONS.md D1) as requirements, even for "inspiration" on an unrelated detail.
- Every protected endpoint authorizes server-side — never client-side-only gating.
- Browser-reported payment success never grants access; only a verified server-side webhook does, and only for the exact `payment.captured` event **and** `status == "captured"` together — never either alone (REQ-PAY-02, DECISIONS.md D11.1).
- Payment processing must be atomic and idempotent (`transaction.atomic()` + row lock + unique constraints) — see API.md §2.
- Every mutating `Attempt` operation (autosave, submit, deadline finalization) locks the row (`select_for_update()`) *before* checking `submitted_at` or the deadline — not just "the deadline is server-authoritative" as a principle, but enforced under real concurrency (API.md §5, DECISIONS.md D11.2).
- No permanent/public premium-PDF URLs — presigned, time-limited, entitlement-gated only; the viewer loads the full file into memory once per open, not via ongoing Range requests against the short-lived URL (DECISIONS.md D11.4).
- No secrets in repo, logs, fixtures, or prompts.
- Schema changes require migrations and tests — not a documentation update alone.
- Every implementation task identifies its requirement ID(s) and the tests that verify it before it's considered done.
- Do not introduce technology outside the approved stack (Django, PostgreSQL, Cloudflare R2, Razorpay, Brevo, Docker Compose on an OCI VM) without explicit client approval — no Redis, Celery, microservices, or event bus.
- Google Login (REQ-ACC-04, DECISIONS.md D14) is additive only — never remove, degrade, or gate local username/password login/signup/password-reset behind it. No other social provider (GitHub/Facebook/Apple/Microsoft) without separate explicit client approval. No custom `AUTH_USER_MODEL` and no custom social-identity table — use `django-allauth`'s own `socialaccount` framework against the stock `User` model. Google OAuth credentials are environment-only, same rule as every other secret.