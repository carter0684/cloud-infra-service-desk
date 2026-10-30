# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private reporting instead: open the **Security** tab of this repository and choose **Report a vulnerability**. Include the affected file or endpoint, steps to reproduce, and the impact you see. You will get a reply as soon as the maintainer can look at it.

## Scope

This project is a template you deploy into your own AWS account. Reports about the code in this repository are welcome. Misconfiguration of your own deployment, or the security of third-party services it integrates with, is outside the scope.

## For people deploying it

- Never commit `infra-cdk/config.json`, `backend/.env`, keystores or credentials.
- Secrets belong in AWS Secrets Manager, not in environment variables.
- Review the IAM policies in `infra-cdk/infra_cdk/stacks/compute_stack.py` and enable only the integrations you need.
