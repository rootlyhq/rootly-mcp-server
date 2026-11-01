# Engineering Standards

A unified source of truth for engineering practices, conventions, and guidelines at Rootly.

## Standards

| Topic                                                          | Description                                                      | Owner       |
| -------------------------------------------------------------- | ---------------------------------------------------------------- | ----------- |
| [Ruby](./ruby)                                                 | Style guides, best practices, and idioms                         | Andy Waite  |
| [Rails](./rails)                                               | Conventions and testing standards                                |             |
| ↳ [ViewComponents](./rails/view-components.md)                 | Standards for building ViewComponents                            |             |
| ↳ [Migrations](./rails/migrations.md)                          | Safe database migrations with strong_migrations                  |             |
| ↳ [Turbo Frames](./rails/turbo-frames.md)                      | POST for form submissions to avoid URL length limits             |             |
| [Observability](./observability)                               | Logging, metrics, tracing, and alerting                          |             |
| ↳ [Alerting](./observability/alerting.md)                      | Alert standards and best practices                               | YJ          |
| [Analytics](./analytics)                                       | Product analytics instrumentation                                |             |
| ↳ [PostHog Events](./analytics/posthog-events.md)              | Adding and instrumenting product analytics events                |             |
| [Data Store](./data-stores)                                    | Postgres, Redis, and data management practices                   |             |
| [GitHub Repo Admin](./github-repo-admin)                       | PR templates, review guidelines, and automation                  |             |
| [Staging](./staging)                                           | Guidance for using the staging environment                       | alexmingoia |
| [Background Jobs](./background-jobs)                           | Job processing patterns and best practices                       |             |
| [Testing](./testing)                                           | Testing conventions and anti-patterns                            |             |
| [Frontend Architecture](./frontend-architecture)               | Frontend patterns and conventions                                | Ted Maloney |
| [Backend Architecture](./backend-architecture)                 | Backend patterns and conventions                                 |             |
| [Code Reviews](./code-reviews)                                 | Review process and expectations                                  |             |
| [Security](./security)                                         | Security practices and guidelines                                |             |
| ↳ [Database Encryption](./security/database-encryption.md)     | Field classification, Lockbox, and safe plaintext migrations     |             |
| [Feature Flags](./feature-flags)                               | LaunchDarkly via `FeatureFlag`, flag metadata and lifecycle      |             |
| [Breaking Changes](./breaking-changes)                         | Planning and communicating breaking changes                      | Dan Sadler  |
| ↳ [Terraform Provider](./breaking-changes/terraform-provider)  | State upgraders for non-backwards-compatible API changes         |             |
| [Dart](./dart)                                                 | Style guides, best practices from Effective Dart                 |             |
| [Flutter](./flutter)                                           | Widget design, state management, Rootly Mobile conventions       |             |
| [Deployments](./deployments)                                   | Deployment processes and guidelines                              |             |
| ↳ [Risk Assessment](./deployments/risk-assessment.md)          | Tiered change classification (High/Medium/Low)                   | Dan Sadler  |

## Goals

- **Consistency** — Shared conventions that make code easier to read and maintain
- **Onboarding** — Help new engineers get up to speed quickly
- **Quality** — Best practices that lead to reliable, performant software
- **Collaboration** — Common language for technical discussions and code reviews

## Contributing

1. Open a PR adding or updating a standard
2. Get review from the topic owner (if listed) and at least one other engineer
3. Standards take effect once merged to `master`

## Inspiration

- [thoughtbot/guides](https://github.com/thoughtbot/guides)
