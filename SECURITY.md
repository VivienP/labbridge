# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities through GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**. The report stays private between
you and the maintainer until a fix is published.

Please do not open a public issue for a suspected vulnerability, and do not include exploit details in
a pull request description.

A useful report names the affected version or commit, the conditions required to trigger the problem,
what an attacker gains, and — where possible — a reproduction against a local checkout.

## What to expect

LabBridge is maintained by one person as time allows. There is no response-time commitment and no
guarantee that a given report will be acted on. You will get an acknowledgement when the report is
read; if a fix is published, the advisory will credit you unless you ask otherwise.

## Supported versions

Only the current `main` branch receives fixes. No release is backported, and no version carries
long-term support.

## Scope

LabBridge runs locally against PostgreSQL and S3-compatible object storage. It has no authentication,
no authorisation, no multi-tenancy, and no network-exposed deployment — see
[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md). Reports that assume a hardened multi-user
deployment are describing something the project does not claim to be.

In scope, and worth reporting:

- a path that lets committed evidence be modified while `labbridge validate-artifacts` still passes;
- a way to make a Package verify against a manifest that does not describe its bytes;
- a way to make synthetic data lose its `data_origin` or `execution_mode` label on any exported or
  rendered surface;
- source-byte modification, deletion of retained observations, or lineage that cannot be closed;
- credential or secret disclosure through logs, artifacts, manifests, or error messages.

Out of scope:

- the development-only credentials in `docker-compose.yml`, which are literals published on purpose
  and must never be reused anywhere;
- missing authentication or rate limiting on the local API, which is a documented boundary rather
  than a defect;
- vulnerabilities in third-party dependencies with no LabBridge-specific exploit path — report those
  upstream.
