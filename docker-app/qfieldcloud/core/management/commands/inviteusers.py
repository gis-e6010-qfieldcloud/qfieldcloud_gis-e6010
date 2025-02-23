from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from qfieldcloud.core.invitations_utils import invite_user_by_email


class Command(BaseCommand):
    """
    Invite one or more users using the CLI.
    """

    help = """
        Create a user with given username, email and password
        Usage: python manage.py createuser --username=user1 --emails=test1@test.com test2@test.com --exit-on-failure
    """

    def add_arguments(self, parser):
        parser.add_argument("--inviter", type=str, required=True)
        parser.add_argument("--emails", type=str, nargs="+", required=True)
        parser.add_argument("--exit-on-failure", action="store_true")

    def handle(self, *args, **options):
        User = get_user_model()

        inviter_username = options.get("inviter")
        emails = options.get("emails", [])
        exit_on_failure = options.get("exit-on-failure")

        try:
            inviter = User.objects.get(username=inviter_username)
        except User.DoesNotExist:
            print(f'ERROR: Failed to find user "{inviter_username}"!')
            exit(1)

        for email in emails:
            success, message = invite_user_by_email(email, inviter)

            if success:
                print(f"SUCCESS: invitation sent to {email}.")
            else:
                print(f"WARNING: invitation not sent to {email}. {message}")

                if exit_on_failure:
                    exit(1)
