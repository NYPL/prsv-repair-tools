import boto3
import botocore.exceptions
import logging
import subprocess

def validate_aws_sso(profile_name):
    if not profile_name:
        return

    logging.info(f"Validating AWS SSO session for profile: {profile_name}...")
    try:
        session = boto3.Session(profile_name=profile_name)
        sts = session.client('sts')
        sts.get_caller_identity()
        logging.info("AWS session is valid.")
    except (botocore.exceptions.TokenRetrievalError,
            botocore.exceptions.ClientError,
            botocore.exceptions.NoCredentialsError):
        
        logging.warning("AWS SSO token has expired or is invalid. Attempting auto-login...")
        try:
            subprocess.check_call(
                ["aws", 
                 "sso", 
                 "login", 
                 "--profile", profile_name])
            logging.info("Login successful.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to refresh AWS SSO token: {e}")
            logging.error("Please run 'aws sso login' manually and retry.")
        except FileNotFoundError:
            logging.error("AWS CLI not found. Cannot auto-refresh SSO token.")