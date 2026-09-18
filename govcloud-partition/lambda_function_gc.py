import json
import boto3
import hmac

ssm = boto3.client('ssm')
org_client = boto3.client('organizations')
sts_client = boto3.client('sts')


def verify_api_key(event):
    """Verify the x-api-key header against the stored parameter."""

    stored_key = ssm.get_parameter(
        Name='cross_partition_api_key',
        WithDecryption=True
    )['Parameter']['Value']

    provided_key = event.get('headers', {}).get('x-api-key', '')

    return hmac.compare_digest(stored_key, provided_key)


def send_org_invitation(account_id):
    """Send an organization invitation to the new GovCloud (US) account."""

    response = org_client.invite_account_to_organization(
        Target={'Id': account_id, 'Type': 'ACCOUNT'},
        Notes='Automated cross-partition enrollment'
    )
    handshake_id = response['Handshake']['Id']
    print(f"Invitation sent to {account_id}, handshake ID: {handshake_id}")
    return handshake_id


def accept_invitation_in_account(account_id, handshake_id):
    """Assume role in new account and accept the org invitation."""

    assumed = sts_client.assume_role(
        RoleArn=f'arn:aws-us-gov:iam::{account_id}:role/OrganizationAccountAccessRole',
        RoleSessionName='CrossPartitionBootstrap'
    )

    new_account_org = boto3.client(
        'organizations',
        aws_access_key_id=assumed['Credentials']['AccessKeyId'],
        aws_secret_access_key=assumed['Credentials']['SecretAccessKey'],
        aws_session_token=assumed['Credentials']['SessionToken']
    )

    new_account_org.accept_handshake(HandshakeId=handshake_id)
    print(f"Invitation accepted in account {account_id}")


def find_ou_by_path(parent_id, ou_path):
    """
    Find an OU by its full path, supports both simple names and slash-separated paths for nested OUs.
    Examples:
        - Sandbox
        - Workloads/Sandbox/Team-A
    """

    parts = ou_path.split('/')
    current_parent = parent_id

    for part in parts:
        ous = org_client.list_organizational_units_for_parent(ParentId=current_parent)
        found = False
        for ou in ous['OrganizationalUnits']:
            if ou['Name'] == part:
                current_parent = ou['Id']
                found = True
                break
        if not found:
            return None

    return current_parent


def move_account_to_ou(account_id, target_ou_path):
    """Move the account from the root to the target OU (supports full path)."""

    roots = org_client.list_roots()
    root_id = roots['Roots'][0]['Id']

    target_ou_id = find_ou_by_path(root_id, target_ou_path)

    if not target_ou_id:
        raise Exception(f'Target OU "{target_ou_path}" not found')

    org_client.move_account(
        AccountId=account_id,
        SourceParentId=root_id,
        DestinationParentId=target_ou_id
    )
    print(f"Moved account {account_id} to OU '{target_ou_path}'")


def parse_request(event):
    """Parse and validate the request body."""

    try:
        body = json.loads(event.get('body', '{}'))
    except (json.JSONDecodeError, TypeError):
        return None, 'Invalid request body'

    account_id = body.get('account_id')
    target_ou = body.get('target_ou')
    dry_run = body.get('dry_run', False)

    if not account_id:
        return None, 'Missing account_id'
    if not target_ou:
        return None, 'Missing target_ou'

    return {'account_id': account_id, 'target_ou': target_ou, 'dry_run': dry_run}, None


def handle_dry_run(account_id, target_ou):
    """Return success without performing org operations."""

    print(f"DRY RUN: Skipping org invitation, role assumption, and account move for {account_id}")
    return {
        'statusCode': 200,
        'body': json.dumps({
            'status': 'success',
            'dry_run': True,
            'account_id': account_id,
            'target_ou': target_ou,
            'message': 'Dry run completed - no org operations performed'
        })
    }


def lambda_handler(event, context):
    """
    Receives a new GovCloud account ID and target OU from the commercial
    partition via API Gateway, then invites the account to the org,
    accepts the invite, and moves it to the target OU.

    Expected request body:
    {
        "dry_run": true  // optional - skips org operations when true
        "account_id": "111122223333",
        "target_ou": "Workloads/Sandbox",
    }
    """

    print(f"Received event: {json.dumps(event)}")

    # Verify API key
    if not verify_api_key(event):
        return {'statusCode': 403, 'body': json.dumps({'error': 'Forbidden - API key verification failed.'})}

    # Parse and validate request
    params, error = parse_request(event)
    if error:
        return {'statusCode': 400, 'body': json.dumps({'error': error})}

    account_id = params['account_id']
    target_ou = params['target_ou']

    # Dry run check
    if params['dry_run']:
        return handle_dry_run(account_id, target_ou)

    try:
        # Step 3: Send org invitation
        handshake_id = send_org_invitation(account_id)

        # Step 4: Accept invitation in new account
        accept_invitation_in_account(account_id, handshake_id)

        # Step 5: Move account to target OU
        move_account_to_ou(account_id, target_ou)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'status': 'success',
                'account_id': account_id,
                'target_ou': target_ou,
                'handshake_id': handshake_id
            })
        }

    except Exception as e:
        print(f"ERROR processing {account_id}: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'account_id': account_id
            })
        }
