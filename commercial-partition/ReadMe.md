# Commercial Partition Resources

## Overview

This folder contains everything needed to deploy the commercial-side Lambda function that creates GovCloud accounts and notifies the GovCloud API Gateway with the new account ID and target OU.

## Files in This Folder

| File | Description |
|------|-------------|
| `create_resources_commercial.yaml` | CloudFormation template (deploys all resources automatically) |
| `lambda_function_commercial.py` | Lambda function code (for manual deployment) |
| `lambda_permission_policy_commercial.json` | IAM permission policy for the Lambda execution role |
| `lambda_trust_policy_commercial.json` | IAM trust policy for the Lambda execution role |
| `ReadMe_commercial.md` | This file |

## Prerequisites

### 1. Deploy GovCloud-side resources first

The GovCloud API Gateway must be deployed before this stack. You'll need the API endpoint URL as a parameter. See the `govcloud-partition/` folder for instructions.

### 2. Create SecureString in Parameter Store

> **Note:** AWS CloudFormation does not support creating `SecureString` parameter types. You must create this parameter manually before deploying.

```bash
aws ssm put-parameter \
  --name "cross_partition_api_key" \
  --value "<your-shared-secret>" \
  --type SecureString \
  --region us-east-1 \
  --profile <your-commercial-profile>
```

> **Important:** Use the same value as the one created in the GovCloud partition.

---

## Option 1: Automated Deployment (CloudFormation)

Use `create_resources_commercial.yaml` to deploy all resources in a single command:

```bash
aws cloudformation deploy \
  --template-file create_resources_commercial.yaml \
  --stack-name create-gc-acct \
  --parameter-overrides \
    GcApiEndpoint=https://<api-id>.execute-api.us-gov-west-1.amazonaws.com/ingest \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --profile <your-commercial-profile>
```

### What the stack creates

- **IAM Role** (`create-gc-acct-role`) with least-privilege policy
- **Lambda function** (`create_gc_acct`) with `GC_API_ENDPOINT` environment variable
- Timeout set to 180s (accounts for polling during account creation)

### Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `GcApiEndpoint` | GovCloud API Gateway endpoint URL | `https://abc123.execute-api.us-gov-west-1.amazonaws.com/ingest` |

Commercial setup is now complete. Next, skip to the [Testing](#testing) section below to validate the solution.

---

## Option 2: Manual Deployment

Use the individual files in this folder to create resources manually.

### Step 1: Create IAM Role

```bash
aws iam create-role \
  --role-name create-gc-acct-role \
  --assume-role-policy-document file://lambda_trust_policy_commercial.json \
  --region us-east-1 \
  --profile <your-commercial-profile>
```

Attach the permission policy:

```bash
aws iam put-role-policy \
  --role-name create-gc-acct-role \
  --policy-name create-gc-acct-policy \
  --policy-document file://lambda_permission_policy_commercial.json \
  --region us-east-1 \
  --profile <your-commercial-profile>
```

> **Note:** Replace the `commercial-mgmt-acct-id` placeholder in `lambda_permission_policy_commercial.json` with your actual commercial management account ID before deploying.

### Step 2: Create Lambda Function

```bash
zip function_commercial.zip lambda_function_commercial.py

aws lambda create-function \
  --function-name create_gc_acct \
  --runtime python3.14 \
  --handler lambda_function_commercial.lambda_handler \
  --role arn:aws:iam::<account-id>:role/create-gc-acct-role \
  --zip-file fileb://function_commercial.zip \
  --timeout 180 \
  --memory-size 128 \
  --environment "Variables={GC_API_ENDPOINT=https://<api-id>.execute-api.us-gov-west-1.amazonaws.com/ingest}" \
  --region us-east-1 \
  --profile <your-commercial-profile>
```

Commercial setup is now complete. Next, continue to the [Testing](#testing) section below to validate the solution.

---

## Testing

### Dry Run (skips account creation, tests cross-partition connectivity)

```bash
aws lambda invoke \
  --function-name create_gc_acct \
  --payload '{"dry_run": true, "test_account_id": "111122223333", "account_name": "DryRunTest", "govcloud_target_ou": "Workloads/Sandbox"}' \
  --cli-binary-format raw-in-base64-out \
  output.json \
  --region us-east-1 \
  --profile <your-commercial-profile>

cat output.json
```

#### Expected Response (Dry Run)

```json
{
  "statusCode": 200,
  "body": "{\"dry_run\": true, \"govcloud_account_id\": \"111122223333\", \"account_name\": \"DryRunTest\", \"govcloud_target_ou\": \"Workloads/Sandbox\"}"
}
```

### Full Run (creates actual GovCloud account)

```bash
aws lambda invoke \
  --function-name create_gc_acct \
  --payload '{"email": "new-gc-account@example.com", "account_name": "new-Sandbox-Account", "commercial_target_ou": "GovCloud-Paired", "govcloud_target_ou": "Workloads/Sandbox"}' \
  --cli-binary-format raw-in-base64-out \
  output.json \
  --cli-read-timeout 180 \
  --region us-east-1 \
  --profile <your-commercial-profile>

cat output.json
```
> **Important:** `--cli-read-timeout 180` is the maximum time the CLI waits for a response, not a fixed delay — it returns as soon as the function completes. Full runs typically take 70–110 seconds, which exceeds the CLI's 60-second default. If you raise this value, increase the Lambda's own timeout (currently 180 seconds) as well.

### Expected Response (Success)

```json
{
  "statusCode": 200,
  "body": "{\"request_id\": \"car-abcd\", \"state\": \"SUCCEEDED\", \"commercial_account_id\": \"111122223333\", \"govcloud_account_id\": \"444455556666\", \"commercial_target_ou\": \"Customers OU\", \"govcloud_target_ou\": \"Sandbox\", \"failure_reason\": null, \"errors\": []}"
}
```

## Security Notes

- API key retrieved from Parameter Store (SecureString) — never hardcoded
- `GC_API_ENDPOINT` stored as Lambda environment variable (non-sensitive URL)
- Traffic to GovCloud API Gateway stays on AWS backbone
- TLS 1.2+ encryption in transit
- Lambda execution role follows least-privilege
- Function timeout of 180s allows for account creation polling
> **NOTE:** Cross-partition calls cannot use IAM authorization because partitions have separate IAM boundaries. This solution uses a shared API key stored as a SecureString in Parameter Store as a lightweight cross-partition authentication mechanism. For organizations that require certificate-based authentication, AWS Private CA with X.509 certificates and IAM Roles Anywhere provides a stronger alternative—though at additional cost for the private CA infrastructure.

## IAM Permissions Summary

| Sid | Actions | Resource |
|-----|---------|----------|
| CloudWatchLogs | CreateLogGroup, CreateLogStream, PutLogEvents | `/aws/lambda/create_gc_acct` |
| SSM | GetParameter | `cross_partition_api_key` |
| KMS | Decrypt | KMS keys (scoped to SSM) |
| Organizations | DescribeCreateAccountStatus, ListRoots, ListOrganizationalUnitsForParent, CreateGovCloudAccount, MoveAccount | `*` |
