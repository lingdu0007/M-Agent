# Release Qualification

The manually dispatched `Release Qualification` workflow implements the
offline artifact gates in ADR 0042. It has read-only repository permissions
and never creates tags, GitHub Releases or package uploads. Normal `Offline
CI` must also pass for the exact source commit.

## One Candidate

The build job uses a clean checkout, pinned build backend and an isolated
wheel installation. It uploads one wheel/sdist pair, its SHA256SUMS and a
build Manifest. Later jobs download those exact bytes; prerequisite sdist
rebuilds do not replace the candidate.
Prerequisite warming uses uv's default cache, matching the acceptance
subprocess environment that intentionally excludes ambient cache overrides.

Linux Python 3.11-3.14 and macOS Python 3.11/3.14 each run all six frozen
`foundation-release-0-5` Scenarios outside the checkout, using the installed
wheel. Shared-SQLite isolation and historical recovery tests also execute
from copied tests outside the checkout; no runtime source tree accompanies
those copies. Each job records its actual installed environment.

The primary Linux 3.11 host additionally runs the existing Core, Session,
Context and Eval correctness-first benchmark workloads. These are local,
environment-qualified attachments, not production capacity claims or
comparisons against an incompatible old machine.

## Fail-Closed Evidence

The repository helper only orchestrates the public `m_agent.testing` API and
CLI. It verifies every Bundle's content digest and frozen declaration, checks
one complete six-Scenario execution per environment, and compares source,
wheel, sdist, fixture and version against build-job expectations.

The aggregate job reconstructs the seven-cell platform matrix from verified
Bundle environments, not directory labels or previously written verdicts.
It requires matching, correctness-passing primary benchmark attachments and
complete Coverage Matrix documentation, then renders the pre-generated demo.
The four benchmark reports must retain their frozen workload counts, database
integrity checks, measured metrics, actual primary-host environment and capacity
non-claims. A content-digest index binds the original JSON bytes; aggregation
revalidates both their contents and the index.
Missing, duplicated, mixed or damaged evidence fails qualification.

Failure artifacts are retained for diagnosis. A failed job prevents the
aggregate success path; failed runs must not be described as release
certification. Old `rel05-evidence/` archives are never relabeled as evidence
for a new wheel.

## Publication

After qualification, independently review the exact source, supplied wheel
and resulting evidence along Standards and Spec axes. Address blocking
findings and rerun qualification if candidate source changes.

Only then create the authorized annotated version tag at that exact commit.
Do not move or replace an existing tag. Publish the downloaded qualified
wheel/sdist, checksums and qualification evidence in the GitHub Release;
do not rebuild the release assets from another checkout. Record the source
commit, workflow run, artifact digests and remaining non-claims in the release
notes. A GitHub Release does not imply publication to a package index.

The workflow does not authorize live-provider calls or claim FIELD,
production availability, SLO/SLA, automatic promotion or exactly-once effects.
