# GovCloud Partition Resources

## Overview

This folder contains everything needed to deploy the GovCloud-side resources that receive a new account ID from the commercial partition, invite it to the GovCloud organization, and move it to the specified OU.

## Files in This Folder

| File | Description |
|------|-------------|
| `apig_gc.yaml` | API Gateway configuration reference |
| `create_resources_gc.yaml` | CloudFormation template (deploys all resources automatically) |
| `lambda_function_gc.py` | Lambda function code (for manual deployment) |
| `lambda_permission_policy_gc.json` | IAM permission policy for the Lambda execution role |
| `lambda_trust_policy_gc.json` | IAM trust policy for the Lambda execution role |
| `ReadMe_gc.md` | This file |

## Prerequisites

### Create SecureString in Parameter Store (Manual Step)

> **Note:** AWS CloudFormation does not support creating `SecureString` parameter types. You must create this parameter manually before deploying.

This parameter stores the shared API key used to authenticate requests between partitions.

#### AWS Console

1. Navigate to **Systems Manager** → **Parameter Store**
2. Click **Create parameter**
3. Configure:
   - **Name:** `cross_partition_api_key`
   - **Tier:** Standard
   - **Type:** SecureString
   - **KMS Key Source:** My current account (default `aws/ssm` key)
   - **Value:** `<your-shared-secret>`
4. Click **Create parameter**

#### AWS CLI

```bash
aws ssm put-parameter \
  --name "cross_partition_api_key" \
  --value "<your-shared-secret>" \
  --type SecureString \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

> **Important:** Use the same value in both commercial and GovCloud partitions.

---

## Option 1: Automated Deployment (CloudFormation)

Use `create_resources_gc.yaml` to deploy all resources in a single command:

```bash
aws cloudformation deploy \
  --template-file create_resources_gc.yaml \
  --stack-name process-new-gc-acct \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

### What the stack creates

- **IAM Role** (`process-new-gc-acct-role`) with least-privilege policy
- **Lambda function** (`process_new_gc_acct`)
- **HTTP API Gateway** (`process_new_gc_account`) with `POST /ingest` route
- **Lambda invoke permission** for API Gateway

After the stack completes, retrieve the API endpoint from the stack outputs or by running the CLI command below. You will need this endpoint to deploy the commercial partition stack.

```bash
aws cloudformation describe-stacks \
  --stack-name process-new-gc-acct \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
  --output text \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

GovCloud setup is now complete. Next, skip to the [Testing](#testing) section below to validate the function. After that, follow the instructions in `ReadMe_commercial.md` to deploy the commercial partition stack.

---

## Option 2: Manual Deployment

Use the individual files in this folder to create resources manually.

### Step 1: Create IAM Role

1. Create the role using `lambda_trust_policy_gc.json` as the trust policy:

```bash
aws iam create-role \
  --role-name process-new-gc-acct-role \
  --assume-role-policy-document file://lambda_trust_policy_gc.json \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

2. Attach the permission policy from `lambda_permission_policy_gc.json`:

```bash
aws iam put-role-policy \
  --role-name process-new-gc-acct-role \
  --policy-name process-new-gc-acct-policy \
  --policy-document file://lambda_permission_policy_gc.json \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

### Step 2: Create Lambda Function

Package and deploy the function using `lambda_function_gc.py`:

```bash
zip function_gc.zip lambda_function_gc.py

aws lambda create-function \
  --function-name process_new_gc_acct \
  --runtime python3.14 \
  --handler lambda_function_gc.lambda_handler \
  --role arn:aws-us-gov:iam::<account-id>:role/process-new-gc-acct-role \
  --zip-file fileb://function.zip \
  --timeout 60 \
  --memory-size 128 \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

### Step 3: Create API Gateway

