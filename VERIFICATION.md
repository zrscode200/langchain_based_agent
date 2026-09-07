# Verification model selection

OG can use a dedicated model for goal criteria and grading. This promotes the
enterprise adaptation's configuration seam while retaining the current pinned
Code criteria/grader implementations, approval handling and retry policy.

For a hosted or CLI session, set a model pair already declared in the trusted
Deep Agents user/managed model configuration:

```toml
[lc_factory]
verification_model = "company-verifier:review-model"
```

Or override that selection for a process:

```sh
export LC_FACTORY_VERIFICATION_MODEL=company-verifier:review-model
lc-code
```

The shell override takes precedence. The variable is reserved before dotenv
loading; a project `.env` cannot choose the verification endpoint. Empty shell
values mean no override. A present but empty/malformed TOML value or an
undeclared provider/model pair fails startup rather than reverting to the main
model. Configured model policy still applies. Credentials and endpoint settings
are resolved inside the server's captured workspace environment.

When no shell override is supplied, an unreadable or syntactically corrupt
user/managed configuration also fails startup, even if the damaged file might
not contain a verification setting. OG cannot safely infer that verification
was absent and fall back to the main model. Missing configuration files remain
valid. This is stricter than upstream's handling of a damaged user layer.

Direct callers can pass `verification_model=` to `create_factory_agent`, using
a model reference or a prebuilt chat model. A prebuilt model is a trusted host
object, subject to the same responsibility as the main `model=` argument.

| Consumer | Selection |
| --- | --- |
| Goal criteria and fallback | `verification_model`, otherwise main model |
| Rubric grader startup default | `rubric_model`, then `verification_model`, then main model |

With no explicit verification or rubric model, existing runtime main-model
inheritance remains unchanged. Explicit criteria/fallback models stay fixed
when the main model changes at runtime; approval, workspace and hook context
still propagate. Existing explicit runtime rubric selections retain upstream
precedence over the grader startup default. Each resolved model keeps its own
provider retry metadata; selecting a verifier does not overwrite main-model
runtime state.

This setting chooses models for the existing verification pipeline. It does
not add autonomous scheduling, an independent correctness guarantee, or a new
multi-judge evaluation system. Goal criteria are constructed when the host
supplies `goal_criteria_tools` (the normal `lc-code` server does this).

Verification configuration is captured at startup and retained across optional
tool/subagent reloads. Restart to change it.
