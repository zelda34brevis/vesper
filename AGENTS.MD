# General Engineering Principles

* Keep configuration separate from code.
* Validate external data at system boundaries.
* Handle errors explicitly and gracefully.
* Fail fast on invalid input.
* Keep dependencies minimal and remove unused code and dependencies.
* Minimize nesting and cyclomatic complexity.
* Avoid magic numbers and hardcoded values.

# Python Guidelines

* Use type hints for public interfaces and wherever they improve readability.
* Document public modules, classes, and functions.
* Prefer dataclasses for simple data containers.
* Use pathlib instead of os.path.
* Use logging instead of print().
* Use context managers for resource management.
* Ensure thread safety when concurrency is involved.

# Testing Guidelines

* Include automated tests for new functionality.
* Prefer fixtures over duplicated setup and teardown logic.
* Use parametrization instead of duplicating tests.
* Ensure proper cleanup of resources created during tests.
* Make test failures easy to diagnose with sufficient information for root cause analysis.
* Avoid arbitrary sleeps; prefer explicit waiting conditions.
* Prefer page objects, drivers, adapters, or service abstractions over direct API calls.

# Integration and External Systems

* Use configurable and centralized timeout values.
* Verify external system state before performing critical operations.
* Restore system state after tests or operations whenever practical.
* Treat hardware, network, and cloud interactions as potentially unreliable and handle failures explicitly.

# Constraints

* Read and understand existing code before making changes.
* Search for existing implementations (utilities, helpers, fixtures, drivers, adapters, abstractions, etc.) before creating new ones and reuse them.
* Do not generate test logic that talks to hardware directly - use existing drivers and adapters.
* Do not create new files unless no existing file is appropriate.
* Prefer the smallest correct solution.
* Keep changes minimal, localized, and easy to review.
* Do not perform large-scale or opportunistic refactoring unless explicitly requested.
* Do not introduce new dependencies without clear justification.
* Do not change existing behavior unless required by the task.
* Do not remove existing functionality unless explicitly requested.
* Do not suppress exceptions without a clear reason.
* Do not ignore warnings, errors, or failed test results.
* Never hardcode credentials, tokens, certificates, or private keys.
* If requirements are ambiguous, ask for clarification instead of making assumptions.

# Domain-Specific Instructions

* Search for existing device drivers, adapters, fixtures, and helper classes before creating new ones.
* Reuse existing MQTT, OCPP, ISO-15118, and cloud integration abstractions.
* Keep hardware interaction logic separate from test logic.
* Preserve established project patterns, naming conventions, and architecture unless there is a strong reason to change them.
* Ensure logs contain sufficient information to diagnose failures in distributed systems.
* Avoid introducing additional test execution time without justification.
