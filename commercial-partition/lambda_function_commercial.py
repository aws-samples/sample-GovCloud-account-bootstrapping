import json
import boto3
import os
import time
import urllib3

org_client = boto3.client('organizations')
ssm_client = boto3.client('ssm')

GC_API_ENDPOINT = os.environ['GC_API_ENDPOINT']
http = urllib3.PoolManager()


def get_api_key():
    """Retrieve the cross-partition API key from Parameter Store."""

    return ssm_client.get_parameter(
        Name="cross_partition_api_key",
        WithDecryption=True
    )["Parameter"]["Value"]


def notify_govcloud(account_id, target_ou, api_key, dry_run=False):
    """Send the new GovCloud account ID and target OU to the GovCloud API Gateway."""

    payload = {'account_id': account_id, 'target_ou': target_ou}
    if dry_run:
        payload['dry_run'] = True

    response = http.request(
        'POST',
        GC_API_ENDPOINT,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key
        },
        body=json.dumps(payload)
    )

    if response.status != 200:
        raise Exception(f"GovCloud API call failed with status {response.status}: {response.data.decode('utf-8')}")

    print(f"Successfully notified GovCloud for account {account_id} (status: {response.status})")
    return json.loads(response.data.decode('utf-8'))


def create_govcloud_account(email, account_name):
    """Create a GovCloud account and return the request ID."""

    response = org_client.create_gov_cloud_account(
        Email=email,
        AccountName=account_name
    )
    request_id = response['CreateAccountStatus']['Id']
    print(f"CreateAccountRequest ID: {request_id}")
    return request_id


def poll_account_status(request_id, initial_wait=60, poll_interval=5, max_attempts=10):
    """Poll for account creation status until terminal state or timeout."""

    time.sleep(initial_wait)

    for attempt in range(max_attempts):
        status_response = org_client.describe_create_account_status(
            CreateAccountRequestId=request_id
        )
        create_status = status_response['CreateAccountStatus']
        print(f"Attempt {attempt}: State={create_status['State']}, Full={json.dumps(create_status, default=str)}")

        if create_status['State'] != 'IN_PROGRESS':
            return create_status
        time.sleep(poll_interval)

    return create_status


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


def handle_dry_run(event):
    """Test cross-partition connectivity without creating accounts."""

    api_key = get_api_key()
    notify_govcloud(event['test_account_id'], event['govcloud_target_ou'], api_key, dry_run=True)
    print(f"DRY RUN: Successfully notified GovCloud for account {event['test_account_id']}")
    return {
        'statusCode': 200,
        'body': json.dumps({
            'dry_run': True,
            'govcloud_account_id': event['test_account_id'],
            'account_name': event.get('account_name', 'DryRunTest'),
            'govcloud_target_ou': event['govcloud_target_ou']
        })
    }


def lambda_handler(event, context):
    """
    Creates a GovCloud account, polls for completion, and notifies the
    GovCloud API Gateway with the new account ID and target OU.

    Expected event:
    {
        "dry_run": true,  // optional - skips account creation when true
        "email": "govcloud-account@example.com",
        "account_name": "New-GovCloud",
        "commercial_target_ou": "GovCloud-Paired",
        "govcloud_target_ou": "Workloads/Sandbox"
    }
    """

    if event.get('dry_run'):
        return handle_dry_run(event)

    # Validate required fields
    try:
        email = event['email']
        account_name = event['account_name']
        commercial_target_ou = event['commercial_target_ou']
        govcloud_target_ou = event['govcloud_target_ou']
    except KeyError as e:
        return {'statusCode': 400, 'body': json.dumps({'error': f'Missing required field: {e}'})}

    # Pre-flight: Validate SSM parameter before creating accounts
    try:
        api_key = get_api_key()
    except Exception as e:
        return {'statusCode': 500, 'body': json.dumps({'error': f'Pre-flight failed - SSM parameter not accessible: {str(e)}'})}

    # Track state so account IDs are always returned
    result = {
        'request_id': None,
        'state': None,
        'commercial_account_id': None,
        'govcloud_account_id': None,
        'commercial_target_ou': commercial_target_ou,
        'govcloud_target_ou': govcloud_target_ou,
        'failure_reason': None,
        'errors': []
    }

    # Step 1: Create GovCloud account
    try:
        result['request_id'] = create_govcloud_account(email, account_name)
    except Exception as e:
        result['errors'].append(f"Account creation failed: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps(result)}

    # Step 2: Poll for completion
    try:
        create_status = poll_account_status(result['request_id'])
        result['state'] = create_status['State']
        result['commercial_account_id'] = create_status.get('AccountId')
        result['govcloud_account_id'] = create_status.get('GovCloudAccountId')
        result['failure_reason'] = create_status.get('FailureReason')
    except Exception as e:
        result['errors'].append(f"Polling failed: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps(result)}

    # Step 3: Notify GovCloud
    if result['govcloud_account_id']:
        try:
            notify_govcloud(result['govcloud_account_id'], govcloud_target_ou, api_key)
        except Exception as e:
            result['errors'].append(f"GovCloud notification failed: {str(e)}")

    # Step 4: Move commercial paired account to OU
    if result['commercial_account_id']:
        try:
            move_account_to_ou(result['commercial_account_id'], commercial_target_ou)
        except Exception as e:
            result['errors'].append(f"Move account failed: {str(e)}")

    status_code = 200 if not result['errors'] else 207
    return {'statusCode': status_code, 'body': json.dumps(result)}
