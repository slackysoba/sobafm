# Security policy

## Supported versions

SobaFM has not been released yet. Until version 1.0, security fixes are made on `main` only; from 1.0, the latest release receives them.

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub's private vulnerability reporting](https://github.com/slackysoba/sobafm/security/advisories/new). Do not open a public issue, pull request, or discussion about them.

Include a description of the vulnerability and its impact, steps to reproduce it or a proof of concept, and the affected version or commit.

## What to expect

SobaFM has a single maintainer, so these targets are best effort:

- Acknowledgment within 5 business days
- An initial assessment within 14 days
- Updates as a fix progresses, and credit in the published advisory unless you prefer otherwise

## Scope

In scope: SobaFM's code, its use of its dependencies, and its published container images.

Out of scope: vulnerabilities in Discord or Google services, which should be reported to those vendors, and deployments whose operator credentials have been exposed outside SobaFM.

## Protecting your deployment

SobaFM runs with the operator's Discord bot token and Gemini API key. Keep both in environment variables or a `.env` file outside version control. If either is exposed, revoke it immediately:

- **Discord bot token:** in the [Discord Developer Portal](https://discord.com/developers/applications), open the application, then **Bot** → **Reset Token**.
- **Gemini API key:** in [Google AI Studio](https://aistudio.google.com/apikey), delete the key and create a new one.