```bash
# Create HTTP API
aws apigatewayv2 create-api \
  --name process_new_gc_acct \
  --protocol-type HTTP \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>

# Note the ApiId from the output, then:

# Create Lambda integration
aws apigatewayv2 create-integration \
  --api-id <api-id> \
  --integration-type AWS_PROXY \
  --integration-method POST \
  --integration-uri arn:aws-us-gov:lambda:us-gov-west-1:<account-id>:function:process_new_gc_acct \
  --payload-format-version "2.0" \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>

# Create route (use IntegrationId from above)
aws apigatewayv2 create-route \
  --api-id <api-id> \
  --route-key "POST /ingest" \
  --target integrations/<integration-id> \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>

# Create stage with auto-deploy
aws apigatewayv2 create-stage \
  --api-id <api-id> \
  --stage-name '$default' \
  --auto-deploy \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>

# Add Lambda invoke permission
aws lambda add-permission \
  --function-name process_new_gc_acct \
  --statement-id AllowAPIGatewayInvoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws-us-gov:execute-api:us-gov-west-1:<account-id>:<api-id>/*/*/ingest" \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

GovCloud setup is now complete. Next, continue to the [Testing](#testing) section below to validate the function. After that, follow the instructions in `ReadMe_commercial.md` to deploy the commercial partition stack.

---

## Testing

### Dry Run (tests connectivity without org operations)

```bash
curl -X POST https://<api-id>.execute-api.us-gov-west-1.amazonaws.com/ingest \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your-shared-secret>" \
  -d '{"dry_run": true, "account_id": "111122223333", "target_ou": "Sandbox"}'
```

#### Expected Response (Dry Run)

```json
{
  "status": "success",
  "dry_run": true,
  "account_id": "111122223333",
  "target_ou": "Sandbox",
  "message": "Dry run completed - no org operations performed"
}
```

### Full Run (invites account to org and moves to OU)

A full run performs the complete bootstrapping workflow against a real account:

1. **Invite** — sends an organization invitation to the target account (`InviteAccountToOrganization`), which returns a handshake ID
2. **Accept** — assumes `OrganizationAccountAccessRole` in the target account and accepts the handshake (`AcceptHandshake`)
3. **Move** — resolves the target OU by path and moves the account from the root into it (`MoveAccount`)

> **Important:** You must supply an account ID that is **not currently a member of the GovCloud organization**. Inviting an account that already belongs to the organization fails with `DuplicateAccountException`. The account must also have the `OrganizationAccountAccessRole` present and assumable by this Lambda's execution role, or the accept step will fail.

Replace `111122223333` with your target account ID and `Workloads/Sandbox` with your OU path. Nested OU paths are supported using `/` separators.

```bash
curl -X POST https://<api-id>.execute-api.us-gov-west-1.amazonaws.com/ingest \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your-shared-secret>" \
  -d '{"account_id": "111122223333", "target_ou": "Workloads/Sandbox"}'
```

#### Expected Response (Full Run)

```json
{
  "status": "success",
  "account_id": "111122223333",
  "target_ou": "Workloads/Sandbox",
  "handshake_id": "h-abc123def456"
}
```

#### Verify the result

Confirm the account landed in the expected OU:

```bash
aws organizations list-parents \
  --child-id 111122223333 \
  --region us-gov-west-1 \
  --profile <your-govcloud-profile>
```

## Security Notes

- Traffic stays on the AWS backbone (does not traverse public internet)
- TLS 1.2+ encryption in transit (headers including `x-api-key` are encrypted)
- API key validated by Lambda via Parameter Store (SecureString)
- `hmac.compare_digest` prevents timing attacks on secret comparison
- Lambda execution role follows least-privilege
- `target_ou` supports nested paths (e.g., `Workloads/Sandbox/Team-A`)
> **NOTE:** Cross-partition calls cannot use IAM authorization because partitions have separate IAM boundaries. This solution uses a shared API key stored as a SecureString in Parameter Store as a lightweight cross-partition authentication mechanism. For organizations that require certificate-based authentication, AWS Private CA with X.509 certificates and IAM Roles Anywhere provides a stronger alternative—though at additional cost for the private CA infrastructure.

## IAM Permissions Summary

| Sid | Actions | Resource |
|-----|---------|----------|
| CloudWatchLogs | CreateLogGroup, CreateLogStream, PutLogEvents | `/aws/lambda/process_new_gc_acct` |
| SSM | GetParameter | `cross_partition_api_key` |
| KMS | Decrypt | KMS keys (scoped to SSM) |
| Organizations | DescribeHandshake, ListRoots, ListOrganizationalUnitsForParent, InviteAccountToOrganization, AcceptHandshake, MoveAccount | `*` |
| STS | AssumeRole | `arn:aws-us-gov:iam::*:role/OrganizationAccountAccessRole` |
